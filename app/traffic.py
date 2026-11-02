"""Privacy-preserving request-log helpers.

Only operational metadata is retained. Prompt text, model output, captcha values and
credentials never enter the request log.
"""

from __future__ import annotations

import re
import time

from .models import Account
from .store import store

_INPUT_RE = re.compile(r'"input_tokens"\s*:\s*(\d+)')
_OUTPUT_RE = re.compile(r'"output_tokens"\s*:\s*(\d+)')


def account_display_name(account: Account | None) -> str | None:
    if account is None:
        return None
    return (account.name or account.id or "").strip()


class UsageTracker:
    """Extract Anthropic token counters incrementally without buffering responses."""

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self._tail = ""

    def feed(self, chunk: bytes) -> None:
        text = self._tail + chunk.decode("utf-8", "ignore")
        inputs = _INPUT_RE.findall(text)
        outputs = _OUTPUT_RE.findall(text)
        if inputs:
            self.input_tokens = max(int(value) for value in inputs)
        if outputs:
            self.output_tokens = max(int(value) for value in outputs)
        self._tail = text[-256:]


def record_request(
    meta: dict | None,
    account: Account | None,
    status_code: int,
    *,
    usage: UsageTracker | None = None,
    error_type: str | None = None,
) -> None:
    if not meta:
        return
    started = float(meta.get("started") or time.monotonic())
    store.record_request_log(
        request_id=str(meta.get("request_id") or ""),
        method=str(meta.get("method") or "POST"),
        path=str(meta.get("path") or "/v1/messages"),
        model=str(meta.get("model") or "-"),
        protocol=str(meta.get("protocol") or "Anthropic"),
        account_id=account.id if account else None,
        account_name=account_display_name(account),
        status_code=status_code,
        latency_ms=round((time.monotonic() - started) * 1000),
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        error_type=error_type,
    )
