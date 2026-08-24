"""Deciding whether a chat message needs the live web.

The decision has to be cheaper than the thing it decides: a search costs
$0.007, so asking a model whether to search would cost more than searching.
Everything here is string work — no network, no model, no money.

Three verdicts, in the design's order:

* `SEARCH`  — a present-tense marker settles it on its own.
* `SKIP`    — short chit-chat, or a follow-up that names no new subject.
* `ASK_MODEL` — undecidable cheaply. The model emits `NEED_WEB` while it
  answers, so the judgement rides along in a completion we were making anyway.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


logger = logging.getLogger(__name__)


class Verdict(Enum):
    SEARCH = "search"
    SKIP = "skip"
    ASK_MODEL = "ask_model"


# A year at or after this one reads as a question about the present. Kept
# deliberately behind the real cutoff: guessing early costs a search, guessing
# late costs a wrong answer.
DEFAULT_CUTOFF_YEAR = 2025

# Phrases that name the present. Matched on word boundaries so "now" does not
# fire inside "know".
_MARKERS = (
    r"today",
    r"right now",
    r"now",
    r"currently",
    r"current",
    r"latest",
    r"newest",
    r"this week",
    r"this month",
    r"so far",
    r"as of",
    r"news",
    r"breaking",
    r"price of",
    r"who is",
    r"what happened",
    r"what's happening",
)
_MARKER_RE = re.compile(r"\b(?:" + "|".join(_MARKERS) + r")\b")

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_BARE_TICKER_RE = re.compile(r"^\$[a-z]{2,6}$")
_BARE_DOMAIN_RE = re.compile(
    r"^(?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?$"
)

_WORD_RE = re.compile(r"[a-z0-9'’-]+")

# Words that carry no subject of their own. A message made only of these is
# chit-chat, whatever its length.
_CONVERSATIONAL = frozenset(
    """
    hi hello hey yo sup thanks thank thx ty please sorry bye cheers
    ok okay k sure yep yes no nope nah lol haha wow nice cool great
    good morning afternoon evening night day
    you u your ur me my i we
    what why how huh really seriously
    go on more again
    """.split()
)

# With a previous turn to lean on, these also fail to introduce a subject.
_FOLLOW_UP_ONLY = frozenset(
    """
    and or but so then also too instead
    the a an of for about with
    it its that this these those they them one ones
    first second third next last other another
    is are was were do does did can could would should
    tell show give explain say mean
    """.split()
)


def classify(
    message: str,
    *,
    has_previous_turn: bool = False,
    cutoff_year: int = DEFAULT_CUTOFF_YEAR,
) -> Verdict:
    """Decide whether `message` needs a paid web search."""
    text = (message or "").strip().lower()
    if not text:
        return Verdict.SKIP

    if _names_the_present(text, cutoff_year=cutoff_year):
        return Verdict.SEARCH

    if _is_subjectless(text, has_previous_turn=has_previous_turn):
        return Verdict.SKIP

    return Verdict.ASK_MODEL


def _names_the_present(text: str, *, cutoff_year: int) -> bool:
    if _MARKER_RE.search(text):
        return True
    if any(int(year) >= cutoff_year for year in _YEAR_RE.findall(text)):
        return True
    # A bare ticker or domain is a lookup, not a conversation.
    single = text.rstrip("?!.,")
    if _BARE_TICKER_RE.match(single) or _BARE_DOMAIN_RE.match(single):
        return True
    return False


def _is_subjectless(text: str, *, has_previous_turn: bool) -> bool:
    words = _WORD_RE.findall(text)
    if not words:
        return True
    allowed = _CONVERSATIONAL
    if has_previous_turn:
        allowed = allowed | _FOLLOW_UP_ONLY
    return all(word in allowed for word in words)


# --- the paid search itself -------------------------------------------------

BASE_MAINNET = "eip155:8453"
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
EXA_SEARCH_URL = "https://api.exa.ai/search"


class SearchUnavailable(RuntimeError):
    """The web could not be reached, and nothing was charged."""


class SearchBudgetExhausted(RuntimeError):
    """Today's search allowance is used up. The chat still answers."""


class SearchTooExpensive(RuntimeError):
    """The provider asked for more than the per-call ceiling."""


class MerchantChanged(RuntimeError):
    """The 402 named a recipient we never approved. Nothing was paid."""


