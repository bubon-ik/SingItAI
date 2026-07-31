import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data

from .models import PurchaseIntent


_DOMAIN = {
    "name": "SingIt Trezor Purchase",
    "version": "1",
    "chainId": 8453,
}

_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "PurchaseIntent": [
        {"name": "intentId", "type": "bytes32"},
        {"name": "productSlug", "type": "string"},
        {"name": "packageId", "type": "string"},
        {"name": "denomination", "type": "string"},
        {"name": "quotedTotalUsdMicros", "type": "uint256"},
        {"name": "maxPaymentUsdcAtomic", "type": "uint256"},
        {"name": "paymentAsset", "type": "string"},
        {"name": "paymentNetwork", "type": "string"},
        {"name": "recipientHash", "type": "bytes32"},
        {"name": "expiresAt", "type": "uint64"},
    ],
}


def _canonical_recipient_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, Mapping):
        if depth > 1:
            raise ValueError("recipient contains nested mappings or lists deeper than one level")
        if not all(isinstance(key, str) for key in value):
            raise ValueError("recipient keys must be strings")
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            normalized[key] = _canonical_recipient_value(value[key], depth + 1)
        return normalized
    if isinstance(value, list):
        if depth > 1:
            raise ValueError("recipient contains nested mappings or lists deeper than one level")
        return [_canonical_recipient_value(item, depth + 1) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("recipient numeric values must be finite")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("recipient values must be JSON scalar values, mappings, or lists")


def recipient_hash(recipient: Mapping[str, Any]) -> str:
    if not isinstance(recipient, Mapping):
        raise ValueError("recipient must be a mapping")
    canonical = _canonical_recipient_value(recipient)
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return "0x" + hashlib.sha256(payload).hexdigest()


def build_typed_data(intent: PurchaseIntent) -> dict[str, Any]:
    if not isinstance(intent, PurchaseIntent):
        raise ValueError("intent must be a PurchaseIntent")
    return {
        "types": {
            type_name: [dict(field) for field in type_fields]
            for type_name, type_fields in _TYPES.items()
        },
        "primaryType": "PurchaseIntent",
        "domain": dict(_DOMAIN),
        "message": {
            "intentId": intent.intent_id,
            "productSlug": intent.product_slug,
            "packageId": intent.package_id,
            "denomination": intent.denomination,
            "quotedTotalUsdMicros": intent.quoted_total_usd_micros,
            "maxPaymentUsdcAtomic": intent.max_payment_usdc_atomic,
            "paymentAsset": intent.payment_asset,
            "paymentNetwork": intent.payment_network,
            "recipientHash": intent.recipient_hash,
            "expiresAt": intent.expires_at,
        },
    }


def recover_intent_signer(intent: PurchaseIntent, signature: str) -> str:
    if not isinstance(signature, str) or not signature:
        raise ValueError("signature must be a non-empty string")
    signable_message = encode_typed_data(full_message=build_typed_data(intent))
    return Account.recover_message(signable_message, signature=signature)
