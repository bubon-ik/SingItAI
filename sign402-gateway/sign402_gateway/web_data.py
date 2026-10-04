"""Live data for the web chat, bought per question over x402 from the account's own limits.

The weather, an exchange rate, a flight's status, flight prices, places to eat or stay, a web page:
each is one paid request to a seller picked from the x402 catalogs (Coinbase's Agentic Market and
PayAI's Bazaar, September 2026) for being real and used, and read-only. Nothing here books, sends
or buys goods: data only.

Every seller is bound here to the address it is paid at on each network, as its 402 named it when
it was added, and to a price ceiling. A 402 that names another address, or asks more, is never paid.

  * Base: paid from the limiter like any purchase (web_internal.pay_from_allowance): the same
    limits, spending memory and agent key. Counted against the limits, kept out of the Purchases
    list, as web searches are.
  * Solana: the agent pays straight from the owner's USDC account as their delegate, within the
    Solana limits (solana_allowance.spend), through the bridge's `data-pay`
    (solana-x402-service/src/resource.mjs), which checks the host, the address and the ceiling again.

What comes back is cut down to what answers the question (`digest`) and handed to Venice as
untrusted data for the answer; it is never stored.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent_allowance import AllowanceError, AllowanceUnavailable
from .web_accounts import SOLANA_PREFIX

logger = logging.getLogger(__name__)

BASE = "eip155:8453"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DIGEST_CHARS = 7000
# Sources switched off until their seller is fixed; SIGN402_WEB_DATA_OFF names them (comma-separated, "" for none).
# Places came from Tripadvisor via paysponge until 1 October: it took 0.01 USDC twice and answered HTTP 403 both
# times, so places are searched on Exa now.
OFF_ENV = "SIGN402_WEB_DATA_OFF"
DEFAULT_OFF = ""


def switched_off() -> set[str]:
    return {name.strip() for name in os.environ.get(OFF_ENV, DEFAULT_OFF).split(",") if name.strip()}

OTTO = {BASE: "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808", SOLANA: "6XcSfqJHr9vNW2vbiRaMqUYVm7shDgLepca54wUTDPN5"}
EXA = {BASE: "0x6d6E695b09861467c7d462f5AAF31cF3540B9192", SOLANA: "12Ec2cJmfR1C9uwejzxcuMhUgEC7wDrLgm1wBvvR5w9E"}
STABLETRAVEL = {BASE: "0xDd257723b86B4947483905cdAcBbBC70fACF2ec0", SOLANA: "6u5LMGQC2qk9peNibahmRWxGVXrBypk8nhTCcqtiuqMY"}


def _q(**params: Any) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


def _cut(value: Any, limit: int = DIGEST_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text[:limit]


def _flights_status(data: Any) -> str:
    keep = ("ident_iata", "ident", "status", "cancelled", "diverted", "progress_percent", "scheduled_out", "estimated_out",
            "actual_out", "scheduled_in", "estimated_in", "actual_in", "departure_delay", "arrival_delay",
            "gate_origin", "terminal_origin", "gate_destination", "terminal_destination", "baggage_claim",
            "aircraft_type", "operator_iata")
    out = []
    for flight in (data.get("flights") if isinstance(data, dict) else None) or []:
        if not isinstance(flight, dict):
            continue
        row = {k: flight.get(k) for k in keep if flight.get(k) not in (None, "")}
        for end in ("origin", "destination"):
            place = flight.get(end) if isinstance(flight.get(end), dict) else {}
            row[end] = {k: place.get(k) for k in ("code_iata", "name", "city") if place.get(k)}
        out.append(row)
        if len(out) == 4:
            break
    return _cut({"flights": out} if out else data)


def _flights_search(data: Any) -> str:
    if not isinstance(data, dict):
        return _cut(data)
    offers = []
    for option in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        if not isinstance(option, dict):
            continue
        legs = [{"from": (leg.get("departure_airport") or {}).get("id"), "at": (leg.get("departure_airport") or {}).get("time"),
                 "to": (leg.get("arrival_airport") or {}).get("id"), "arrives": (leg.get("arrival_airport") or {}).get("time"),
                 "airline": leg.get("airline"), "flight": leg.get("flight_number")}
                for leg in option.get("flights") or [] if isinstance(leg, dict)]
        offers.append({"price": option.get("price"), "minutes": option.get("total_duration"),
                       "stops": max(0, len(legs) - 1), "legs": legs})
        if len(offers) == 6:
            break
    insights = data.get("price_insights") if isinstance(data.get("price_insights"), dict) else {}
    return _cut({"offers": offers, "lowest": insights.get("lowest_price"),
                 "typical": insights.get("typical_price_range"), "level": insights.get("price_level")})


def _search(data: Any) -> str:
    """Exa's pages for a places question: title, address of the page, and what it says (cut)."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return _cut(data)
    return _cut([{"title": page.get("title"), "url": page.get("url"), "text": str(page.get("text") or "")[:1200]}
                 for page in results if isinstance(page, dict)])


