from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.models import Account, FailureKind, Status
from app.routes import gateway


class FakeResponse:
    def __init__(self, status_code: int, body: bytes, content_type: str) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = {"content-type": content_type}
        self.read_count = 0

    async def aread(self) -> bytes:
        self.read_count += 1
        if self.status_code < 400:
            raise AssertionError("successful response must not be pre-read")
        return self._body

    async def aiter_bytes(self):
        midpoint = max(1, len(self._body) // 2)
        yield self._body[:midpoint]
        yield self._body[midpoint:]


class FakeStreamContext:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.exited = False

    async def __aenter__(self) -> FakeResponse:
        return self.response

    async def __aexit__(self, *_args) -> None:
        self.exited = True


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.context = FakeStreamContext(response)
        self.sent_content = b""
        self.closed = False

    def stream(self, _method: str, _url: str, *, headers: dict, content: bytes):
        self.headers = headers
        self.sent_content = content
        return self.context

    async def aclose(self) -> None:
        self.closed = True


class FakeStore:
    def __init__(self) -> None:
        self.release_count = 0
        self.update_count = 0

    def update_account(self, _account: Account) -> None:
        self.update_count += 1

    def release(self, account: Account) -> None:
        account.active_requests = max(0, account.active_requests - 1)
        self.release_count += 1


async def no_refresh(_account: Account) -> None:
    return None


class FakeCaptcha:
    """记录 get_verify_param / invalidate 调用次数的桩。"""

    def __init__(self) -> None:
        self.solve_calls = 0
        self.invalidate_calls = 0

    async def get_verify_param(self, _port=None):
        self.solve_calls += 1
        return "verify-param-" + "x" * 40, "hzn"

    def invalidate(self) -> None:
        self.invalidate_calls += 1


class GatewayStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_sse_is_not_buffered_and_preserves_stream_false_request(self) -> None:
        upstream = FakeResponse(
            200,
            b"event: message_stop\ndata: {}\n\n",
            "text/event-stream",
        )
        client = FakeClient(upstream)
        store = FakeStore()
        account = Account.create("zai", "stream-test", "header.payload.signature")
        account.active_requests = 1

        with (
            patch.object(gateway.httpx, "AsyncClient", return_value=client),
            patch.object(gateway, "store", store),
            patch.object(gateway, "_safe_refresh", no_refresh),
        ):
            response = await gateway._try_account(
                "test",
                account,
                {"model": "glm-5.2", "stream": False, "messages": []},
                {},
                3000,
                False,
            )
            chunks = [chunk async for chunk in response.body_iterator]

        sent = json.loads(client.sent_content)
        self.assertIs(sent["stream"], False)
        self.assertEqual(upstream.read_count, 0)
        self.assertEqual(b"".join(chunks), upstream._body)
        self.assertTrue(client.context.exited)
        self.assertTrue(client.closed)
        self.assertEqual(store.release_count, 1)
        self.assertEqual(account.active_requests, 0)

    async def test_3012_enters_cooldown_instead_of_invalidating_account(self) -> None:
        upstream = FakeResponse(
            405,
            b'{"code":3012,"message":"blocked"}',
            "application/json",
        )
        client = FakeClient(upstream)
        store = FakeStore()
        account = Account.create("zai", "risk-test", "header.payload.signature")
        account.active_requests = 1

        with (
            patch.object(gateway.httpx, "AsyncClient", return_value=client),
            patch.object(gateway, "store", store),
        ):
            result = await gateway._try_account(
                "test",
                account,
                {"model": "glm-5.2", "stream": False, "messages": []},
                {},
                3000,
                False,
            )

        self.assertIs(result, gateway._NEXT_ACCOUNT)
        self.assertEqual(account.status, Status.COOLING)
        self.assertEqual(account.last_failure_kind, FailureKind.RISK_3012)
        self.assertEqual(account.cooldown_failures, 1)
        self.assertIsNotNone(account.cooling_until)
        self.assertEqual(store.release_count, 1)
        self.assertEqual(account.active_requests, 0)


class SequenceClient:
    """按调用顺序返回多个响应，记录每次请求头。用于多次 attempt 链路。"""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.contexts: list[FakeStreamContext] = []
        self.sent_headers: list[dict] = []
        self.closed = 0

    def stream(self, _method: str, _url: str, *, headers: dict, content: bytes):
        self.sent_headers.append(headers)
        ctx = FakeStreamContext(self._responses.pop(0))
        self.contexts.append(ctx)
        return ctx

    async def aclose(self) -> None:
        self.closed += 1


class GatewayCaptchaWiringTests(unittest.IsolatedAsyncioTestCase):
    def _jwt_account(self, name: str) -> Account:
        account = Account.create("zai", name, "header.payload.signature")
        account.active_requests = 1
        return account

    async def test_captcha_403_retries_once_then_succeeds_with_region_header(self) -> None:
        client = SequenceClient([
            FakeResponse(403, b'{"code":3007,"message":"captcha required"}', "application/json"),
            FakeResponse(200, b"event: message_stop\ndata: {}\n\n", "text/event-stream"),
        ])
        store = FakeStore()
        fake_captcha = FakeCaptcha()
        account = self._jwt_account("captcha-retry")

        with (
            patch.object(gateway.httpx, "AsyncClient", return_value=client),
            patch.object(gateway, "store", store),
            patch.object(gateway, "captcha_manager", fake_captcha),
            patch.object(gateway, "_safe_refresh", no_refresh),
        ):
            response = await gateway._try_account(
                "test",
                account,
                {"model": "glm-5.2", "stream": False, "messages": []},
                {},
                3000,
                True,
            )
            chunks = [chunk async for chunk in response.body_iterator]

        self.assertEqual(b"".join(chunks), b"event: message_stop\ndata: {}\n\n")
        self.assertEqual(fake_captcha.solve_calls, 2)       # 初次 + 一次重试
        self.assertEqual(fake_captcha.invalidate_calls, 1)  # 仅失效一次
        # region 必须随求解结果进入最终成功请求头
        self.assertEqual(
            client.sent_headers[1].get("X-Aliyun-Captcha-Verify-Region"), "hzn"
        )
        self.assertEqual(account.active_requests, 0)

    async def test_captcha_403_twice_switches_account(self) -> None:
        client = SequenceClient([
            FakeResponse(403, b'{"code":3007}', "application/json"),
            FakeResponse(403, b'{"code":3007}', "application/json"),
        ])
        store = FakeStore()
        fake_captcha = FakeCaptcha()
        account = self._jwt_account("captcha-fail")

        with (
            patch.object(gateway.httpx, "AsyncClient", return_value=client),
            patch.object(gateway, "store", store),
            patch.object(gateway, "captcha_manager", fake_captcha),
            patch.object(gateway, "_safe_refresh", no_refresh),
        ):
            result = await gateway._try_account(
                "test",
                account,
                {"model": "glm-5.2", "stream": False, "messages": []},
                {},
                3000,
                True,
            )

        self.assertIs(result, gateway._NEXT_ACCOUNT)
        self.assertEqual(fake_captcha.solve_calls, 2)
        self.assertEqual(fake_captcha.invalidate_calls, 1)  # 只在还能重试时失效一次
        self.assertEqual(account.last_failure_kind, FailureKind.CAPTCHA)
        self.assertEqual(account.active_requests, 0)

    async def test_3012_with_captcha_does_not_invalidate_or_resolve_again(self) -> None:
        client = SequenceClient([
            FakeResponse(405, b'{"code":3012,"message":"blocked"}', "application/json"),
        ])
        store = FakeStore()
        fake_captcha = FakeCaptcha()
        account = self._jwt_account("risk-with-captcha")

        with (
            patch.object(gateway.httpx, "AsyncClient", return_value=client),
            patch.object(gateway, "store", store),
            patch.object(gateway, "captcha_manager", fake_captcha),
            patch.object(gateway, "_safe_refresh", no_refresh),
        ):
            result = await gateway._try_account(
                "test",
                account,
                {"model": "glm-5.2", "stream": False, "messages": []},
                {},
                3000,
                True,
            )

        self.assertIs(result, gateway._NEXT_ACCOUNT)
        self.assertEqual(account.status, Status.COOLING)
        self.assertEqual(account.last_failure_kind, FailureKind.RISK_3012)
        self.assertEqual(fake_captcha.solve_calls, 1)       # 仅初次求解
        self.assertEqual(fake_captcha.invalidate_calls, 0)  # 3012 不触发 captcha 重试
        self.assertEqual(account.active_requests, 0)


if __name__ == "__main__":
    unittest.main()
