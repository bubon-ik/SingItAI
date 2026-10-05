"""Where a purchase the user is waiting on stands, for the page to show while it runs.

A Solana gift card takes about 15 s, most of it Bitrefill settling the payment on chain; a bare
"Thinking" for that long reads as stuck. The page names an attempt when it starts a purchase and
asks for its stage while it waits; the purchase marks each stage as it really reaches it.

The attempt travels with the request (`tracking`), so the code that buys marks stages without
being handed anything. Kept in memory for ten minutes; nothing here is needed after the answer.
"""
from __future__ import annotations

import contextvars
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

STAGES = ("ordering", "paying", "paid")  # then the answer itself: delivered, or still being delivered
KEEP_SECONDS = 600
_ATTEMPT = re.compile(r"[0-9a-f]{32}")

_current: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("purchase_progress", default=None)
_stages: dict[tuple[str, str], tuple[str, float]] = {}
_lock = threading.Lock()


def _valid(attempt: Any) -> str | None:
    attempt = str(attempt or "")
    return attempt if _ATTEMPT.fullmatch(attempt) else None


@contextmanager
def tracking(account: str, attempt: Any) -> Iterator[None]:
    """Marks of the purchase run inside this block go to this account's attempt; no attempt, no marks."""
    attempt = _valid(attempt)
    if attempt is None:
        yield
        return
    token = _current.set((account, attempt))
    try:
        mark("ordering")
        yield
    finally:
        _current.reset(token)


def mark(stage: str) -> None:
    key = _current.get()
    if key is None or stage not in STAGES:
        return
    now = time.time()
    with _lock:
        for old in [k for k, (_, at) in _stages.items() if now - at > KEEP_SECONDS]:
            del _stages[old]
        _stages[key] = (stage, now)


def stage_of(account: str, attempt: Any) -> str:
    """This account's attempt's stage, or "working" before its first mark (and for anyone else's attempt)."""
    attempt = _valid(attempt)
    with _lock:
        found = _stages.get((account, attempt)) if attempt else None
    return found[0] if found and time.time() - found[1] <= KEEP_SECONDS else "working"
