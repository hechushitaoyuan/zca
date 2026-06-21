from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import settings
from app.store import Store

# 全部使用明显的占位值，绝不涉及任何真实网关 Key。
_DUMMY_NEW = "dummy-gw-new"
_DUMMY_BACKFILL = "dummy-gw-backfill"
_DUMMY_USER_SET = "dummy-user-set-key"
_DUMMY_ENV_OVERRIDE = "dummy-env-override-attempt"


class GatewayKeyInitTests(unittest.TestCase):
    """ZCODE_GATEWAY_KEY 初始化语义：新库用环境值；既有库仅补空、绝不覆盖非空。"""

    def setUp(self) -> None:
        # 独立临时库，绝不触碰真实 data/accounts.db。
        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)
        self._patches = [
            patch.object(settings, "DATA_DIR", data_dir),
            patch.object(settings, "DB_PATH", data_dir / "accounts.db"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _make_store(self, gateway_key: str) -> Store:
        # 仅在构造期间生效，模拟该次进程启动时的 ZCODE_GATEWAY_KEY。
        with patch.object(settings, "GATEWAY_KEY", gateway_key):
            return Store()

    def test_new_db_uses_env_gateway_key(self) -> None:
        store = self._make_store(_DUMMY_NEW)
        self.assertEqual(store.gateway_key(), _DUMMY_NEW)

    def test_existing_empty_is_backfilled_by_env(self) -> None:
        # 首次以空值建库（gateway_key=''）
        first = self._make_store("")
        self.assertEqual(first.gateway_key(), "")
        # 同一临时库再次启动，环境值非空 → 补齐空值
        store = self._make_store(_DUMMY_BACKFILL)
        self.assertEqual(store.gateway_key(), _DUMMY_BACKFILL)

    def test_existing_nonempty_is_never_overwritten(self) -> None:
        # 建库后模拟用户在后台设置了非空网关 Key
        first = self._make_store("")
        first.set_setting("gateway_key", _DUMMY_USER_SET)
        # 再次启动且环境值非空，绝不能覆盖用户已设的非空值
        store = self._make_store(_DUMMY_ENV_OVERRIDE)
        self.assertEqual(store.gateway_key(), _DUMMY_USER_SET)

    def test_no_env_value_preserves_existing_compat(self) -> None:
        # 未配置环境值：新库保持原有空值行为
        store = self._make_store("")
        self.assertEqual(store.gateway_key(), "")

    def test_no_env_value_does_not_touch_existing_nonempty(self) -> None:
        first = self._make_store("")
        first.set_setting("gateway_key", _DUMMY_USER_SET)
        # 环境值为空 → 整段补齐逻辑跳过，用户值原样保留
        store = self._make_store("")
        self.assertEqual(store.gateway_key(), _DUMMY_USER_SET)


if __name__ == "__main__":
    unittest.main()
