from copy import deepcopy
import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .secure_state import (
    SensitiveStateCipher,
    SensitiveStateConfigurationError,
    SensitiveStateError,
    ensure_private_directory,
    ensure_private_file,
)


STATE_ORDER = {
    "QUOTED": 10,
    "FIREFLY_APPROVED": 20,
    "USER_APPROVED": 20,
    "SINGIT_AUTHORIZED": 30,
    "SINGIT_SETTLED": 35,
    "FULFILLING": 40,
    "BITREFILL_PURCHASED": 50,
    "DELIVERED": 70,
    "QUOTE_EXPIRED": 900,
    "FIREFLY_REJECTED": 901,
    "USER_REJECTED": 901,
    "FULFILLMENT_FAILED": 902,
    "RECONCILIATION_REQUIRED": 903,
    "REFUND_REQUIRED": 904,
}


def _bounded_scalar(value: Any, *, field: str, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(
        value,
        (str, int, float, Decimal),
    ):
        raise ValueError(f"{field} must be a scalar value")
    text = str(value).strip()
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return text


def _copy_scalar(
    target: dict[str, Any],
    source: Mapping[str, Any],
    key: str,
    *,
    field: str,
    limit: int = 512,
    aliases: tuple[str, ...] = (),
) -> None:
    source_key = next((name for name in (key, *aliases) if name in source), None)
    if source_key is None:
        return
    target[key] = _bounded_scalar(source[source_key], field=field, limit=limit)


def _copy_bool(
    target: dict[str, Any],
    source: Mapping[str, Any],
    key: str,
    *,
    field: str,
) -> None:
    if key not in source:
        return
    value = source[key]
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    target[key] = value


def _treasury_snapshot(
    value: Any,
    *,
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    result: dict[str, Any] = {}
    _copy_scalar(
        result,
        value,
        "txId",
        field=f"{field}.txId",
        aliases=("transactionHash", "hash"),
    )
    _copy_scalar(
        result,
        value,
        "network",
        field=f"{field}.network",
        aliases=("chain",),
    )
    _copy_scalar(
        result,
        value,
        "asset",
        field=f"{field}.asset",
        aliases=("currency", "token"),
    )
    _copy_scalar(result, value, "amount", field=f"{field}.amount")
    _copy_scalar(
        result,
        value,
        "amountAtomic",
        field=f"{field}.amountAtomic",
    )
    return result


def sanitize_bitrefill_provider_snapshot(
    provider_result: Mapping[str, Any],
    quote: dict[str, Any],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key in (
        "provider",
        "invoiceId",
        "orderId",
        "status",
        "paymentMethod",
        "createdAt",
        "updatedAt",
        "expiresAt",
    ):
        _copy_scalar(
            snapshot,
            provider_result,
            key,
            field=f"bitrefill.{key}",
        )
    if "status" in snapshot:
        snapshot["status"] = snapshot["status"].lower()
    for key in ("productId", "packageId", "packageValue"):
        if key in quote:
            snapshot[key] = _bounded_scalar(
                quote[key],
                field=f"bitrefill.{key}",
                limit=512,
            )
    if "treasuryPayment" in provider_result:
        snapshot["treasuryPayment"] = _treasury_snapshot(
            provider_result["treasuryPayment"],
            field="bitrefill.treasuryPayment",
        )
    return snapshot


def sanitize_bitrefill_checkpoint(
    checkpoint: Mapping[str, Any],
    quote: dict[str, Any],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key in ("invoiceId", "status"):
        _copy_scalar(
            snapshot,
            checkpoint,
            key,
            field=f"bitrefillCheckpoint.{key}",
        )
    if "status" in snapshot:
        snapshot["status"] = snapshot["status"].lower()
    if "orderIds" in checkpoint:
        order_ids = checkpoint["orderIds"]
        if not isinstance(order_ids, list):
            raise ValueError("bitrefillCheckpoint.orderIds must be a list")
        validated_order_ids = [
            _bounded_scalar(
                value,
                field=f"bitrefillCheckpoint.orderIds[{index}]",
                limit=512,
            )
            for index, value in enumerate(order_ids)
        ]
        snapshot["orderIds"] = validated_order_ids[:16]
    for key in ("productId", "packageId", "packageValue"):
        if key in quote:
            snapshot[key] = _bounded_scalar(
                quote[key],
                field=f"bitrefillCheckpoint.{key}",
                limit=512,
            )
    if "paymentInfo" in checkpoint:
        payment = checkpoint["paymentInfo"]
        if not isinstance(payment, Mapping):
            raise ValueError("bitrefillCheckpoint.paymentInfo must be an object")
        safe_payment: dict[str, Any] = {}
        _copy_scalar(
            safe_payment,
            payment,
            "amount",
            field="bitrefillCheckpoint.paymentInfo.amount",
        )
        _copy_scalar(
            safe_payment,
            payment,
            "asset",
            field="bitrefillCheckpoint.paymentInfo.asset",
        )
        _copy_scalar(
            safe_payment,
            payment,
            "network",
            field="bitrefillCheckpoint.paymentInfo.network",
        )
        snapshot["paymentInfo"] = safe_payment
    if "treasuryPayment" in checkpoint:
        snapshot["treasuryPayment"] = _treasury_snapshot(
            checkpoint["treasuryPayment"],
            field="bitrefillCheckpoint.treasuryPayment",
        )
    return snapshot


def sanitize_bankr_reconciliation_snapshot(
    bankr_result: Mapping[str, Any],
    quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del quote
    snapshot: dict[str, Any] = {}
    _copy_bool(snapshot, bankr_result, "ok", field="bankr.ok")
    _copy_scalar(
        snapshot,
        bankr_result,
        "status",
        field="bankr.status",
    )
    _copy_scalar(
        snapshot,
        bankr_result,
        "transactionHash",
        field="bankr.transactionHash",
        aliases=("txHash", "transaction", "txId"),
    )
    _copy_scalar(
        snapshot,
        bankr_result,
        "startBlock",
        field="bankr.startBlock",
    )
    if "paymentMade" in bankr_result:
        payment = bankr_result["paymentMade"]
        if not isinstance(payment, Mapping):
            raise ValueError("bankr.paymentMade must be an object")
        safe_payment: dict[str, Any] = {}
        _copy_scalar(
            safe_payment,
            payment,
            "network",
            field="bankr.paymentMade.network",
        )
        _copy_scalar(
            safe_payment,
            payment,
            "transactionHash",
            field="bankr.paymentMade.transactionHash",
            aliases=("txHash", "transaction", "txId"),
        )
        for key in (
            "payTo",
            "amountUsd",
            "amount",
            "amountAtomic",
            "asset",
        ):
            _copy_scalar(
                safe_payment,
                payment,
                key,
                field=f"bankr.paymentMade.{key}",
            )
        snapshot["paymentMade"] = safe_payment
    return snapshot


def _assert_reserved_snapshots_are_canonical(
    metadata: dict[str, Any],
    quote: dict[str, Any],
) -> None:
    for key, sanitizer in (
        ("bitrefill", sanitize_bitrefill_provider_snapshot),
        ("bitrefillCheckpoint", sanitize_bitrefill_checkpoint),
        ("bankr", sanitize_bankr_reconciliation_snapshot),
    ):
        if key not in metadata:
            continue
        value = metadata[key]
        error = (
            f"unsafe legacy {key} snapshot must be migrated "
            "before updating Bitrefill order state"
        )
        if not isinstance(value, Mapping):
            raise SensitiveStateError(error)
        try:
            canonical = sanitizer(value, quote)
        except (TypeError, ValueError):
            raise SensitiveStateError(error) from None
        if dict(value) != canonical:
            raise SensitiveStateError(error)


class BitrefillCommerceStore:
    def __init__(
        self,
        path: Path,
        *,
        cipher: SensitiveStateCipher | None = None,
    ):
        self.path = path
        self.cipher = cipher
        self.lock = threading.Lock()
        ensure_private_directory(self.path.parent)
        self._prepare_private_database_file()
        self._init_db()
        ensure_private_file(self.path)

    def require_sensitive_state_cipher(self) -> None:
        if self.cipher is None:
            raise SensitiveStateConfigurationError(
                "SIGN402_WALLET_MASTER_KEY is required "
                "for managed-wallet Bitrefill purchases"
            )

    def save_quote(self, quote: dict[str, Any]) -> None:
        quote_id = str(quote["quoteId"])
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO bitrefill_orders (quote_id, state, quote_json, updated_at)
                VALUES (?, 'QUOTED', ?, ?)
                ON CONFLICT(quote_id) DO NOTHING
                """,
                (quote_id, _dumps(quote), int(time.time())),
            )

    def get_quote(self, quote_id: str) -> dict[str, Any]:
        with self.lock, self._database() as db:
            row = db.execute(
                """
                SELECT quote_id, state, quote_json, metadata_json
                FROM bitrefill_orders
                WHERE quote_id = ?
                """,
                (quote_id,),
            ).fetchone()
        if row is None:
            raise ValueError("quote not found")
        return {
            "quoteId": row["quote_id"],
            "state": row["state"],
            "quote": json.loads(row["quote_json"]),
            "metadata": self._decoded_metadata(
                json.loads(row["metadata_json"] or "{}")
            ),
        }

    def advance_state(
        self,
        quote_id: str,
        new_state: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if new_state not in STATE_ORDER:
            raise ValueError(f"unknown order state: {new_state}")
        with self.lock, self._database() as db:
            row = db.execute(
                "SELECT state, quote_json, metadata_json "
                "FROM bitrefill_orders WHERE quote_id = ?",
                (quote_id,),
            ).fetchone()
            if row is None:
                raise ValueError("quote not found")
            old_state = str(row["state"])
            if STATE_ORDER[new_state] < STATE_ORDER[old_state]:
                raise ValueError("cannot move order state backward")
            existing = json.loads(row["metadata_json"] or "{}")
            quote = json.loads(row["quote_json"])
            _assert_reserved_snapshots_are_canonical(existing, quote)
            if "recipient" in existing:
                raise SensitiveStateError(
                    "legacy plaintext recipient must be migrated "
                    "before updating Bitrefill order state"
                )
            update = self._encoded_metadata_update(
                metadata or {},
                quote,
            )
            if "encryptedRecipient" in update:
                existing.pop("recipient", None)
            existing.update(update)
            db.execute(
                """
                UPDATE bitrefill_orders
                SET state = ?, metadata_json = ?, updated_at = ?
                WHERE quote_id = ?
                """,
                (new_state, _dumps(existing), int(time.time()), quote_id),
            )

    def checkpoint(self, quote_id: str, metadata: dict[str, Any]) -> None:
        with self.lock, self._database() as db:
            row = db.execute(
                "SELECT quote_json, metadata_json "
                "FROM bitrefill_orders WHERE quote_id = ?",
                (quote_id,),
            ).fetchone()
            if row is None:
                raise ValueError("quote not found")
            existing = json.loads(row["metadata_json"] or "{}")
            quote = json.loads(row["quote_json"])
            _assert_reserved_snapshots_are_canonical(existing, quote)
            if "recipient" in existing:
                raise SensitiveStateError(
                    "legacy plaintext recipient must be migrated "
                    "before updating Bitrefill order state"
                )
            update = self._encoded_metadata_update(
                metadata or {},
                quote,
            )
            if "encryptedRecipient" in update:
                existing.pop("recipient", None)
            existing.update(update)
            db.execute(
                """
                UPDATE bitrefill_orders
                SET metadata_json = ?, updated_at = ?
                WHERE quote_id = ?
                """,
                (_dumps(existing), int(time.time()), quote_id),
            )

    def try_mark_fulfilling(self, quote_id: str) -> bool:
        # Claim the order with a single conditional UPDATE so the check-and-set
        # is atomic at the database level. A shared threading.Lock only
        # serializes within one process; the UPDATE...WHERE guarded by rowcount
        # keeps concurrent processes from double-fulfilling the same order.
        claimable_states = [
            state
            for state, order in STATE_ORDER.items()
            if order < STATE_ORDER["BITREFILL_PURCHASED"] and state != "FULFILLING"
        ]
        placeholders = ",".join("?" for _ in claimable_states)
        with self.lock, self._database() as db:
            row = db.execute(
                "SELECT state, quote_json, metadata_json "
                "FROM bitrefill_orders WHERE quote_id = ?",
                (quote_id,),
            ).fetchone()
            if row is None:
                raise ValueError("quote not found")
            metadata = json.loads(row["metadata_json"] or "{}")
            _assert_reserved_snapshots_are_canonical(
                metadata,
                json.loads(row["quote_json"]),
            )
            if "recipient" in metadata:
                raise SensitiveStateError(
                    "legacy plaintext recipient must be migrated "
                    "before updating Bitrefill order state"
                )
            cursor = db.execute(
                f"""
                UPDATE bitrefill_orders
                SET state = 'FULFILLING', updated_at = ?
                WHERE quote_id = ? AND state IN ({placeholders})
                AND json_type(metadata_json, '$.recipient') IS NULL
                """,
                (int(time.time()), quote_id, *claimable_states),
            )
            if cursor.rowcount == 1:
                return True
            return False

    def _prepare_private_database_file(self) -> None:
        if self.path.exists():
            ensure_private_file(self.path)
            return
        try:
            fd = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
        except FileExistsError:
            ensure_private_file(self.path)
        else:
            os.close(fd)
            ensure_private_file(self.path)

    def _encoded_metadata_update(
        self,
        metadata: dict[str, Any],
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        update = deepcopy(metadata)
        if "encryptedRecipient" in update:
            raise SensitiveStateError(
                "encryptedRecipient is reserved for the commerce store"
            )
        if "recipient" in update:
            if self.cipher is None:
                raise SensitiveStateError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to persist Bitrefill recipient state"
                )
            recipient = update.pop("recipient")
            if not isinstance(recipient, dict):
                raise ValueError("recipient must be an object")
            update["encryptedRecipient"] = self.cipher.encrypt_json(recipient)
        for key, sanitizer in (
            ("bitrefill", sanitize_bitrefill_provider_snapshot),
            ("bitrefillCheckpoint", sanitize_bitrefill_checkpoint),
            ("bankr", sanitize_bankr_reconciliation_snapshot),
        ):
            if key not in update:
                continue
            value = update[key]
            if not isinstance(value, Mapping):
                raise ValueError(f"{key} must be an object")
            update[key] = sanitizer(value, quote)
        return update

    def _decoded_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        decoded = deepcopy(metadata)
        if "encryptedRecipient" in decoded:
            encrypted = decoded.pop("encryptedRecipient")
            if self.cipher is None:
                raise SensitiveStateError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to read Bitrefill recipient state"
                )
            decoded.pop("recipient", None)
            decoded["recipient"] = self.cipher.decrypt_json(str(encrypted))
        return decoded

    def _init_db(self) -> None:
        with self._database() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bitrefill_orders (
                    quote_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    quote_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        # timeout makes a connection wait (rather than immediately erroring)
        # when another process holds the write lock, so concurrent claims
        # serialize instead of raising "database is locked".
        db = sqlite3.connect(str(self.path), timeout=5.0)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
