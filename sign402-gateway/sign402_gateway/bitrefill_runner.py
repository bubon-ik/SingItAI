import hashlib
import hmac
import secrets
from copy import deepcopy
from typing import Any, Callable

from .bitrefill import BitrefillClient
from .bitrefill_quote import (
    build_purchase_commitment,
    build_quote,
    build_real_rate_quote,
    hash_purchase_commitment,
    new_quote_id,
    now_epoch,
)
from .commerce_store import BitrefillCommerceStore


def lookup_bitrefill_order(
    store: BitrefillCommerceStore,
    quote_id: str,
    *,
    include_redemption: bool = False,
    recipient: dict[str, Any] | None = None,
    bitrefill_client: BitrefillClient | None = None,
) -> dict[str, Any]:
    record = store.get_quote(quote_id)
    quote = record["quote"]
    metadata = record["metadata"]
    provider_result = metadata.get("bitrefill")
    if not isinstance(provider_result, dict):
        provider_result = {}
    if record["state"] == "BITREFILL_PURCHASED" and bitrefill_client is not None and provider_result:
        refreshed_provider_result = bitrefill_client.refresh_purchase(provider_result, quote)
        store.advance_state(quote_id, "BITREFILL_PURCHASED", {"bitrefill": refreshed_provider_result})
        if _provider_is_delivered(refreshed_provider_result, quote):
            store.advance_state(quote_id, "DELIVERED")
        record = store.get_quote(quote_id)
        metadata = record["metadata"]
        provider_result = metadata.get("bitrefill")
        if not isinstance(provider_result, dict):
            provider_result = {}
    result = {
        "ok": True,
        "quoteId": record["quoteId"],
        "state": record["state"],
        "productId": quote.get("productId"),
        "productName": quote.get("productName"),
        "packageValue": quote.get("packageValue"),
        "orderId": provider_result.get("orderId"),
        "status": provider_result.get("status", record["state"].lower()),
    }
    if include_redemption:
        stored_recipient = metadata.get("recipient") if isinstance(metadata, dict) else {}
        if isinstance(stored_recipient, dict) and stored_recipient:
            if recipient != stored_recipient:
                raise ValueError("recipient does not match order")
        redemption = provider_result.get("redemption")
        if redemption is not None:
            result["redemption"] = deepcopy(redemption)
            result["telegramText"] = _bitrefill_delivery_telegram_text(quote)
    return result


class BitrefillSearchService:
    def __init__(self, *, bitrefill_client: BitrefillClient):
        self.bitrefill_client = bitrefill_client

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        products = self.bitrefill_client.search_products(
            query=str(payload.get("query", "")),
            country=str(payload.get("country", "")),
            category=str(payload.get("category", "")),
            product_type=str(payload.get("productType", "")),
            include_test_products=bool(payload.get("includeTestProducts", False)),
        )
        return {"ok": True, "products": products}


class BitrefillProductDetailsService:
    def __init__(self, *, bitrefill_client: BitrefillClient):
        self.bitrefill_client = bitrefill_client

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        product_id = str(payload.get("productId", "")).strip()
        if not product_id:
            raise ValueError("productId is required")
        details = self.bitrefill_client.get_product_details(
            product_id=product_id,
            country=str(payload.get("country", "")),
        )
        return {"ok": True, **details}


