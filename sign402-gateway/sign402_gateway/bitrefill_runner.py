import hashlib
import hmac
import re
import secrets
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
from typing import Any, Callable

from .bankr_swap import BASE_USDC_MAINNET
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
from .numeric import format_decimal


BITREFILL_BROWSE_CATEGORIES = {
    "all": "",
    "shopping": "retail,ecommerce,gifts,electronics,apparel",
    "food": "food,restaurants,food-delivery,groceries",
    "games": "games",
    "mobile": "refill,phone,data,bundles",
    "travel": "travel,flights,experiences",
    "entertainment": "entertainment,streaming,music",
}

COINBASE_NATIVE_TOKEN_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


class WalletPaymentTokenResolver:
    def __init__(self, token_provider: Callable[[str], dict[str, Any]]):
        self.token_provider = token_provider

    def resolve(self, user_id: str, raw_token: Any) -> dict[str, Any]:
        if not isinstance(raw_token, dict):
            raise ValueError("paymentToken is required")
        requested_address = str(
            raw_token.get("address") or raw_token.get("contractAddress") or ""
        ).strip()
        if not requested_address:
            raise ValueError("paymentToken address is required")

        inventory = self.token_provider(str(user_id))
        tokens = inventory.get("tokens", []) if isinstance(inventory, dict) else []
        for candidate in tokens if isinstance(tokens, list) else []:
            if not isinstance(candidate, dict):
                continue
            candidate_address = str(
                candidate.get("contractAddress") or candidate.get("address") or ""
            ).strip()
            if candidate_address.casefold() != requested_address.casefold():
                continue
            return {
                "address": candidate_address,
                "symbol": str(candidate.get("symbol", "")).strip(),
                "decimals": int(candidate["decimals"]),
                "balance": str(candidate["balance"]),
                "verified": bool(candidate.get("verified", False)),
                "native": bool(candidate.get("native", False)),
            }
        raise ValueError("selected payment token is not available in this wallet")


def _fulfillment_token_matches(metadata: dict[str, Any], fulfillment_token: str | None) -> bool:
    expected_hash = str(metadata.get("fulfillmentTokenHash", "")) if isinstance(metadata, dict) else ""
    token = str(fulfillment_token or "")
    if not expected_hash or not token:
        return False
    supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(supplied_hash, expected_hash)


