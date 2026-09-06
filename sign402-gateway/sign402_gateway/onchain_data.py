"""Onchain answers, bought from The Graph, for questions the web answers worse.

`web_search.classify` already sends "price of WETH" and a bare `$WETH` to a paid
web search. That search comes back with an article, a scraped aggregator page, or
whatever a crawler saw last — a secondhand number with no block behind it.

The same question has a first-hand answer. The Graph's x402 gateway sells one
subgraph query for $0.01 in USDC on Base and asks for no API key, so an agent
can read the actual pool on the actual DEX at the current block. For the narrow
class of questions a subgraph can answer, that is strictly better than a search
result, and it costs about the same.

## What this will and will not answer

A subgraph indexes **one contract's events**, so it knows about protocols, not
about addresses. Uniswap V3 on Base is a subgraph; "every address on Base" is
not and cannot be (see `docs/checks.md`, G3). So:

    "price of WETH"          -> here, from the pool
    "how much is $cbBTC"     -> here, from the pool
    "what happened to Base"  -> web search, as before
    "who is Vitalik"         -> web search, as before

Anything this module declines falls through to the existing web search
untouched. It is a branch in the router, not a replacement for it.

## Why the payment goes through the spending policy

Every query is authorised by the same `SpendingPolicy` that governs a
twenty-five-dollar gift card, so a one-cent query draws on the same daily cap,
lands in the same journal, and is subject to the same rules — including the one
that matters most for a keyless endpoint: if The Graph's payout address ever
changes, the payment is blocked and the whole fleet is warned. An endpoint where
payment *is* authentication has no API key to notice a redirected address with,
which makes it precisely the place that check earns its keep.

That also means the first ever query escalates, like any unknown merchant. In a
chat that is not an error to show the user: the question falls through to web
search and the escalation is recorded. A human approving a merchant is the point
of the system, not an obstacle to route around.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

UNISWAP_V3_BASE_SUBGRAPH = "GqzP4Xaehti8KSfQmv3ZctFSjnSUYZ4En5NRsiTbvZpz"
"""Uniswap V3 on Base, by subgraph id.

Pinned rather than discovered. Resolving a subgraph by keyword at request time
would mean a chat message choosing which contract to trust, and the answer to
"which Uniswap is the real one" must not be a search result.
"""

GRAPH_GATEWAY = "https://gateway.thegraph.com/api/x402/subgraphs/id"

BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

DEFAULT_MAX_PER_CALL_ATOMIC = 20_000
"""$0.02. The gateway quotes $0.01; twice that is room for a price change and
far short of anything worth paying by accident."""

DEFAULT_MIN_LIQUIDITY_USD = Decimal("50000")
"""How much has to be in a pool before its price is worth repeating.

Not a nicety. Querying Base USDC pools by liquidity returns, at the top, a pool
holding a nominal twenty-one billion dollars whose token has the symbol
`unknown` and a price of exactly 1000000 — a fabrication that outranks every
real pool. Depth is the cheapest thing that separates a market from a decoration.
"""

_PRICE_QUESTION = re.compile(
    r"(?:^|\b)(?:price\s+of|how\s+much\s+is|what\s+is\s+the\s+price\s+of)\s+\$?([a-z0-9]{2,12})\b",
    re.I,
)
_BARE_TICKER = re.compile(r"^\s*\$([a-z0-9]{2,12})\s*$", re.I)

SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{2,12}$")
"""What may be sent as a GraphQL variable.