def _places_search(p: dict[str, str]) -> tuple[str, str, Any]:
    query, kind = p["query"], p.get("kind") or ""
    if kind == "hotels" and "hotel" not in query.lower():
        query = f"hotels {query}"
    elif kind == "attractions" and "things to do" not in query.lower():
        query = f"things to do {query}"
    return "POST", "https://api.exa.ai/search", {"query": query, "numResults": 5,
                                                 "contents": {"text": {"maxCharacters": 1200}}}


def _page(data: Any) -> str:
    results = data.get("results") if isinstance(data, dict) else None
    if results and isinstance(results[0], dict):
        page = results[0]
        return _cut({"title": page.get("title"), "url": page.get("url"), "text": page.get("text")})
    return _cut(data)


@dataclass(frozen=True)
class DataTool:
    id: str
    name: str                  # what the user sees under the answer
    source: str
    pay_to: dict[str, str]     # network -> the address this seller is paid at
    cap_atomic: int            # never more than this per request
    request: Callable[[dict[str, str]], tuple[str, str, Any]]  # params -> (method, url, body)
    required: tuple[str, ...] = ()
    digest: Callable[[Any], str] = _cut
    follow: Callable[[Any], list[str]] | None = None  # further requests of the same seller, e.g. details
    link: Callable[[dict[str, str]], str] | None = None
    networks: tuple[str, ...] = field(default=(BASE, SOLANA))


def _flights_link(p: dict[str, str]) -> str:
    words = f"Flights from {p['from']} to {p['to']} on {p['date']}" + (f" returning {p['return']}" if p.get("return") else "")
    return "https://www.google.com/travel/flights?" + _q(q=words)


OTTO_URL = "https://x402.ottoai.services"
TOOLS: dict[str, DataTool] = {t.id: t for t in (
    DataTool("weather", "Weather", "Otto AI", OTTO, 5_000,
             lambda p: ("GET", f"{OTTO_URL}/weather?{_q(location=p['place'])}", None), ("place",)),
    DataTool("fx", "Exchange rates", "Otto AI", OTTO, 3_000, lambda p: ("GET", f"{OTTO_URL}/fx-rates", None)),
    DataTool("token", "Token price", "Otto AI", OTTO, 3_000,
             lambda p: ("GET", f"{OTTO_URL}/token-details?{_q(symbol=p['symbol'])}", None), ("symbol",)),
    DataTool("markets", "Markets", "Otto AI", OTTO, 5_000,
             lambda p: ("GET", f"{OTTO_URL}/tradfi-data" + (f"?{_q(symbol=p['symbol'])}" if p.get("symbol") else ""), None)),
    DataTool("polymarket", "Polymarket", "Otto AI", OTTO, 3_000, lambda p: ("GET", f"{OTTO_URL}/pm-markets", None)),
    DataTool("crypto_news", "Crypto News", "Otto AI", OTTO, 3_000, lambda p: ("GET", f"{OTTO_URL}/crypto-news", None)),
    DataTool("funding", "Funding Rates", "Otto AI", OTTO, 3_000,
             lambda p: ("GET", f"{OTTO_URL}/funding-rates" + (f"?{_q(symbol=p['symbol'])}" if p.get("symbol") else ""), None)),
    DataTool("read_link", "Web page", "Exa", EXA, 3_000,
             lambda p: ("POST", "https://api.exa.ai/contents", {"urls": [p["url"]], "text": {"maxCharacters": 6000}}),
             ("url",), _page, link=lambda p: p["url"]),
    DataTool("flight_status", "FlightAware", "StableTravel", STABLETRAVEL, 15_000,
             lambda p: ("GET", f"https://stabletravel.dev/api/flightaware/flights/{urllib.parse.quote(p['flight'])}?"
                               + _q(ident_type="designator", max_pages=1), None),
             ("flight",), _flights_status,
             link=lambda p: f"https://www.flightaware.com/live/flight/{urllib.parse.quote(p['flight'])}"),
    DataTool("flight_search", "Google Flights", "StableTravel", STABLETRAVEL, 25_000,
             lambda p: ("GET", "https://stabletravel.dev/api/google-flights/search?" + _q(
                 departure_id=p["from"], arrival_id=p["to"], outbound_date=p["date"], return_date=p.get("return"),
                 type="1" if p.get("return") else "2", currency=p.get("currency") or "USD", hl="en", adults=1), None),
             ("from", "to", "date"), _flights_search, link=_flights_link),
    DataTool("places", "Web search", "Exa", EXA, 10_000, _places_search, ("query",), _search),
)}

