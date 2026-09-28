"""Bitrefill for a web account signed in with a Solana wallet.

The path Bitrefill documents, and the one this project verified with a live purchase on
September 18, 2026 (docs/bitrefill-solana-checks.md): an MCP guest invoice paid in USDC
on Solana through Bitrefill's x402 route.

  1. `buy-products` with `payment_method: usdc_solana` and the buyer's email (Bitrefill
     requires it on guest invoices; it is where codes also land) returns the invoice, its
     access token and `x402_payment_url`;
  2. the agent pays the x402 route straight from the owner's account as their approved
     delegate, within their limits (solana_allowance.py, solana-x402-service/src/invoice.mjs);
     Bitrefill's fee payer pays the network fee;
  3. `get-invoice-by-id` with the access token reports delivery; the code is shown once,
     on request, from the purchase record's encrypted access token.

The user chose that, inside their limits, the agent buys without asking again; what they
buy is still exactly the product and value they picked, at a price checked against the quote.
"""

from __future__ import annotations

import math
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from .agent_allowance import AllowanceError, AllowanceUnavailable
from .allowance_bitrefill import usage_instructions

PAY_URL = "https://api.bitrefill.com/x402/invoice/pay"
PRICE_SLACK = Decimal("1.02")        # the invoice may round up a little from the quote, never more
DELIVERY_WAIT_SECONDS = 90
DONE = {"complete", "completed", "delivered", "all_delivered"}


class NeedsEmail(AllowanceError):
    """Bitrefill needs the buyer's email once before a Solana purchase."""


def _client(server: Any) -> Any:
    client = getattr(getattr(server, "bitrefill_search_service", None), "bitrefill_client", None)
    if client is None or getattr(client, "checkout_mode", "") != "guest" or not hasattr(client, "quote_product"):
        raise AllowanceUnavailable("Bitrefill on Solana is not set up on this server.")
    return client


def _lane(server: Any) -> Any:
    lane = getattr(server, "solana_allowance", None)
    if lane is None:
        raise AllowanceUnavailable("Solana payments are not set up on this server.")
    return lane


def _atomic(usdc: Any) -> int:
    try:
        return math.ceil(Decimal(str(usdc)) * 1_000_000)
    except (InvalidOperation, ValueError):
        raise AllowanceError("Bitrefill quoted an unreadable price. Nothing was paid.") from None


def email(server: Any, account: str) -> str | None:
    store = getattr(server, "buyer_email_store", None)
    return store.get_email(account) if store is not None else None


def set_email(server: Any, account: str, address: Any) -> str:
    store = getattr(server, "buyer_email_store", None)
    if store is None:
        raise AllowanceUnavailable("Emails cannot be saved on this server.")
    try:
        return store.set_email(account, str(address or ""))
    except ValueError as exc:
        raise AllowanceError(f"That email does not look right: {exc}") from None


def packages(server: Any, slug: Any) -> dict[str, Any]:
    """What can be bought of one product and today's price, from Bitrefill's catalog."""
    product = _client(server).get_product_details(product_id=str(slug or ""), country="")
    return {"slug": product["productId"], "name": product["name"],
            "packages": [{"value": p["value"], "currency": product.get("currency") or "", "priceUsd": str(p["priceUsd"])}
                         for p in product.get("packages") or []][:40],
            "recipientRequired": bool(product.get("requiredRecipientFields"))}


