"""核心网关：兼容 Anthropic Messages 协议的 /v1/messages。

实现多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import logs, settings
from ..agent import build_request
from ..auth_admin import verify_gateway_key
from ..captcha import InteractiveCaptchaRequired, captcha_manager
from ..models import Account, FailureKind, Status
from ..quota import fetch_quota
from ..store import store
from ..traffic import UsageTracker, record_request
from ..upstream_errors import classify_upstream_failure

router = APIRouter()

# Initial attempt plus one narrowly-scoped captcha retry.
MAX_CAPTCHA_RETRIES = 2
MAX_ACCOUNT_ATTEMPTS = 5

# Z.AI 上游模型名大小写敏感
MODEL_NAME_MAP = {
    "glm-5.3": "GLM-5.3",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-5": "GLM-5",
    "glm-4.7": "GLM-4.7",
}

# /v1/models 对外公布的可用模型
AVAILABLE_MODELS = ["GLM-5.3", "GLM-5.2", "GLM-5-Turbo"]


@router.get("/v1", dependencies=[Depends(verify_gateway_key)])
async def api_root():
    """API base discovery endpoint for clients configured with a /v1 base URL."""
    return {
        "object": "api",
        "protocol": "anthropic-messages",
        "messages": "/v1/messages",
        "models": "/v1/models",
    }


def _detect_provider(body: dict, headers) -> str:
    model = body.get("model") or ""
    if model.startswith("bigmodel/") or headers.get("x-provider") == "bigmodel":
        return "bigmodel"
    return "zai"


def _normalize_body(body: dict) -> dict:
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        model = MODEL_NAME_MAP.get(model.lower(), model)
        body["model"] = model

    messages = body.get("messages")
    if isinstance(messages, list):
        bridged = []
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                bridged.append({**msg, "content": [{"type": "text", "text": msg["content"]}]})
            else:
                bridged.append(msg)
        body["messages"] = bridged
    return body


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
    return ""


@router.get("/v1/models", dependencies=[Depends(verify_gateway_key)])
async def list_models():
    """列出可用模型（Anthropic /v1/models 风格）。"""
    return {
        "object": "list",
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2025-01-01T00:00:00Z"}
            for i in AVAILABLE_MODELS
        ],
    }


@router.post("/v1/messages", dependencies=[Depends(verify_gateway_key)])
async def messages(request: Request):
    req_id = secrets.token_hex(3)
    request_meta = {
        "request_id": req_id,
        "started": time.monotonic(),
        "method": request.method,
        "path": request.url.path,
        "model": "-",
        "protocol": "Anthropic",
    }
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        record_request(request_meta, None, 400, error_type="invalid_request")
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)
    request_meta["model"] = str(body.get("model") or "-")
    # 验证码页面由本服务托管，端口取实际请求端口（兼容任意启动端口）
    port = request.url.port or settings.PORT
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))

    tried: set[str] = set()

    for _ in range(MAX_ACCOUNT_ATTEMPTS):
        account = await _select_account(provider, tried)
        if account is None:
            break
        tried.add(account.id)
        needs_captcha = provider == "zai" and account.mode == "jwt"

        result = await _try_account(
            req_id,
            account,
            body,
            incoming_headers,
            port,
            needs_captcha,
            request_meta=request_meta,
        )
        if result is _NEXT_ACCOUNT:
            continue
        return result

    error_type, message = _pool_error(provider)
    logs.req_err(req_id, message)
    record_request(request_meta, None, 503, error_type=error_type)
    return JSONResponse(
        {"error": {"message": message, "type": error_type}},
        status_code=503,
    )


_NEXT_ACCOUNT = object()


async def _select_account(provider: str, tried: set[str]):
    """唯一账号短暂忙碌时等待释放，避免并发探针被误报成额度耗尽。"""
    account = store.select(provider, skip_ids=tried)
    if account is not None or tried or settings.ACCOUNT_BUSY_WAIT_TIMEOUT <= 0:
        return account
    deadline = time.monotonic() + settings.ACCOUNT_BUSY_WAIT_TIMEOUT
    while time.monotonic() < deadline:
        state = store.pool_state(provider)
        if state["busy"] <= 0:
            return store.select(provider, skip_ids=tried)
        await asyncio.sleep(0.1)
        account = store.select(provider, skip_ids=tried)
        if account is not None:
            return account
    return None


def _pool_error(provider: str) -> tuple[str, str]:
    state = store.pool_state(provider)
    if state["busy"]:
        return "accounts_busy", "可用账号正在处理其他请求或人机验证，请稍后重试"
    if state["cooling"]:
        return "accounts_cooling", "账号正处于上游限流冷却期，请稍后重试"
    if state["exhausted"] and not state["selectable"]:
        return "quota_exhausted", "所有账号的模型额度均已用完"
    if state["invalid"] and not state["selectable"]:
        return "accounts_invalid", "所有账号凭证均已失效，请重新授权"
    if state["total"] == 0:
        return "no_accounts", "账号池中没有账号，请先添加账号"
    return "no_available_account", "当前没有可调度账号，请在后台检查启用状态"


async def _try_account(
    req_id,
    account,
    body,
    incoming_headers,
    port,
    needs_captcha,
    *,
    request_meta: dict | None = None,
):
    """尝试用单个账号转发，含验证码续期。返回 Response 或 _NEXT_ACCOUNT。"""
    stream_owns_reservation = False
    try:
        for attempt in range(MAX_CAPTCHA_RETRIES):
            verify_param = None
            verify_region = "sgp"
            if needs_captcha:
                try:
                    verify_param, verify_region = await captcha_manager.get_verify_param(port)
                except InteractiveCaptchaRequired as err:
                    logs.req_err(req_id, f"需要交互式人机校验: {err}")
                    record_request(
                        request_meta,
                        account,
                        409,
                        error_type="captcha_interactive_required",
                    )
                    return JSONResponse(
                        {
                            "error": {
                                "message": "当前风控要求人工验证，请在账号池生成“本地验证”链接，完成后立即重试",
                                "type": "captcha_interactive_required",
                            }
                        },
                        status_code=409,
                    )
                except Exception as err:  # noqa: BLE001
                    logs.req_err(req_id, f"人机校验失败: {err}")
                    record_request(request_meta, account, 503, error_type="captcha_error")
                    return JSONResponse(
                        {"error": {"message": "无法完成人机校验", "type": "captcha_error"}},
                        status_code=503,
                    )

            try:
                url, headers, upstream_body = build_request(
                    account, body, verify_param, incoming_headers, verify_region
                )
                payload = json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")
            except RuntimeError as err:
                account.mark_failure(FailureKind.AUTH, str(err), status=Status.INVALID)
                store.update_account(account)
                logs.warn(req_id, f"账号 {account.name} 凭证无效，切换下一个")
                return _NEXT_ACCOUNT

            client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0)
            )
            cm = client.stream("POST", url, headers=headers, content=payload)
            try:
                resp = await cm.__aenter__()
            except httpx.HTTPError as err:
                await client.aclose()
                account.start_cooldown(
                    kind=FailureKind.TRANSPORT,
                    reason=f"连接失败: {err}",
                    base_seconds=settings.COOLING_SECONDS,
                    max_seconds=settings.COOLING_SECONDS,
                )
                store.update_account(account)
                logs.warn(req_id, f"账号 {account.name} 连接失败，切换下一个")
                return _NEXT_ACCOUNT

            status_code = resp.status_code

            # Only error responses are buffered for classification. Successful SSE is
            # handed directly to StreamingResponse without clone/read/tee.
            if status_code >= 400:
                text = (await resp.aread()).decode("utf-8", "ignore")
                await cm.__aexit__(None, None, None)
                await client.aclose()
                failure = classify_upstream_failure(status_code, text) or FailureKind.UPSTREAM

                if failure == FailureKind.CAPTCHA and needs_captcha:
                    if attempt + 1 < MAX_CAPTCHA_RETRIES:
                        captcha_manager.invalidate()
                        logs.warn(req_id, f"账号 {account.name} 验证码失效，仅重试一次")
                        continue
                    account.mark_failure(FailureKind.CAPTCHA, "验证码校验失败")
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 验证码重试失败，切换下一个")
                    return _NEXT_ACCOUNT

                if failure == FailureKind.RISK_3012:
                    delay = account.start_cooldown(
                        kind=FailureKind.RISK_3012,
                        reason="上游风控 3012",
                        base_seconds=settings.RISK_3012_COOLDOWN_BASE,
                        max_seconds=settings.RISK_3012_COOLDOWN_MAX,
                    )
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 进入风控冷冻 {delay} 秒")
                    return _NEXT_ACCOUNT

                if failure == FailureKind.EXHAUSTED:
                    account.mark_failure(FailureKind.EXHAUSTED, "额度已用完", status=Status.EXHAUSTED)
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 额度用完，切换下一个")
                    asyncio.create_task(_safe_refresh(account))
                    return _NEXT_ACCOUNT

                if failure == FailureKind.AUTH:
                    account.mark_failure(
                        FailureKind.AUTH,
                        f"鉴权失败 HTTP {status_code}",
                        status=Status.INVALID,
                    )
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 鉴权失败 {status_code}，切换下一个")
                    return _NEXT_ACCOUNT

                if failure == FailureKind.RATE_LIMIT:
                    account.start_cooldown(
                        kind=FailureKind.RATE_LIMIT,
                        reason="上游限流 429",
                        base_seconds=settings.COOLING_SECONDS,
                        max_seconds=settings.COOLING_SECONDS,
                    )
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 被限流 429，切换下一个")
                    return _NEXT_ACCOUNT

                account.mark_failure(FailureKind.UPSTREAM, f"上游错误 HTTP {status_code}")
                store.update_account(account)
                logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
                record_request(request_meta, account, status_code, error_type=failure)
                return JSONResponse(
                    _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                    status_code=status_code,
                )

            account.mark_success()
            store.update_account(account)
            asyncio.create_task(_safe_refresh(account))

            content_type = resp.headers.get("content-type", "application/json")
            usage = UsageTracker()

            async def _body_iter():
                completed = False
                stream_error: str | None = None
                try:
                    async for chunk in resp.aiter_bytes():
                        usage.feed(chunk)
                        yield chunk
                    completed = True
                    logs.req_ok(req_id)
                except Exception as err:  # noqa: BLE001
                    stream_error = "stream_error"
                    logs.req_err(req_id, f"流传输中断: {err}")
                finally:
                    await cm.__aexit__(None, None, None)
                    await client.aclose()
                    record_request(
                        request_meta,
                        account,
                        status_code if completed else 499,
                        usage=usage,
                        error_type=stream_error or (None if completed else "client_disconnected"),
                    )
                    store.release(account)

            out_headers = {"Cache-Control": "no-cache"}
            stream_owns_reservation = True
            return StreamingResponse(
                _body_iter(),
                status_code=status_code,
                media_type=content_type,
                headers=out_headers,
            )

        logs.warn(req_id, f"账号 {account.name} 验证码连续失败，切换下一个")
        return _NEXT_ACCOUNT
    finally:
        if not stream_owns_reservation:
            store.release(account)


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


async def _safe_refresh(account: Account) -> None:
    try:
        if account.provider == "zai" and account.mode == "jwt":
            await fetch_quota(account)
    except Exception:  # noqa: BLE001
        pass
