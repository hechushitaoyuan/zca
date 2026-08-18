"""Z.AI 3.7.7 OAuth authorization-code flow.

The account owner opens the official authorize URL in a local browser. Z.ai
returns through its official HTTPS bridge and then attempts to launch the
``zcode://oauth/callback`` desktop deep link. Since that deep link belongs to
the user's local ZCode installation, zca accepts the final bridge/deep-link URL
copied back into the admin panel, validates its random state, and exchanges the
one-time code. Only the final Coding Plan JWT enters the account store.
"""

from __future__ import annotations

import secrets
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import settings

_DEEP_LINK = "zcode://oauth/callback"
_MIN_CODE_LEN = 8
_MAX_CODE_LEN = 4096
_MAX_CALLBACK_URL_LEN = 16_384
_MIN_JWT_LEN = 32


class OAuthError(RuntimeError):
    """Safe OAuth error suitable for the admin UI."""


class OAuthCallbackError(OAuthError):
    """The pasted callback URL is missing, malformed, or belongs to another flow."""


class OAuthTokenError(OAuthError):
    """The provider rejected the authorization-code exchange."""


def _official_redirect_uri() -> str:
    return f"{settings.OAUTH_BRIDGE_URL}?{urlencode({'redirect': _DEEP_LINK})}"


class ZaiAuthFlow:
    def __init__(self) -> None:
        self.flow_id = secrets.token_urlsafe(18)
        self.state = secrets.token_urlsafe(32)
        self.redirect_uri = _official_redirect_uri()
        self.authorize_url = self._build_authorize_url()

    def _build_authorize_url(self) -> str:
        query = urlencode(
            {
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "client_id": settings.OAUTH_CLIENT_ID,
                "state": self.state,
            }
        )
        return f"{settings.OAUTH_AUTHORIZE_URL}?{query}"

    async def init(self) -> tuple[str, str]:
        return self.flow_id, self.authorize_url

    def code_from_callback_url(self, callback_url: str) -> str:
        value = str(callback_url or "").strip()
        if not value or len(value) > _MAX_CALLBACK_URL_LEN:
            raise OAuthCallbackError("请粘贴认证完成后地址栏里的完整回调网址")
        try:
            parsed = urlparse(value)
        except ValueError as err:
            raise OAuthCallbackError("回调网址格式无效") from err

        query = parse_qs(parsed.query, keep_blank_values=True)
        is_official_bridge = (
            parsed.scheme == "https"
            and parsed.hostname == "zcode.z.ai"
            and parsed.path.rstrip("/") == "/app/oauth/login"
            and query.get("redirect", [""])[0] == _DEEP_LINK
        )
        is_official_deep_link = (
            parsed.scheme == "zcode"
            and parsed.hostname == "oauth"
            and parsed.path.rstrip("/") == "/callback"
        )
        if not (is_official_bridge or is_official_deep_link):
            raise OAuthCallbackError("这不是 ZCode 官方授权回调网址")

        if query.get("error", [""])[0]:
            raise OAuthCallbackError("Z.ai 授权已取消或被拒绝")
        if query.get("state", [""])[0] != self.state:
            raise OAuthCallbackError("回调 state 不匹配，请使用本次生成的认证链接")
        code = query.get("code", query.get("authCode", [""]))[0]
        if not (_MIN_CODE_LEN <= len(code) <= _MAX_CODE_LEN):
            raise OAuthCallbackError("回调网址中缺少有效授权码")
        return code

    async def exchange_callback_url(self, callback_url: str) -> dict:
        return await self.exchange_code(self.code_from_callback_url(callback_url))

    async def exchange_code(self, code: str) -> dict:
        if not (_MIN_CODE_LEN <= len(code) <= _MAX_CODE_LEN):
            raise OAuthTokenError("授权码格式无效")
        body = {
            "provider": "zai",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "state": self.state,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    settings.OAUTH_TOKEN_URL,
                    headers={"Content-Type": "application/json"},
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") or {}
                if payload.get("code") not in (None, 0):
                    raise OAuthTokenError(
                        f"Z.ai 拒绝 token 兑换（code={payload.get('code')}）"
                    )
                token = str(data.get("token") or "").strip()
                access_token = str(
                    (data.get("zai") or {}).get("access_token") or ""
                ).strip()
                if len(token) < _MIN_JWT_LEN or token.count(".") != 2:
                    raise OAuthTokenError("token 兑换结果缺少有效 ZCode JWT")

                user = data.get("user") if isinstance(data.get("user"), dict) else {}
                if access_token:
                    try:
                        profile_response = await client.get(
                            settings.OAUTH_USERINFO_URL,
                            headers={"Authorization": f"Bearer {access_token}"},
                        )
                        profile_response.raise_for_status()
                        profile = profile_response.json()
                        if isinstance(profile, dict):
                            user = {**user, **profile}
                    except (httpx.HTTPError, ValueError):
                        pass
        except OAuthTokenError:
            raise
        except httpx.HTTPStatusError as err:
            raise OAuthTokenError(
                f"token 兑换请求失败（HTTP {err.response.status_code}）"
            ) from err
        except (httpx.HTTPError, ValueError) as err:
            raise OAuthTokenError("token 兑换请求失败") from err

        return {"token": token, "user": user}

    @staticmethod
    def account_name(result: dict) -> str:
        user = result.get("user") if isinstance(result.get("user"), dict) else {}
        for key in ("email", "username", "name", "display_name", "nickname", "sub"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:120]
        return "zai-oauth"