def buy(server: Any, account: str, slug: Any, package: Any, *, sleep: Any = time.sleep,
        now: Any = time.time) -> dict[str, Any]:
    client, lane = _client(server), _lane(server)
    status = lane.status(account)
    if status.get("state") != "granted":
        raise AllowanceUnavailable("Set your limits and approve them from your wallet first.")
    buyer = email(server, account)
    if not buyer:
        raise NeedsEmail("Bitrefill needs an email for your purchases once (it also sends your codes there).")
    quote = client.quote_product(product_id=str(slug or ""), package_id=str(package or ""), country="", recipient={})
    if quote.get("requiredRecipientFields"):
        raise AllowanceError("This product is delivered to a phone or account; only products delivered as a code can be bought here.")
    price = _atomic(quote["priceUsd"])
    if price > int(status["perPurchaseCapAtomic"]) or price > int(status["remainingTodayAtomic"]):
        raise AllowanceError(f"{quote['priceUsd']} USDC does not fit your limits today. Nothing was bought.")

    invoice = client._normalize_invoice(client._call_tool("buy-products", {
        "cart_items": [{"product_id": quote["productId"], "package_value": quote["packageValue"]}],
        "payment_method": "usdc_solana", "email": buyer, "return_payment_link": False}))
    invoice_id, token = client._invoice_id(invoice), client._invoice_access_token(invoice)
    if not invoice_id:
        raise AllowanceError("Bitrefill did not create the order. Nothing was paid.")
    if str(invoice.get("x402_payment_url") or PAY_URL) != PAY_URL:
        raise AllowanceError("Bitrefill offered an unexpected payment route. Nothing was paid.")
    info = invoice.get("payment_info") or invoice.get("paymentInfo") or {}
    amount = _atomic(info.get("amount")) if isinstance(info, dict) and info.get("amount") else price
    if amount > math.ceil(price * PRICE_SLACK):
        raise AllowanceError("Bitrefill's invoice is above the price you were shown. Nothing was paid.")

    owner = lane.owner(account)
    paid = lane.spend(account, amount, f"Bitrefill {quote['name']} {quote['packageValue']}", lambda: lane._call(
        account, "bitrefill-invoice-pay", url=PAY_URL, invoiceId=invoice_id, maxAmount=str(amount), owner=owner))

    delivered, order = False, {}
    deadline = now() + DELIVERY_WAIT_SECONDS
    while now() < deadline:
        current = client.invoice_status(invoice_id=invoice_id, invoice_access_token=token)
        if client._invoice_status(current) in {"blocked", "denied", "payment_error"}:
            break
        orders = client._orders(current)
        if client._invoice_status(current) in DONE or (orders and str(orders[0].get("status")).lower() == "delivered"):
            delivered, order = True, orders[0] if orders else {}
            break
        sleep(6)
    how_to_use = usage_instructions(order.get("redemption_info") or order.get("redemptionInfo")) if delivered else ""

    name = quote["name"]
    denomination = f"{quote['packageValue']} {quote.get('currency') or ''}".strip()
    event = {
        "ok": True, "mode": "bitrefill_mcp_solana", "quoteId": invoice_id, "invoiceId": invoice_id,
        "bitrefill": {"status": "delivered" if delivered else "paid", "productName": name},
        "productName": name, "productSlug": quote["productId"], "package": quote["packageValue"],
        "network": "solana", "txId": paid.get("transaction"), "amountAtomic": str(amount),
        "fulfillmentToken": token,  # encrypted by the purchase store; reveals the code once
        "receipt": {"name": name, "denomination": denomination, "paid": f"{Decimal(amount) / 1_000_000:f} USDC",
                    "network": "Solana", "txId": paid.get("transaction"), "status": "Completed" if delivered else "Paid"},
    }
    if token:
        server.user_event_store.write(account, event)
    else:
        server.user_event_store.write(account, {k: v for k, v in event.items() if k != "fulfillmentToken"})
    from .purchase_history import purchase_id
    return {"ok": True, "invoiceId": invoice_id, "purchaseId": purchase_id(event), "delivered": delivered, "name": name,
            "package": quote["packageValue"],
            "packageCurrency": quote.get("currency") or "", "priceUsd": f"{Decimal(amount) / 1_000_000:f}".rstrip("0").rstrip("."),
            "txId": paid.get("transaction"), **({"howToUse": how_to_use} if how_to_use else {})}


def reveal(server: Any, event: dict[str, Any], account: str) -> dict[str, Any]:
    """The code of a Solana Bitrefill order, once: its access token is dropped after it is shown."""
    name = str(event.get("productName") or "Your Bitrefill order")
    token = str(event.get("fulfillmentToken") or "").strip()
    if not token:
        return {"ok": True, "telegramText": f"{name} was already shown once — check where you saved the code."}
    client = _client(server)
    invoice = client.invoice_status(invoice_id=str(event.get("invoiceId") or ""), invoice_access_token=token)
    orders = client._orders(invoice)
    codes = (orders[0].get("redemption_info") or orders[0].get("redemptionInfo")) if orders else None
    if not codes:
        return {"ok": True, "telegramText": f"{name} is still being delivered. Try again in a minute."}
    lines = [f"{name} — your code. Store it safely, do not share it, redeem it soon:"]
    for item in codes if isinstance(codes, list) else [codes]:
        if isinstance(item, dict):
            lines.extend(f"{key}: {value}" for key, value in item.items() if value not in (None, "", {}))
        else:
            lines.append(str(item))
    from .allowance_bitrefill import redemption_fields
    from .purchase_history import purchase_id
    server.user_event_store.clear_fulfillment_token(account, purchase_id(event))
    first = next((i for i in (codes if isinstance(codes, list) else [codes]) if isinstance(i, dict)), {})
    return {"ok": True, "telegramText": "\n".join(lines), "fields": redemption_fields(codes),
            "howToUse": " ".join(str(first[k]) for k in ("instructions", "redemption_instructions", "how_to_redeem")
                                 if isinstance(first.get(k), str))}