def lookup_bitrefill_order(
    store: BitrefillCommerceStore,
    quote_id: str,
    *,
    include_redemption: bool = False,
    recipient: dict[str, Any] | None = None,
    fulfillment_token: str | None = None,
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
        if not isinstance(stored_recipient, dict):
            stored_recipient = {}
        recipient_ok = bool(stored_recipient) and recipient == stored_recipient
        token_ok = _fulfillment_token_matches(metadata, fulfillment_token)
        if not (recipient_ok or token_ok):
            if stored_recipient:
                raise ValueError("recipient does not match order")
            raise ValueError("valid fulfillmentToken is required to reveal redemption")
        redemption = provider_result.get("redemption")
        if redemption is not None:
            result["redemption"] = deepcopy(redemption)
            result["telegramText"] = _bitrefill_delivery_telegram_text(
                quote,
                redemption=redemption,
            )
    return result


class BitrefillSearchService:
    def __init__(self, *, bitrefill_client: BitrefillClient):
        self.bitrefill_client = bitrefill_client

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        search_all_countries = payload.get("searchAllCountries", False)
        if not isinstance(search_all_countries, bool):
            raise ValueError("searchAllCountries must be a boolean")
        products = self.bitrefill_client.search_products(
            query=str(payload.get("query", "")),
            country="" if search_all_countries else str(payload.get("country", "")),
            category=str(payload.get("category", "")),
            product_type=str(payload.get("productType", "")),
            include_test_products=bool(payload.get("includeTestProducts", False)),
        )
        return {"ok": True, "products": products}


class BitrefillCatalogService:
    def __init__(self, *, bitrefill_client: BitrefillClient):
        self.bitrefill_client = bitrefill_client

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        country = str(payload.get("country", ""))
        if re.fullmatch(r"[A-Za-z]{2}", country) is None:
            raise ValueError("country must be exactly two ASCII letters")
        country = country.upper()

        category = str(payload.get("category", "all")).casefold()
        if category not in BITREFILL_BROWSE_CATEGORIES:
            raise ValueError("category is not supported")

        start = payload.get("start", 0)
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("start must be an integer greater than or equal to 0")

        limit = payload.get("limit", 8)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 20
        ):
            raise ValueError("limit must be an integer from 1 to 20")

        include_international = payload.get("includeInternational", False)
        if not isinstance(include_international, bool):
            raise ValueError("includeInternational must be a boolean")

        include_test_products = payload.get("includeTestProducts", False)
        if not isinstance(include_test_products, bool):
            raise ValueError("includeTestProducts must be a boolean")

        country_filter = country
        if include_international and country != "XI":
            country_filter = f"{country},XI"

        products = self.bitrefill_client.list_products(
            country=country_filter,
            category=BITREFILL_BROWSE_CATEGORIES[category],
            start=start,
            limit=limit + 1,
            include_test_products=include_test_products,
        )
        return {
            "ok": True,
            "products": products[:limit],
            "start": start,
            "limit": limit,
            "hasPrevious": start > 0,
            "hasNext": len(products) > limit,
        }


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
        payment_token_resolver: WalletPaymentTokenResolver | None = None,
        quote_id_provider: Callable[[], str] = new_quote_id,
        now_provider: Callable[[], int] = now_epoch,
        ttl_seconds: int = 120,
    ):
        self.bitrefill_client = bitrefill_client
        self.store = store
        self.singit_usd_price_provider = singit_usd_price_provider
        self.real_rate_pricer = real_rate_pricer
        self.payment_token_resolver = payment_token_resolver
        self.quote_id_provider = quote_id_provider
        self.now_provider = now_provider
        self.ttl_seconds = int(ttl_seconds)

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
            user_id = str(payload.get("telegramUserId", "")).strip()
            payment_token = None
            if user_id:
                if self.payment_token_resolver is None:
                    raise ValueError("payment token selection is not configured")
                if not isinstance(payload.get("paymentToken"), dict):
                    raise ValueError("paymentToken is required")
                payment_token = self.payment_token_resolver.resolve(
                    user_id,
                    payload["paymentToken"],
                )
                pricing_address = (
                    COINBASE_NATIVE_TOKEN_ADDRESS
                    if payment_token["native"]
                    else payment_token["address"]
                )
                if pricing_address.casefold() == BASE_USDC_MAINNET.casefold():
                    pricing = _price_direct_usdc(
                        product["priceUsd"],
                        decimals=payment_token["decimals"],
                        balance=payment_token["balance"],
                    )
                else:
                    pricing = self.real_rate_pricer.price_for_usdc(
                        product["priceUsd"],
                        from_token=pricing_address,
                        decimals=payment_token["decimals"],
                        max_amount=payment_token["balance"],
                    )
            else:
                pricing = self.real_rate_pricer.price_for_usdc(product["priceUsd"])
            quote = build_real_rate_quote(
                request=payload,
                product=product,
                pricing=pricing,
                payment_token=payment_token,
                quote_id=self.quote_id_provider(),
                now_epoch=self.now_provider(),
                ttl_seconds=self.ttl_seconds,
            )
        else:
            quote = build_quote(
                request=payload,
                product=product,
                singit_usd_price=self.singit_usd_price_provider(),
                quote_id=self.quote_id_provider(),
                now_epoch=self.now_provider(),
                ttl_seconds=self.ttl_seconds,
            )
        self.store.save_quote(quote)
        return quote


