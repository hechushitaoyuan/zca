"""后台管理 API：/admin/api/*（账号池、设置、用量监控）。"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import settings
from ..agent import build_request
from ..auth_admin import verify_admin_key
from ..captcha import InteractiveCaptchaRequired, captcha_manager
from ..models import PROVIDERS, FailureKind, Status
from ..oauth import OAuthError, ZaiAuthFlow
from ..quota import fetch_quota, refresh_accounts
from ..store import store
from ..traffic import UsageTracker, record_request
from ..upstream_errors import classify_upstream_failure

router = APIRouter(prefix="/admin/api", dependencies=[Depends(verify_admin_key)])


@dataclass
class _LoginSession:
    flow: ZaiAuthFlow
    created_at: float


# OAuth 授权链接由用户在本地浏览器打开；同一时刻只保留一个待完成流程。
_login_flows: dict[str, _LoginSession] = {}
_TEST_MODELS = {"GLM-5.3", "GLM-5.2", "GLM-5-Turbo"}


# ── 鉴权探针 ─────────────────────────────────────────────────────────────────
@router.get("/verify")
async def verify():
    return {"status": "ok"}


# ── 账号列表 + 概览统计 ──────────────────────────────────────────────────────
@router.get("/accounts")
async def list_accounts():
    now = time.time()
    accounts = [a.public_view() for a in store.list_accounts()]
    stats = {"total": len(accounts), "active": 0, "exhausted": 0,
             "cooling": 0, "invalid": 0, "disabled": 0,
             "calls": 0, "fail": 0}
    for a in accounts:
        st = a["status"]
        if st in stats:
            stats[st] += 1
        stats["calls"] += a["use_count"]
        stats["fail"] += a["fail_count"]
    return {"accounts": accounts, "stats": stats, "providers": list(PROVIDERS), "ts": now}


@router.get("/status")
async def status_info():
    return {
        "providers": list(PROVIDERS),
        "gateway_key_set": bool(store.gateway_key()),
        "quota_pool": {
            p: sum(1 for a in store.list_accounts(p) if a.is_selectable())
            for p in PROVIDERS
        },
    }


# ── 新增账号 ─────────────────────────────────────────────────────────────────
@router.post("/accounts")
async def add_accounts(payload: dict = Body(...)):
    provider = payload.get("provider", "zai")
    if provider not in PROVIDERS:
        raise HTTPException(400, "不支持的 provider")
    tokens = payload.get("tokens") or []
    if isinstance(tokens, str):
        tokens = [t.strip() for t in tokens.splitlines() if t.strip()]
    tokens = [t.strip() for t in tokens if t and t.strip()]
    if not tokens:
        raise HTTPException(400, "请输入至少一个 Token / API Key")

    added = []
    for tok in dict.fromkeys(tokens):  # 去重保序
        name = payload.get("name") or f"{provider}-{len(store.list_accounts(provider)) + 1}"
        acc = store.add_account(provider, name, tok)
        added.append(acc.id)
    # 立即刷新一次额度（仅 zai jwt）
    fresh = [a for a in store.list_accounts(provider) if a.id in added and a.mode == "jwt"]
    if fresh:
        await refresh_accounts(fresh)
    return {"count": len(added), "ids": added}


# ── 删除账号 ─────────────────────────────────────────────────────────────────
@router.delete("/accounts")
async def delete_accounts(ids: list[str] = Body(...)):
    deleted = 0
    for aid in ids:
        acc = store.find_any(aid)
        if acc and store.remove_account(acc.provider, aid):
            deleted += 1
    return {"deleted": deleted}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str):
    """删除单个账号；使用路径参数避免代理丢弃 DELETE 请求体。"""
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not store.remove_account(acc.provider, account_id):
        raise HTTPException(409, "账号删除失败，请重试")
    return {"deleted": 1}


# ── 编辑账号 ─────────────────────────────────────────────────────────────────
@router.put("/accounts/{account_id}")
async def edit_account(account_id: str, payload: dict = Body(...)):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if "name" in payload and payload["name"]:
        acc.name = payload["name"].strip()
    secret = payload.get("token") or payload.get("secret")
    if secret:
        secret = secret.strip()
        acc.mode = "jwt" if (secret.count(".") == 2 and acc.provider == "zai") else "apiKey"
        acc.jwt_token = secret if acc.mode == "jwt" else None
        acc.api_key = None if acc.mode == "jwt" else secret
        acc.status = Status.ACTIVE
        acc.last_error = None
    store.update_account(acc)
    return {"ok": True}


# ── 启用 / 禁用 ──────────────────────────────────────────────────────────────
@router.post("/accounts/{account_id}/enabled")
async def set_enabled(account_id: str, payload: dict = Body(...)):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    enabled = bool(payload.get("enabled", True))
    store.set_enabled(acc.provider, account_id, enabled)
    return {"ok": True}


# ── 刷新额度（实时用量监控）─────────────────────────────────────────────────
@router.post("/accounts/refresh")
async def refresh(payload: dict = Body(default=None)):
    payload = payload or {}
    if payload.get("all"):
        targets = [a for a in store.list_accounts("zai") if a.mode == "jwt"]
    else:
        ids = set(payload.get("ids") or [])
        targets = [a for a in store.list_accounts() if a.id in ids and a.mode == "jwt"]
    summary = await refresh_accounts(targets)
    return {"summary": summary, "count": len(targets)}


@router.post("/accounts/{account_id}/refresh")
async def refresh_one(account_id: str):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if acc.mode != "jwt":
        return {"ok": False, "message": "仅 Coding Plan (JWT) 账号支持额度查询"}
    res = await fetch_quota(acc)
    return {"ok": "error" not in res, "result": res, "account": acc.public_view()}


# ── 逐账号模型调用测试 ──────────────────────────────────────────────────────
@router.post("/accounts/{account_id}/test")
async def test_account(account_id: str, request: Request, payload: dict = Body(...)):
    account = store.find_any(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    model = str(payload.get("model") or "GLM-5.3").strip()
    model = next((item for item in _TEST_MODELS if item.lower() == model.lower()), "")
    if not model:
        raise HTTPException(400, "不支持的测试模型")
    if not store.reserve_specific(account):
        raise HTTPException(409, "账号正在处理其他请求，请稍后再试")

    request_meta = {
        "request_id": f"test-{secrets.token_hex(3)}",
        "started": time.monotonic(),
        "method": "TEST",
        "path": request.url.path,
        "model": model,
        "protocol": "账号测试",
    }
    test_body = {
        "model": model,
        "max_tokens": 16,
        "stream": False,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Reply with exactly: OK"}]}
        ],
    }
    try:
        for attempt in range(2):
            verify_param = None
            verify_region = "sgp"
            if account.provider == "zai" and account.mode == "jwt":
                try:
                    verify_param, verify_region = await asyncio.wait_for(
                        captcha_manager.get_verify_param(request.url.port or settings.PORT),
                        timeout=settings.ACCOUNT_TEST_CAPTCHA_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    record_request(
                        request_meta,
                        account,
                        504,
                        error_type="captcha_timeout",
                    )
                    return {
                        "ok": False,
                        "status_code": 504,
                        "type": "captcha_timeout",
                        "message": (
                            "测试验证码生成超时"
                            f"（{settings.ACCOUNT_TEST_CAPTCHA_TIMEOUT} 秒）"
                        ),
                    }
                except InteractiveCaptchaRequired:
                    record_request(
                        request_meta,
                        account,
                        409,
                        error_type="captcha_interactive_required",
                    )
                    return {
                        "ok": False,
                        "status_code": 409,
                        "type": "captcha_interactive_required",
                        "message": "需要在 noVNC 中完成人机验证后重试",
                    }
                except Exception:  # noqa: BLE001
                    record_request(request_meta, account, 503, error_type="captcha_error")
                    return {
                        "ok": False,
                        "status_code": 503,
                        "type": "captcha_error",
                        "message": "无法完成人机验证",
                    }

            try:
                url, headers, upstream_body = build_request(
                    account,
                    test_body,
                    verify_param,
                    {},
                    verify_region,
                )
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        float(settings.ACCOUNT_TEST_UPSTREAM_TIMEOUT),
                        connect=min(15.0, float(settings.ACCOUNT_TEST_UPSTREAM_TIMEOUT)),
                    )
                ) as client:
                    response = await client.post(
                        url,
                        headers=headers,
                        content=json.dumps(upstream_body, ensure_ascii=False).encode("utf-8"),
                    )
            except (httpx.HTTPError, RuntimeError):
                account.mark_failure(FailureKind.TRANSPORT, "账号测试连接失败")
                account.last_checked_at = time.time()
                store.update_account(account)
                record_request(request_meta, account, 503, error_type=FailureKind.TRANSPORT)
                return {
                    "ok": False,
                    "status_code": 503,
                    "type": FailureKind.TRANSPORT,
                    "message": "连接上游失败",
                }

            usage = UsageTracker()
            usage.feed(response.content)
            if 200 <= response.status_code < 300:
                account.mark_success()
                account.last_checked_at = time.time()
                store.update_account(account)
                record_request(request_meta, account, response.status_code, usage=usage)
                return {
                    "ok": True,
                    "status_code": response.status_code,
                    "model": model,
                    "message": "模型调用成功",
                }

            failure = (
                classify_upstream_failure(response.status_code, response.text)
                or FailureKind.UPSTREAM
            )
            if failure == FailureKind.CAPTCHA and attempt == 0 and account.mode == "jwt":
                captcha_manager.invalidate()
                continue
            if failure == FailureKind.AUTH:
                account.mark_failure(failure, "账号测试鉴权失败", status=Status.INVALID)
            elif failure == FailureKind.EXHAUSTED:
                account.mark_failure(failure, "账号测试额度用完", status=Status.EXHAUSTED)
            elif failure in (FailureKind.RATE_LIMIT, FailureKind.RISK_3012):
                account.start_cooldown(
                    kind=failure,
                    reason="账号测试触发上游风控",
                    base_seconds=settings.COOLING_SECONDS,
                    max_seconds=settings.RISK_3012_COOLDOWN_MAX,
                )
            else:
                account.mark_failure(failure, f"账号测试 HTTP {response.status_code}")
            account.last_checked_at = time.time()
            store.update_account(account)
            record_request(
                request_meta,
                account,
                response.status_code,
                usage=usage,
                error_type=failure,
            )
            return {
                "ok": False,
                "status_code": response.status_code,
                "type": failure,
                "message": f"模型调用失败（HTTP {response.status_code}）",
            }
    finally:
        store.release(account)


# ── 流量日志 ─────────────────────────────────────────────────────────────────
@router.get("/logs")
async def request_logs(
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    query: str = Query("", max_length=120),
    status: str = Query("all", pattern="^(all|success|error)$"),
):
    return store.list_request_logs(limit=limit, offset=offset, query=query, status=status)


@router.delete("/logs")
async def clear_request_logs():
    return {"deleted": store.clear_request_logs()}


# ── OAuth 登录（Z.AI 3.7.7 authorization-code flow）────────────────────────
def _get_login_session(flow_id: str) -> _LoginSession:
    session = _login_flows.get(flow_id)
    if session is None:
        raise HTTPException(404, "登录会话不存在或已过期")
    if time.time() - session.created_at > settings.OAUTH_FLOW_TIMEOUT:
        _login_flows.pop(flow_id, None)
        raise HTTPException(410, "登录会话已过期，请重新生成认证链接")
    return session


async def _exchange_and_save_oauth(
    callback_url: str, flow: ZaiAuthFlow | None = None
) -> dict:
    try:
        if flow is None:
            flow, result = await ZaiAuthFlow.exchange_external_callback_url(callback_url)
        else:
            result = await flow.exchange_callback_url(callback_url)
        token = str(result.get("token") or "").strip()
        if not token:
            raise OAuthError("授权结果缺少 ZCode JWT")
        account = store.add_account(
            "zai",
            flow.account_name(result),
            token,
        )
        oauth_record = result.get("oauth")
        if isinstance(oauth_record, dict):
            account.oauth = oauth_record
            store.update_account(account)
        try:
            await refresh_accounts([account])
        except Exception:  # noqa: BLE001 - 额度失败不应撤销已成功的授权
            pass
        return account.public_view()
    except OAuthError as err:
        raise HTTPException(400, str(err)) from err
    except Exception as err:  # noqa: BLE001 - 不向前端泄漏未知异常或凭据
        raise HTTPException(502, "授权登录失败，请重新尝试") from err


@router.post("/login/start")
async def login_start():
    """生成可在用户本地浏览器打开的官方 Z.AI 授权链接。"""
    _login_flows.clear()
    flow = ZaiAuthFlow()
    flow_id, authorize_url = await flow.init()
    session = _LoginSession(flow=flow, created_at=time.time())
    _login_flows[flow_id] = session
    return {
        "flow_id": flow_id,
        "authorize_url": authorize_url,
        "expires_in": settings.OAUTH_FLOW_TIMEOUT,
    }


@router.post("/login/complete/{flow_id}")
async def login_complete(flow_id: str, payload: dict = Body(...)):
    """校验用户粘贴的官方回调网址，兑换 JWT 并加入账号池。"""
    session = _get_login_session(flow_id)
    callback_url = str(payload.get("callback_url") or "").strip()
    account = await _exchange_and_save_oauth(callback_url, session.flow)
    _login_flows.pop(flow_id, None)
    return {"status": "ready", "account": account}


@router.post("/login/import-callback")
async def login_import_callback(payload: dict = Body(...)):
    """直接导入由本地 ZCode/浏览器生成、尚未消费的官方回调网址。"""
    callback_url = str(payload.get("callback_url") or "").strip()
    account = await _exchange_and_save_oauth(callback_url)
    return {"status": "ready", "account": account}


@router.get("/login/poll/{flow_id}")
async def login_poll(flow_id: str):
    """兼容旧前端：本地浏览器模式始终等待用户粘贴回调网址。"""
    session = _get_login_session(flow_id)
    remaining = max(0, settings.OAUTH_FLOW_TIMEOUT - int(time.time() - session.created_at))
    return {"status": "pending", "expires_in": remaining}


@router.delete("/login/{flow_id}")
async def login_cancel(flow_id: str):
    _login_flows.pop(flow_id, None)
    return {"ok": True}


async def shutdown_login_flows() -> None:
    _login_flows.clear()


# ── 设置 ─────────────────────────────────────────────────────────────────────
@router.get("/settings")
async def get_settings():
    return {
        "admin_key": store.admin_key(),
        "gateway_key": store.gateway_key(),
        "quota_refresh_interval": store.quota_refresh_interval(),
    }


@router.put("/settings")
async def update_settings(payload: dict = Body(...)):
    if "admin_key" in payload:
        key = (payload["admin_key"] or "").strip()
        if not key:
            raise HTTPException(400, "后台密钥不能为空")
        store.set_setting("admin_key", key)
    if "gateway_key" in payload:
        store.set_setting("gateway_key", (payload["gateway_key"] or "").strip())
    if "quota_refresh_interval" in payload:
        try:
            interval = max(0, int(payload["quota_refresh_interval"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "刷新间隔必须是非负整数")
        store.set_setting("quota_refresh_interval", str(interval))
    return {"ok": True}


# ── 导入 / 导出 ─────────────────────────────────────────────────────────────
@router.get("/export")
async def export_accounts():
    return store.export()


@router.post("/import")
async def import_accounts(payload: dict = Body(...)):
    count = store.import_accounts(payload)
    return {"count": count}
