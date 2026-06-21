"""Fair, quota-aware account selection helpers."""

from __future__ import annotations

from collections.abc import Iterable

from .models import Account


def _quota_metrics(account: Account) -> tuple[float, float]:
    total = 0.0
    used = 0.0
    remaining = 0.0
    has_quota = False
    for item in account.quota.values():
        if not isinstance(item, dict):
            continue
        try:
            item_total = float(item.get("total") or 0)
            item_used = float(item.get("used") or 0)
            item_remaining = float(item.get("remaining") or 0)
        except (TypeError, ValueError):
            continue
        has_quota = True
        total += max(0.0, item_total)
        used += max(0.0, item_used)
        remaining += max(0.0, item_remaining)
    utilization = used / total if has_quota and total > 0 else 0.0
    return utilization, remaining


def selection_key(account: Account) -> tuple[float, int, float, float, float, str]:
    """Prefer lower quota utilization/call count and the least recently used account."""
    utilization, remaining = _quota_metrics(account)
    return (
        utilization,
        account.use_count,
        account.last_used_at or 0.0,
        -remaining,
        account.created_at,
        account.id,
    )


def choose_account(
    accounts: Iterable[Account],
    *,
    now: float,
    skip_ids: set[str] | None = None,
) -> Account | None:
    skipped = skip_ids or set()
    candidates = [
        account
        for account in accounts
        if account.id not in skipped and account.is_selectable(now)
    ]
    return min(candidates, key=selection_key) if candidates else None