# The bot's paid tools a Solana account can now use too: the same seller, the Solana leg.
FROM_PAID_TOOLS = {"otto.crypto_news": "crypto_news", "otto.funding_rates": "funding"}


def network_of(account: str) -> str:
    return SOLANA if str(account).startswith(SOLANA_PREFIX) else BASE


# Seconds for the seller's unpaid payment request, and once more. On 4 October Exa's answered after 40 s, then 20 s,
# then 0.4 s, while connections took milliseconds: a stall that asking again gets past. Asking costs nothing.
OFFER_DEADLINES = (6.0, 6.0)
_asking = ThreadPoolExecutor(max_workers=8, thread_name_prefix="offer")  # a late answer finishes here, unused


def _payment_request(gw: Any, tool: DataTool, method: str, url: str, body: Any) -> dict[str, Any]:
    for attempt, deadline in enumerate(OFFER_DEADLINES, 1):
        started = time.monotonic()
        asked = _asking.submit(gw.fetch_x402_payment_required, url, request_body=body if method == "POST" else None)
        try:
            return asked.result(timeout=deadline)
        except FutureTimeout:
            logger.warning("web data: %s did not ask for payment in %.1fs, try %d", tool.name, time.monotonic() - started,
                           attempt)
    raise AllowanceError(f"{tool.name} did not answer. Nothing was paid.")


def _offer(gw: Any, tool: DataTool, network: str, method: str, url: str, body: Any) -> dict[str, Any]:
    """The seller's payment request for this call, reduced to the one leg we pay: our network, USDC, the bound address."""
    payload = _payment_request(gw, tool, method, url, body)
    asset = BASE_USDC if network == BASE else SOLANA_USDC
    legs = [a for a in payload.get("accepts") or [] if isinstance(a, dict) and a.get("scheme", "exact") == "exact"
            and str(a.get("network")) == network and str(a.get("asset") or "").lower() == asset.lower()]
    if not legs:
        raise AllowanceError(f"{tool.name} does not take USDC on {'Solana' if network == SOLANA else 'Base'}. Nothing was paid.")
    bound = [a for a in legs if str(a.get("payTo") or "").lower() == tool.pay_to[network].lower()]
    if not bound:
        raise AllowanceError(f"{tool.name} asked to be paid somewhere unexpected. Nothing was paid.")
    leg = bound[0]
    if int(leg.get("amount") or 0) > tool.cap_atomic or int(leg.get("amount") or 0) <= 0:
        raise AllowanceError(f"{tool.name} asks more than its usual price. Nothing was paid.")
    return {**payload, "accepts": [leg]}


def _pay_base(server: Any, gw: Any, account: str, tool: DataTool, method: str, url: str, body: Any,
              record: bool = False) -> tuple[int, Any, str]:
    from .web_internal import pay_from_allowance  # noqa: PLC0415 - avoids an import cycle
    row = server.allowance.lane_for(account)
    if row is None:
        raise AllowanceUnavailable("Set your limits and approve them first.")
    offer = _offer(gw, tool, BASE, method, url, body)
    requirements = gw.normalize_x402_payment_required(offer, resource_url=url)
    gw._validate_base_usdc_x402_requirement(requirements)
    amount = int(requirements["amountAtomic"])
    if amount > int(row["per_purchase_cap"]):
        raise AllowanceError(f"{tool.name} costs more than your per-purchase limit. Nothing was paid.")
    event = pay_from_allowance(
        server, gw, account, {"id": f"data.{tool.id}", "name": tool.name, "kind": "x402_data", "source": tool.source,
                              "description": f"{tool.name} for a chat answer.", "resourceUrl": url.split("?")[0],
                              "mcpStyleName": f"data_{tool.id}", "inputSchema": {"type": "object", "properties": {}},
                              "command": "/chat"},
        url, requirements, request_body=body if method == "POST" else None,
        payment_context={"title": tool.name.upper(), "subject": "chat answer"},
        approval={"ok": True, "status": "approved", "source": "web_allowance", "approvalId": "web-data-" + secrets.token_hex(6)},
        claim_scope="data-" + secrets.token_hex(6), record=record)
    if not event.get("ok"):
        raise AllowanceError(f"{tool.name} did not answer. Nothing more was paid.")
    result = event.get("resourceResult") or {}
    return amount, result.get("body") if "body" in result else result.get("data"), str(event.get("txId") or "")