@dataclass(frozen=True)
class SearchConfig:
    bound_pay_to: str
    endpoint: str = EXA_SEARCH_URL
    network: str = BASE_MAINNET
    asset: str = BASE_USDC
    max_per_call_atomic: int = 20_000
    max_per_day: int = 20
    # No trial by default: a search spends the user's own wallet from the
    # first call. Set a number here and the gateway account pays for that
    # many per user instead.
    free_calls: int = 0
    results: int = 3


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    text: str


@dataclass(frozen=True)
class SearchOutcome:
    results: tuple[SearchHit, ...]
    cost_atomic: int
    free: bool
    searches_left_today: int


class InMemorySearchLedger:
    """Per-user counters. Task 3 replaces this with the sqlite chat store."""

    def __init__(self, now: Callable[[], int] | None = None):
        self.now = now or (lambda: int(time.time()))
        self._days: dict[tuple[str, int], int] = {}
        self._totals: dict[str, int] = {}
        self._paused: set[str] = set()

    def count_today(self, user_id: str) -> int:
        return self._days.get((user_id, self._day()), 0)

    def total_count(self, user_id: str) -> int:
        return self._totals.get(user_id, 0)

    def record(self, user_id: str, cost_atomic: int) -> None:
        key = (user_id, self._day())
        self._days[key] = self._days.get(key, 0) + 1
        self._totals[user_id] = self._totals.get(user_id, 0) + 1

    def pause(self, user_id: str, reason: str) -> None:
        self._paused.add(user_id)

    def is_paused(self, user_id: str) -> bool:
        return user_id in self._paused

    def _day(self) -> int:
        return self.now() // 86_400


