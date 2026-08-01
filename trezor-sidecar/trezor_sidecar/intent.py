import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from .models import PurchaseIntent


_MESSAGE_HEADER = "Sign402 purchase"
# Both USD micros and USDC atomic units carry six decimals, so a fixed six
# places is exact for either and never rounds a commitment away.
_DECIMALS = 6


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


def _fixed(units: int) -> str:
    if type(units) is not int or units < 0:
        raise ValueError("amount must be a non-negative integer")
    scale = 10**_DECIMALS
    return f"{units // scale}.{units % scale:0{_DECIMALS}d}"


def build_intent_message(intent: PurchaseIntent) -> str:
    """Build the exact text the buyer approves on the Trezor screen.

    A Safe 3 signing EIP-712 through Trezor Suite MCP shows only the domain
    and the account, so the buyer cannot see what they are paying for. A
    plain message renders its text, so the commitment is carried as readable
    lines instead.

    The human-meaningful lines come first, before the hashes, because the
    device is paged through a few lines at a time. The text is canonical:
    verification rebuilds it byte for byte, so changing any field invalidates
    the signature.
    """
    if not isinstance(intent, PurchaseIntent):
        raise ValueError("intent must be a PurchaseIntent")
    return "\n".join(
        (
            _MESSAGE_HEADER,
            f"Product: {intent.product_slug}",
            f"Item: {intent.denomination}",
            f"Price: {_fixed(intent.quoted_total_usd_micros)} USD",
            f"Max pay: {_fixed(intent.max_payment_usdc_atomic)} USDC",
            f"Asset: {intent.payment_asset} on {intent.payment_network}",
            f"Package: {intent.package_id}",
            f"Recipient: {intent.recipient_hash}",
            f"Intent: {intent.intent_id}",
            f"Expires: {intent.expires_at}",
        )
    )


def recover_intent_signer(intent: PurchaseIntent, signature: str) -> str:
    if not isinstance(signature, str) or not signature:
        raise ValueError("signature must be a non-empty string")
    signable_message = encode_defunct(text=build_intent_message(intent))
    return Account.recover_message(signable_message, signature=signature)
