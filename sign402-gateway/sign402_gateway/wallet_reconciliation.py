"""A free, public spend report for one wallet: GET /v1/wallet/{address}/reconciliation (web_api.py).

Unauthenticated, so it says only what the chain already shows or what is a coarse total: the limit the owner set,
what settled in the window by category, what is left today. Never a resource URL, invoice id, purchase description,
transaction hash, chat or contact. No RPC call is made here; everything comes from the two allowance databases.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from typing import Any, Callable

from .web_accounts import WebAuthError, account_id_for, chain_of, normalized

DAY = 86400
DEFAULT_DAYS, MAX_DAYS = 30, 90
CACHE_SECONDS = 60
CACHE_MAX = 2048
CATEGORIES = ("chat_answer", "data_tool", "gift_card", "other")
_BASE_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def _usd(atomic: int) -> str:
    return f"{atomic / 1_000_000:.2f}"


def base_category(resource: str) -> str:
    """Mirrors _SETTLEMENT_CATEGORY_SQL in scripts/daily-metrics.py; a credit top-up is its own thing."""
    if resource.startswith("bitrefill:invoice:"):
        return "gift_card"
    if resource.endswith("/v1/credit"):
        return "credit_topup"
    if resource.endswith("/chat/completions/metered") or resource.endswith("/chat/completions"):
        return "chat_answer"
    return "data_tool"


def _solana_tool_names() -> frozenset[str]:
    from .web_data import TOOLS
    # "Exa web search" is web_venice's search; "SingIt · Exa" and "Video" are web_data's search and video.
    return frozenset(t.name for t in TOOLS.values()) | {"Exa web search", "SingIt · Exa", "Video", "Web search"}


def solana_category(purpose: str) -> str:
    """The purposes our own code writes (solana_allowance.spend callers). Anything else is "other"."""
    if purpose.startswith("Bitrefill"):
        return "gift_card"
    if purpose == "SingIt credit":
        return "credit_topup"
    if purpose.startswith("SingIt Ask"):
        return "chat_answer"
    return "data_tool" if purpose in _solana_tool_names() else "other"


def parse_address(raw: str) -> tuple[str, str]:
    """(chain, canonical address) or WebAuthError. Base: 0x + 40 hex, checksummed; Solana: canonical base58."""
    text = raw.strip()
    if text.startswith(("0x", "0X")) and not _BASE_ADDRESS.fullmatch("0x" + text[2:]):
        raise WebAuthError("That is not a valid address.")
    if not text.startswith(("0x", "0X")) and not 32 <= len(text) <= 44:
        raise WebAuthError("That is not a valid address.")
    if text.startswith("0X"):
        text = "0x" + text[2:]
    chain = chain_of(text)
    return chain, normalized(text)


class Reconciliation:
    def __init__(self, base_store: Any, solana_store: Any, now: Callable[[], float] = time.time):
        self.base, self.solana, self.now = base_store, solana_store, now
        self._cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def report(self, address: str, days: int = DEFAULT_DAYS) -> dict[str, Any]:
        chain, address = parse_address(address)
        now = self.now()
        key = (address, days)
        with self._lock:
            hit = self._cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        out = self._build(chain, address, days, int(now))
        with self._lock:
            if len(self._cache) >= CACHE_MAX:
                self._cache.clear()
            self._cache[key] = (now, out)
        return out

    def _build(self, chain: str, address: str, days: int, now: int) -> dict[str, Any]:
        since, today = now - days * DAY, now // DAY * DAY
        account = account_id_for(address)
        if chain == "base":
            rows = [(a, base_category(r), t) for a, r, t in self.base.settlements_between(account, since, now)]
            known, allowance = self._base_allowance(account, today, now)
        elif self.solana is None:
            rows, known, allowance = [], False, None
        else:
            rows = [(a, solana_category(p), t) for a, p, t in self.solana.settled_spends_between(account, since, now)]
            known, allowance = self._solana_allowance(account, today, now)
        known = known or bool(rows)
        by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        by_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        topups = [0, 0]
        for amount, category, at in rows:
            if category == "credit_topup":
                topups[0] += 1
                topups[1] += amount
                continue
            by_category[category][0] += 1
            by_category[category][1] += amount
            date = time.strftime("%Y-%m-%d", time.gmtime(at))
            by_day[date][0] += 1
            by_day[date][1] += amount
        total = sum(v[1] for v in by_category.values())
        return {
            "address": address, "chain": chain, "known": known,
            "window": {"days": days, "since": since, "until": now},
            "spent": {
                "totalAtomic": str(total), "totalUsd": _usd(total), "count": sum(v[0] for v in by_category.values()),
                "byCategory": {c: {"count": v[0], "atomic": str(v[1]), "usd": _usd(v[1])}
                               for c, v in sorted(by_category.items())},
                "byDay": [{"date": d, "atomic": str(v[1]), "usd": _usd(v[1]), "count": v[0]}
                          for d, v in sorted(by_day.items())],
            },
            "creditTopUps": {"count": topups[0], "atomic": str(topups[1]), "usd": _usd(topups[1])},
            "allowance": allowance,
            "generatedAt": now,
        }

    def _base_allowance(self, account: str, today: int, now: int) -> tuple[bool, dict[str, Any] | None]:
        agent, limiter = self.base.agent(account), self.base.active_limiter(account)
        known = agent is not None or limiter is not None
        if limiter is None:
            return known, ({"configured": False, "state": "none"} if known else None)
        # Every settlement today, credit top-ups included: a top-up is paid through the limiter too, so it uses the
        # daily cap even though the spend breakdown above reports it separately (Codex review).
        spent = sum(a for a, _, _ in self.base.settlements_between(account, today, now))
        state = ("paused" if self.base.limiter_paused(limiter["limiter_address"])
                 else "expired" if limiter["expiry"] <= now else "active")
        # Our own bookkeeping of today's settled spend. The limiter's remainingToday() on chain is authoritative.
        return True, {
            "configured": True, "state": state, "dailyCapAtomic": str(limiter["daily_cap"]),
            "perPurchaseCapAtomic": str(limiter["per_purchase_cap"]), "spentTodayAtomic": str(spent),
            "remainingTodayAtomic": str(max(0, limiter["daily_cap"] - spent)), "expiry": limiter["expiry"],
            "limiter": limiter["limiter_address"],
        }

    def _solana_allowance(self, account: str, today: int, now: int) -> tuple[bool, dict[str, Any] | None]:
        agent, limits = self.solana.agent(account), self.solana.limits(account)
        known = agent is not None or limits is not None
        if limits is None:
            return known, ({"configured": False, "state": "none"} if known else None)
        spent = self.solana.spent_since(account, today)  # as the page's status: settled spends and metered holds
        # Without a chain read, "granted" is our record of the last finished GRANT/REVOKE from the wallet.
        state = ("expired" if limits["expiry"] <= now
                 else "granted" if self.solana.last_approval(account) == "GRANT" else "waiting_for_grant")
        return True, {
            "configured": True, "state": state, "dailyCapAtomic": str(limits["daily_cap"]),
            "perPurchaseCapAtomic": str(limits["per_purchase_cap"]), "spentTodayAtomic": str(spent),
            "remainingTodayAtomic": str(max(0, limits["daily_cap"] - spent)), "expiry": limits["expiry"],
            "limiter": agent["agent_address"] if agent is not None else None,
        }
