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
from .web_accounts import ACCOUNT_PREFIX, SOLANA_PREFIX

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


# What a Solana account may do before the Solana allowance exists: look, never pay.
SOLANA_READ_ONLY = {"tools", "catalog-search", "purchases", "purchase-reveal", "venice-models", "venice-model",
                    "venice-usage", "bitrefill-packages", "buyer-email", "buyer-email-set",
                    # these pay, and only through the account's own Solana allowance:
                    "venice-chat", "venice-solana-topup", "bitrefill-solana-buy"}


def _account(server: Any, payload: dict[str, Any], action: str = "") -> str:
    account = str(payload.get("account") or "")
    service = getattr(server, "allowance", None)
    if service is None:
        raise AllowanceUnavailable("The allowance lane is off on this server.")
    if account.startswith(SOLANA_PREFIX) and action in SOLANA_READ_ONLY:
        accounts = getattr(server, "web_accounts", None)
        if accounts is not None and accounts.account(account) is not None:
            return account
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
    return f"{Decimal(atomic) / Decimal(1_000_000):.6f}".rstrip("0").rstrip(".")  # .6f: never strip a whole number


def handle(server: Any, action: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from . import server as gw  # the gateway's purchase helpers; imported here to avoid a cycle

    account = _account(server, payload, action)
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

    if action in ("venice-chat", "venice-models", "venice-model", "venice-usage", "venice-solana-topup"):
        from . import web_venice  # noqa: PLC0415 - Venice only loads when the chat is used
        solana = account.startswith(SOLANA_PREFIX)
        if action == "venice-solana-topup":
            return web_venice.topup_solana(server, account, payload.get("quoteId"), payload.get("approvalHash"))
        if action == "venice-usage":
            return web_venice.usage_solana(server, account) if solana else web_venice.usage(server, account)
        if action == "venice-chat" and solana:
            return web_venice.chat_solana(server, account, payload.get("messages"))
        if action == "venice-models":
            return web_venice.models(server, account)
        if action == "venice-model":
            return web_venice.choose_model(server, account, payload.get("model"))
        return web_venice.chat(server, gw, account, payload.get("messages"))

    if account.startswith(SOLANA_PREFIX) and action in ("bitrefill-packages", "bitrefill-solana-buy", "buyer-email",
                                                        "buyer-email-set"):
        from . import solana_bitrefill  # noqa: PLC0415
        if action == "bitrefill-packages":
            return 200, {"ok": True, **solana_bitrefill.packages(server, payload.get("productId"))}
        if action == "buyer-email":
            return 200, {"ok": True, "hasEmail": bool(solana_bitrefill.email(server, account))}
        if action == "buyer-email-set":
            solana_bitrefill.set_email(server, account, payload.get("email"))
            return 200, {"ok": True, "hasEmail": True}
        try:
            return 200, solana_bitrefill.buy(server, account, payload.get("productId"), payload.get("package"))
        except solana_bitrefill.NeedsEmail as exc:
            return 409, {"ok": False, "error": "email_needed", "text": str(exc)}

    if action in ("bitrefill-search", "bitrefill-packages", "bitrefill-quote", "bitrefill-buy"):
        if action == "bitrefill-buy":
            _limits_from_limiter(server, gw, account)
        result = gw._allowance_bitrefill_action(server, account, action, payload, web_confirmed=True)
        if result.get("ok", True) is False:
            return 400, result
        return 200, {"ok": True, **result}

    if action == "catalog-search":
        return catalog_search(server, payload)

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
    enriched = pay_from_allowance(server, gw, account, tool, resource_url, requirements, request_body=request_body,
                                  payment_context=gw._tool_payment_context(tool, quote["payload"]),
                                  approval=web_confirmation(quote_id), claim_scope=quote_id)
    return (200 if enriched["ok"] else 400), enriched


def pay_from_allowance(server: Any, gw: Any, account: str, tool: dict[str, Any], resource_url: str,
                       requirements: dict[str, Any], *, request_body: dict[str, Any] | None,
                       payment_context: dict[str, str] | None, approval: dict[str, Any],
                       claim_scope: str) -> dict[str, Any]:
    """Pay one x402 resource from the account's limiter, held to the same caps, memory and history as the bot.

    `approval` is what stands for the owner's yes when spending memory asks for one: a
    clicked quote, or the standing consent of the limits they set for their agent.
    """
    service = server.allowance
    reservation_id, claim_id, settled = None, None, False
    try:
        reservation_id, decision, claim_id = gw._reserve_user_wallet_spend(
            server, account, requirements, resource_url=resource_url, claim_scope=claim_scope)
        payment = gw._payment_from_requirements(requirements, owner=account, resource_url=resource_url)
        if decision is not None and not decision.needs_human:
            approval = {"ok": True, "status": "approved", "source": "spending_memory",
                        "approvalId": f"sm-{decision.journal_id}", "reason": decision.reason, "rule": decision.rule}
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
        return enriched
    finally:
        if not settled:
            gw._release_user_wallet_spend(server, reservation_id)
            if claim_id and server.spending_policy is not None:
                server.spending_policy.memory.release_claim(claim_id)


CATALOG_TYPES = {"gift_card", "esim", "phone_refill"}


def catalog_search(server: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Candidates from Bitrefill's catalog (the bot's MCP client): by words, or a country's shops of a kind.

    Read-only; nothing here signs in as the account or quotes a price. The page's agent
    ranks these and asks the allowance lane for packages and prices of the few it shows.
    """
    from .bitrefill_runner import BITREFILL_BROWSE_CATEGORIES  # noqa: PLC0415

    client = getattr(getattr(server, "bitrefill_search_service", None), "bitrefill_client", None)
    if client is None or not hasattr(client, "search_products") or type(client).__name__.startswith("Test"):
        return 200, {"ok": False, "error": "catalog_off", "products": []}
    query = " ".join(str(payload.get("query") or "").split())[:60]
    country = str(payload.get("country") or "").upper()
    country = country if len(country) == 2 and country.isalpha() else ""
    product_type = str(payload.get("productType") or "")
    product_type = product_type if product_type in CATALOG_TYPES else ""
    category = BITREFILL_BROWSE_CATEGORIES.get(str(payload.get("category") or "all"), "")
    if query:
        products = client.search_products(query=query, country=country, category=category,
                                          product_type=product_type, include_test_products=False)
        if not products and category:  # the words may name a shop filed under another kind
            products = client.search_products(query=query, country=country, category="",
                                              product_type=product_type, include_test_products=False)
    elif country:
        products = client.list_products(country=country, category=category, start=0, limit=100,
                                        include_test_products=False)
        products = [p for p in products if not product_type or p.get("productType") == product_type]
    else:
        products = []
    return 200, {"ok": True, "products": [
        {"slug": p.get("productId"), "name": p.get("name"), "country": p.get("country"), "type": p.get("productType"),
         "categories": list(p.get("categories") or ([p["category"]] if p.get("category") else [])),
         "needsRecipient": str(p.get("recipientType") or "none") != "none"}
        for p in products if p.get("productId") and p.get("inStock", True) is not False][:80]}
