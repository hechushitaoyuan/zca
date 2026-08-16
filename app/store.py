"""账号与设置的持久化存储（SQLite）。

数据保存在项目本目录下的 data/accounts.db，采用 WAL 模式，
与 grok2api 的本地 (local) 账号后端保持一致。

运行期账号对象常驻内存（保证轮询游标与状态实时性），
每次变更同步落库；进程启动时从 SQLite 读取快照。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing

from . import settings
from .models import PROVIDERS, Account, Status
from .scheduling import choose_account

_TBL = "accounts"
_META = "meta"
_REQUEST_LOGS = "request_logs"
_REQUEST_LOG_LIMIT = 10_000


class Store:
    """线程安全的账号 / 设置存储，含轮询游标。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accounts: dict[str, list[Account]] = {p: [] for p in PROVIDERS}
        self._settings: dict = {}
        self._rotation: dict[str, int] = {p: 0 for p in PROVIDERS}
        self._init_db()
        self._load()

    # ── SQLite 基础 ──────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {_META} (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {_TBL} (
                    id          TEXT PRIMARY KEY,
                    provider    TEXT NOT NULL,
                    name        TEXT,
                    mode        TEXT,
                    status      TEXT,
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    created_at  REAL,
                    data        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_acc_provider ON {_TBL} (provider);
                CREATE INDEX IF NOT EXISTS idx_acc_status   ON {_TBL} (status);
                CREATE TABLE IF NOT EXISTS {_REQUEST_LOGS} (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id    TEXT,
                    created_at    REAL NOT NULL,
                    method        TEXT NOT NULL,
                    path          TEXT NOT NULL,
                    model         TEXT,
                    protocol      TEXT,
                    account_id    TEXT,
                    account_name  TEXT,
                    status_code   INTEGER NOT NULL,
                    latency_ms    INTEGER NOT NULL DEFAULT 0,
                    input_tokens  INTEGER,
                    output_tokens INTEGER,
                    error_type    TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_reqlog_created
                    ON {_REQUEST_LOGS} (created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_reqlog_account
                    ON {_REQUEST_LOGS} (account_id);
                """
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('admin_key', ?)",
                (settings.DEFAULT_ADMIN_KEY,),
            )
            # 网关 Key：新库用环境值（未配置则空）初始化。
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('gateway_key', ?)",
                (settings.GATEWAY_KEY,),
            )
            # 既有库：仅当当前值为空且环境值非空时补齐；WHERE value='' 确保
            # 绝不覆盖用户已在后台设置的非空网关 Key。环境值为空则整段跳过。
            if settings.GATEWAY_KEY:
                conn.execute(
                    f"UPDATE {_META} SET value = ? WHERE key = 'gateway_key' AND value = ''",
                    (settings.GATEWAY_KEY,),
                )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('quota_refresh_interval', ?)",
                (str(settings.QUOTA_REFRESH_INTERVAL),),
            )
            conn.commit()

    def _load(self) -> None:
        with closing(self._connect()) as conn:
            meta_rows = conn.execute(f"SELECT key, value FROM {_META}").fetchall()
            self._settings = {r["key"]: r["value"] for r in meta_rows}
            self._settings.setdefault("admin_key", settings.DEFAULT_ADMIN_KEY)
            self._settings.setdefault("gateway_key", "")
            self._settings.setdefault("quota_refresh_interval", str(settings.QUOTA_REFRESH_INTERVAL))

            self._accounts = {p: [] for p in PROVIDERS}
            rows = conn.execute(
                f"SELECT data FROM {_TBL} ORDER BY created_at ASC"
            ).fetchall()
            for row in rows:
                try:
                    account = Account.from_dict(json.loads(row["data"]))
                except (json.JSONDecodeError, TypeError):
                    continue
                if account.provider in self._accounts:
                    account.concurrency_limit = settings.ACCOUNT_CONCURRENCY_LIMIT
                    self._accounts[account.provider].append(account)

    def _persist_account(self, account: Account) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                f"""INSERT OR REPLACE INTO {_TBL}
                    (id, provider, name, mode, status, enabled, created_at, data)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (
                    account.id, account.provider, account.name, account.mode,
                    account.status, 1 if account.enabled else 0, account.created_at,
                    json.dumps(account.to_dict(), ensure_ascii=False),
                ),
            )
            conn.commit()

    def _delete_account(self, account_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(f"DELETE FROM {_TBL} WHERE id = ?", (account_id,))
            conn.commit()

    def _set_meta(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {_META} (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def save(self) -> None:
        """全量落库（兜底接口）。"""
        with self._lock:
            for accounts in self._accounts.values():
                for account in accounts:
                    self._persist_account(account)

    # ── 设置 ─────────────────────────────────────────────────────────────────
    def get_setting(self, key: str, default=None):
        with self._lock:
            return self._settings.get(key, default)

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self._settings[key] = str(value)
            self._set_meta(key, str(value))

    def admin_key(self) -> str:
        return str(self.get_setting("admin_key", settings.DEFAULT_ADMIN_KEY) or "")

    def gateway_key(self) -> str:
        return str(self.get_setting("gateway_key", "") or "")

    def quota_refresh_interval(self) -> int:
        try:
            return max(0, int(self.get_setting("quota_refresh_interval", settings.QUOTA_REFRESH_INTERVAL)))
        except (TypeError, ValueError):
            return settings.QUOTA_REFRESH_INTERVAL

    # ── 账号读取 ─────────────────────────────────────────────────────────────
    def list_accounts(self, provider: str | None = None) -> list[Account]:
        with self._lock:
            if provider:
                return list(self._accounts.get(provider, []))
            return [a for p in PROVIDERS for a in self._accounts[p]]

    def find(self, provider: str, id_or_name: str) -> Account | None:
        with self._lock:
            return self._find_locked(provider, id_or_name)

    def find_any(self, id_or_name: str) -> Account | None:
        with self._lock:
            for p in PROVIDERS:
                for a in self._accounts[p]:
                    if a.id == id_or_name:
                        return a
        return None

    def _find_locked(self, provider: str, id_or_name: str) -> Account | None:
        for a in self._accounts.get(provider, []):
            if a.id == id_or_name or a.name == id_or_name:
                return a
        return None

    # ── 账号增删改 ───────────────────────────────────────────────────────────
    def add_account(self, provider: str, name: str, secret: str) -> Account:
        if provider not in PROVIDERS:
            raise ValueError(f"不支持的 provider: {provider}")
        account = Account.create(provider, name, secret)
        account.concurrency_limit = settings.ACCOUNT_CONCURRENCY_LIMIT
        with self._lock:
            for a in self._accounts[provider]:
                if a.secret and a.secret == account.secret:
                    return a  # 跳过重复 token
            self._accounts[provider].append(account)
            self._persist_account(account)
        return account

    def remove_account(self, provider: str, id_or_name: str) -> bool:
        with self._lock:
            items = self._accounts.get(provider, [])
            target = next((a for a in items if a.id == id_or_name or a.name == id_or_name), None)
            if not target:
                return False
            self._accounts[provider] = [a for a in items if a.id != target.id]
            self._delete_account(target.id)
            return True

    def update_account(self, account: Account) -> None:
        """持久化某个账号的当前状态。"""
        with self._lock:
            self._persist_account(account)

    def set_enabled(self, provider: str, id_or_name: str, enabled: bool) -> bool:
        with self._lock:
            account = self._find_locked(provider, id_or_name)
            if not account:
                return False
            account.enabled = enabled
            if not enabled:
                account.status = Status.DISABLED
            elif account.status == Status.DISABLED:
                account.status = Status.ACTIVE
            self._persist_account(account)
            return True

    # ── 轮询选择 ─────────────────────────────────────────────────────────────
    def select(self, provider: str, skip_ids: set[str] | None = None) -> Account | None:
        """Fairly select and atomically reserve one available account."""
        skip_ids = skip_ids or set()
        now = time.time()
        with self._lock:
            account = choose_account(
                self._accounts.get(provider, []),
                now=now,
                skip_ids=skip_ids,
            )
            if account is None:
                return None
            account.active_requests += 1
            return account

    def release(self, account: Account) -> None:
        """Release an in-flight reservation without persisting transient state."""
        with self._lock:
            account.active_requests = max(0, account.active_requests - 1)

    def reserve_specific(self, account: Account) -> bool:
        """Reserve a chosen account for an admin test without running the scheduler."""
        with self._lock:
            if account.active_requests >= max(1, account.concurrency_limit):
                return False
            account.active_requests += 1
            return True

    # ── 请求日志 ─────────────────────────────────────────────────────────────
    def record_request_log(
        self,
        *,
        request_id: str,
        method: str,
        path: str,
        model: str,
        protocol: str,
        account_id: str | None,
        account_name: str | None,
        status_code: int,
        latency_ms: int,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error_type: str | None = None,
        created_at: float | None = None,
    ) -> None:
        """Persist request metadata only; prompts, responses and credentials are excluded."""
        with self._lock, closing(self._connect()) as conn:
            cursor = conn.execute(
                f"""INSERT INTO {_REQUEST_LOGS}
                    (request_id, created_at, method, path, model, protocol,
                     account_id, account_name, status_code, latency_ms,
                     input_tokens, output_tokens, error_type)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    time.time() if created_at is None else created_at,
                    method[:12],
                    path[:160],
                    model[:80],
                    protocol[:32],
                    account_id,
                    account_name[:160] if account_name else None,
                    int(status_code),
                    max(0, int(latency_ms)),
                    input_tokens,
                    output_tokens,
                    error_type[:80] if error_type else None,
                ),
            )
            # 定期裁剪，避免长期运行后日志无限增长。
            if cursor.lastrowid and cursor.lastrowid % 100 == 0:
                conn.execute(
                    f"""DELETE FROM {_REQUEST_LOGS}
                        WHERE id NOT IN (
                            SELECT id FROM {_REQUEST_LOGS}
                            ORDER BY id DESC LIMIT ?
                        )""",
                    (_REQUEST_LOG_LIMIT,),
                )
            conn.commit()

    def list_request_logs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        query: str = "",
        status: str = "all",
    ) -> dict:
        limit = min(200, max(1, int(limit)))
        offset = max(0, int(offset))
        clauses: list[str] = []
        params: list[object] = []
        query = query.strip().lower()
        if query:
            clauses.append(
                "(LOWER(model) LIKE ? OR LOWER(path) LIKE ? OR "
                "LOWER(COALESCE(account_name,'')) LIKE ? OR LOWER(COALESCE(error_type,'')) LIKE ?)"
            )
            needle = f"%{query}%"
            params.extend([needle, needle, needle, needle])
        if status == "success":
            clauses.append("status_code BETWEEN 200 AND 299")
        elif status == "error":
            clauses.append("NOT (status_code BETWEEN 200 AND 299)")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""

        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT id, request_id, created_at, method, path, model, protocol,
                           account_id, account_name, status_code, latency_ms,
                           input_tokens, output_tokens, error_type
                    FROM {_REQUEST_LOGS}{where}
                    ORDER BY id DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
            stats = conn.execute(
                f"""SELECT COUNT(*) AS total,
                           COALESCE(SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END),0) AS success,
                           COALESCE(SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 0 ELSE 1 END),0) AS error
                    FROM {_REQUEST_LOGS}{where}""",
                params,
            ).fetchone()
        return {
            "items": [dict(row) for row in rows],
            "stats": dict(stats) if stats else {"total": 0, "success": 0, "error": 0},
            "limit": limit,
            "offset": offset,
        }

    def clear_request_logs(self) -> int:
        with self._lock, closing(self._connect()) as conn:
            count = conn.execute(f"SELECT COUNT(*) FROM {_REQUEST_LOGS}").fetchone()[0]
            conn.execute(f"DELETE FROM {_REQUEST_LOGS}")
            conn.commit()
        return int(count)

    # ── 导入 / 导出 ─────────────────────────────────────────────────────────
    def export(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "exported_at": time.time(),
                "providers": {
                    p: [
                        {"name": a.name, "mode": a.mode, "secret": a.secret}
                        for a in self._accounts[p]
                    ]
                    for p in PROVIDERS
                },
            }

    def import_accounts(self, payload: dict) -> int:
        providers = payload.get("providers", {})
        count = 0
        for provider, items in providers.items():
            if provider not in PROVIDERS or not isinstance(items, list):
                continue
            for it in items:
                secret = it.get("secret") or it.get("token") or it.get("jwtToken") or it.get("apiKey")
                if not secret:
                    continue
                self.add_account(provider, it.get("name", provider), secret)
                count += 1
        return count


# 单例
store = Store()