class WebSearchClient:
    """One search: probe the 402, check the recipient, pay, read the results.

    Simpler than the chat lane by design — no prefund, no outstanding credit,
    no local ledger of money. One call, one payment.
    """

    def __init__(
        self,
        *,
        ledger: InMemorySearchLedger,
        transport: Callable[..., Any],
        settle_from_gateway: Callable[..., dict[str, Any]],
        settle_from_user: Callable[..., dict[str, Any]],
        config: SearchConfig,
        purchases_paused: Callable[[], bool] | None = None,
        on_merchant_change: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.ledger = ledger
        self.transport = transport
        self.settle_from_gateway = settle_from_gateway
        self.settle_from_user = settle_from_user
        self.config = config
        self.purchases_paused = purchases_paused or (lambda: False)
        self.on_merchant_change = on_merchant_change or (lambda notice: None)

    def search(
        self, user_id: str, query: str, *, wallet_address: str
    ) -> SearchOutcome:
        # Every refusal that can be decided without a network call is decided
        # before one is made.
        if self.ledger.is_paused(user_id):
            raise SearchUnavailable("Web search is paused for this account.")
        used_today = self.ledger.count_today(user_id)
        if used_today >= self.config.max_per_day:
            raise SearchBudgetExhausted(
                "Today's web searches are used up. They reset at 00:00 UTC."
            )
        if self.purchases_paused():
            raise SearchUnavailable("Purchases are paused right now.")

        body = {"query": query, "numResults": self.config.results}
        requirement = self._challenge(user_id, body)

        amount = int(requirement.get("amount") or 0)
        if amount > self.config.max_per_call_atomic:
            raise SearchTooExpensive(
                "The search provider asked for more than the agreed price."
            )

        free = self.ledger.total_count(user_id) < self.config.free_calls
        settle = self.settle_from_gateway if free else self.settle_from_user
        try:
            settlement = settle(
                requirement, user_id=user_id, request_body=body
            )
        except Exception as exc:
            # Type and message only, truncated: never the query, never a key.
            logger.warning(
                "web search settlement failed: %s: %s",
                type(exc).__name__,
                str(exc)[:200],
            )
            raise SearchUnavailable("The web search did not go through.") from None

        if not isinstance(settlement, dict) or not settlement.get("ok"):
            logger.warning(
                "web search returned status %s",
                (settlement or {}).get("status") if isinstance(settlement, dict) else "?",
            )
            raise SearchUnavailable("The web search did not go through.")

        # Paid. From here the search is billed whatever came back — including
        # nothing. A delivered service that found no pages is still delivered.
        self.ledger.record(user_id, 0 if free else amount)
        return SearchOutcome(
            results=_hits(settlement.get("body")),
            cost_atomic=0 if free else amount,
            free=free,
            searches_left_today=max(
                0, self.config.max_per_day - self.ledger.count_today(user_id)
            ),
        )

    def _challenge(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Fetch the 402 and pick the Base leg, or fail without paying."""
        try:
            response = self.transport(
                "POST",
                self.config.endpoint,
                headers={"Accept": "application/json"},
                json_body=body,
            )
        except Exception as exc:
            logger.warning(
                "web search challenge failed: %s", type(exc).__name__
            )
            raise SearchUnavailable("The web is unavailable right now.") from None

        if getattr(response, "status", None) != 402:
            logger.warning(
                "web search expected 402, got %s", getattr(response, "status", "?")
            )
            raise SearchUnavailable("The web is unavailable right now.")

        payload = response.json() or {}
        for accept in payload.get("accepts") or []:
            if str(accept.get("network") or "").lower() != self.config.network:
                continue
            if str(accept.get("asset") or "").lower() != self.config.asset:
                continue
            pay_to = str(accept.get("payTo") or "")
            if pay_to.lower() != self.config.bound_pay_to.lower():
                # The one failure that must never become a payment: the money
                # would be correct in amount and wrong in destination.
                self.ledger.pause(user_id, "merchant_changed")
                # The operator hears about it as the same event: a binding
                # that no longer matches is a merchant change, not a glitch.
                self.on_merchant_change(
                    {
                        "resource": self.config.endpoint,
                        "expected": self.config.bound_pay_to,
                        "seen": pay_to,
                    }
                )
                raise MerchantChanged(
                    "The search provider asked to be paid somewhere unexpected."
                )
            return dict(accept)

        raise SearchUnavailable("The web is unavailable right now.")


def _hits(body: Any) -> tuple[SearchHit, ...]:
    results = (body or {}).get("results") if isinstance(body, dict) else None
    hits = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        hits.append(
            SearchHit(
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                text=str(item.get("text") or item.get("summary") or ""),
            )
        )
    return tuple(hits)


class ChatStoreSearchLedger:
    """The same counters, kept in the chat store the rest of the lane uses.

    Search shares the user's daily window with chat — one rollover at 00:00
    UTC, one row — but is counted in searches so it can be shown and capped
    on its own.
    """

    def __init__(self, store: Any):
        self.store = store

    def count_today(self, user_id: str) -> int:
        return self.store.get_session(user_id).searches_this_window

    def total_count(self, user_id: str) -> int:
        return self.store.get_session(user_id).searches_total

    def record(self, user_id: str, cost_atomic: int) -> None:
        self.store.record_search(user_id)

    def pause(self, user_id: str, reason: str) -> None:
        self.store.set_search_paused(user_id, True)

    def is_paused(self, user_id: str) -> bool:
        return self.store.get_session(user_id).search_paused


# --- one turn ---------------------------------------------------------------

NEED_WEB = "NEED_WEB:"

_NEED_WEB_RE = re.compile(r"NEED_WEB:\s*(.+)", re.IGNORECASE)


@dataclass(frozen=True)
class WebAnswer:
    text: str
    searched: bool
    footer: str


def answer_with_web(
    *,
    ask: Callable[[str], str],
    search: Any,
    user_id: str,
    message: str,
    wallet_address: str,
    has_previous_turn: bool = False,
) -> WebAnswer:
    """Answer one message, searching the live web at most once.

    Two ways in, and never both: the pre-filter decides for free before the
    model runs, or the model asks for the web mid-answer with a `NEED_WEB`
    token and we spend one more completion. A search that fails is never
    allowed to fail the message.
    """
    verdict = classify(message, has_previous_turn=has_previous_turn)

    if verdict is Verdict.SEARCH:
        outcome, note = _try_search(search, user_id, message, wallet_address)
        if outcome is not None:
            return WebAnswer(
                text=_clean(ask(_with_results(message, outcome))),
                searched=True,
                footer=_footer(outcome),
            )
        return WebAnswer(text=_clean(ask(message)), searched=False, footer=note)

    prompt = message if verdict is Verdict.SKIP else _with_need_web_offer(message)
    first = ask(prompt)

    asked_for = _need_web_query(first)
    if asked_for is None:
        return WebAnswer(text=_clean(first), searched=False, footer="")

    outcome, note = _try_search(search, user_id, asked_for, wallet_address)
    if outcome is None:
        # The model already said it lacks current facts; make it answer anyway
        # rather than showing the user a bare token.
        return WebAnswer(text=_clean(ask(message)), searched=False, footer=note)

    # Exactly one search per message: whatever the second completion says, it
    # is the answer. A second NEED_WEB is stripped, not obeyed.
    return WebAnswer(
        text=_clean(ask(_with_results(message, outcome))),
        searched=True,
        footer=_footer(outcome),
    )


def _try_search(search, user_id, query, wallet_address):
    """Returns (outcome, footer-note). Never raises: the message must survive."""
    try:
        return search.search(user_id, query, wallet_address=wallet_address), ""
    except SearchBudgetExhausted:
        return None, "web search is off until tomorrow · answered from memory"
    except (SearchUnavailable, MerchantChanged, SearchTooExpensive):
        return None, "the web was unavailable · answered from memory"


def _with_results(message: str, outcome: SearchOutcome) -> str:
    if not outcome.results:
        return message
    lines = [
        f"[{index}] {hit.title} — {hit.url}\n{hit.text}"
        for index, hit in enumerate(outcome.results, start=1)
    ]
    return (
        "Current web results, retrieved just now:\n\n"
        + "\n\n".join(lines)
        + "\n\nUsing them where they are relevant, answer: "
        + message
    )


def _with_need_web_offer(message: str) -> str:
    return (
        "If answering this needs facts newer than your training data, reply "
        "with exactly `NEED_WEB: <search query>` and nothing else. Otherwise "
        "answer normally.\n\n" + message
    )


def _need_web_query(text: str) -> str | None:
    match = _NEED_WEB_RE.search(text or "")
    if match is None:
        return None
    return match.group(1).strip().strip("`").strip() or None


def _clean(text: str) -> str:
    return _NEED_WEB_RE.sub("", text or "").strip()


def _footer(outcome: SearchOutcome) -> str:
    dollars = outcome.cost_atomic / 1_000_000
    return (
        f"searched the web · ${dollars:.3f} · "
        f"{outcome.searches_left_today} searches left today"
    )


# --- wiring -----------------------------------------------------------------


def build_web_search_from_env(
    *,
    store: Any,
    settle_from_gateway: Callable[..., dict[str, Any]],
    settle_from_user: Callable[..., dict[str, Any]],
    env: Any = None,
    transport: Callable[..., Any] | None = None,
    purchases_paused: Callable[[], bool] | None = None,
    on_merchant_change: Callable[[dict[str, Any]], None] | None = None,
) -> WebSearchClient | None:
    """Build the search client, or return None when the feature is off.

    None rather than a disabled object, for the same reason the chat service
    does it: with the flag unset there is nothing to accidentally call.
    """
    import os

    values = os.environ if env is None else env
    if str(values.get("SIGN402_AI_SEARCH_ENABLED", "")).strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None

    pay_to = str(values.get("SIGN402_AI_SEARCH_MERCHANT_PAYTO", "") or "").strip()
    if not pay_to:
        raise ValueError(
            "SIGN402_AI_SEARCH_MERCHANT_PAYTO is required when AI search is "
            "enabled"
        )

    def number(name: str, default: int) -> int:
        raw = str(values.get(name, "") or "").strip()
        return int(raw) if raw else default

    return WebSearchClient(
        ledger=ChatStoreSearchLedger(store),
        transport=transport or _urllib_transport,
        settle_from_gateway=settle_from_gateway,
        settle_from_user=settle_from_user,
        config=SearchConfig(
            bound_pay_to=pay_to,
            endpoint=str(
                values.get("SIGN402_AI_SEARCH_URL", "") or EXA_SEARCH_URL
            ),
            max_per_call_atomic=number(
                "SIGN402_AI_SEARCH_MAX_PER_CALL_ATOMIC", 20_000
            ),
            max_per_day=number("SIGN402_AI_SEARCH_MAX_PER_DAY", 20),
            free_calls=number("SIGN402_AI_SEARCH_FREE_CALLS", 0),
            results=number("SIGN402_AI_SEARCH_RESULTS", 3),
        ),
        purchases_paused=purchases_paused,
        on_merchant_change=on_merchant_change,
    )


def _urllib_transport(method: str, url: str, *, headers=None, json_body=None):
    import json as _json
    import urllib.error
    import urllib.request

    data = None if json_body is None else _json.dumps(json_body).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers=dict(headers or {})
    )
    if data is not None:
        request.add_header("Content-Type", "application/json")

    class _Response:
        def __init__(self, status, body):
            self.status = status
            self._body = body
            self.headers = {}

        def json(self):
            try:
                return _json.loads(self._body or "{}")
            except ValueError:
                return {}

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return _Response(response.status, response.read().decode())
    except urllib.error.HTTPError as error:
        # A 402 arrives here, not above: it is the answer we came for.
        return _Response(error.code, error.read().decode())