def _pay_solana(server: Any, account: str, tool: DataTool, gw: Any, method: str, url: str, body: Any) -> tuple[int, Any, str]:
    lane = getattr(server, "solana_allowance", None)
    if lane is None:
        raise AllowanceUnavailable("Solana payments are not set up on this server.")
    # spend() reads the wallet fresh and checks the limits, their expiry and the approval: no status() read first,
    # which cost a Node process of its own.
    if lane.store.limits(account) is None:
        raise AllowanceUnavailable("Set your limits and approve them from your wallet first.")
    started = time.monotonic()
    amount = int(_offer(gw, tool, SOLANA, method, url, body)["accepts"][0]["amount"])
    asked = time.monotonic()
    owner = lane.owner(account)
    paid = lane.spend(account, amount, tool.name, lambda: lane._call(
        account, "data-pay", callId="data-" + secrets.token_urlsafe(12), url=url, method=method, body=body,
        payTo=tool.pay_to[SOLANA], maxAmount=str(amount), owner=owner))
    logger.info("web data: %s on Solana: price %.1fs, wallet check and paid request %.1fs", tool.name, asked - started,
                time.monotonic() - asked)
    if paid.get("state") not in ("accepted", "confirmed"):
        raise AllowanceError(f"{tool.name} did not answer. It was not repeated.")
    return amount, paid.get("data"), str(paid.get("transaction") or "")


def pay_once(server: Any, gw: Any, account: str, tool: DataTool, method: str, url: str, body: Any, *,
             record: bool = False) -> tuple[int, Any, str]:
    """One paid request to a bound seller, from this account's limits on its own network."""
    if network_of(account) not in tool.networks:
        raise AllowanceError(f"{tool.name} is not sold on {'Solana' if network_of(account) == SOLANA else 'Base'}.")
    if network_of(account) == SOLANA:
        return _pay_solana(server, account, tool, gw, method, url, body)
    return _pay_base(server, gw, account, tool, method, url, body, record=record)


def buy(server: Any, gw: Any, account: str, tool_id: Any, params: Any) -> dict[str, Any]:
    """One question's data: the request, any follow-ups of the same seller, digested for the answer."""
    tool = TOOLS.get(str(tool_id or ""))
    if tool is None:
        raise AllowanceError("Unknown data source.")
    if tool.id in switched_off():
        raise AllowanceError(f"{tool.name} is switched off for now: its seller took payment without answering. "
                             "Nothing was paid.")
    params = {str(k): str(v).strip()[:300] for k, v in (params or {}).items() if str(v or "").strip()}
    missing = [name for name in tool.required if not params.get(name)]
    if missing:
        raise AllowanceError(f"{tool.name} needs {', '.join(missing)}.")
    network = network_of(account)
    if network not in tool.networks:
        raise AllowanceError(f"{tool.name} is not sold on {'Solana' if network == SOLANA else 'Base'}.")
    if getattr(gw, "_purchases_paused", lambda: False)():
        raise AllowanceError("Purchases are paused right now. Try again later.")

    def pay(method: str, url: str, body: Any) -> tuple[int, Any, str]:
        return pay_once(server, gw, account, tool, method, url, body)

    method, url, body = tool.request(params)
    spent, data, tx = pay(method, url, body)
    parts = [tool.digest(data)]
    if tool.follow is not None:
        for more in tool.follow(data):
            try:
                cost, extra, _ = pay("GET", more, None)
            except (AllowanceError, AllowanceUnavailable):
                break  # what was already bought still answers the question
            spent += cost
            parts.append(tool.digest(extra))
    return {"ok": True, "tool": tool.id, "name": tool.name, "source": tool.source, "costAtomic": spent,
            "costUsd": f"{spent / 1_000_000:.3f}", "network": "solana" if network == SOLANA else "base", "txId": tx,
            "digest": "\n".join(parts)[:DIGEST_CHARS * 2], **({"link": tool.link(params)} if tool.link else {})}