class BitrefillQuoteService:
    def __init__(
        self,
        *,
        bitrefill_client: BitrefillClient,
        store: BitrefillCommerceStore,
        singit_usd_price_provider: Callable[[], str],
        real_rate_pricer: Any | None = None,
        quote_id_provider: Callable[[], str] = new_quote_id,
        now_provider: Callable[[], int] = now_epoch,
    ):
        self.bitrefill_client = bitrefill_client
        self.store = store
        self.singit_usd_price_provider = singit_usd_price_provider
        self.real_rate_pricer = real_rate_pricer
        self.quote_id_provider = quote_id_provider
        self.now_provider = now_provider

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.quote(payload)

    def quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "query" in payload or "value" in payload:
            raise ValueError(
                "legacy Bitrefill quote request is unsupported; use productId and packageId"
            )
        product_id = str(payload.get("productId", "")).strip()
        package_id = str(payload.get("packageId", "")).strip()
        if not product_id:
            raise ValueError("productId is required")
        if not package_id:
            raise ValueError("packageId is required")
        recipient = payload.get("recipient")
        if not isinstance(recipient, dict):
            recipient = {}
        product = self.bitrefill_client.quote_product(
            product_id=product_id,
            package_id=package_id,
            country=str(payload.get("country", "US")),
            recipient=recipient,
        )
        if self.real_rate_pricer is not None:
            pricing = self.real_rate_pricer.price_for_usdc(product["priceUsd"])
            quote = build_real_rate_quote(
                request=payload,
                product=product,
                pricing=pricing,
                quote_id=self.quote_id_provider(),
                now_epoch=self.now_provider(),
            )
        else:
            quote = build_quote(
                request=payload,
                product=product,
                singit_usd_price=self.singit_usd_price_provider(),
                quote_id=self.quote_id_provider(),
                now_epoch=self.now_provider(),
            )
        self.store.save_quote(quote)
        return quote


