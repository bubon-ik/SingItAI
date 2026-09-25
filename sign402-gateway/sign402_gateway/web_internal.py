"""The gateway's side of the web page: the shop and purchase history for a web account.

Design: docs/allowance-web-v1.md, step 4. The public web API
(sign402_gateway.web_api) signs users in and calls these over loopback with
SIGN402_WEB_INTERNAL_TOKEN, naming the account (`wallet:0x…`) it has verified.
Purchases run here, where the spending limits, spending memory, rate limits and
purchase history already live, so the web and the bot are held to the same rules.

A web account only ever pays from its own limiter; there is no custodial wallet
to fall back to. The human approval that spending memory may ask for is the
user's click on "Buy" for a quote they were shown: the quote fixes the price and
the recipient, and a purchase that would pay more, or someone else, is refused.
"""

from __future__ import annotations

import secrets
import threading
import time
from decimal import Decimal
from typing import Any

from .agent_allowance import AllowanceError, AllowanceUnavailable
from .web_accounts import ACCOUNT_PREFIX

QUOTE_SECONDS = 600
PUBLIC_TOOL_FIELDS = ("id", "name", "description", "source", "resourceUrl", "inputSchema")


def web_confirmation(quote_id: str) -> dict[str, Any]:
    """The approval a web purchase carries: the user confirmed this quote on the page."""
    return {"ok": True, "status": "approved", "source": "web_confirmation", "approvalId": f"web-{quote_id}"}


class ToolQuotes:
    """Quotes for x402 tools, in memory: a restart only means quoting again."""

    def __init__(self) -> None:
        self._quotes: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def add(self, quote: dict[str, Any]) -> str:
        quote_id = "tq_" + secrets.token_urlsafe(12)
        now = time.time()
        with self._lock:
            for key in [k for k, v in self._quotes.items() if v["expiresAt"] < now]:
                del self._quotes[key]
            self._quotes[quote_id] = quote
        return quote_id

    def take(self, account: str, quote_id: str) -> dict[str, Any]:
        with self._lock:
            quote = self._quotes.get(str(quote_id))
            if quote is None or quote["account"] != account:
                raise AllowanceError("That quote is unknown or not yours. Get a new one.")
            del self._quotes[str(quote_id)]
        if quote["expiresAt"] < time.time():
            raise AllowanceError("That quote expired. Get a new one.")
        return quote


def _account(server: Any, payload: dict[str, Any]) -> str:
    account = str(payload.get("account") or "")
    service = getattr(server, "allowance", None)
    if service is None:
        raise AllowanceUnavailable("The allowance lane is off on this server.")
    if not account.startswith(ACCOUNT_PREFIX) or service.owner_lookup is None or not service.owner_lookup(account):
        raise AllowanceUnavailable("Unknown web account.")
    return account


def _receiver(requirements: dict[str, Any]) -> str:
    """Who the seller asks to be paid: `receiver` once normalized, `payTo` in a raw x402 requirement."""
    receiver = str(requirements.get("receiver") or requirements.get("payTo") or "")
    if not receiver:
        raise AllowanceError("The seller named no recipient. Nothing was paid.")
    return receiver


def _limits_from_limiter(server: Any, gw: Any, account: str) -> None:
    """A web account's spending limits are the ones it signed into its limiter.

    The bot's /set_limits has no counterpart on the page, and a second, lower
    set of limits the user never chose would refuse purchases their limiter
    allows. The operator's hard ceilings still apply on top.
    """
    row = server.allowance.store.active_limiter(account)
    if row is None:
        return
    defaults, ceilings = gw._operator_user_wallet_limits(), gw._operator_user_wallet_ceilings()
    per_tx, daily = int(row["per_purchase_cap"]), int(row["daily_cap"])
    if ceilings["ceilingPerTxAtomic"] is not None:
        per_tx = min(per_tx, ceilings["ceilingPerTxAtomic"])
    if ceilings["ceilingDailyAtomic"] is not None:
        daily = min(daily, ceilings["ceilingDailyAtomic"])
    server.user_spend_limit_store.set_limit_settings(
        account, max_per_tx_atomic=per_tx, daily_cap_atomic=max(daily, per_tx),
        operator_max_per_tx_atomic=defaults["maxPerTxAtomic"], operator_daily_cap_atomic=defaults["dailyCapAtomic"],
        operator_ceiling_per_tx_atomic=ceilings["ceilingPerTxAtomic"],
        operator_ceiling_daily_atomic=ceilings["ceilingDailyAtomic"])


def _usd(atomic: int) -> str:
    return f"{Decimal(atomic) / Decimal(1_000_000):f}".rstrip("0").rstrip(".") or "0"


