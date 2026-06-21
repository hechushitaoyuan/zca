"""账号数据模型与状态枚举。"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field

PROVIDERS = ("zai", "bigmodel")


class Status:
    """账号运行状态。"""

    ACTIVE = "active"        # 正常，可参与轮询
    EXHAUSTED = "exhausted"  # 额度用完
    COOLING = "cooling"      # 临时限流（冷却中）
    INVALID = "invalid"      # 凭证失效 / 鉴权失败
    DISABLED = "disabled"    # 手动禁用

    MANAGEABLE = (ACTIVE, COOLING, EXHAUSTED)


class FailureKind:
    """Failure categories kept separate for scheduling and the admin UI."""

    CAPTCHA = "captcha_failure"
    RISK_3012 = "risk_3012"
    AUTH = "auth_invalid"
    EXHAUSTED = "quota_exhausted"
    RATE_LIMIT = "rate_limited"
    TRANSPORT = "transport_error"
    UPSTREAM = "upstream_error"


def _account_id(name: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in (name or "account").lower())
    safe = safe.strip("-")[:32] or "account"
    return f"{safe}-{secrets.token_hex(4)}"


@dataclass
class Account:
    """单个可轮询的账号凭证 + 运行时状态。"""

    id: str
    name: str
    provider: str
    mode: str  # "jwt" | "apiKey"
    jwt_token: str | None = None
    api_key: str | None = None
    user_id: str | None = None
    enabled: bool = True
    status: str = Status.ACTIVE

    # 额度快照：{ model_show_name: {total, used, remaining, expires_at} }
    quota: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)        # 当前激活方案
    usage: dict = field(default_factory=dict)       # 近期用量原始数据

    use_count: int = 0
    fail_count: int = 0
    last_used_at: float | None = None
    last_checked_at: float | None = None
    cooldown_started_at: float | None = None
    cooling_until: float | None = None
    cooldown_reason: str | None = None
    cooldown_failures: int = 0
    last_failure_kind: str | None = None
    last_error: str | None = None
    concurrency_limit: int = 1
    active_requests: int = field(default=0, repr=False, compare=False)
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def create(provider: str, name: str, secret: str) -> "Account":
        secret = (secret or "").strip()
        is_jwt = secret.count(".") == 2 and provider == "zai"
        return Account(
            id=_account_id(name),
            name=name or f"{provider}-account",
            provider=provider,
            mode="jwt" if is_jwt else "apiKey",
            jwt_token=secret if is_jwt else None,
            api_key=None if is_jwt else secret,
        )

    @property
    def secret(self) -> str | None:
        return self.jwt_token if self.mode == "jwt" else self.api_key

    def is_selectable(self, now: float | None = None) -> bool:
        """是否可被轮询选中。"""
        if not self.enabled or self.status in (Status.DISABLED, Status.INVALID):
            return False
        if self.active_requests >= max(1, self.concurrency_limit):
            return False
        if self.status == Status.EXHAUSTED:
            return False
        if self.status == Status.COOLING:
            now = now or time.time()
            return bool(self.cooling_until and now >= self.cooling_until)
        return True

    def to_dict(self) -> dict:
        data = asdict(self)
        # In-flight reservations are process-local and must never survive restart.
        data.pop("active_requests", None)
        return data

    @staticmethod
    def from_dict(data: dict) -> "Account":
        known = {f for f in Account.__dataclass_fields__}  # type: ignore[attr-defined]
        restored = Account(**{k: v for k, v in data.items() if k in known and k != "active_requests"})
        restored.active_requests = 0
        return restored

    @property
    def account_type(self) -> str:
        return "start_plan" if self.provider == "zai" and self.mode == "jwt" else "coding_plan"

    def start_cooldown(
        self,
        *,
        kind: str,
        reason: str,
        now: float | None = None,
        base_seconds: int = 300,
        max_seconds: int = 7200,
    ) -> int:
        """Enter cooldown; repeated 3012 failures use capped exponential backoff."""
        now = time.time() if now is None else now
        if kind == FailureKind.RISK_3012:
            self.cooldown_failures = self.cooldown_failures + 1
            delay = min(max_seconds, base_seconds * (2 ** (self.cooldown_failures - 1)))
        else:
            self.cooldown_failures = 0
            delay = min(max_seconds, base_seconds)
        self.status = Status.COOLING
        self.cooldown_started_at = now
        self.cooling_until = now + delay
        self.cooldown_reason = reason
        self.last_failure_kind = kind
        self.last_error = reason
        self.fail_count += 1
        return delay

    def mark_failure(self, kind: str, reason: str, *, status: str | None = None) -> None:
        if status is not None:
            self.status = status
        if kind != FailureKind.RISK_3012:
            self.cooldown_failures = 0
        self.last_failure_kind = kind
        self.last_error = reason
        self.fail_count += 1

    def mark_success(self, now: float | None = None) -> None:
        self.status = Status.ACTIVE
        self.use_count += 1
        self.last_used_at = time.time() if now is None else now
        self.cooldown_started_at = None
        self.cooling_until = None
        self.cooldown_reason = None
        self.cooldown_failures = 0
        self.last_failure_kind = None
        self.last_error = None

    def public_view(self) -> dict:
        """返回给前端的视图（脱敏 token）。"""
        secret = self.secret or ""
        masked = secret if len(secret) <= 16 else f"{secret[:8]}…{secret[-6:]}"
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "mode": self.mode,
            "account_type": self.account_type,
            "token_masked": masked,
            "enabled": self.enabled,
            "status": self.effective_status(),
            "quota": self.quota,
            "plan": self.plan,
            "use_count": self.use_count,
            "fail_count": self.fail_count,
            "last_used_at": self.last_used_at,
            "last_checked_at": self.last_checked_at,
            "cooldown_started_at": self.cooldown_started_at,
            "cooling_until": self.cooling_until,
            "cooldown_reason": self.cooldown_reason,
            "cooldown_failures": self.cooldown_failures,
            "last_failure_kind": self.last_failure_kind,
            "last_error": self.last_error,
            "concurrency_limit": self.concurrency_limit,
            "active_requests": self.active_requests,
            "created_at": self.created_at,
        }

    def effective_status(self, now: float | None = None) -> str:
        """考虑冷却到期后的实时状态。"""
        if self.status == Status.COOLING:
            now = now or time.time()
            if self.cooling_until and now >= self.cooling_until:
                return Status.ACTIVE
        return self.status
