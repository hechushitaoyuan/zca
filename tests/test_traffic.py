from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from starlette.requests import Request

from app import settings
from app.models import Account, Status
from app.routes import admin_api, gateway, pages
from app.store import Store
from app.traffic import UsageTracker, account_display_name


class RequestLogStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)
        self._patches = [
            patch.object(settings, "DATA_DIR", data_dir),
            patch.object(settings, "DB_PATH", data_dir / "accounts.db"),
        ]
        for item in self._patches:
            item.start()
        self.store = Store()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def _record(self, request_id: str, status_code: int, model: str) -> None:
        self.store.record_request_log(
            request_id=request_id,
            method="POST",
            path="/v1/messages",
            model=model,
            protocol="Anthropic",
            account_id="account-1",
            account_name="dem***@example.com",
            status_code=status_code,
            latency_ms=123,
            input_tokens=10,
            output_tokens=2,
            error_type=None if status_code == 200 else "upstream_error",
        )

    def test_log_filter_pagination_and_clear(self) -> None:
        self._record("one", 200, "GLM-5.3")
        self._record("two", 503, "GLM-5.2")
        self._record("three", 200, "GLM-5-Turbo")

        all_logs = self.store.list_request_logs(limit=2)
        self.assertEqual(all_logs["stats"], {"total": 3, "success": 2, "error": 1})
        self.assertEqual(len(all_logs["items"]), 2)
        self.assertEqual(all_logs["items"][0]["request_id"], "three")

        failures = self.store.list_request_logs(status="error")
        self.assertEqual(failures["stats"]["total"], 1)
        self.assertEqual(failures["items"][0]["status_code"], 503)

        searched = self.store.list_request_logs(query="5-turbo")
        self.assertEqual(searched["stats"]["total"], 1)
        self.assertEqual(self.store.clear_request_logs(), 3)
        self.assertEqual(self.store.list_request_logs()["stats"]["total"], 0)

    def test_specific_account_reservation_is_atomic(self) -> None:
        account = Account.create("zai", "test", "header.payload.signature")
        self.assertTrue(self.store.reserve_specific(account))
        self.assertFalse(self.store.reserve_specific(account))
        self.store.release(account)
        self.assertTrue(self.store.reserve_specific(account))

    def test_old_masked_log_is_displayed_with_current_full_account_name(self) -> None:
        account = self.store.add_account(
            "zai", "full-address@example.com", "header.payload.signature"
        )
        self.store.record_request_log(
            request_id="old",
            method="TEST",
            path=f"/admin/api/accounts/{account.id}/test",
            model="GLM-5.3",
            protocol="账号测试",
            account_id=account.id,
            account_name="ful***@example.com",
            status_code=400,
            latency_ms=10,
        )
        item = self.store.list_request_logs()["items"][0]
        self.assertEqual(item["account_name"], "full-address@example.com")

    def test_oauth_metadata_round_trips_without_public_secret_exposure(self) -> None:
        account = self.store.add_account(
            "zai", "oauth@example.com", "header.payload.signature"
        )
        account.oauth = {
            "token_response": {"zai": {"access_token": "saved-sensitive-token"}},
            "user": {"email": "oauth@example.com"},
        }
        self.store.update_account(account)

        restored = Store().find("zai", account.id)
        self.assertIsNotNone(restored)
        self.assertEqual(
            restored.oauth["token_response"]["zai"]["access_token"],
            "saved-sensitive-token",
        )
        public = restored.public_view()
        self.assertTrue(public["oauth_saved"])
        self.assertNotIn("saved-sensitive-token", str(public))

    def test_deleted_account_cannot_be_resurrected_by_stale_request(self) -> None:
        account = self.store.add_account(
            "zai", "deleted@example.com", "header.payload.signature"
        )
        self.assertTrue(self.store.remove_account("zai", account.id))
        account.last_error = "late request finished"
        self.assertFalse(self.store.update_account(account))
        self.assertIsNone(Store().find("zai", account.id))

    def test_pool_state_distinguishes_busy_from_exhausted(self) -> None:
        busy = self.store.add_account("zai", "busy@example.com", "one.two.three")
        exhausted = self.store.add_account(
            "zai", "empty@example.com", "four.five.six"
        )
        busy.active_requests = busy.concurrency_limit
        exhausted.status = Status.EXHAUSTED
        state = self.store.pool_state("zai")
        self.assertEqual(state["busy"], 1)
        self.assertEqual(state["exhausted"], 1)
        self.assertEqual(state["selectable"], 0)