def handle(server: Any, action: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from . import server as gw  # the gateway's purchase helpers; imported here to avoid a cycle

    account = _account(server, payload)
    service = server.allowance
    if action == "tools":
        return 200, {"ok": True, "tools": [
            {k: tool[k] for k in PUBLIC_TOOL_FIELDS if k in tool} for tool in gw.PAID_TOOLS.values()]}

    if action == "tool-quote":
        if service.lane_for(account) is None:
            raise AllowanceUnavailable("Set up your allowance first.")
        tool = gw._resolve_paid_tool(payload)
        resource_url = gw._paid_tool_resource_url(tool, payload)
        request_body = tool.get("requestBody") if isinstance(tool.get("requestBody"), dict) else None
        requirements = gw.normalize_x402_payment_required(
            gw.fetch_x402_payment_required(resource_url, request_body=request_body), resource_url=resource_url)
        gw._validate_base_usdc_x402_requirement(requirements)
        amount = gw._payment_amount_atomic(requirements)
        now = time.time()
        quote = {"account": account, "toolId": tool["id"], "resourceUrl": resource_url, "amount": amount,
                 "payTo": _receiver(requirements), "payload": dict(payload), "expiresAt": now + QUOTE_SECONDS}
        quote_id = server.web_tool_quotes.add(quote)
        return 200, {
            "ok": True, "quoteId": quote_id, "tool": {"id": tool["id"], "name": tool.get("name")},
            "priceAtomic": str(amount), "priceUsd": _usd(amount), "payTo": quote["payTo"], "network": "base",
            "resourceUrl": resource_url, "expiresAt": int(quote["expiresAt"]),
            "text": f"{tool.get('name')}: {_usd(amount)} USDC on Base, paid from your allowance. Confirm to buy.",
        }

    if action == "tool-buy":
        return _buy_tool(server, gw, account, str(payload.get("quoteId") or ""))

    if action in ("bitrefill-search", "bitrefill-quote", "bitrefill-buy"):
        if action == "bitrefill-buy":
            _limits_from_limiter(server, gw, account)
        result = gw._allowance_bitrefill_action(server, account, action, payload, web_confirmed=True)
        if result.get("ok", True) is False:
            return 400, result
        return 200, {"ok": True, **result}

    if action == "purchases":
        summaries = server.user_event_store.summaries(account)
        offset = max(0, int(payload.get("offset") or 0))
        return 200, {"ok": True, "purchases": summaries[offset:offset + 20], "hasNext": len(summaries) > offset + 20}

    if action == "purchase-reveal":
        record_id = str(payload.get("purchaseId") or "")
        if not any(item["id"] == record_id for item in server.user_event_store.summaries(account)):
            return 404, {"ok": False, "text": "Purchase not found."}
        server.user_event_store.preflight_write()
        event = server.user_event_store.read(account, record_id)
        result = gw._last_bitrefill_purchase_response(server, event, account)
        return 200, result or {"ok": True, "text": "This purchase has no gift card code."}

    return 404, {"ok": False, "error": "not_found"}


def _buy_tool(server: Any, gw: Any, account: str, quote_id: str) -> tuple[int, dict[str, Any]]:
    service = server.allowance
    quote = server.web_tool_quotes.take(account, quote_id)
    tool = dict(gw.PAID_TOOLS[quote["toolId"]])
    resource_url = quote["resourceUrl"]
    if service.lane_for(account) is None:
        raise AllowanceUnavailable("Set up your allowance first.")
    gw._enforce_user_purchase_rate(account)
    _limits_from_limiter(server, gw, account)
    server.user_event_store.preflight_write()
    request_body = tool.get("requestBody") if isinstance(tool.get("requestBody"), dict) else None
    requirements = gw.normalize_x402_payment_required(
        gw.fetch_x402_payment_required(resource_url, request_body=request_body), resource_url=resource_url)
    gw._validate_base_usdc_x402_requirement(requirements)
    if (gw._payment_amount_atomic(requirements) > quote["amount"]
            or _receiver(requirements).lower() != quote["payTo"].lower()):
        raise AllowanceError("The price or the recipient changed since your quote. Nothing was paid; get a new quote.")

    reservation_id, claim_id, settled = None, None, False
    try:
        reservation_id, decision, claim_id = gw._reserve_user_wallet_spend(
            server, account, requirements, resource_url=resource_url, claim_scope=quote_id)
        payment = gw._payment_from_requirements(requirements, owner=account, resource_url=resource_url)
        if decision is None or decision.needs_human:
            approval = web_confirmation(quote_id)
        else:
            approval = {"ok": True, "status": "approved", "source": "spending_memory",
                        "approvalId": f"sm-{decision.journal_id}", "reason": decision.reason, "rule": decision.rule}
        payment_context = gw._tool_payment_context(tool, quote["payload"])
        paid = service.pay_x402(
            account, resource_url, requirements, server.user_x402_buyer.base_payment_client,
            method="POST" if request_body is not None else "GET", request_body=request_body)
        result = gw._allowance_x402_event(resource_url, approval, requirements, payment_context, paid)
        enriched = gw._tool_result(tool, result, resource_url)
        enriched["decision"] = result.get("decision", "approved_and_executed")
        enriched["ok"] = bool(result.get("ok", False))
        enriched["account"] = account
        if enriched["ok"]:
            gw._settle_user_wallet_spend(server, reservation_id, tool, resource_url, requirements, enriched,
                                         payment=payment, claim_id=claim_id)
            settled = True
            server.user_event_store.write(account, enriched)
        return (200 if enriched["ok"] else 400), enriched
    finally:
        if not settled:
            gw._release_user_wallet_spend(server, reservation_id)
            if claim_id and server.spending_policy is not None:
                server.spending_policy.memory.release_claim(claim_id)
