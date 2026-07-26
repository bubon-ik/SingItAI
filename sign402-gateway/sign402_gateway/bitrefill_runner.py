import hashlib
import hmac
import logging
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
    calculate_service_fee,
    hash_purchase_commitment,
    new_quote_id,
    now_epoch,
)
from .commerce_store import (
    BitrefillCommerceStore,
    sanitize_bankr_reconciliation_snapshot,
)
from .diagnostics import log_swallowed_failure
from .numeric import format_decimal


logger = logging.getLogger(__name__)

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


class RepriceRequiredError(ValueError):
    pass


class CdpWalletServiceError(ValueError):
    def __init__(self, message: str, *, stage: str = ""):
        super().__init__(message)
        self.stage = stage if stage == "pre_swap" else ""


def _amount_to_atomic(value: Any, *, decimals: int, field: str) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field} must be a non-negative finite amount")
    scaled = amount * (Decimal(10) ** int(decimals))
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{field} exceeds token decimal precision")
    return int(scaled)


def _quote_with_execution_pricing(
    quote: dict[str, Any],
    execution_pricing: Any,
) -> dict[str, Any]:
    if not isinstance(execution_pricing, dict):
        raise ValueError("execution pricing is missing")
    token_address = str(execution_pricing.get("paymentTokenAddress") or "").strip()
    if token_address.casefold() != str(
        quote.get("paymentTokenAddress") or ""
    ).strip().casefold():
        raise ValueError("execution payment token changed")
    decimals = int(quote["paymentTokenDecimals"])
    if int(execution_pricing.get("paymentTokenDecimals", -1)) != decimals:
        raise ValueError("execution payment token decimals changed")
    maximum_atomic = int(quote["maxPaymentTokenAtomic"])
    approved_maximum = int(
        execution_pricing.get("approvedMaximumAtomic", -1)
    )
    if approved_maximum != maximum_atomic:
        raise ValueError("execution maximum does not match approval")
    actual_amount = str(
        execution_pricing.get("actualPaymentTokenAmount") or ""
    ).strip()
    actual_atomic = int(
        str(execution_pricing.get("actualPaymentTokenAtomic") or "0")
    )
    if actual_atomic <= 0 or actual_atomic > maximum_atomic:
        raise ValueError("execution amount exceeds approved maximum")
    if _amount_to_atomic(
        actual_amount,
        decimals=decimals,
        field="execution payment-token amount",
    ) != actual_atomic:
        raise ValueError("execution payment-token amount is inconsistent")
    expected_usdc = Decimal(str(execution_pricing.get("expectedUsdc") or "0"))
    minimum_usdc = Decimal(str(execution_pricing.get("minUsdc") or "0"))
    required_usdc = Decimal(str(quote["totalUsd"]))
    if (
        not expected_usdc.is_finite()
        or not minimum_usdc.is_finite()
        or minimum_usdc < required_usdc
    ):
        raise ValueError("execution pricing does not cover the purchase total")
    safe_pricing = {
        "paymentTokenAddress": token_address,
        "paymentTokenDecimals": decimals,
        "actualPaymentTokenAmount": actual_amount,
        "actualPaymentTokenAtomic": str(actual_atomic),
        "expectedUsdc": format_decimal(expected_usdc),
        "minUsdc": format_decimal(minimum_usdc),
        "approvedMaximumAtomic": str(maximum_atomic),
        "pricedAtEpoch": int(execution_pricing["pricedAtEpoch"]),
    }
    effective_quote = deepcopy(quote)
    effective_quote.update(
        {
            "actualPaymentTokenAmount": actual_amount,
            "actualPaymentTokenAtomic": str(actual_atomic),
            "expectedUsdc": safe_pricing["expectedUsdc"],
            "minUsdc": safe_pricing["minUsdc"],
            "executionPricing": safe_pricing,
        }
    )
    return effective_quote


