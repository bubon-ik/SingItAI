import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any

from .numeric import format_decimal


SINGIT_DECIMALS = 18
DEFAULT_QUOTE_TTL_SECONDS = 120
SERVICE_FEE_BPS = 100


def calculate_service_fee(price_usd: Any) -> tuple[Decimal, Decimal]:
    price = Decimal(str(price_usd))
    if price <= 0:
        raise ValueError("product priceUsd must be positive")
    fee = price * Decimal(SERVICE_FEE_BPS) / Decimal(10_000)
    return fee, price + fee


def new_quote_id() -> str:
    return f"quote_{secrets.token_urlsafe(18)}"


def now_epoch() -> int:
    return int(time.time())


def iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_quote(
    *,
    request: dict[str, Any],
    product: dict[str, Any],
    singit_usd_price: str,
    quote_id: str | None = None,
    now_epoch: int | None = None,
    ttl_seconds: int = DEFAULT_QUOTE_TTL_SECONDS,
) -> dict[str, Any]:
    product_id = str(product["productId"])
    package_id = str(product["packageId"])
    if str(request.get("productId", "")).strip() != product_id:
        raise ValueError("selected Bitrefill product does not match request")
    if str(request.get("packageId", "")).strip() != package_id:
        raise ValueError("selected Bitrefill package does not match request")
    country = str(product.get("country") or request.get("country", "")).upper()
    value = str(product.get("packageValue", "")).strip()
    if not value:
        raise ValueError("packageValue is required")

    price_usd = Decimal(str(product.get("priceUsd", value)))
    singit_price = Decimal(str(singit_usd_price))
    service_fee_usd, total_usd = calculate_service_fee(price_usd)
    if singit_price <= 0:
        raise ValueError("singit_usd_price must be positive")

    singit_amount = (total_usd / singit_price).quantize(
        Decimal(1).scaleb(-SINGIT_DECIMALS),
        rounding=ROUND_CEILING,
    )
    max_singit_atomic = int(singit_amount * (Decimal(10) ** SINGIT_DECIMALS))
    started_at = int(now_epoch if now_epoch is not None else time.time())
    expires_at_epoch = started_at + int(ttl_seconds)
    product_name = str(product.get("name") or product_id)

    return {
        "quoteId": quote_id or new_quote_id(),
        "productId": product_id,
        "productName": product_name,
        "productType": str(product["productType"]),
        "packageId": package_id,
        "country": country,
        "currency": str(product.get("currency", "USD")),
        "packageValue": value,
        "priceUsd": f"{price_usd:.2f}",
        "singitUsdPrice": str(singit_price),
        "serviceFeeBps": SERVICE_FEE_BPS,
        "serviceFeeUsd": format_decimal(service_fee_usd),
        "totalUsd": format_decimal(total_usd),
        "singitAmount": format_decimal(singit_amount),
        "maxSingitAtomic": str(max_singit_atomic),
        "createdAtEpoch": started_at,
        "expiresAtEpoch": expires_at_epoch,
        "expiresAt": iso_from_epoch(expires_at_epoch),
        "quoteText": (
            f"{product_name} ${value}: pay up to {format_decimal(singit_amount)} SINGIT. "
            f"Quote expires in {ttl_seconds}s."
        ),
    }