The symbol arrives from a chat message. It travels as a bound variable rather
than as interpolated text, so this is a second line rather than the only one,
but a value that cannot be a token symbol is not worth a paid query either.
"""


def classify_onchain(message: str) -> str | None:
    """The token symbol this message is asking the price of, or None.

    Deliberately narrow. Everything it does not recognise keeps the behaviour
    the gateway has today, which makes this branch safe to add and cheap to
    remove.
    """
    text = (message or "").strip()
    if not text:
        return None
    for pattern in (_BARE_TICKER, _PRICE_QUESTION):
        found = pattern.search(text)
        if found:
            symbol = found.group(1).upper()
            return symbol if SYMBOL_RE.match(symbol) else None
    return None


PRICE_QUERY = """
query PoolsForSymbol($symbol: String!, $usdc: String!, $minTvl: BigDecimal!) {
  asToken0: pools(
    first: 3
    orderBy: totalValueLockedUSD
    orderDirection: desc
    where: {token1: $usdc, token0_: {symbol: $symbol}, totalValueLockedUSD_gt: $minTvl}
  ) { id feeTier token1Price totalValueLockedUSD volumeUSD token0 { symbol } }
  asToken1: pools(
    first: 3
    orderBy: totalValueLockedUSD
    orderDirection: desc
    where: {token0: $usdc, token1_: {symbol: $symbol}, totalValueLockedUSD_gt: $minTvl}
  ) { id feeTier token0Price totalValueLockedUSD volumeUSD token1 { symbol } }
}
""".strip()
"""Both orientations, because which side USDC sits on is the pool's business.

