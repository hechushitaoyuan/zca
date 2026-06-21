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
from ..captcha import captcha_manager
from ..models import Account, FailureKind, Status
from ..quota import fetch_quota
from ..store import store
from ..upstream_errors import classify_upstream_failure

router = APIRouter()

# Initial attempt plus one narrowly-scoped captcha retry.
MAX_CAPTCHA_RETRIES = 2
MAX_ACCOUNT_ATTEMPTS = 5

# Z.AI 上游模型名大小写敏感
MODEL_NAME_MAP = {
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
}

# /v1/models 对外公布的可用模型
AVAILABLE_MODELS = ["GLM-5.2", "GLM-5-Turbo"]

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
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)
    # 验证码页面由本服务托管，端口取实际请求端口（兼容任意启动端口）
    port = request.url.port or settings.PORT
    req_id = secrets.token_hex(3)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))

    tried: set[str] = set()

    for _ in range(MAX_ACCOUNT_ATTEMPTS):
        account = store.select(provider, skip_ids=tried)
        if account is None:
            break
        tried.add(account.id)
        needs_captcha = provider == "zai" and account.mode == "jwt"

        result = await _try_account(req_id, account, body, incoming_headers, port, needs_captcha)
        if result is _NEXT_ACCOUNT:
            continue
        return result

    logs.req_err(req_id, "无可用账号 / 额度均已耗尽")
    return JSONResponse(
        {"error": {"message": "所有账号均不可用或额度已用完，请在后台检查账号状态", "type": "no_available_account"}},
        status_code=503,
    )


_NEXT_ACCOUNT = object()


async def _try_account(req_id, account, body, incoming_headers, port, needs_captcha):
    """尝试用单个账号转发，含验证码续期。返回 Response 或 _NEXT_ACCOUNT。"""
    stream_owns_reservation = False
    try:
        for attempt in range(MAX_CAPTCHA_RETRIES):
            verify_param = None
            if needs_captcha:
                try:
                    verify_param = await captcha_manager.get_verify_param(port)
                except Exception as err:  # noqa: BLE001
                    logs.req_err(req_id, f"人机校验失败: {err}")
                    return JSONResponse(
                        {"error": {"message": "无法完成人机校验", "type": "captcha_error"}},
                        status_code=503,
                    )

            try:
                url, headers, upstream_body = build_request(
                    account, body, verify_param, incoming_headers
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
                failure = classify_upstream_failure(status_code, text)

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
                return JSONResponse(
                    _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                    status_code=status_code,
                )

            account.mark_success()
            store.update_account(account)
            asyncio.create_task(_safe_refresh(account))

            content_type = resp.headers.get("content-type", "application/json")

            async def _body_iter():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                    logs.req_ok(req_id)
                except Exception as err:  # noqa: BLE001
                    logs.req_err(req_id, f"流传输中断: {err}")
                finally:
                    await cm.__aexit__(None, None, None)
                    await client.aclose()
                    store.release(account)

            out_headers = {"Cache-Control": "no-cache"}
            stream_owns_reservation = True
            return StreamingResponse(
                _body_iter(), status_code=status_code, media_type=content_type, headers=out_headers
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