def build_real_rate_quote(
    *,
    request: dict[str, Any],
    product: dict[str, Any],
    pricing: dict[str, Any],
    payment_token: dict[str, Any] | None = None,
    quote_id: str | None = None,
    now_epoch: int | None = None,
    ttl_seconds: int = DEFAULT_QUOTE_TTL_SECONDS,
) -> dict[str, Any]:
    product_id = str(product["productId"])
    package_id = str(product["packageId"])
    if str(request.get("productId", "")).strip() != product_id:
        raise ValueError("selected Bitrefill product does not match request")
    if str(request.get("packageId", "")).strip() != package_id:
        raise ValueError("selected Bitrefill package does not match request")
    price_usd = Decimal(str(product.get("priceUsd", product.get("packageValue", ""))))
    service_fee_usd, total_usd = calculate_service_fee(price_usd)
    if Decimal(str(pricing["targetUsdc"])) != total_usd:
        raise ValueError("pricing targetUsdc must equal Bitrefill totalUsd")
    required_amount_key = "requiredAmount" if payment_token is not None else "requiredSingit"
    required_atomic_key = (
        "requiredAmountAtomic" if payment_token is not None else "requiredSingitAtomic"
    )
    required_singit = Decimal(str(pricing[required_amount_key]))
    if required_singit <= 0:
        raise ValueError("requiredSingit must be positive")
    started_at = int(now_epoch if now_epoch is not None else time.time())
    expires_at_epoch = started_at + int(ttl_seconds)
    product_name = str(product.get("name") or product_id)
    value = str(product.get("packageValue", "")).strip()
    if not value:
        raise ValueError("packageValue is required")

    quote = {
        "quoteId": quote_id or new_quote_id(),
        "productId": product_id,
        "productName": product_name,
        "productType": str(product["productType"]),
        "packageId": package_id,
        "country": str(product.get("country") or request.get("country", "")).upper(),
        "currency": str(product.get("currency", "USD")),
        "packageValue": value,
        "priceUsd": f"{price_usd:.2f}",
        "serviceFeeBps": SERVICE_FEE_BPS,
        "serviceFeeUsd": format_decimal(service_fee_usd),
        "totalUsd": format_decimal(total_usd),
        "pricingMode": "bankr_real_rate",
        "requiredUsdc": str(pricing["targetUsdc"]),
        "bufferedTargetUsdc": str(pricing["bufferedTargetUsdc"]),
        "expectedUsdc": str(pricing["expectedUsdc"]),
        "minUsdc": str(pricing["minUsdc"]),
        "createdAtEpoch": started_at,
        "expiresAtEpoch": expires_at_epoch,
        "expiresAt": iso_from_epoch(expires_at_epoch),
    }
    if payment_token is None:
        quote.update(
            {
                "singitAmount": format_decimal(required_singit),
                "maxSingitAtomic": str(pricing[required_atomic_key]),
                "quoteText": (
                    f"{product_name} ${value}: pay {format_decimal(required_singit)} SINGIT "
                    f"at the real-rate Bankr route for about {pricing['expectedUsdc']} USDC. "
                    f"Quote expires in {ttl_seconds}s."
                ),
            }
        )
        return quote
    symbol = str(payment_token["symbol"])
    quote.update(
        {
            "paymentTokenAddress": str(payment_token["address"]),
            "paymentTokenSymbol": symbol,
            "paymentTokenDecimals": int(payment_token["decimals"]),
            "paymentTokenNative": bool(payment_token.get("native", False)),
            "paymentTokenAmount": format_decimal(required_singit),
            "maxPaymentTokenAtomic": str(pricing[required_atomic_key]),
            "quoteText": (
                f"{product_name} ${value}: pay up to {format_decimal(required_singit)} "
                f"{symbol} for about {pricing['expectedUsdc']} USDC. "
                f"Quote expires in {ttl_seconds}s."
            ),
        }
    )
    return quote


def build_purchase_commitment(
    quote: dict[str, Any],
    *,
    recipient: dict[str, Any] | None = None,
) -> dict[str, Any]:
    commitment = {
        "type": "singit-bitrefill-purchase",
        "quoteId": str(quote["quoteId"]),
        "productId": str(quote["productId"]),
        "productType": str(quote["productType"]),
        "packageId": str(quote["packageId"]),
        "packageValue": str(quote["packageValue"]),
        "priceUsd": str(quote["priceUsd"]),
        "recipientCommitment": recipient_commitment(recipient or {}),
        "expiresAt": str(quote["expiresAt"]),
    }
    if quote.get("serviceFeeBps") is not None:
        commitment.update(
            {
                "serviceFeeBps": int(quote["serviceFeeBps"]),
                "serviceFeeUsd": str(quote["serviceFeeUsd"]),
                "totalUsd": str(quote["totalUsd"]),
            }
        )
    if quote.get("maxPaymentTokenAtomic") is not None:
        commitment.update(
            {
                "paymentTokenAddress": str(quote["paymentTokenAddress"]),
                "paymentTokenSymbol": str(quote["paymentTokenSymbol"]),
                "paymentTokenDecimals": int(quote["paymentTokenDecimals"]),
                "paymentTokenNative": bool(quote.get("paymentTokenNative", False)),
                "maxPaymentTokenAtomic": str(quote["maxPaymentTokenAtomic"]),
            }
        )
    else:
        commitment["maxSingitAtomic"] = str(quote["maxSingitAtomic"])
    if quote.get("pricingMode"):
        commitment["pricingMode"] = str(quote["pricingMode"])
    if quote.get("requiredUsdc"):
        commitment["requiredUsdc"] = str(quote["requiredUsdc"])
    return commitment


def hash_purchase_commitment(commitment: dict[str, Any]) -> str:
    canonical = json.dumps(
        commitment,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def recipient_commitment(recipient: dict[str, Any]) -> str:
    canonical = json.dumps(
        recipient,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
