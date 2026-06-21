"""上游请求构建。

负责根据账号凭证选择端点、组装请求头。实际发送与流式透传在 routes/gateway.py。
"""

from __future__ import annotations

import uuid

from . import settings
from .models import Account
from .start_plan import decode_jwt_user_id, prepare_start_plan_body

# The only client header intentionally forwarded upstream.
_PASSTHROUGH_HEADERS = {"anthropic-beta"}


def build_request(
    account: Account,
    body: dict,
    verify_param: str | None,
    incoming_headers: dict | None = None,
    verify_region: str = "sgp",
) -> tuple[str, dict, dict]:
    """Return target URL, sanitized upstream headers, and transformed body."""
    provider = account.provider
    is_start_plan = provider == "zai" and account.mode == "jwt"

    if provider == "zai":
        if account.mode == "jwt" and account.jwt_token:
            target_url = settings.UPSTREAM["zai"]
            auth = {"Authorization": f"Bearer {account.jwt_token}"}
        elif account.api_key:
            target_url = settings.UPSTREAM["zai_fallback"]
            auth = {"x-api-key": account.api_key}
        else:
            raise RuntimeError("账号缺少有效凭证")
    elif provider == "bigmodel":
        target_url = settings.UPSTREAM["bigmodel"]
        if not account.api_key:
            raise RuntimeError("BigModel 账号缺少 API Key")
        auth = {"x-api-key": account.api_key}
    else:
        raise RuntimeError(f"未知提供商: {provider}")

    headers = {
        "content-type": "application/json",
        **auth,
        "User-Agent": settings.USER_AGENT,
        "X-ZCode-App-Version": settings.ZCODE_CLIENT_VERSION,
        "X-Title": f"Z Code@{settings.ZCODE_SOURCE_TITLE}",
        "X-ZCode-Agent": "glm",
        "HTTP-Referer": settings.ZCODE_REFERER,
        "x-request-id": str(uuid.uuid4()),
        "x-zcode-trace-id": str(uuid.uuid4()),
        "x-query-id": f"query_{uuid.uuid4()}",
        "x-session-id": str(uuid.uuid4()),
    }
    if not is_start_plan:
        headers["anthropic-version"] = "2023-06-01"
    if verify_param:
        headers["X-Aliyun-Captcha-Verify-Param"] = verify_param
        headers["X-Aliyun-Captcha-Verify-Region"] = verify_region

    for key, value in (incoming_headers or {}).items():
        lower = key.lower()
        if lower not in _PASSTHROUGH_HEADERS:
            continue
        headers[lower] = value

    upstream_body = body
    if is_start_plan:
        user_id = account.user_id or decode_jwt_user_id(account.jwt_token)
        upstream_body = prepare_start_plan_body(body, user_id=user_id)

    return target_url, headers, upstream_body
