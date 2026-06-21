from __future__ import annotations

import unittest

from app.models import Account, FailureKind, Status
from app.scheduling import choose_account
from app.upstream_errors import classify_upstream_failure


def account(name: str, *, use_count: int = 0, used: int = 0, remaining: int = 100) -> Account:
    item = Account.create("zai", name, "header.payload.signature")
    item.use_count = use_count
    item.quota = {
        "model": {"total": used + remaining, "used": used, "remaining": remaining}
    }
    return item


class SchedulingTests(unittest.TestCase):
    def test_prefers_lower_quota_utilization_then_lower_call_count(self) -> None:
        heavily_used = account("heavy", use_count=1, used=80, remaining=20)
        lightly_used = account("light", use_count=9, used=10, remaining=90)
        self.assertIs(
            choose_account([heavily_used, lightly_used], now=100),
            lightly_used,
        )

        lightly_used.use_count = 5
        peer = account("peer", use_count=2, used=10, remaining=90)
        self.assertIs(choose_account([lightly_used, peer], now=100), peer)

    def test_skips_cooling_disabled_exhausted_and_busy_accounts(self) -> None:
        cooling = account("cooling")
        cooling.start_cooldown(
            kind=FailureKind.RISK_3012,
            reason="risk",
            now=100,
            base_seconds=20,
            max_seconds=60,
        )
        busy = account("busy")
        busy.active_requests = busy.concurrency_limit
        exhausted = account("empty")
        exhausted.status = Status.EXHAUSTED
        disabled = account("disabled")
        disabled.enabled = False

        self.assertIsNone(
            choose_account([cooling, busy, exhausted, disabled], now=119)
        )
        self.assertIs(choose_account([cooling, busy], now=120), cooling)

    def test_3012_uses_capped_exponential_cooldown_and_success_resets_it(self) -> None:
        item = account("risk")
        first = item.start_cooldown(
            kind=FailureKind.RISK_3012,
            reason="3012",
            now=100,
            base_seconds=10,
            max_seconds=25,
        )
        second = item.start_cooldown(
            kind=FailureKind.RISK_3012,
            reason="3012",
            now=110,
            base_seconds=10,
            max_seconds=25,
        )
        third = item.start_cooldown(
            kind=FailureKind.RISK_3012,
            reason="3012",
            now=130,
            base_seconds=10,
            max_seconds=25,
        )
        self.assertEqual((first, second, third), (10, 20, 25))
        self.assertEqual(item.cooldown_failures, 3)
        self.assertEqual(item.cooling_until, 155)

        item.mark_success(now=200)
        self.assertEqual(item.status, Status.ACTIVE)
        self.assertEqual(item.cooldown_failures, 0)
        self.assertIsNone(item.cooling_until)
        self.assertEqual(
            item.start_cooldown(
                kind=FailureKind.RISK_3012,
                reason="3012",
                now=300,
                base_seconds=10,
                max_seconds=25,
            ),
            10,
        )

    def test_transient_reservations_are_not_persisted(self) -> None:
        item = account("busy")
        item.active_requests = 1
        data = item.to_dict()
        self.assertNotIn("active_requests", data)
        self.assertEqual(Account.from_dict(data).active_requests, 0)


class FailureClassificationTests(unittest.TestCase):
    def test_keeps_3012_captcha_auth_quota_and_rate_limit_distinct(self) -> None:
        self.assertEqual(
            classify_upstream_failure(405, '{"code":3012,"message":"blocked"}'),
            FailureKind.RISK_3012,
        )
        self.assertEqual(
            classify_upstream_failure(403, '{"code":3007,"message":"captcha"}'),
            FailureKind.CAPTCHA,
        )
        self.assertEqual(classify_upstream_failure(401, "invalid jwt"), FailureKind.AUTH)
        self.assertEqual(classify_upstream_failure(402, "quota exhausted"), FailureKind.EXHAUSTED)
        self.assertEqual(classify_upstream_failure(429, "slow down"), FailureKind.RATE_LIMIT)

    def test_does_not_misclassify_3012_as_captcha(self) -> None:
        self.assertEqual(
            classify_upstream_failure(405, '{"code":3012,"message":"captcha-looking text"}'),
            FailureKind.RISK_3012,
        )


if __name__ == "__main__":
    unittest.main()
