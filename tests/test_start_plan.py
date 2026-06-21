from __future__ import annotations

import base64
import json
import unittest

from app.agent import build_request
from app.models import Account
from app.start_plan import ZCODE_SYSTEM_BLOCKS, prepare_start_plan_body


def fake_jwt(**claims: object) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class StartPlanRequestTests(unittest.TestCase):
    def test_builds_start_plan_auth_identity_and_captcha_headers(self) -> None:
        account = Account.create("zai", "test", fake_jwt(user_id="user-42"))
        body = {"model": "glm-5.2", "stream": False, "messages": []}

        url, headers, prepared = build_request(
            account,
            body,
            "test-verify-param",
            {
                "Authorization": "Bearer client-value",
                "x-request-id": "client-request-id",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            verify_region="sgp",
        )

        self.assertEqual(url, "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages")
        self.assertEqual(headers["Authorization"], f"Bearer {account.jwt_token}")
        self.assertNotIn("anthropic-version", headers)
        self.assertNotEqual(headers["x-request-id"], "client-request-id")
        self.assertTrue(headers["x-session-id"])
        self.assertTrue(headers["x-zcode-trace-id"])
        self.assertTrue(headers["x-query-id"].startswith("query_"))
        self.assertEqual(headers["X-ZCode-Agent"], "glm")
        self.assertEqual(headers["X-Aliyun-Captcha-Verify-Param"], "test-verify-param")
        self.assertEqual(headers["X-Aliyun-Captcha-Verify-Region"], "sgp")
        self.assertEqual(headers["anthropic-beta"], "prompt-caching-2024-07-31")
        self.assertEqual(prepared["metadata"]["user_id"], "user-42")

    def test_preserves_stream_false_and_does_not_mutate_input(self) -> None:
        source = {
            "model": "glm-5.2",
            "stream": False,
            "system": "custom rules",
            "metadata": {"trace": "keep"},
            "messages": [{"role": "user", "content": "hello"}],
        }
        original = json.loads(json.dumps(source))

        prepared = prepare_start_plan_body(source, user_id="u-test")

        self.assertIs(prepared["stream"], False)
        self.assertEqual(source, original)
        self.assertEqual(prepared["model"], "GLM-5.2")
        self.assertEqual(prepared["system"][:2], ZCODE_SYSTEM_BLOCKS)
        self.assertEqual(prepared["system"][2]["text"], "custom rules")
        self.assertEqual(prepared["metadata"], {"trace": "keep", "user_id": "u-test"})
        self.assertEqual(
            prepared["messages"][0]["content"][0]["cache_control"],
            {"type": "ephemeral"},
        )

    def test_system_injection_is_idempotent(self) -> None:
        once = prepare_start_plan_body({"system": "custom", "messages": []})
        twice = prepare_start_plan_body(once)
        self.assertEqual(twice["system"], once["system"])

    def test_coding_plan_keeps_api_key_protocol(self) -> None:
        account = Account.create("zai", "paid", "api-key-value")
        _, headers, prepared = build_request(account, {"messages": []}, None)
        self.assertEqual(headers["x-api-key"], "api-key-value")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("system", prepared)


if __name__ == "__main__":
    unittest.main()
