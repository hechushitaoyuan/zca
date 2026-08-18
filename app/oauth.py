"""Z.AI 3.7.7 OAuth authorization-code flow.

The official desktop client opens the Z.AI authorize page, returns through the
official ``/app/oauth/login`` bridge, and receives a ``zcode://oauth/callback``
deep link.  zca runs that page in an isolated, visible Chromium session and
captures only the one-time authorization code. Browser cookies and OAuth access
tokens are never persisted; only the final Coding Plan JWT enters the account
store.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import shutil
import signal
import tempfile
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import settings

_DEEP_LINK = "zcode://oauth/callback"
_CODE_MARKER = b"OAUTH_CODE_B64="
_DIAG_MAX = 200
_MIN_CODE_LEN = 8
_MAX_CODE_LEN = 4096
_MIN_JWT_LEN = 32


class OAuthError(RuntimeError):
    """Safe, bounded OAuth error suitable for the admin UI."""


class OAuthBrowserError(OAuthError):
    """The visible authorization browser did not produce a callback code."""


class OAuthTokenError(OAuthError):
    """The provider rejected the authorization-code exchange."""


def _tail(raw: bytes | str | None, limit: int = _DIAG_MAX) -> str:
    if not raw:
        return ""
    text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
    return " ".join(text.split())[-limit:]


def _official_redirect_uri() -> str:
    return f"{settings.OAUTH_BRIDGE_URL}?{urlencode({'redirect': _DEEP_LINK})}"


class ZaiAuthFlow:
    def __init__(self) -> None:
        self.flow_id = secrets.token_urlsafe(18)
        self.state = secrets.token_urlsafe(32)
        self.redirect_uri = _official_redirect_uri()
        self.authorize_url = self._build_authorize_url()
        self._profile_dir: Path | None = None

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
        """Compatibility wrapper used by the web and CLI entry points."""
        return self.flow_id, self.authorize_url

    async def run(self) -> dict:
        code = await self._run_browser()
        return await self.exchange_code(code)

    async def _run_browser(self) -> str:
        if not settings.OAUTH_BROWSER_JS.exists():
            raise OAuthBrowserError("授权浏览器脚本不存在")
        argv = [
            settings.NODE_PATH,
            str(settings.OAUTH_BROWSER_JS),
            self.authorize_url,
            self.state,
        ]
        self._profile_dir = Path(tempfile.mkdtemp(prefix="zca-oauth-"))
        try:
            try:
                proc = await self._create_subprocess(argv)
            except FileNotFoundError as err:
                raise OAuthBrowserError("无法启动授权浏览器") from err

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=settings.OAUTH_FLOW_TIMEOUT
                )
            except asyncio.TimeoutError as err:
                await self._terminate(proc)
                raise OAuthBrowserError("授权等待超时，已关闭浏览器") from err
            except asyncio.CancelledError:
                await self._terminate(proc)
                raise

            if proc.returncode != 0:
                detail = _tail(stderr)
                raise OAuthBrowserError(
                    f"授权浏览器未完成（code={proc.returncode}）{detail}".strip()
                )
            return self._extract_code(stdout)
        finally:
            profile_dir, self._profile_dir = self._profile_dir, None
            if profile_dir is not None:
                await asyncio.shield(
                    asyncio.to_thread(shutil.rmtree, profile_dir, True)
                )

    async def _create_subprocess(self, argv: list[str]):
        env = os.environ.copy()
        env.setdefault("ZCODE_CHROMIUM_PATH", settings.CHROMIUM_PATH)
        env["DISPLAY"] = settings.OAUTH_DISPLAY
        env["ZCODE_OAUTH_BROWSER_TIMEOUT"] = str(settings.OAUTH_BROWSER_TIMEOUT)
        env.setdefault("ZCODE_OAUTH_BROWSER_HEADLESS", "0")
        if self._profile_dir is not None:
            env["ZCODE_OAUTH_PROFILE_DIR"] = str(self._profile_dir)
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(settings.CAPTCHA_SOLVER_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

    @staticmethod
    def _extract_code(stdout: bytes) -> str:
        marker_line = next(
            (line for line in stdout.splitlines() if line.startswith(_CODE_MARKER)), None
        )
        if marker_line is None:
            raise OAuthBrowserError("授权浏览器输出缺少回调标记")
        encoded = marker_line[len(_CODE_MARKER) :]
        try:
            code = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)).decode()
        except (ValueError, UnicodeDecodeError) as err:
            raise OAuthBrowserError("授权回调编码无效") from err
        if not (_MIN_CODE_LEN <= len(code) <= _MAX_CODE_LEN):
            raise OAuthBrowserError(f"授权回调长度无效（len={len(code)}）")
        return code

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
                access_token = str((data.get("zai") or {}).get("access_token") or "").strip()
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

    @staticmethod
    async def _terminate(proc) -> None:
        try:
            pid = getattr(proc, "pid", None)
            if pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.shield(proc.wait())
        except (asyncio.CancelledError, ProcessLookupError, ChildProcessError):
            pass
