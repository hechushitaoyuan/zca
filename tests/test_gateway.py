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


if __name__ == "__main__":
    unittest.main()