class TrafficHelpersTests(unittest.TestCase):
    def test_usage_tracker_handles_split_json_fields(self) -> None:
        tracker = UsageTracker()
        tracker.feed(b'data: {"usage":{"input_tok')
        tracker.feed(b'ens":123,"output_tokens":4}}\n')
        tracker.feed(b'data: {"usage":{"output_tokens":17}}\n')
        self.assertEqual(tracker.input_tokens, 123)
        self.assertEqual(tracker.output_tokens, 17)

    def test_account_name_is_shown_in_full(self) -> None:
        account = Account.create("zai", "someone@example.com", "header.payload.signature")
        self.assertEqual(account_display_name(account), "someone@example.com")


class PublicRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_renders_login_without_redirect_suffix(self) -> None:
        response = await pages.root()
        self.assertEqual(response.status_code, 200)
        self.assertIn("后台管理", response.body.decode("utf-8"))
        self.assertNotIn("location", {key.lower() for key in response.headers})

    async def test_v1_is_a_base_discovery_endpoint(self) -> None:
        body = await gateway.api_root()
        self.assertEqual(body["messages"], "/v1/messages")
        self.assertEqual(body["models"], "/v1/models")


class AccountMutationEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_single_account_uses_path_parameter(self) -> None:
        account = Account.create("zai", "delete@example.com", "header.payload.signature")

        class FakeStore:
            removed = None

            @staticmethod
            def find_any(account_id):
                return account if account_id == account.id else None

            def remove_account(self, provider, account_id):
                self.removed = (provider, account_id)
                return True

        fake_store = FakeStore()
        with patch.object(admin_api, "store", fake_store):
            result = await admin_api.delete_account(account.id)

        self.assertEqual(result, {"deleted": 1})
        self.assertEqual(fake_store.removed, ("zai", account.id))


class AccountTestEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_captcha_wait_is_bounded_for_fast_account_probe(self) -> None:
        account = Account.create("zai", "slow@example.com", "header.payload.signature")

        class FakeStore:
            released = False

            @staticmethod
            def find_any(account_id):
                return account if account_id == account.id else None

            @staticmethod
            def reserve_specific(selected):
                selected.active_requests += 1
                return True

            def release(self, selected):
                selected.active_requests -= 1
                self.released = True

        class SlowCaptcha:
            cancelled = False

            async def get_verify_param(self, _port):
                try:
                    await asyncio.sleep(3600)
                finally:
                    self.cancelled = True

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "server": ("testserver", 3000),
                "path": f"/admin/api/accounts/{account.id}/test",
                "query_string": b"",
                "headers": [],
            }
        )
        fake_store = FakeStore()
        captcha = SlowCaptcha()
        with (
            patch.object(admin_api, "store", fake_store),
            patch.object(admin_api, "captcha_manager", captcha),
            patch.object(settings, "ACCOUNT_TEST_CAPTCHA_TIMEOUT", 0.01),
            patch.object(admin_api, "record_request", Mock()),
        ):
            result = await admin_api.test_account(
                account.id, request, {"model": "GLM-5.3"}
            )

        self.assertEqual(result["status_code"], 504)
        self.assertEqual(result["type"], "captcha_timeout")
        self.assertTrue(captcha.cancelled)
        self.assertTrue(fake_store.released)

    async def test_selected_account_and_model_are_used(self) -> None:
        account = Account.create("bigmodel", "tester@example.com", "test-api-key")

        class FakeStore:
            released = False
            updated = False

            def find_any(self, account_id):
                return account if account_id == account.id else None

            def reserve_specific(self, selected):
                self.assert_selected(selected)
                selected.active_requests += 1
                return True

            @staticmethod
            def assert_selected(selected):
                if selected is not account:
                    raise AssertionError("wrong account selected")

            def update_account(self, selected):
                self.assert_selected(selected)
                self.updated = True

            def release(self, selected):
                self.assert_selected(selected)
                selected.active_requests -= 1
                self.released = True

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return httpx.Response(
                    200,
                    content=b'{"usage":{"input_tokens":8,"output_tokens":1}}',
                )

        fake_store = FakeStore()
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "server": ("testserver", 3000),
                "path": f"/admin/api/accounts/{account.id}/test",
                "query_string": b"",
                "headers": [],
            }
        )
        record = Mock()
        with (
            patch.object(admin_api, "store", fake_store),
            patch.object(
                admin_api,
                "build_request",
                return_value=("https://upstream.invalid", {}, {"model": "GLM-5.2"}),
            ) as build,
            patch.object(admin_api.httpx, "AsyncClient", FakeClient),
            patch.object(admin_api, "record_request", record),
        ):
            result = await admin_api.test_account(account.id, request, {"model": "glm-5.2"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "GLM-5.2")
        self.assertEqual(build.call_args.args[1]["model"], "GLM-5.2")
        self.assertEqual(account.use_count, 1)
        self.assertTrue(fake_store.updated)
        self.assertTrue(fake_store.released)
        self.assertEqual(record.call_args.args[2], 200)

    async def test_reused_captcha_400_is_retried_once(self) -> None:
        account = Account.create("zai", "jwt@example.com", "header.payload.signature")

        class FakeStore:
            released = False

            @staticmethod
            def find_any(account_id):
                return account if account_id == account.id else None

            @staticmethod
            def reserve_specific(selected):
                selected.active_requests += 1
                return True

            @staticmethod
            def update_account(_selected):
                pass

            def release(self, selected):
                selected.active_requests -= 1
                self.released = True

        class FakeCaptcha:
            solve_calls = 0
            invalidate_calls = 0

            async def get_verify_param(self, _port):
                self.solve_calls += 1
                return f"fresh-{self.solve_calls}", "sgp"

            def invalidate(self):
                self.invalidate_calls += 1

        class FakeClient:
            responses = [
                httpx.Response(
                    400,
                    content=b'{"code":"F018","message":"CaptchaVerifyParam reused"}',
                ),
                httpx.Response(200, content=b'{"usage":{"output_tokens":1}}'),
            ]

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return self.responses.pop(0)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "server": ("testserver", 3000),
                "path": f"/admin/api/accounts/{account.id}/test",
                "query_string": b"",
                "headers": [],
            }
        )
        fake_store = FakeStore()
        fake_captcha = FakeCaptcha()
        with (
            patch.object(admin_api, "store", fake_store),
            patch.object(admin_api, "captcha_manager", fake_captcha),
            patch.object(
                admin_api,
                "build_request",
                return_value=("https://upstream.invalid", {}, {"model": "GLM-5.3"}),
            ),
            patch.object(admin_api.httpx, "AsyncClient", FakeClient),
            patch.object(admin_api, "record_request"),
        ):
            result = await admin_api.test_account(account.id, request, {"model": "GLM-5.3"})

        self.assertTrue(result["ok"])
        self.assertEqual(fake_captcha.solve_calls, 2)
        self.assertEqual(fake_captcha.invalidate_calls, 1)
        self.assertTrue(fake_store.released)


if __name__ == "__main__":
    unittest.main()