def _price_direct_usdc(
    target_usdc: Any,
    *,
    decimals: int,
    balance: Any,
) -> dict[str, Any]:
    token_decimals = int(decimals)
    if token_decimals < 0:
        raise ValueError("USDC decimals must not be negative")
    target = Decimal(str(target_usdc))
    if target <= 0:
        raise ValueError("target USDC must be positive")
    quantum = Decimal(1).scaleb(-token_decimals)
    required = target.quantize(quantum, rounding=ROUND_CEILING)
    if Decimal(str(balance)) < required:
        raise ValueError("USDC balance is insufficient for this Bitrefill purchase")
    amount = format_decimal(required)
    return {
        "pricingMode": "bankr_real_rate",
        "targetUsdc": format_decimal(target),
        "bufferedTargetUsdc": amount,
        "requiredAmount": amount,
        "requiredAmountAtomic": str(
            int(required * (Decimal(10) ** token_decimals))
        ),
        "expectedUsdc": amount,
        "minUsdc": amount,
        "fromToken": BASE_USDC_MAINNET,
        "toToken": "USDC",
        "chain": "base",
        "quote": None,
    }


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
        pre_payment_guard: Callable[[dict[str, Any]], None] | None = None,
        settlement_verifier: Callable[..., dict[str, Any]] | None = None,
        fulfillment_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.store = store
        self.firefly = firefly
        self.bankr_payment_client = bankr_payment_client
        self.bankr_resource_url = bankr_resource_url
        self.now_provider = now_provider
        self.fulfillment_token_provider = fulfillment_token_provider
        self.pre_payment_guard = pre_payment_guard
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
        if self.pre_payment_guard is not None:
            self.pre_payment_guard(quote)
        commitment = build_purchase_commitment(quote, recipient=recipient)
        payment_hash = hash_purchase_commitment(commitment)
        payment_symbol = str(quote.get("paymentTokenSymbol") or "SINGIT")
        payment_amount = str(
            quote.get("paymentTokenAmount") or quote.get("singitAmount") or ""
        )
        approval = self.firefly.approve_payment_hash(
            payment_hash,
            context_lines=[
                "BUY BITREFILL",
                str(quote.get("productName", quote.get("productId", "")))[:20],
                f"MAX {payment_amount} {payment_symbol}"[:20],
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
                "fulfillmentToken": fulfillment_token,
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
            "fulfillmentToken": fulfillment_token,
            "bankr": bankr_result,
            "telegramText": _bitrefill_purchase_telegram_text(quote),
        }


class WalletBitrefillPurchaseRunner:
    def __init__(
        self,
        *,
        store: BitrefillCommerceStore,
        approval_client: Callable[..., dict[str, Any]],
        fulfillment_runner: Callable[[dict[str, Any]], dict[str, Any]],
        user_funding_runner: Callable[..., dict[str, Any]] | None = None,
        source_wallet_provider: Callable[[str], str] | None = None,
        now_provider: Callable[[], int] = now_epoch,
        fulfillment_token_provider: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ):
        self.store = store
        self.approval_client = approval_client
        self.fulfillment_runner = fulfillment_runner
        self.user_funding_runner = user_funding_runner
        self.source_wallet_provider = source_wallet_provider
        self.now_provider = now_provider
        self.fulfillment_token_provider = fulfillment_token_provider

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
        telegram_user_id = str(payload.get("telegramUserId", "") or "").strip()
        source_wallet = ""
        if telegram_user_id and self.source_wallet_provider is not None:
            source_wallet = str(self.source_wallet_provider(telegram_user_id) or "").strip()
        approval = self.approval_client(
            payment_hash,
            telegram_user_id=telegram_user_id or None,
            context_lines=_bitrefill_approval_context_lines(
                quote,
                source_wallet=source_wallet,
                now_epoch_value=self.now_provider(),
            ),
        )
        if not approval.get("approved"):
            self.store.advance_state(
                quote_id,
                "USER_REJECTED",
                {
                    "paymentHash": payment_hash,
                    "walletCheckout": {
                        "paymentApprovalHash": payment_hash,
                        "approval": approval,
                    },
                },
            )
            result = {
                "ok": False,
                "decision": "rejected_by_user",
                "quoteId": quote_id,
                "paymentApprovalHash": payment_hash,
                "walletCheckout": {
                    "paymentApprovalHash": payment_hash,
                    "approval": approval,
                },
            }
            telegram_text = approval.get("telegramText")
            if isinstance(telegram_text, str) and telegram_text.strip():
                result["telegramText"] = telegram_text.strip()
            return result
        approved_hash = str(approval.get("approvedHash", "")).lower()
        if approved_hash and approved_hash != payment_hash:
            raise ValueError("approved hash does not match Bitrefill purchase hash")

        wallet_checkout = {
            "paymentApprovalHash": payment_hash,
            "approval": approval,
            "mode": "wallet_native",
        }
        try:
            if telegram_user_id:
                if self.user_funding_runner is None:
                    raise ValueError("user wallet funding runner is required")
                wallet_checkout["userFunding"] = self.user_funding_runner(
                    telegram_user_id=telegram_user_id,
                    quote=quote,
                    recipient=recipient,
                )
        except Exception as exc:
            self.store.advance_state(
                quote_id,
                "RECONCILIATION_REQUIRED",
                {"walletCheckout": {**wallet_checkout, "fundingError": str(exc)}},
            )
            raise

        fulfillment_token = self.fulfillment_token_provider()
        token_hash = hashlib.sha256(fulfillment_token.encode("utf-8")).hexdigest()
        self.store.advance_state(
            quote_id,
            "USER_APPROVED",
            {
                "paymentHash": payment_hash,
                "paymentCommitment": commitment,
                "walletCheckout": wallet_checkout,
                "fulfillmentTokenHash": token_hash,
                "recipient": recipient,
            },
        )
        try:
            bitrefill_result = self.fulfillment_runner(
                {"quoteId": quote_id, "fulfillmentToken": fulfillment_token}
            )
        except Exception as exc:
            self.store.advance_state(
                quote_id,
                "RECONCILIATION_REQUIRED",
                {"walletCheckout": {**wallet_checkout, "fulfillmentError": str(exc)}},
            )
            raise

        return {
            "ok": bool(bitrefill_result.get("ok", False)),
            "decision": "approved_and_fulfilled",
            "quoteId": quote_id,
            "priceUsd": quote.get("priceUsd"),
            "paymentApprovalHash": payment_hash,
            "paymentCommitment": commitment,
            "fulfillmentToken": fulfillment_token,
            "walletCheckout": wallet_checkout,
            "bitrefill": bitrefill_result,
            "telegramText": _bitrefill_purchase_telegram_text(
                quote,
                source_wallet=str(
                    wallet_checkout.get("userFunding", {}).get("fromWallet", "")
                    if isinstance(wallet_checkout.get("userFunding"), dict)
                    else ""
                ),
                singit_spent=str(quote.get("singitAmount", "")),
                transfer_tx_id=str(
                    wallet_checkout.get("userFunding", {}).get("transfer", {}).get("txId", "")
                    if isinstance(wallet_checkout.get("userFunding"), dict)
                    and isinstance(wallet_checkout.get("userFunding", {}).get("transfer"), dict)
                    else ""
                ),
            ),
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
            "pricingMode": quote.get("pricingMode", "fixed"),
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
        redacted = {
            "ok": True,
            "quoteId": quote["quoteId"],
            "orderId": result["orderId"],
            "status": result.get("status", "delivered"),
        }
        if quote.get("maxPaymentTokenAtomic") is not None:
            maximum = str(quote["maxPaymentTokenAtomic"])
            redacted.update(
                {
                    "settleAmountAtomic": maximum,
                    "maxPaymentTokenAtomic": maximum,
                }
            )
            payment_token_symbol = str(quote.get("paymentTokenSymbol") or "").strip()
            if payment_token_symbol:
                redacted["paymentTokenSymbol"] = payment_token_symbol
            return redacted
        if quote.get("maxSingitAtomic") is not None:
            maximum = str(quote["maxSingitAtomic"])
            redacted.update(
                {
                    "settleAmountAtomic": maximum,
                    "maxSingitAtomic": maximum,
                }
            )
            return redacted
        raise ValueError("quote settlement maximum is missing")


def _provider_is_delivered(result: dict[str, Any], quote: dict[str, Any]) -> bool:
    if str(result.get("status", "")).strip().lower() != "delivered":
        return False
    if quote.get("productType") in {"gift_card", "esim"}:
        redemption = result.get("redemption")
        return isinstance(redemption, dict) and redemption.get("value") is not None
    return True


def _bitrefill_purchase_telegram_text(
    quote: dict[str, Any],
    *,
    source_wallet: str = "",
    singit_spent: str = "",
    transfer_tx_id: str = "",
) -> str:
    product_name = str(quote.get("productName") or quote.get("productId") or "Your item")
    package_value = str(quote.get("packageValue") or "").strip()
    value_text = f" ${package_value}" if package_value else ""
    source_text = f" Paid from {_short_address(source_wallet)}." if source_wallet else ""
    payment_symbol = str(quote.get("paymentTokenSymbol") or "SINGIT")
    payment_amount = str(quote.get("paymentTokenAmount") or singit_spent or "").strip()
    spent_text = (
        f"\nSpent: {_format_amount(payment_amount)} {payment_symbol}"
        if payment_amount
        else ""
    )
    tx_url = _base_tx_url(transfer_tx_id)
    tx_text = f"\nTransfer tx: {tx_url}" if tx_url else ""
    return (
        f"✅ {product_name}{value_text} is ready. "
        f"The purchase was paid with {payment_symbol}.{source_text} "
        "Use /last_purchase to reveal your code."
        f"{spent_text}"
        f"{tx_text}"
    )


def _bitrefill_approval_context_lines(
    quote: dict[str, Any],
    *,
    source_wallet: str = "",
    now_epoch_value: int,
) -> list[str]:
    expires_in = max(0, int(quote.get("expiresAtEpoch", now_epoch_value)) - int(now_epoch_value))
    expires_minutes = max(1, (expires_in + 59) // 60)
    payment_symbol = str(quote.get("paymentTokenSymbol") or "").strip()
    payment_amount = str(quote.get("paymentTokenAmount") or "").strip()
    lines = [
        "Action: BUY BITREFILL",
        f"Product: {str(quote.get('productName', quote.get('productId', '')))}",
        f"Cost: {_format_amount(str(quote.get('priceUsd', '')))} USD",
    ]
    if payment_symbol and payment_amount:
        lines.extend(
            [
                f"Payment token: {payment_symbol}",
                f"Maximum spend: {_format_amount(payment_amount)} {payment_symbol}",
            ]
        )
    else:
        lines.append(
            f"Max spend: {_format_amount(str(quote.get('singitAmount', '')))} SINGIT"
        )
    if source_wallet:
        lines.append(f"Paid from: {_short_address(source_wallet)}")
    lines.append(f"Expires: {expires_minutes} minute{'s' if expires_minutes != 1 else ''}")
    return [line[:80] for line in lines if line.strip()]


def _bitrefill_delivery_telegram_text(
    quote: dict[str, Any],
    *,
    redemption: Any | None = None,
) -> str:
    product_name = str(quote.get("productName") or quote.get("productId") or "Your item")
    package_value = str(quote.get("packageValue") or "").strip()
    value_text = f" ${package_value}" if package_value else ""
    detail = _redemption_detail_text(redemption)
    if detail:
        return f"✅ {product_name}{value_text} is ready.\n{detail}"
    return f"✅ {product_name}{value_text} is ready. Your code is ready."


def _redemption_detail_text(redemption: Any | None) -> str:
    if not isinstance(redemption, dict):
        value = str(redemption or "").strip()
        return f"Redemption: {value}" if value else ""
    value = redemption.get("value")
    if isinstance(value, dict):
        for key, label in (
            ("code", "Code"),
            ("pin", "PIN"),
            ("url", "Link"),
            ("link", "Link"),
            ("voucher", "Voucher"),
        ):
            item = str(value.get(key, "") or "").strip()
            if item:
                return f"{label}: {item}"
        if value:
            return "Redemption: " + json.dumps(value, ensure_ascii=False, sort_keys=True)
        return ""
    value_text = str(value or "").strip()
    if value_text:
        label = str(redemption.get("label", "") or "Redemption").strip()
        return f"{label}: {value_text}"
    return ""


def _short_address(address: str) -> str:
    value = str(address or "").strip()
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


def _format_amount(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


def _base_tx_url(tx_id: str) -> str:
    value = str(tx_id or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("0x"):
        return f"https://basescan.org/tx/{value}"
    return ""