class BitrefillPurchaseRunner:
    def __init__(
        self,
        *,
        store: BitrefillCommerceStore,
        firefly: Any,
        bankr_payment_client: Callable[..., dict[str, Any]],
        bankr_resource_url: str,
        now_provider: Callable[[], int] = now_epoch,
        fulfillment_token_provider: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        settlement_verifier: Callable[..., dict[str, Any]] | None = None,
        fulfillment_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.store = store
        self.firefly = firefly
        self.bankr_payment_client = bankr_payment_client
        self.bankr_resource_url = bankr_resource_url
        self.now_provider = now_provider
        self.fulfillment_token_provider = fulfillment_token_provider
        self.settlement_verifier = settlement_verifier
        self.fulfillment_runner = fulfillment_runner

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.buy(payload)

    def payment_hash_for_quote(self, quote: dict[str, Any], *, recipient: dict[str, Any]) -> str:
        return hash_purchase_commitment(build_purchase_commitment(quote, recipient=recipient))

    def buy(self, payload: dict[str, Any]) -> dict[str, Any]:
        quote_id = str(payload.get("quoteId", "")).strip()
        if not quote_id:
            raise ValueError("quoteId is required")
        recipient = payload.get("recipient") if isinstance(payload.get("recipient"), dict) else {}
        record = self.store.get_quote(quote_id)
        quote = record["quote"]
        if record["state"] != "QUOTED":
            raise ValueError(f"quote is not purchasable (state: {record['state']})")
        if self.now_provider() >= int(quote["expiresAtEpoch"]):
            self.store.advance_state(quote_id, "QUOTE_EXPIRED")
            raise ValueError("quote expired")
        commitment = build_purchase_commitment(quote, recipient=recipient)
        payment_hash = hash_purchase_commitment(commitment)
        approval = self.firefly.approve_payment_hash(
            payment_hash,
            context_lines=[
                "BUY BITREFILL",
                str(quote.get("productName", quote.get("productId", "")))[:20],
                f"MAX {quote['singitAmount']} SINGIT"[:20],
            ],
        )
        if not approval.get("approved"):
            self.store.advance_state(
                quote_id,
                "FIREFLY_REJECTED",
                {"paymentHash": payment_hash, "firefly": approval},
            )
            return {
                "ok": False,
                "decision": "rejected_by_firefly",
                "quoteId": quote_id,
                "paymentApprovalHash": payment_hash,
                "firefly": approval,
            }
        if str(approval.get("approvedHash", "")).lower() != payment_hash:
            raise ValueError("Firefly approved hash does not match Bitrefill purchase hash")
        self.store.advance_state(
            quote_id,
            "FIREFLY_APPROVED",
            {"paymentHash": payment_hash, "firefly": approval},
        )
        fulfillment_token = self.fulfillment_token_provider()
        self.store.advance_state(
            quote_id,
            "FIREFLY_APPROVED",
            {
                "fulfillmentTokenHash": hashlib.sha256(
                    fulfillment_token.encode("utf-8")
                ).hexdigest(),
                "recipient": recipient,
            },
        )
        bankr_body = {"quoteId": quote_id, "fulfillmentToken": fulfillment_token}
        try:
            bankr_result = self.bankr_payment_client(
                self.bankr_resource_url,
                request_body=bankr_body,
            )
        except Exception as exc:
            self.store.advance_state(
                quote_id,
                "RECONCILIATION_REQUIRED",
                {"bankrError": str(exc)},
            )
            raise
        if self.settlement_verifier is not None or self.fulfillment_runner is not None:
            if self.settlement_verifier is None or self.fulfillment_runner is None:
                raise ValueError("settlement_verifier and fulfillment_runner must be configured together")
            try:
                settlement_proof = self.settlement_verifier(
                    bankr_result=bankr_result,
                    quote=quote,
                )
                self.store.advance_state(
                    quote_id,
                    "SINGIT_SETTLED",
                    {"bankr": bankr_result, "singitSettlement": settlement_proof},
                )
                bitrefill_result = self.fulfillment_runner(
                    {"quoteId": quote_id, "fulfillmentToken": fulfillment_token}
                )
            except Exception as exc:
                self.store.advance_state(
                    quote_id,
                    "RECONCILIATION_REQUIRED",
                    {"bankr": bankr_result, "singitSettlementError": str(exc)},
                )
                raise
            return {
                "ok": bool(bankr_result.get("ok", False)) and bool(bitrefill_result.get("ok", False)),
                "decision": "approved_and_executed",
                "quoteId": quote_id,
                "paymentApprovalHash": payment_hash,
                "paymentCommitment": commitment,
                "bankr": bankr_result,
                "singitSettlement": settlement_proof,
                "bitrefill": bitrefill_result,
                "telegramText": _bitrefill_purchase_telegram_text(quote),
            }
        refreshed = self.store.get_quote(quote_id)
        if refreshed["state"] == "DELIVERED":
            self.store.advance_state(quote_id, "DELIVERED", {"bankr": bankr_result})
        else:
            self.store.advance_state(quote_id, "SINGIT_SETTLED", {"bankr": bankr_result})
        return {
            "ok": bool(bankr_result.get("ok", False)),
            "decision": "approved_and_executed",
            "quoteId": quote_id,
            "paymentApprovalHash": payment_hash,
            "paymentCommitment": commitment,
            "bankr": bankr_result,
            "telegramText": _bitrefill_purchase_telegram_text(quote),
        }


class BitrefillSettlementPreparationRunner:
    def __init__(
        self,
        *,
        store: BitrefillCommerceStore,
        now_provider: Callable[[], int] = now_epoch,
    ):
        self.store = store
        self.now_provider = now_provider

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.prepare(payload)

    def prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        quote_id = str(payload.get("quoteId", "")).strip()
        if not quote_id:
            raise ValueError("quoteId is required")

        record = self.store.get_quote(quote_id)
        quote = record["quote"]
        if self.now_provider() >= int(quote["expiresAtEpoch"]):
            self.store.advance_state(quote_id, "QUOTE_EXPIRED")
            raise ValueError("quote expired")
        if record["state"] != "FIREFLY_APPROVED":
            raise ValueError(f"quote is not ready for SINGIT settlement (state: {record['state']})")

        metadata = record["metadata"]
        fulfillment_token = str(payload.get("fulfillmentToken", ""))
        expected_token_hash = str(metadata.get("fulfillmentTokenHash", ""))
        supplied_token_hash = hashlib.sha256(fulfillment_token.encode("utf-8")).hexdigest()
        if not fulfillment_token or not expected_token_hash or not hmac.compare_digest(
            supplied_token_hash,
            expected_token_hash,
        ):
            raise ValueError("invalid fulfillment token")

        return {
            "ok": True,
            "quoteId": quote_id,
            "status": "ready_for_singit_settlement",
            "settleAmountAtomic": quote["maxSingitAtomic"],
            "maxSingitAtomic": quote["maxSingitAtomic"],
        }


class BitrefillFulfillmentRunner:
    def __init__(
        self,
        *,
        store: BitrefillCommerceStore,
        bitrefill_client: BitrefillClient,
        funding_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        now_provider: Callable[[], int] = now_epoch,
    ):
        self.store = store
        self.bitrefill_client = bitrefill_client
        self.funding_runner = funding_runner
        self.now_provider = now_provider

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.fulfill(payload)

    def fulfill(self, payload: dict[str, Any]) -> dict[str, Any]:
        quote_id = str(payload.get("quoteId", "")).strip()
        if not quote_id:
            raise ValueError("quoteId is required")

        record = self.store.get_quote(quote_id)
        if self.now_provider() >= int(record["quote"]["expiresAtEpoch"]):
            self.store.advance_state(quote_id, "QUOTE_EXPIRED")
            raise ValueError("quote expired")
        metadata = record["metadata"]
        fulfillment_token = str(payload.get("fulfillmentToken", ""))
        expected_token_hash = str(metadata.get("fulfillmentTokenHash", ""))
        supplied_token_hash = hashlib.sha256(fulfillment_token.encode("utf-8")).hexdigest()
        if not fulfillment_token or not expected_token_hash or not hmac.compare_digest(
            supplied_token_hash,
            expected_token_hash,
        ):
            raise ValueError("invalid fulfillment token")
        if not self.store.try_mark_fulfilling(quote_id):
            raise ValueError("quote is already fulfilled or being fulfilled")

        if self.funding_runner is not None:
            try:
                funding_result = self.funding_runner(record["quote"])
                self.store.advance_state(
                    quote_id,
                    "FULFILLING",
                    {"bankrSwap": funding_result},
                )
            except Exception as exc:
                self.store.advance_state(
                    quote_id,
                    "RECONCILIATION_REQUIRED",
                    {"fundingError": str(exc)},
                )
                raise

        try:
            result = self.bitrefill_client.buy_product(
                quote=record["quote"],
                recipient=(
                    metadata.get("recipient")
                    if isinstance(metadata.get("recipient"), dict)
                    else {}
                ),
                checkpoint_callback=lambda checkpoint: self.store.checkpoint(
                    quote_id,
                    {"bitrefillCheckpoint": checkpoint},
                ),
            )
        except Exception as exc:
            self.store.advance_state(
                quote_id,
                "FULFILLMENT_FAILED",
                {"fulfillmentError": str(exc)},
            )
            raise
        self.store.advance_state(quote_id, "BITREFILL_PURCHASED", {"bitrefill": result})
        if _provider_is_delivered(result, record["quote"]):
            self.store.advance_state(quote_id, "DELIVERED", {})
        return self._redacted_result(record["quote"], result)

    def _redacted_result(self, quote: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "quoteId": quote["quoteId"],
            "orderId": result["orderId"],
            "status": result.get("status", "delivered"),
            "settleAmountAtomic": quote["maxSingitAtomic"],
            "maxSingitAtomic": quote["maxSingitAtomic"],
        }


def _provider_is_delivered(result: dict[str, Any], quote: dict[str, Any]) -> bool:
    if str(result.get("status", "")).strip().lower() != "delivered":
        return False
    if quote.get("productType") in {"gift_card", "esim"}:
        redemption = result.get("redemption")
        return isinstance(redemption, dict) and redemption.get("value") is not None
    return True


def _bitrefill_purchase_telegram_text(quote: dict[str, Any]) -> str:
    product_name = str(quote.get("productName") or quote.get("productId") or "Your item")
    package_value = str(quote.get("packageValue") or "").strip()
    value_text = f" ${package_value}" if package_value else ""
    return (
        f"✅ {product_name}{value_text} is ready. "
        "The purchase was paid with SINGIT. "
        "Use get-bitrefill-order to reveal the code in this chat."
    )


def _bitrefill_delivery_telegram_text(quote: dict[str, Any]) -> str:
    product_name = str(quote.get("productName") or quote.get("productId") or "Your item")
    package_value = str(quote.get("packageValue") or "").strip()
    value_text = f" ${package_value}" if package_value else ""
    return f"✅ {product_name}{value_text} is ready. Your code is ready."
