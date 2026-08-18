from __future__ import annotations

import asyncio
import base64
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch

import httpx

from app import oauth, settings
from app.models import Account
from app.oauth import OAuthBrowserError, OAuthTokenError, ZaiAuthFlow
from app.routes import admin_api


VALID_CODE = "authorization-code-123"
VALID_JWT = f"{'h' * 12}.{'p' * 12}.{'s' * 12}"


class FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(3600)
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        return self.returncode


class OAuthFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_authorize_url_matches_official_377_flow(self) -> None:
        flow = ZaiAuthFlow()
        parsed = urlparse(flow.authorize_url)
        query = parse_qs(parsed.query)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), (
            "https",
            "chat.z.ai",
            "/api/oauth/authorize",
        ))
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], [settings.OAUTH_CLIENT_ID])
        self.assertEqual(query["state"], [flow.state])
        self.assertEqual(query["redirect_uri"], [flow.redirect_uri])

        redirect = urlparse(flow.redirect_uri)
        self.assertEqual(
            (redirect.scheme, redirect.netloc, redirect.path),
            ("https", "zcode.z.ai", "/app/oauth/login"),
        )
        self.assertEqual(parse_qs(redirect.query)["redirect"], ["zcode://oauth/callback"])

    def test_extracts_base64url_code_without_exposing_marker_noise(self) -> None:
        encoded = base64.urlsafe_b64encode(VALID_CODE.encode()).rstrip(b"=")
        stdout = b"ignored\nOAUTH_CODE_B64=" + encoded + b"\n"
        self.assertEqual(ZaiAuthFlow._extract_code(stdout), VALID_CODE)

    def test_missing_or_short_code_is_rejected(self) -> None:
        with self.assertRaises(OAuthBrowserError):
            ZaiAuthFlow._extract_code(b"no marker")
        encoded = base64.urlsafe_b64encode(b"tiny").rstrip(b"=")
        with self.assertRaises(OAuthBrowserError) as ctx:
            ZaiAuthFlow._extract_code(b"OAUTH_CODE_B64=" + encoded)
        self.assertNotIn("tiny", str(ctx.exception))

    async def test_browser_timeout_kills_and_reaps_process(self) -> None:
        proc = FakeProc(hang=True)
        flow = ZaiAuthFlow()
        profile_dirs = []

        async def create(_argv):
            profile_dirs.append(flow._profile_dir)
            return proc

        flow._create_subprocess = create  # type: ignore[method-assign]
        with patch.object(settings, "OAUTH_FLOW_TIMEOUT", 0.02):
            with self.assertRaises(OAuthBrowserError):
                await flow._run_browser()
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertEqual(len(profile_dirs), 1)
        self.assertFalse(profile_dirs[0].exists())

    async def test_browser_cancellation_kills_and_reaps_process(self) -> None:
        proc = FakeProc(hang=True)
        flow = ZaiAuthFlow()
        profile_dirs = []

        async def create(_argv):
            profile_dirs.append(flow._profile_dir)
            return proc

        flow._create_subprocess = create  # type: ignore[method-assign]
        task = asyncio.create_task(flow._run_browser())
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)
        self.assertEqual(len(profile_dirs), 1)
        self.assertFalse(profile_dirs[0].exists())

    async def test_exchange_returns_only_final_jwt_and_user_profile(self) -> None:
        calls = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload
                self.status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                calls.append(("POST", url, kwargs))
                return FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "token": VALID_JWT,
                            "zai": {"access_token": "sensitive-access-token"},
                            "user": {"username": "fallback-name"},
                        },
                    }
                )

            async def get(self, url, **kwargs):
                calls.append(("GET", url, kwargs))
                return FakeResponse({"email": "owner@example.com"})

        flow = ZaiAuthFlow()
        with patch.object(oauth.httpx, "AsyncClient", FakeClient):
            result = await flow.exchange_code(VALID_CODE)

        self.assertEqual(result, {"token": VALID_JWT, "user": {
            "username": "fallback-name",
            "email": "owner@example.com",
        }})
        self.assertNotIn("access_token", result)
        post_body = calls[0][2]["json"]
        self.assertEqual(post_body["provider"], "zai")
        self.assertEqual(post_body["code"], VALID_CODE)
        self.assertEqual(post_body["state"], flow.state)
        self.assertEqual(post_body["redirect_uri"], flow.redirect_uri)

    async def test_invalid_token_response_is_rejected(self) -> None:
        class FakeResponse:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"code": 0, "data": {"token": "not-a-jwt"}}

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return FakeResponse()

        with patch.object(oauth.httpx, "AsyncClient", FakeClient):
            with self.assertRaises(OAuthTokenError):
                await ZaiAuthFlow().exchange_code(VALID_CODE)


class OAuthAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await admin_api.shutdown_login_flows()

    async def test_completed_flow_adds_only_the_zcode_jwt(self) -> None:
        class FakeFlow:
            flow_id = "flow-one"

            async def run(self):
                return {"token": VALID_JWT, "user": {"email": "owner@example.com"}}

            @staticmethod
            def account_name(_result):
                return "owner@example.com"

        class FakeStore:
            added = None

            def add_account(self, provider, name, token):
                self.added = (provider, name, token)
                return Account.create(provider, name, token)

        fake_store = FakeStore()
        session = admin_api._LoginSession(flow=FakeFlow())  # type: ignore[arg-type]
        admin_api._login_flows["flow-one"] = session
        with (
            patch.object(admin_api, "store", fake_store),
            patch.object(admin_api, "refresh_accounts", AsyncMock(return_value={})),
        ):
            await admin_api._complete_login("flow-one")

        self.assertEqual(fake_store.added, ("zai", "owner@example.com", VALID_JWT))
        self.assertEqual(session.status, "ready")
        self.assertEqual(session.account["mode"], "jwt")

    async def test_failed_flow_does_not_add_account(self) -> None:
        class FakeFlow:
            async def run(self):
                raise OAuthBrowserError("授权等待超时")

        fake_store = unittest.mock.Mock()
        session = admin_api._LoginSession(flow=FakeFlow())  # type: ignore[arg-type]
        admin_api._login_flows["failed"] = session
        with patch.object(admin_api, "store", fake_store):
            await admin_api._complete_login("failed")

        fake_store.add_account.assert_not_called()
        self.assertEqual(session.status, "failed")
        self.assertEqual(session.message, "授权等待超时")

    async def test_start_returns_only_the_private_desktop_and_cancel_stops_flow(self) -> None:
        class FakeFlow:
            flow_id = "flow-start"

            async def init(self):
                return self.flow_id, "https://chat.z.ai/sensitive-authorize-url"

            async def run(self):
                await asyncio.sleep(3600)

        with (
            patch.object(admin_api, "ZaiAuthFlow", FakeFlow),
            patch.object(settings, "OAUTH_NOVNC_URL", "http://100.64.0.1:6081/vnc.html"),
            patch.object(settings, "OAUTH_BROWSER_TIMEOUT", 600_000),
        ):
            result = await admin_api.login_start()

        self.assertEqual(result["flow_id"], "flow-start")
        self.assertEqual(result["novnc_url"], "http://100.64.0.1:6081/vnc.html")
        self.assertEqual(result["expires_in"], 600)
        self.assertNotIn("authorize_url", result)
        self.assertTrue(await admin_api.login_cancel("flow-start"))
        self.assertNotIn("flow-start", admin_api._login_flows)


if __name__ == "__main__":
    unittest.main()