`token1Price` is token1 per token0, so with USDC as token1 it is the USD price
of the other token; `token0Price` is the mirror. Asking for both in one query
costs one payment rather than two.
"""


@dataclass(frozen=True)
class OnchainPrice:
    symbol: str
    usd: Decimal
    liquidity_usd: Decimal
    pool: str
    fee_tier: str
    paid: bool
    journal_id: str

    def as_fact(self) -> str:
        """One line for the model to answer from, source named.

        The pool address is in it on purpose: an answer about a market should
        say which market, and a reader who wants to check has what they need.
        """
        return (
            f"{self.symbol} trades at ${self.usd:,.6f} USDC on Uniswap V3 "
            f"(Base), pool {self.pool} at the {int(self.fee_tier) / 10_000:.2f}% "
            f"fee tier, with ${self.liquidity_usd:,.0f} of liquidity. Read from "
            "the subgraph at the current block."
        )

    def footer(self) -> str:
        return (
            "onchain · Uniswap V3 on Base via The Graph"
            + ("" if self.paid else " · answered from the journal, nothing paid")
        )


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def read_price(payload: Any, symbol: str) -> OnchainPrice | None:
    """The deepest sane pool in the answer, or None.

    Returns None rather than raising: a question this cannot answer is a
    question the web search should get, and an exception here would turn a
    missing price into a failed chat message.
    """
    data = ((payload or {}).get("data") or {}) if isinstance(payload, Mapping) else {}
    best: tuple[Decimal, Decimal, str, str] | None = None

    for key, price_field, token_field in (
        ("asToken0", "token1Price", "token0"),
        ("asToken1", "token0Price", "token1"),
    ):
        for pool in data.get(key) or []:
            if not isinstance(pool, Mapping):
                continue
            found = ((pool.get(token_field) or {}).get("symbol") or "").upper()
            if found != symbol.upper():
                # The `symbol` filter is the subgraph's; this is ours. A
                # mismatch here means the schema moved, and repeating a price
                # for a token nobody asked about is worse than saying nothing.
                continue
            price = _decimal(pool.get(price_field))
            tvl = _decimal(pool.get("totalValueLockedUSD"))
            if price is None or tvl is None or price <= 0:
                continue
            if best is None or tvl > best[1]:
                best = (price, tvl, str(pool.get("id") or ""), str(pool.get("feeTier") or "0"))

    if best is None:
        return None
    price, tvl, pool_id, fee = best
    return OnchainPrice(
        symbol=symbol.upper(),
        usd=price,
        liquidity_usd=tvl,
        pool=pool_id,
        fee_tier=fee,
        paid=False,
        journal_id="",
    )


class OnchainUnavailable(RuntimeError):
    """No answer, for any reason. Never fatal — the caller falls back."""


@dataclass(frozen=True)
class OnchainConfig:
    subgraph: str = UNISWAP_V3_BASE_SUBGRAPH
    gateway: str = GRAPH_GATEWAY
    max_per_call_atomic: int = DEFAULT_MAX_PER_CALL_ATOMIC
    min_liquidity_usd: Decimal = DEFAULT_MIN_LIQUIDITY_USD

    @property
    def resource_url(self) -> str:
        return f"{self.gateway}/{self.subgraph}"


class OnchainDataClient:
    """Ask The Graph one question, paying for it out of the agent's budget.

    Holds a `PaidGraphQueries` and adds no rules of its own. The cap, the
    provider memory, the don't-pay-twice journal read and the receipt are all
    the library's, which is what keeps this file a router rather than a second
    place where money decisions are made.
    """

    def __init__(
        self,
        *,
        paid_queries_factory: Callable[[str], Any],
        fetch_402: Callable[[str], tuple[Mapping[str, str], Any]],
        pay_and_fetch: Callable[..., tuple[Any, str | None]],
        config: OnchainConfig | None = None,
        purchases_paused: Callable[[], bool] | None = None,
    ) -> None:
        self.paid_queries_factory = paid_queries_factory
        self.fetch_402 = fetch_402
        self.pay_and_fetch = pay_and_fetch
        self.config = config or OnchainConfig()
        self.purchases_paused = purchases_paused or (lambda: False)

    def price(self, user_id: str, symbol: str) -> OnchainPrice:
        if self.purchases_paused():
            raise OnchainUnavailable("Purchases are paused right now.")
        if not SYMBOL_RE.match(symbol or ""):
            raise OnchainUnavailable("Not a token symbol.")

        variables = {
            "symbol": symbol.upper(),
            "usdc": BASE_USDC,
            "minTvl": str(self.config.min_liquidity_usd),
        }
        queries = self.paid_queries_factory(user_id)
        url = self.config.resource_url

        def guarded_pay(payment: Any, requirements: Mapping[str, Any]):
            amount = int(str(requirements.get("amount") or requirements.get("maxAmountRequired") or 0))
            if amount > self.config.max_per_call_atomic:
                # The one refusal that must happen before money moves. The
                # policy caps a day; this caps a single surprise.
                raise OnchainUnavailable(
                    "The Graph asked for more than the agreed price."
                )
            return self.pay_and_fetch(payment, requirements, body=self._body(variables))

        try:
            answer = queries.query(
                resource_url=url,
                deployment=self.config.subgraph,
                query=PRICE_QUERY,
                variables=variables,
                fetch_402=lambda: self.fetch_402(url),
                pay_and_fetch=guarded_pay,
                owner=user_id,
            )
        except OnchainUnavailable:
            raise
        except Exception as exc:
            logger.warning(
                "onchain price failed: %s: %s", type(exc).__name__, str(exc)[:200]
            )
            raise OnchainUnavailable("The onchain lookup did not go through.") from None

        if answer.needs_human:
            # An escalation or a block. Not an error and not something to route
            # around: the verdict is in the journal, and the message falls
            # through to the web search the gateway already had.
            logger.info(
                "onchain price not paid: %s (%s)",
                answer.decision.action.value,
                answer.decision.rule,
            )
            raise OnchainUnavailable(str(answer.decision.reason))

        price = read_price(answer.answer, symbol)
        if price is None:
            raise OnchainUnavailable(f"No liquid {symbol.upper()}/USDC pool on Base.")

        from dataclasses import replace

        return replace(price, paid=answer.paid, journal_id=answer.journal_id)

    def _body(self, variables: Mapping[str, Any]) -> dict[str, Any]:
        return {"query": PRICE_QUERY, "variables": dict(variables)}


def with_fact(message: str, fact: str) -> str:
    """Hand the model the reading and tell it where the reading came from.

    Mirrors `web_search._with_results`: the model answers the user's question,
    but the number in front of it is the pool's rather than its own.
    """
    return (
        f"{message}\n\n"
        "Onchain reading, taken just now from the subgraph. Use this number "
        "rather than anything you remember, and say it is from the pool on "
        f"Uniswap V3 on Base:\n{fact}"
    )


def _urllib_402(url: str) -> tuple[Mapping[str, str], Any]:
    """Fetch the unpaid 402. Headers matter more than the body here.

    The Graph answers with `content-length: 0` and the requirements
    base64-encoded in a `payment-required` header, so a transport that returns
    only the body — which is what most x402 clients do — hands back nothing at
    all. See `docs/checks.md`, G1.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        # A 402 arrives here, not above: it is the expected answer, not a fault.
        return dict(exc.headers or {}), exc.read()


