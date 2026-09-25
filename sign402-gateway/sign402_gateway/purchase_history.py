"""Private purchase history with encrypted reveal capabilities and safe receipt views."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .secure_state import SensitiveStateCipher, SensitiveStateConfigurationError, SensitiveStateError, atomic_write_private_json


def purchase_id(event):
    # Provider quote/transaction ids are stable across reconciliation updates.
    identity = event.get("quoteId") or event.get("txId") or event.get("transactionHash")
    if not identity:
        identity = json.dumps({k: event.get(k) for k in ("toolId", "resourceUrl", "timestamp", "telegramText")}, sort_keys=True)
    return hashlib.sha256(str(identity).encode()).hexdigest()[:24]


def purchase_summary(event):
    # An allowlist, not a filtered copy: never return provider snapshots,
    # Telegram result text, buyer email, redemption or fulfillment credentials.
    receipt = event.get("receipt") if isinstance(event.get("receipt"), dict) else {}
    provider = event.get("bitrefill") if isinstance(event.get("bitrefill"), dict) else {}
    allowance_order = event.get("mode") == "bitrefill_x402_allowance"  # its code is fetched once from Bitrefill
    bitrefill = ("bitrefill" in event and bool(event.get("quoteId"))) or allowance_order
    name = str(receipt.get("name") or event.get("productName") or provider.get("productName")
               or event.get("toolName") or event.get("toolId") or ("Bitrefill order" if bitrefill else "Purchase"))[:120]
    network = str(receipt.get("network") or event.get("network") or "Base")
    tx = str(receipt.get("txId") or event.get("txId") or event.get("transactionHash") or "")
    url = ""
    if network.lower() in {"base", "eip155:8453"} and re.fullmatch(r"0x[0-9a-fA-F]{64}", tx):
        url = f"https://basescan.org/tx/{tx}"
        network = "Base"
    elif network.lower() == "solana" and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{87,88}", tx):
        url = f"https://solscan.io/tx/{tx}"
        network = "Solana"
    return {"id": purchase_id(event), "name": name,
            "denomination": str(receipt.get("denomination") or "")[:80],
            "paid": str(receipt.get("paid") or "")[:80], "network": network[:64],
            "status": str(receipt.get("status") or provider.get("status") or "Completed")[:64],
            "recordedAt": str(event.get("_recordedAt") or ""), "transactionUrl": url,
            "isBitrefill": bitrefill,
            "canReveal": allowance_order or (bitrefill and bool(event.get("encryptedFulfillmentToken")
                                                              or event.get("fulfillmentToken")))}


class UserPurchaseStore:
    """Per-user purchase history, kept OUT of the public global event store.

    Purchases made from a user's managed wallet carry their telegram id, wallet
    address and payment details. The global LatestEventStore is served
    unauthenticated by /events/latest (the demo dashboard), so per-user
    purchases are persisted here instead, keyed by telegram user id, and read
    back only through the token-gated /agent/last-purchase.
    """

    def __init__(
        self,
        path: Path,
        *,
        cipher: SensitiveStateCipher | None = None,
    ) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.cipher = cipher

    def _read_all_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _assert_no_legacy_tokens(data: dict[str, Any]) -> None:
        def contains_plaintext(value):
            if isinstance(value, dict):
                return "fulfillmentToken" in value or any(contains_plaintext(v) for v in value.values())
            if isinstance(value, list):
                return any(contains_plaintext(v) for v in value)
            return False
        if contains_plaintext(data):
            raise SensitiveStateError(
                "legacy plaintext fulfillment tokens must be migrated "
                "before updating user purchase state"
            )

    def _persisted_event(self, event: dict[str, Any]) -> dict[str, Any]:
        persisted = dict(event)
        persisted.pop("_history", None)
        persisted.pop("_recordedAt", None)
        persisted.pop("encryptedFulfillmentToken", None)
        if "fulfillmentToken" in persisted:
            if self.cipher is None:
                raise SensitiveStateConfigurationError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to persist fulfillment tokens"
                )
            token = str(persisted.pop("fulfillmentToken"))
            persisted["encryptedFulfillmentToken"] = self.cipher.encrypt_text(token)
        return persisted

    def preflight_write(self) -> None:
        with self.lock:
            if self.cipher is None:
                raise SensitiveStateConfigurationError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to persist user purchase state"
                )
            self._assert_no_legacy_tokens(self._read_all_unlocked())

    def write(
        self,
        telegram_user_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        key = str(telegram_user_id)
        with self.lock:
            data = self._read_all_unlocked()
            # Refuse copying legacy plaintext, including this user's old token.
            self._assert_no_legacy_tokens(data)
            previous = data.get(key)
            history = []
            if isinstance(previous, dict):
                history = [dict(previous, _history=None)] + list(previous.get("_history") or [])
                history[0].pop("_history", None)
            persisted = self._persisted_event(event)
            record_id = purchase_id(persisted)
            history = [item for item in history if purchase_id(item) != record_id][:99]
            persisted["_recordedAt"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            if history:
                persisted["_history"] = history
            data[key] = persisted
            self._assert_no_legacy_tokens(data)
            atomic_write_private_json(self.path, data)
            return event

    def read(self, telegram_user_id: str, record_id: str | None = None) -> dict[str, Any] | None:
        with self.lock:
            value = self._read_all_unlocked().get(str(telegram_user_id))
            if not isinstance(value, dict):
                return None
            if record_id is not None:
                value = next((item for item in [value] + list(value.get("_history") or [])
                              if purchase_id(item) == record_id), None)
                if value is None:
                    return None
            event = dict(value)
            event.pop("_history", None)
            event.pop("_recordedAt", None)
            if "encryptedFulfillmentToken" in event:
                encrypted = event.pop("encryptedFulfillmentToken")
                if self.cipher is None:
                    raise SensitiveStateConfigurationError(
                        "SIGN402_WALLET_MASTER_KEY is required "
                        "to read encrypted fulfillment tokens"
                    )
                event.pop("fulfillmentToken", None)
                event["fulfillmentToken"] = self.cipher.decrypt_text(str(encrypted))
            return event

    def summaries(self, telegram_user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            value = self._read_all_unlocked().get(str(telegram_user_id))
            if not isinstance(value, dict):
                return []
            return [purchase_summary(item) for item in [value] + list(value.get("_history") or []) if item.get("ok")]

    def clear_fulfillment_token(self, telegram_user_id: str, record_id: str | None = None) -> None:
        key = str(telegram_user_id)
        with self.lock:
            data = self._read_all_unlocked()
            latest = data.get(key)
            if not isinstance(latest, dict):
                return
            target = record_id or purchase_id(latest)
            for item in [latest] + list(latest.get("_history") or []):
                if purchase_id(item) == target:
                    item.pop("fulfillmentToken", None)
                    item.pop("encryptedFulfillmentToken", None)
            self._assert_no_legacy_tokens(data)
            atomic_write_private_json(self.path, data)

