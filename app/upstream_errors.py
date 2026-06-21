"""Narrow classification of Start Plan upstream failures."""

from __future__ import annotations

import json
import re

from .models import FailureKind

_EXHAUST_KEYWORDS = ("quota", "insufficient", "balance", "exhaust", "额度", "余额不足")


def _contains_business_code(text: str, expected: int) -> bool:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return bool(re.search(rf'(?i)["\']?(?:code|businessCode)["\']?\s*[:=]\s*["\']?{expected}\b', text))

    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"code", "businesscode", "business_code"} and str(child) == str(expected):
                    return True
                pending.append(child)
        elif isinstance(value, list):
            pending.extend(value)
    return False


def classify_upstream_failure(status_code: int, text: str) -> str | None:
    """Keep captcha rejection, risk control, auth, and quota failures distinct."""
    low = text.lower()
    if status_code == 405 and _contains_business_code(text, 3012):
        return FailureKind.RISK_3012
    if status_code == 403 and (
        _contains_business_code(text, 3007)
        or "captcha" in low
        or "verify token" in low
        or "verify failed" in low
    ):
        return FailureKind.CAPTCHA
    if status_code == 402 or any(keyword in low for keyword in _EXHAUST_KEYWORDS):
        return FailureKind.EXHAUSTED
    if status_code in (401, 403):
        return FailureKind.AUTH
    if status_code == 429:
        return FailureKind.RATE_LIMIT
    return None
