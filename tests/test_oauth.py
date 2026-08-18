from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import AsyncMock, patch

from app import oauth, settings
from app.models import Account
from app.oauth import OAuthCallbackError, OAuthTokenError, ZaiAuthFlow
from app.routes import admin_api


VALID_CODE = "authorization-code-123"
VALID_JWT = f"{'h' * 12}.{'p' * 12}.{'s' * 12}"
DEEP_LINK = "zcode://oauth/callback"


def bridge_callback(flow: ZaiAuthFlow, **overrides: str) -> str:
    query = {
        "redirect": DEEP_LINK,
        "code": VALID_CODE,
        "state": flow.state,
        **overrides,
    }
    return f"https://zcode.z.ai/app/oauth/login?{urlencode(query)}"


class OAuthFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_authorize_url_matches_official_377_flow(self) -> None:
        flow = ZaiAuthFlow()
        parsed = urlparse(flow.authorize_url)
        query = parse_qs(parsed.query)
        self.assertEqual(
            (parsed.scheme, parsed.netloc, parsed.path),
            ("https", "chat.z.ai", "/api/oauth/authorize"),
        )
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], [settings.OAUTH_CLIENT_ID])
        self.assertEqual(query["state"], [flow.state])
        self.assertEqual(query["redirect_uri"], [flow.redirect_uri])

        redirect = urlparse(flow.redirect_uri)
        self.assertEqual(
            (redirect.scheme, redirect.netloc, redirect.path),
            ("https", "zcode.z.ai", "/app/oauth/login"),
        )
        self.assertEqual(parse_qs(redirect.query)["redirect"], [DEEP_LINK])

    def test_accepts_official_bridge_callback_from_local_browser(self) -> None:
        flow = ZaiAuthFlow()
        self.assertEqual(flow.code_from_callback_url(bridge_callback(flow)), VALID_CODE)

    def test_accepts_official_zcode_deep_link(self) -> None:
        flow = ZaiAuthFlow()
        callback = f"zcode://oauth/callback?{urlencode({'code': VALID_CODE, 'state': flow.state})}"
        self.assertEqual(flow.code_from_callback_url(callback), VALID_CODE)

    def test_rejects_wrong_state_origin_and_missing_code_without_leaking_code(self) -> None:
        flow = ZaiAuthFlow()
        cases = (
            bridge_callback(flow, state="wrong-state"),
            f"https://example.com/app/oauth/login?code={VALID_CODE}&state={flow.state}",
            bridge_callback(flow, code=""),
        )
        for callback in cases:
            with self.subTest(callback=urlparse(callback).netloc):
                with self.assertRaises(OAuthCallbackError) as ctx:
                    flow.code_from_callback_url(callback)
                self.assertNotIn(VALID_CODE, str(ctx.exception))

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
            result = await flow.exchange_callback_url(bridge_callback(flow))

        self.assertEqual(
            result,
            {
                "token": VALID_JWT,
                "user": {
                    "username": "fallback-name",
                    "email": "owner@example.com",
                },
            },
        )
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

        flow = ZaiAuthFlow()
        with patch.object(oauth.httpx, "AsyncClient", FakeClient):
            with self.assertRaises(OAuthTokenError):
                await flow.exchange_callback_url(bridge_callback(flow))


class OAuthAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await admin_api.shutdown_login_flows()

    async def test_start_returns_official_authorize_url_and_no_vps_desktop(self) -> None:
        class FakeFlow:
            flow_id = "flow-start"
            authorize_url = "https://chat.z.ai/api/oauth/authorize?state=safe"

            async def init(self):
                return self.flow_id, self.authorize_url

        with (
            patch.object(admin_api, "ZaiAuthFlow", FakeFlow),
            patch.object(settings, "OAUTH_FLOW_TIMEOUT", 600),
        ):
            result = await admin_api.login_start()

        self.assertEqual(result["flow_id"], "flow-start")
        self.assertEqual(result["authorize_url"], FakeFlow.authorize_url)
        self.assertEqual(result["expires_in"], 600)
        self.assertNotIn("novnc_url", result)
        self.assertEqual(await admin_api.login_cancel("flow-start"), {"ok": True})

    async def test_complete_adds_only_final_zcode_jwt(self) -> None:
        class FakeFlow:
            async def exchange_callback_url(self, callback_url):
                self.callback_url = callback_url
                return {"token": VALID_JWT, "user": {"email": "owner@example.com"}}

            @staticmethod
            def account_name(_result):
                return "owner@example.com"

        class FakeStore:
            added = None

            def add_account(self, provider, name, token):
                self.added = (provider, name, token)
                return Account.create(provider, name, token)

        flow = FakeFlow()
        fake_store = FakeStore()
        admin_api._login_flows["flow-complete"] = admin_api._LoginSession(
            flow=flow,  # type: ignore[arg-type]
            created_at=admin_api.time.time(),
        )
        with (
            patch.object(admin_api, "store", fake_store),
            patch.object(admin_api, "refresh_accounts", AsyncMock(return_value={})),
        ):
            result = await admin_api.login_complete(
                "flow-complete", {"callback_url": "https://zcode.z.ai/callback"}
            )

        self.assertEqual(fake_store.added, ("zai", "owner@example.com", VALID_JWT))
        self.assertEqual(flow.callback_url, "https://zcode.z.ai/callback")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["account"]["mode"], "jwt")
        self.assertNotIn("flow-complete", admin_api._login_flows)


if __name__ == "__main__":
    unittest.main()
