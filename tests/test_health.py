from __future__ import annotations

import unittest
from unittest.mock import patch

from app import settings
from app.routes import pages


class HealthMetaTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_returns_status_version_commit_only(self) -> None:
        with (
            patch.object(settings, "ZCA_VERSION", "1.2.3"),
            patch.object(settings, "ZCA_COMMIT", "abc1234"),
        ):
            body = await pages.health()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], "1.2.3")
        self.assertEqual(body["commit"], "abc1234")
        # 只暴露存活与构建标识，键集合必须精确，绝不泄露账号/配置/凭据。
        self.assertEqual(set(body.keys()), {"status", "version", "commit"})

    async def test_health_does_not_leak_sensitive_fields(self) -> None:
        body = await pages.health()
        forbidden = {
            "admin_key", "gateway_key", "accounts", "account", "token",
            "jwt_token", "api_key", "secret", "data", "config", "settings",
            "verify_param", "password",
        }
        self.assertEqual(forbidden & set(body.keys()), set())

    async def test_meta_returns_version_and_commit(self) -> None:
        with (
            patch.object(settings, "ZCA_VERSION", "9.9.9"),
            patch.object(settings, "ZCA_COMMIT", "deadbee"),
        ):
            body = await pages.meta()
        # header.js 消费 .version，必须保留该键
        self.assertEqual(body["version"], "9.9.9")
        self.assertEqual(body["commit"], "deadbee")

    async def test_meta_preserves_version_key_for_header_js(self) -> None:
        body = await pages.meta()
        self.assertIn("version", body)


if __name__ == "__main__":
    unittest.main()