ENABLED_ENV = "SIGN402_ONCHAIN_DATA_ENABLED"


def build_onchain_data_from_env(
    *,
    policy: Any,
    pay: Callable[..., dict[str, Any]],
    purchases_paused: Callable[[], bool] | None = None,
    env: Any = None,
    fetch_402: Callable[[str], tuple[Mapping[str, str], Any]] | None = None,
) -> OnchainDataClient | None:
    """The client, or None when the feature is off.

    None rather than a disabled object, for the same reason `web_search` does
    it: with the flag unset there is nothing to accidentally call, and every
    chat path is exactly the one that ran before this existed.

    Returns None when there is no spending policy either. Paying for data with
    no cap, no provider memory and no journal is the thing this module exists
    to avoid, so it declines to run rather than falling back to doing it
    unguarded.
    """
    import os

    values = os.environ if env is None else env
    if str(values.get(ENABLED_ENV, "")).strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    if policy is None:
        logger.warning(
            "%s is on but spending memory is off, so there is no cap, no "
            "provider memory and no journal. Onchain data stays disabled.",
            ENABLED_ENV,
        )
        return None

    from spending_memory.adapters.thegraph import PaidGraphQueries

    def number(name: str, default: int) -> int:
        raw = str(values.get(name, "") or "").strip()
        return int(raw) if raw else default

    config = OnchainConfig(
        subgraph=str(values.get("SIGN402_ONCHAIN_SUBGRAPH", "") or UNISWAP_V3_BASE_SUBGRAPH),
        max_per_call_atomic=number("SIGN402_ONCHAIN_MAX_PER_CALL_ATOMIC", DEFAULT_MAX_PER_CALL_ATOMIC),
        min_liquidity_usd=Decimal(
            str(values.get("SIGN402_ONCHAIN_MIN_LIQUIDITY_USD", "") or DEFAULT_MIN_LIQUIDITY_USD)
        ),
    )
    cache_ttl = number("SIGN402_ONCHAIN_CACHE_TTL_SECONDS", 300)

    def settle(payment: Any, requirements: Mapping[str, Any], body: dict[str, Any] | None = None):
        # Funded by the gateway, budgeted to the user: the money is the
        # operator's, but the cap, the journal line and the daily total belong
        # to whoever asked. Web search already splits it this way.
        result = pay(
            config.resource_url,
            max_atomic=str(requirements.get("amount") or requirements.get("maxAmountRequired") or ""),
            expected_receiver=str(requirements.get("payTo") or ""),
            expected_asset=str(requirements.get("asset") or ""),
            method="POST",
            request_body=body or {},
        )
        if not isinstance(result, dict) or not result.get("ok"):
            raise OnchainUnavailable("The onchain lookup did not go through.")
        return result.get("body"), str(result.get("txId") or "") or None

    return OnchainDataClient(
        paid_queries_factory=lambda owner: PaidGraphQueries(
            policy, owner=owner, cache_ttl_seconds=cache_ttl
        ),
        fetch_402=fetch_402 or _urllib_402,
        pay_and_fetch=settle,
        config=config,
        purchases_paused=purchases_paused,
    )