class WalletBitrefillExecutionPricer:
    def __init__(
        self,
        *,
        real_rate_pricer: Any,
        payment_token_resolver: WalletPaymentTokenResolver,
        now_provider: Callable[[], int] = now_epoch,
    ):
        self.real_rate_pricer = real_rate_pricer
        self.payment_token_resolver = payment_token_resolver
        self.now_provider = now_provider

    def __call__(
        self,
        telegram_user_id: str,
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            token = self.payment_token_resolver.resolve(
                str(telegram_user_id),
                {"address": quote["paymentTokenAddress"]},
            )
            committed_address = str(quote["paymentTokenAddress"])
            if str(token["address"]).casefold() != committed_address.casefold():
                raise ValueError("payment token changed")
            decimals = int(quote["paymentTokenDecimals"])
            if int(token["decimals"]) != decimals:
                raise ValueError("payment token decimals changed")
            maximum_atomic = int(quote["maxPaymentTokenAtomic"])
            if maximum_atomic <= 0:
                raise ValueError("approved maximum is invalid")
            balance_atomic = _amount_to_atomic(
                token["balance"],
                decimals=decimals,
                field="payment-token balance",
            )
            allowed_atomic = min(maximum_atomic, balance_atomic)
            if allowed_atomic <= 0:
                raise ValueError("payment-token balance is insufficient")
            pricing_address = (
                COINBASE_NATIVE_TOKEN_ADDRESS
                if token["native"]
                else token["address"]
            )
            if committed_address.casefold() == BASE_USDC_MAINNET.casefold():
                pricing = _price_direct_usdc(
                    quote["totalUsd"],
                    decimals=decimals,
                    balance=token["balance"],
                )
            else:
                pricing = self.real_rate_pricer.price_for_usdc(
                    quote["totalUsd"],
                    from_token=pricing_address,
                    decimals=decimals,
                    max_amount=format_decimal(
                        Decimal(allowed_atomic)
                        / (Decimal(10) ** decimals)
                    ),
                )
            actual_atomic = int(pricing["requiredAmountAtomic"])
            actual_amount = str(pricing["requiredAmount"])
            if (
                actual_atomic > maximum_atomic
                or actual_atomic > balance_atomic
                or _amount_to_atomic(
                    actual_amount,
                    decimals=decimals,
                    field="fresh payment-token amount",
                )
                != actual_atomic
            ):
                raise ValueError("fresh price exceeds approved maximum")
            execution_pricing = {
                "paymentTokenAddress": committed_address,
                "paymentTokenDecimals": decimals,
                "actualPaymentTokenAmount": actual_amount,
                "actualPaymentTokenAtomic": str(actual_atomic),
                "expectedUsdc": str(pricing["expectedUsdc"]),
                "minUsdc": str(pricing["minUsdc"]),
                "approvedMaximumAtomic": str(maximum_atomic),
                "pricedAtEpoch": int(self.now_provider()),
            }
            return _quote_with_execution_pricing(quote, execution_pricing)
        except RepriceRequiredError:
            raise
        except Exception:
            raise RepriceRequiredError("fresh pricing is unavailable") from None


def _validated_token_return(
    result: Any,
    *,
    token_address: str,
    to_address: str,
    amount_atomic: str,
) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValueError("token return was not confirmed")
    transaction_hash = str(
        result.get("transactionHash") or result.get("txId") or ""
    ).strip()
    network = str(result.get("network") or "").strip()
    token = str(result.get("token") or "").strip()
    destination = str(result.get("to") or "").strip()
    returned_atomic = str(result.get("amountAtomic") or "").strip()
    if not transaction_hash:
        raise ValueError("token return transaction hash is missing")
    if network != "base":
        raise ValueError("token return network changed")
    if token.casefold() != str(token_address).casefold():
        raise ValueError("token return asset changed")
    if destination.casefold() != str(to_address).casefold():
        raise ValueError("token return destination changed")
    if returned_atomic != str(amount_atomic):
        raise ValueError("token return amount changed")
    return {
        "transactionHash": transaction_hash,
        "network": network,
        "token": token,
        "amountAtomic": returned_atomic,
        "from": str(result.get("from") or "").strip(),
        "to": destination,
    }


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
    persisted_status = str(
        provider_result.get("status", record["state"].lower())
    )
    public_status = persisted_status
    if (
        persisted_status.strip().lower() == "delivered"
        and record["state"] != "DELIVERED"
    ):
        public_status = record["state"].lower()
    status_result = {
        "ok": True,
        "quoteId": record["quoteId"],
        "state": record["state"],
        "productId": quote.get("productId"),
        "productName": quote.get("productName"),
        "packageValue": quote.get("packageValue"),
        "orderId": provider_result.get("orderId"),
        "status": public_status,
    }
    if not include_redemption:
        return status_result

    stored_recipient = metadata.get("recipient")
    recipient_ok = (
        isinstance(stored_recipient, dict)
        and bool(stored_recipient)
        and recipient == stored_recipient
    )
    token_ok = _fulfillment_token_matches(metadata, fulfillment_token)
    if not (recipient_ok or token_ok):
        if stored_recipient:
            raise ValueError("recipient does not match order")
        raise ValueError("valid fulfillmentToken is required to reveal redemption")

    if (
        bitrefill_client is None
        or not str(provider_result.get("invoiceId") or "").strip()
    ):
        return {**status_result, "redemptionUnavailable": True}
    try:
        refreshed = bitrefill_client.refresh_purchase(provider_result, quote)
        if not isinstance(refreshed, dict):
            raise ValueError("Bitrefill refresh must return an object")
        store.checkpoint(quote_id, {"bitrefill": refreshed})
    except Exception:
        return {**status_result, "redemptionUnavailable": True}

    redemption = refreshed.get("redemption")
    provider_delivered = _provider_is_delivered(refreshed, quote)
    if not provider_delivered or not _redemption_detail_text(redemption):
        return {
            **status_result,
            "redemptionAvailable": False,
        }
    store.advance_state(quote_id, "DELIVERED", {})
    result = {
        **status_result,
        "state": "DELIVERED",
        "orderId": refreshed.get("orderId", status_result["orderId"]),
        "status": refreshed.get("status", "delivered"),
        "redemption": deepcopy(redemption),
    }
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
        max_reprice_bps: int = 500,
    ):
        self.bitrefill_client = bitrefill_client
        self.store = store
        self.singit_usd_price_provider = singit_usd_price_provider
        self.real_rate_pricer = real_rate_pricer
        self.payment_token_resolver = payment_token_resolver
        self.quote_id_provider = quote_id_provider
        self.now_provider = now_provider
        self.ttl_seconds = int(ttl_seconds)
        self.max_reprice_bps = int(max_reprice_bps)

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
        _, total_usd = calculate_service_fee(product["priceUsd"])
        total_usd_text = format_decimal(total_usd)
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
                        total_usd_text,
                        decimals=payment_token["decimals"],
                        balance=payment_token["balance"],
                    )
                else:
                    pricing = self.real_rate_pricer.price_for_usdc(
                        total_usd_text,
                        from_token=pricing_address,
                        decimals=payment_token["decimals"],
                        max_amount=payment_token["balance"],
                    )
            else:
                pricing = self.real_rate_pricer.price_for_usdc(total_usd_text)
            quote = build_real_rate_quote(
                request=payload,
                product=product,
                pricing=pricing,
                payment_token=payment_token,
                max_reprice_bps=self.max_reprice_bps,
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
        except Exception:
            self.store.advance_state(
                quote_id,
                "RECONCILIATION_REQUIRED",
                {"bankrError": "Bankr payment request failed"},
            )
            raise ValueError("Bankr payment request failed") from None
        bankr_snapshot = sanitize_bankr_reconciliation_snapshot(bankr_result)
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
                    {
                        "bankr": bankr_snapshot,
                        "singitSettlement": settlement_proof,
                    },
                )
                bitrefill_result = self.fulfillment_runner(
                    {"quoteId": quote_id, "fulfillmentToken": fulfillment_token}
                )
            except Exception:
                self.store.advance_state(
                    quote_id,
                    "RECONCILIATION_REQUIRED",
                    {
                        "bankr": bankr_snapshot,
                        "singitSettlementError": (
                            "Bitrefill settlement or fulfillment failed"
                        ),
                    },
                )
                raise ValueError(
                    "Bitrefill settlement or fulfillment failed"
                ) from None
            return {
                "ok": bool(bankr_snapshot.get("ok", False))
                and bool(bitrefill_result.get("ok", False)),
                "decision": "approved_and_executed",
                "quoteId": quote_id,
                "paymentApprovalHash": payment_hash,
                "paymentCommitment": commitment,
                "fulfillmentToken": fulfillment_token,
                "bankr": bankr_snapshot,
                "singitSettlement": settlement_proof,
                "bitrefill": bitrefill_result,
                "telegramText": _bitrefill_purchase_telegram_text(quote),
            }
        refreshed = self.store.get_quote(quote_id)
        if refreshed["state"] == "DELIVERED":
            self.store.advance_state(
                quote_id,
                "DELIVERED",
                {"bankr": bankr_snapshot},
            )
        else:
            self.store.advance_state(
                quote_id,
                "SINGIT_SETTLED",
                {"bankr": bankr_snapshot},
            )
        return {
            "ok": bool(bankr_snapshot.get("ok", False)),
            "decision": "approved_and_executed",
            "quoteId": quote_id,
            "paymentApprovalHash": payment_hash,
            "paymentCommitment": commitment,
            "fulfillmentToken": fulfillment_token,
            "bankr": bankr_snapshot,
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
        execution_pricer: Callable[[str, dict[str, Any]], dict[str, Any]]
        | None = None,
        return_runner: Callable[..., dict[str, Any]] | None = None,
        source_wallet_provider: Callable[[str], str] | None = None,
        now_provider: Callable[[], int] = now_epoch,
        fulfillment_token_provider: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        enforce_spend: Callable[[str, dict[str, Any]], str | None] | None = None,
        release_spend: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.approval_client = approval_client
        self.fulfillment_runner = fulfillment_runner
        self.user_funding_runner = user_funding_runner
        self.execution_pricer = execution_pricer
        self.return_runner = return_runner
        self.source_wallet_provider = source_wallet_provider
        self.now_provider = now_provider
        self.fulfillment_token_provider = fulfillment_token_provider
        self.enforce_spend = enforce_spend
        self.release_spend = release_spend

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.buy(payload)

    def payment_hash_for_quote(self, quote: dict[str, Any], *, recipient: dict[str, Any]) -> str:
        return hash_purchase_commitment(build_purchase_commitment(quote, recipient=recipient))

    def buy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hold the user's budget across the whole approve-then-fulfil window.

        `enforce_spend` reserves the amount, which is only safe if every exit
        path gives it back. The caller settles the returned reservation id once
        the purchase has been recorded.
        """
        holder: dict[str, Any] = {"reservationId": None}
        try:
            result = self._buy(payload, holder)
        except BaseException:
            self._release_spend(holder["reservationId"])
            raise
        if not isinstance(result, dict) or not result.get("ok"):
            self._release_spend(holder["reservationId"])
            return result
        result["spendReservationId"] = holder["reservationId"]
        return result

    def _release_spend(self, reservation_id: Any) -> None:
        if not reservation_id or self.release_spend is None:
            return
        try:
            self.release_spend(reservation_id)
        except Exception:
            # A stranded hold expires on its own; never mask the real failure.
            pass

    def _buy(self, payload: dict[str, Any], holder: dict[str, Any]) -> dict[str, Any]:
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

        self.store.require_sensitive_state_cipher()
        commitment = build_purchase_commitment(quote, recipient=recipient)
        payment_hash = hash_purchase_commitment(commitment)
        telegram_user_id = str(payload.get("telegramUserId", "") or "").strip()
        if telegram_user_id and self.enforce_spend is not None:
            holder["reservationId"] = self.enforce_spend(telegram_user_id, quote)
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
        execution_quote = quote
        if telegram_user_id and quote.get("paymentTokenAddress") is not None:
            try:
                if self.execution_pricer is None:
                    raise RepriceRequiredError(
                        "execution repricing is not configured"
                    )
                repriced = self.execution_pricer(telegram_user_id, quote)
                execution_quote = _quote_with_execution_pricing(
                    quote,
                    repriced.get("executionPricing")
                    if isinstance(repriced, dict)
                    else None,
                )
            except Exception:
                self.store.advance_state(
                    quote_id,
                    "QUOTE_EXPIRED",
                    {
                        "paymentHash": payment_hash,
                        "walletCheckout": wallet_checkout,
                        "repriceError": "Fresh pricing requires a new approval",
                    },
                )
                return {
                    "ok": False,
                    "decision": "reprice_required",
                    "quoteId": quote_id,
                    "paymentApprovalHash": payment_hash,
                    "telegramText": (
                        "The exchange rate changed. No funds were moved. "
                        "Request a new quote and confirm it again."
                    ),
                }

        fulfillment_token = self.fulfillment_token_provider()
        token_hash = hashlib.sha256(fulfillment_token.encode("utf-8")).hexdigest()
        approval_metadata = {
            "paymentHash": payment_hash,
            "paymentCommitment": commitment,
            "walletCheckout": wallet_checkout,
            "fulfillmentTokenHash": token_hash,
            "recipient": recipient,
        }
        if execution_quote.get("executionPricing") is not None:
            approval_metadata["executionPricing"] = execution_quote[
                "executionPricing"
            ]
        self.store.advance_state(
            quote_id,
            "USER_APPROVED",
            approval_metadata,
        )
        prepare_purchase = getattr(self.fulfillment_runner, "prepare", None)
        if not callable(prepare_purchase):
            raise ValueError(
                "Bitrefill invoice preparation runner is required"
            )
        prepared = prepare_purchase(
            {
                "quoteId": quote_id,
                "fulfillmentToken": fulfillment_token,
            }
        )
        if not isinstance(prepared, dict) or not str(
            prepared.get("invoiceId", "")
        ).strip():
            raise ValueError("Bitrefill prepared invoice is invalid")
        wallet_checkout["bitrefillInvoiceId"] = str(
            prepared["invoiceId"]
        ).strip()
        self.store.checkpoint(
            quote_id,
            {"walletCheckout": wallet_checkout},
        )
        try:
            if telegram_user_id:
                if self.user_funding_runner is None:
                    raise ValueError("user wallet funding runner is required")
                user_funding = self.user_funding_runner(
                    telegram_user_id=telegram_user_id,
                    quote=execution_quote,
                    recipient=recipient,
                )
                if not isinstance(user_funding, dict):
                    raise ValueError(
                        "user wallet funding result is invalid"
                    )
                transfer = user_funding.get("transfer")
                transfer_tx_id = (
                    str(
                        transfer.get("txId")
                        or transfer.get("transactionHash")
                        or ""
                    ).strip()
                    if isinstance(transfer, dict)
                    else ""
                )
                if not transfer_tx_id:
                    raise ValueError(
                        "user wallet funding transaction is unconfirmed"
                    )
                wallet_checkout["userFunding"] = user_funding
        except Exception as exc:
            log_swallowed_failure(
                logger,
                "Managed-wallet funding request failed",
                exc,
                quoteId=quote_id,
            )
            self.store.advance_state(
                quote_id,
                "RECONCILIATION_REQUIRED",
                {
                    "walletCheckout": {
                        **wallet_checkout,
                        "fundingError": "Managed-wallet funding request failed",
                    }
                },
            )
            raise ValueError("Managed-wallet funding request failed") from None

        self.store.checkpoint(
            quote_id,
            {
                "walletCheckout": wallet_checkout,
            },
        )
        try:
            bitrefill_result = self.fulfillment_runner(
                {"quoteId": quote_id, "fulfillmentToken": fulfillment_token}
            )
        except CdpWalletServiceError as exc:
            log_swallowed_failure(
                logger,
                "Bitrefill fulfillment request failed",
                exc,
                quoteId=quote_id,
                stage=exc.stage or "unknown",
            )
            user_funding = wallet_checkout.get("userFunding")
            transfer = (
                user_funding.get("transfer")
                if isinstance(user_funding, dict)
                else None
            )
            transfer_tx_id = (
                str(
                    transfer.get("txId")
                    or transfer.get("transactionHash")
                    or ""
                ).strip()
                if isinstance(transfer, dict)
                else ""
            )
            can_return = (
                exc.stage == "pre_swap"
                and self.return_runner is not None
                and quote.get("paymentTokenAddress") is not None
                and not bool(quote.get("paymentTokenNative", False))
                and bool(transfer_tx_id)
                and isinstance(user_funding, dict)
                and bool(str(user_funding.get("fromWallet") or "").strip())
                and bool(
                    str(
                        execution_quote.get(
                            "actualPaymentTokenAtomic"
                        )
                        or ""
                    ).strip()
                )
            )
            if can_return:
                try:
                    token_return = self.return_runner(
                        quote_id=quote_id,
                        token_address=str(quote["paymentTokenAddress"]),
                        to_address=str(user_funding["fromWallet"]),
                        amount_atomic=str(
                            execution_quote["actualPaymentTokenAtomic"]
                        ),
                        chain="base",
                    )
                    token_return = _validated_token_return(
                        token_return,
                        token_address=str(quote["paymentTokenAddress"]),
                        to_address=str(user_funding["fromWallet"]),
                        amount_atomic=str(
                            execution_quote["actualPaymentTokenAtomic"]
                        ),
                    )
                except Exception as return_exc:
                    log_swallowed_failure(
                        logger,
                        "Token return confirmation failed",
                        return_exc,
                        quoteId=quote_id,
                    )
                    self.store.advance_state(
                        quote_id,
                        "RECONCILIATION_REQUIRED",
                        {
                            "walletCheckout": {
                                **wallet_checkout,
                                "fulfillmentError": (
                                    "Bitrefill fulfillment request failed"
                                ),
                            },
                            "returnError": "Token return confirmation failed",
                        },
                    )
                    raise ValueError(
                        "Bitrefill fulfillment request failed"
                    ) from None
                self.store.advance_state(
                    quote_id,
                    "REFUNDED",
                    {
                        "walletCheckout": {
                            **wallet_checkout,
                            "fulfillmentError": (
                                "Bitrefill funding changed before swap"
                            ),
                        },
                        "tokenReturn": token_return,
                    },
                )
                return {
                    "ok": False,
                    "decision": "refunded_after_rate_change",
                    "quoteId": quote_id,
                    "paymentApprovalHash": payment_hash,
                    "telegramText": (
                        "The exchange rate changed after the wallet transfer. "
                        "The exact token amount was returned to your wallet."
                    ),
                }
            self.store.advance_state(
                quote_id,
                "RECONCILIATION_REQUIRED",
                {
                    "walletCheckout": {
                        **wallet_checkout,
                        "fulfillmentError": (
                            "Bitrefill fulfillment request failed"
                        ),
                    }
                },
            )
            raise ValueError("Bitrefill fulfillment request failed") from None
        except Exception as exc:
            log_swallowed_failure(
                logger,
                "Bitrefill fulfillment request failed",
                exc,
                quoteId=quote_id,
            )
            self.store.advance_state(
                quote_id,
                "RECONCILIATION_REQUIRED",
                {
                    "walletCheckout": {
                        **wallet_checkout,
                        "fulfillmentError": "Bitrefill fulfillment request failed",
                    }
                },
            )
            raise ValueError("Bitrefill fulfillment request failed") from None

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
                execution_quote,
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

    def _authorized_context(
        self,
        payload: dict[str, Any],
        *,
        allow_prepared_after_expiry: bool = False,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        quote_id = str(payload.get("quoteId", "")).strip()
        if not quote_id:
            raise ValueError("quoteId is required")

        record = self.store.get_quote(quote_id)
        prepared_can_finish = (
            allow_prepared_after_expiry
            and record["state"] == "INVOICE_CREATED"
        )
        if (
            self.now_provider() >= int(record["quote"]["expiresAtEpoch"])
            and not prepared_can_finish
        ):
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
        effective_quote = record["quote"]
        if record["quote"].get("paymentTokenAddress") is not None:
            effective_quote = _quote_with_execution_pricing(
                record["quote"],
                metadata.get("executionPricing"),
            )
        return quote_id, record, effective_quote

    def prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        quote_id, record, effective_quote = self._authorized_context(payload)
        if record["state"] not in {"USER_APPROVED", "FIREFLY_APPROVED"}:
            raise ValueError(
                f"quote cannot create an invoice (state: {record['state']})"
            )
        metadata = record["metadata"]
        try:
            prepared = self.bitrefill_client.prepare_purchase(
                quote=effective_quote,
                recipient=(
                    metadata.get("recipient")
                    if isinstance(metadata.get("recipient"), dict)
                    else {}
                ),
            )
            if not isinstance(prepared, dict):
                raise ValueError("Bitrefill prepared invoice is invalid")
            self.store.advance_state(
                quote_id,
                "INVOICE_CREATED",
                {"bitrefillCheckpoint": prepared},
            )
        except Exception as exc:
            log_swallowed_failure(
                logger,
                "Bitrefill provider invoice preparation failed",
                exc,
                quoteId=quote_id,
                productId=str(effective_quote.get("productId", "")),
                packageValue=str(effective_quote.get("packageValue", "")),
            )
            self.store.advance_state(
                quote_id,
                "FULFILLMENT_FAILED",
                {"fulfillmentError": "Bitrefill provider request failed"},
            )
            raise ValueError("Bitrefill provider request failed") from None
        stored = self.store.get_quote(quote_id)["metadata"].get(
            "bitrefillCheckpoint"
        )
        if not isinstance(stored, dict):
            raise ValueError("Bitrefill prepared invoice was not persisted")
        return stored

    def fulfill(self, payload: dict[str, Any]) -> dict[str, Any]:
        quote_id, record, effective_quote = self._authorized_context(
            payload,
            allow_prepared_after_expiry=True,
        )
        metadata = record["metadata"]
        prepared = metadata.get("bitrefillCheckpoint")
        prepared_flow = record["state"] == "INVOICE_CREATED"
        if record["state"] == "USER_APPROVED" or (
            prepared_flow and not isinstance(prepared, dict)
        ):
            raise ValueError("prepared invoice is required before funding")
        if not self.store.try_mark_fulfilling(quote_id):
            raise ValueError("quote is already fulfilled or being fulfilled")

        if self.funding_runner is not None:
            try:
                funding_result = self.funding_runner(effective_quote)
                if not isinstance(funding_result, dict) or funding_result.get(
                    "ok"
                ) is False:
                    raise CdpWalletServiceError(
                        "CDP wallet service failed"
                    )
                payment_token_address = str(
                    effective_quote.get("paymentTokenAddress") or ""
                ).strip()
                direct_usdc = (
                    bool(payment_token_address)
                    and payment_token_address.casefold()
                    == BASE_USDC_MAINNET.casefold()
                )
                swap = funding_result.get("swap")
                funding_tx_id = str(
                    funding_result.get("txId")
                    or funding_result.get("transactionHash")
                    or (
                        swap.get("txId")
                        or swap.get("transactionHash")
                        if isinstance(swap, dict)
                        else ""
                    )
                    or ""
                ).strip()
                if prepared_flow and not direct_usdc and not funding_tx_id:
                    raise CdpWalletServiceError(
                        "CDP wallet service failed"
                    )
                self.store.advance_state(
                    quote_id,
                    "FULFILLING",
                    {"bankrSwap": funding_result},
                )
            except CdpWalletServiceError as exc:
                log_swallowed_failure(
                    logger,
                    "Bitrefill funding request failed",
                    exc,
                    quoteId=quote_id,
                    stage=exc.stage or "unknown",
                )
                self.store.advance_state(
                    quote_id,
                    "RECONCILIATION_REQUIRED",
                    {"fundingError": "Bitrefill funding request failed"},
                )
                raise
            except Exception as exc:
                log_swallowed_failure(
                    logger,
                    "Bitrefill funding request failed",
                    exc,
                    quoteId=quote_id,
                )
                self.store.advance_state(
                    quote_id,
                    "RECONCILIATION_REQUIRED",
                    {"fundingError": "Bitrefill funding request failed"},
                )
                raise ValueError("Bitrefill funding request failed") from None

        try:
            if prepared_flow:
                result = self.bitrefill_client.complete_purchase(
                    quote=effective_quote,
                    prepared=prepared,
                    checkpoint_callback=lambda checkpoint: self.store.checkpoint(
                        quote_id,
                        {"bitrefillCheckpoint": checkpoint},
                    ),
                )
            else:
                result = self.bitrefill_client.buy_product(
                    quote=effective_quote,
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
            log_swallowed_failure(
                logger,
                "Bitrefill provider request failed",
                exc,
                quoteId=quote_id,
                productId=str(effective_quote.get("productId", "")),
                packageValue=str(effective_quote.get("packageValue", "")),
            )
            failure_state = (
                "RECONCILIATION_REQUIRED"
                if prepared_flow
                else "FULFILLMENT_FAILED"
            )
            self.store.advance_state(
                quote_id,
                failure_state,
                {"fulfillmentError": "Bitrefill provider request failed"},
            )
            raise ValueError("Bitrefill provider request failed") from None
        self.store.advance_state(quote_id, "BITREFILL_PURCHASED", {"bitrefill": result})
        if _provider_is_delivered(result, record["quote"]):
            self.store.advance_state(quote_id, "DELIVERED", {})
        return self._redacted_result(effective_quote, result)

    def _redacted_result(self, quote: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        redacted = {
            "ok": True,
            "quoteId": quote["quoteId"],
            "orderId": result["orderId"],
            "status": result.get("status", "delivered"),
        }
        if quote.get("maxPaymentTokenAtomic") is not None:
            maximum = str(quote["maxPaymentTokenAtomic"])
            actual = str(
                quote.get("actualPaymentTokenAtomic") or maximum
            )
            redacted.update(
                {
                    "settleAmountAtomic": actual,
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
        return _redemption_has_nonempty_value(result.get("redemption"))
    return True


def _redemption_has_nonempty_value(redemption: Any) -> bool:
    if not isinstance(redemption, dict) or "value" not in redemption:
        return False

    def has_value(value: Any) -> bool:
        if isinstance(value, dict):
            return any(has_value(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(has_value(item) for item in value)
        return bool(str(value or "").strip())

    return has_value(redemption["value"])


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
    payment_amount = str(
        quote.get("actualPaymentTokenAmount")
        or quote.get("paymentTokenAmount")
        or singit_spent
        or ""
    ).strip()
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
    fee_percent = format_decimal(
        Decimal(str(quote.get("serviceFeeBps", 0))) / Decimal(100)
    )
    payment_symbol = str(quote.get("paymentTokenSymbol") or "").strip()
    estimated_payment_amount = str(
        quote.get("estimatedPaymentTokenAmount") or ""
    ).strip()
    maximum_payment_amount = str(
        quote.get("maxPaymentTokenAmount") or ""
    ).strip()
    lines = [
        "Action: BUY BITREFILL",
        f"Product: {str(quote.get('productName', quote.get('productId', '')))}",
        f"Product price: {_format_amount(str(quote.get('priceUsd', '')))} USD",
        f"Service fee ({fee_percent}%): "
        f"{_format_amount(str(quote.get('serviceFeeUsd', '')))} USD",
        f"Total: {_format_amount(str(quote.get('totalUsd', '')))} USD",
    ]
    if payment_symbol:
        if not estimated_payment_amount or not maximum_payment_amount:
            raise ValueError("quote does not contain a bounded payment-token maximum")
        lines.extend(
            [
                f"Payment token: {payment_symbol}",
                f"Estimated spend: {_format_amount(estimated_payment_amount)} "
                f"{payment_symbol}",
                f"Maximum spend: {_format_amount(maximum_payment_amount)} "
                f"{payment_symbol}",
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
