"""Isolated SQLite state for the local Trezor sidecar.

The schema deliberately contains only the minimum durable state needed to
coordinate approvals and payments. Sensitive signing payloads and personal
fulfilment data never enter this store.
"""

import errno
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import IntentRecord, Pairing, PaymentRequest, PaymentState, PaymentView, PurchaseIntent


_SQLITE_INT_MAX = (1 << 63) - 1
_UINT256_MAX = (1 << 256) - 1
_TEXT_LIMIT = 256
_DERIVATION_LIMIT = 160
_DENOMINATION_LIMIT = 128
_TX_HASH_LIMIT = 128

_PAYMENT_EDGES = {
    PaymentState.INVOICE_CREATED: {
        PaymentState.TX_SIGNED,
        PaymentState.CANCELLED,
        PaymentState.FAILED,
    },
    PaymentState.TX_SIGNED: {
        PaymentState.TX_BROADCAST,
        PaymentState.FAILED,
        PaymentState.RECONCILIATION_REQUIRED,
    },
    PaymentState.TX_BROADCAST: {
        PaymentState.COMPLETE,
        PaymentState.RECONCILIATION_REQUIRED,
    },
}


def _text(value: object, name: str, *, limit: int = _TEXT_LIMIT) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty bounded string")
    return value


def _timestamp(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _SQLITE_INT_MAX:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _amount(value: object, name: str) -> tuple[int, str]:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    if isinstance(value, int):
        numeric = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        numeric = int(value)
    else:
        raise ValueError(f"{name} must be a positive integer")
    if not 0 < numeric <= _UINT256_MAX:
        raise ValueError(f"{name} must be a positive integer")
    return numeric, str(numeric)


class SidecarStore:
    """A per-sidecar SQLite database with short transactions and CAS updates.

    The final state directory is either created by this class as ``0700`` or
    must already be a same-user, non-symlink directory with exactly that mode.
    The database is created with ``O_EXCL`` and mode ``0600`` before SQLite
    opens it. Existing database paths are checked through a no-follow
    descriptor before every connection. This makes any symlink or unsafe file
    a fail-closed error, rather than allowing SQLite or chmod to follow it.
    """

    def __init__(self, path: Path):
        if not isinstance(path, Path):
            raise ValueError("path must be a Path")
        self.path = Path(os.path.abspath(os.fspath(path)))
        self._prepare_database()
        self._initialize()

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    @staticmethod
    def _validate_state_directory(info: os.stat_result) -> None:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("state parent must be a real directory, not a symlink")
        if info.st_uid != os.getuid():
            raise ValueError("state parent must be owned by the current user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("state parent must have mode 0700")

    @staticmethod
    def _validate_database(info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("state database must be a regular file, not a symlink")
        if info.st_uid != os.getuid():
            raise ValueError("state database must be owned by the current user")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("state database must have mode 0600")

    def _state_directory_fd(self) -> int:
        """Open the final state directory without traversing a symlink.

        Only the final component may be created, so callers cannot accidentally
        chmod or create through an arbitrary existing state parent. Ancestors
        are left alone: platform-managed paths can legitimately contain an
        ancestor symlink, but the supplied state directory itself may not.
        """
        parent = self.path.parent
        try:
            descriptor = os.open(parent, self._directory_flags())
        except FileNotFoundError:
            try:
                ancestor = os.open(parent.parent, self._directory_flags())
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError("state parent must be a real directory, not a symlink") from None
                raise
            try:
                os.mkdir(parent.name, 0o700, dir_fd=ancestor)
                descriptor = os.open(parent.name, self._directory_flags(), dir_fd=ancestor)
            finally:
                os.close(ancestor)
            os.fchmod(descriptor, 0o700)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError("state parent must be a real directory, not a symlink") from None
            raise
        self._validate_state_directory(os.fstat(descriptor))
        return descriptor

    def _prepare_database(self) -> None:
        directory = self._state_directory_fd()
        try:
            try:
                info = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                descriptor = os.open(
                    self.path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    os.fchmod(descriptor, 0o600)
                    self._validate_database(os.fstat(descriptor))
                finally:
                    os.close(descriptor)
                return
            self._validate_database(info)
            descriptor = os.open(
                self.path.name,
                os.O_RDWR | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            try:
                self._validate_database(os.fstat(descriptor))
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)

    def _connect(self) -> sqlite3.Connection:
        self._prepare_database()
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pairings (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    pairing_id TEXT NOT NULL UNIQUE CHECK (length(pairing_id) BETWEEN 1 AND 256),
                    address TEXT NOT NULL CHECK (length(address) BETWEEN 1 AND 256),
                    derivation_path TEXT NOT NULL CHECK (length(derivation_path) BETWEEN 1 AND 160),
                    created_at INTEGER NOT NULL CHECK (created_at > 0),
                    updated_at INTEGER NOT NULL CHECK (updated_at > 0)
                );

                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY CHECK (length(intent_id) = 66),
                    product_slug TEXT NOT NULL CHECK (length(product_slug) BETWEEN 1 AND 256),
                    package_id TEXT NOT NULL CHECK (length(package_id) BETWEEN 1 AND 256),
                    denomination TEXT NOT NULL CHECK (length(denomination) BETWEEN 1 AND 128),
                    quoted_total_usd_micros TEXT NOT NULL CHECK (length(quoted_total_usd_micros) BETWEEN 1 AND 78),
                    max_payment_usdc_atomic TEXT NOT NULL CHECK (length(max_payment_usdc_atomic) BETWEEN 1 AND 78),
                    commitment TEXT NOT NULL CHECK (length(commitment) = 66),
                    expires_at INTEGER NOT NULL CHECK (expires_at > 0),
                    state TEXT NOT NULL CHECK (length(state) BETWEEN 1 AND 64),
                    created_at INTEGER NOT NULL CHECK (created_at > 0),
                    approved_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS payments (
                    payment_id TEXT PRIMARY KEY CHECK (length(payment_id) BETWEEN 1 AND 256),
                    intent_id TEXT NOT NULL UNIQUE CHECK (length(intent_id) = 66)
                        REFERENCES intents(intent_id),
                    invoice_id TEXT NOT NULL UNIQUE CHECK (length(invoice_id) BETWEEN 1 AND 256),
                    idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 256),
                    pay_to TEXT NOT NULL CHECK (length(pay_to) BETWEEN 1 AND 256),
                    amount_atomic TEXT NOT NULL CHECK (length(amount_atomic) BETWEEN 1 AND 78),
                    expires_at INTEGER NOT NULL CHECK (expires_at > 0),
                    state TEXT NOT NULL CHECK (length(state) BETWEEN 1 AND 64),
                    created_at INTEGER NOT NULL CHECK (created_at > 0),
                    updated_at INTEGER NOT NULL CHECK (updated_at > 0),
                    tx_hash TEXT CHECK (tx_hash IS NULL OR length(tx_hash) BETWEEN 1 AND 128)
                );

                CREATE TABLE IF NOT EXISTS purchase_log (
                    invoice_id TEXT PRIMARY KEY CHECK (length(invoice_id) BETWEEN 1 AND 256)
                        REFERENCES payments(invoice_id),
                    product_slug TEXT NOT NULL CHECK (length(product_slug) BETWEEN 1 AND 256),
                    amount TEXT NOT NULL CHECK (length(amount) BETWEEN 1 AND 78),
                    payment_method TEXT NOT NULL CHECK (length(payment_method) BETWEEN 1 AND 256),
                    timestamp INTEGER NOT NULL CHECK (timestamp > 0)
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _pairing(row: sqlite3.Row) -> Pairing:
        return Pairing(
            pairing_id=row["pairing_id"],
            address=row["address"],
            derivation_path=row["derivation_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _intent(row: sqlite3.Row) -> IntentRecord:
        return IntentRecord(
            intent=PurchaseIntent(
                intent_id=row["intent_id"],
                product_slug=row["product_slug"],
                package_id=row["package_id"],
                denomination=row["denomination"],
                quoted_total_usd_micros=int(row["quoted_total_usd_micros"]),
                max_payment_usdc_atomic=int(row["max_payment_usdc_atomic"]),
                recipient_hash=row["commitment"],
                expires_at=row["expires_at"],
            ),
            state=PaymentState(row["state"]),
            created_at=row["created_at"],
            approved_at=row["approved_at"],
        )

    @staticmethod
    def _payment(row: sqlite3.Row) -> PaymentView:
        return PaymentView(
            payment_id=row["payment_id"],
            intent_id=row["intent_id"],
            invoice_id=row["invoice_id"],
            state=PaymentState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tx_hash=row["tx_hash"],
        )

    def save_pairing(self, pairing: Pairing, *, allow_repair: bool = False) -> Pairing:
        if not isinstance(pairing, Pairing):
            raise ValueError("pairing must be a Pairing")
        if not isinstance(allow_repair, bool):
            raise ValueError("allow_repair must be a boolean")
        _text(pairing.pairing_id, "pairing_id")
        _text(pairing.derivation_path, "derivation_path", limit=_DERIVATION_LIMIT)
        _timestamp(pairing.created_at, "created_at")
        _timestamp(pairing.updated_at, "updated_at")
        if pairing.updated_at < pairing.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        with self._transaction() as connection:
            current_row = connection.execute("SELECT * FROM pairings WHERE singleton = 1").fetchone()
            if current_row is None:
                connection.execute(
                    """INSERT INTO pairings
                    (singleton, pairing_id, address, derivation_path, created_at, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?)""",
                    (
                        pairing.pairing_id,
                        pairing.address,
                        pairing.derivation_path,
                        pairing.created_at,
                        pairing.updated_at,
                    ),
                )
                return pairing
            current = self._pairing(current_row)
            same_device = (
                current.pairing_id == pairing.pairing_id
                and current.address == pairing.address
                and current.derivation_path == pairing.derivation_path
            )
            if same_device:
                if pairing.updated_at > current.updated_at:
                    connection.execute(
                        "UPDATE pairings SET updated_at = ? WHERE singleton = 1",
                        (pairing.updated_at,),
                    )
                    return Pairing(
                        pairing_id=current.pairing_id,
                        address=current.address,
                        derivation_path=current.derivation_path,
                        created_at=current.created_at,
                        updated_at=pairing.updated_at,
                    )
                return current
            if not allow_repair:
                raise ValueError("different Trezor pairing requires explicit repair")
            if pairing.updated_at < current.updated_at:
                raise ValueError("updated_at cannot move backwards")
            connection.execute(
                """UPDATE pairings
                SET pairing_id = ?, address = ?, derivation_path = ?, created_at = ?, updated_at = ?
                WHERE singleton = 1""",
                (
                    pairing.pairing_id,
                    pairing.address,
                    pairing.derivation_path,
                    pairing.created_at,
                    pairing.updated_at,
                ),
            )
            return pairing

    def get_pairing(self) -> Pairing | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM pairings WHERE singleton = 1").fetchone()
        finally:
            connection.close()
        return None if row is None else self._pairing(row)

    def insert_intent(self, intent: PurchaseIntent, *, created_at: int) -> IntentRecord:
        if not isinstance(intent, PurchaseIntent):
            raise ValueError("intent must be a PurchaseIntent")
        created_at = _timestamp(created_at, "created_at")
        _timestamp(intent.expires_at, "expires_at")
        _text(intent.product_slug, "product_slug")
        _text(intent.package_id, "package_id")
        _text(intent.denomination, "denomination", limit=_DENOMINATION_LIMIT)
        with self._transaction() as connection:
            existing_row = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if existing_row is not None:
                existing = self._intent(existing_row)
                if existing.intent != intent:
                    raise ValueError("intent conflicts with existing intent_id")
                return existing
            connection.execute(
                """INSERT INTO intents
                (intent_id, product_slug, package_id, denomination, quoted_total_usd_micros,
                 max_payment_usdc_atomic, commitment, expires_at, state, created_at, approved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    intent.intent_id,
                    intent.product_slug,
                    intent.package_id,
                    intent.denomination,
                    str(intent.quoted_total_usd_micros),
                    str(intent.max_payment_usdc_atomic),
                    intent.recipient_hash,
                    intent.expires_at,
                    PaymentState.QUOTED.value,
                    created_at,
                ),
            )
            return IntentRecord(intent=intent, state=PaymentState.QUOTED, created_at=created_at)

    def approve_intent(self, intent_id: str, *, approved_at: int) -> IntentRecord:
        _text(intent_id, "intent_id", limit=66)
        approved_at = _timestamp(approved_at, "approved_at")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
            if row is None:
                raise ValueError("intent not found")
            current = self._intent(row)
            if current.state is PaymentState.DEVICE_APPROVED:
                return current
            if current.state is not PaymentState.QUOTED:
                raise ValueError("intent state changed")
            updated = connection.execute(
                """UPDATE intents SET state = ?, approved_at = ?
                WHERE intent_id = ? AND state = ?""",
                (PaymentState.DEVICE_APPROVED.value, approved_at, intent_id, PaymentState.QUOTED.value),
            )
            if updated.rowcount != 1:
                raise ValueError("intent state changed")
            return IntentRecord(
                intent=current.intent,
                state=PaymentState.DEVICE_APPROVED,
                created_at=current.created_at,
                approved_at=approved_at,
            )

    def get_intent(self, intent_id: str) -> IntentRecord | None:
        _text(intent_id, "intent_id", limit=66)
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
        finally:
            connection.close()
        return None if row is None else self._intent(row)

    def create_payment(
        self,
        *,
        payment_id: str,
        intent_id: str,
        invoice_id: str,
        idempotency_key: str,
        pay_to: str,
        amount_atomic: int | str,
        expires_at: int,
        created_at: int | None = None,
    ) -> PaymentView:
        payment_id = _text(payment_id, "payment_id")
        invoice_id = _text(invoice_id, "invoice_id")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        amount_value, amount_text = _amount(amount_atomic, "amount_atomic")
        expires_at = _timestamp(expires_at, "expires_at")
        request = PaymentRequest(
            intent_id=intent_id,
            invoice_id=invoice_id,
            pay_to=pay_to,
            amount_atomic=amount_value,
            expires_at=expires_at,
        )
        created_at = _timestamp(
            int(time.time()) if created_at is None else created_at,
            "created_at",
        )
        with self._transaction() as connection:
            intent_row = connection.execute(
                "SELECT state FROM intents WHERE intent_id = ?", (request.intent_id,)
            ).fetchone()
            if intent_row is None:
                raise ValueError("intent not found")
            if intent_row["state"] != PaymentState.DEVICE_APPROVED.value:
                raise ValueError("intent is not approved")
            invoice_row = connection.execute(
                "SELECT * FROM payments WHERE invoice_id = ?", (request.invoice_id,)
            ).fetchone()
            idempotency_row = connection.execute(
                "SELECT * FROM payments WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if invoice_row is not None or idempotency_row is not None:
                if invoice_row is None or idempotency_row is None:
                    raise ValueError("payment identity conflict")
                if invoice_row["payment_id"] != idempotency_row["payment_id"]:
                    raise ValueError("payment identity conflict")
                existing = self._payment(invoice_row)
                if (
                    invoice_row["intent_id"] != request.intent_id
                    or invoice_row["pay_to"] != request.pay_to
                    or invoice_row["amount_atomic"] != amount_text
                    or invoice_row["expires_at"] != request.expires_at
                ):
                    raise ValueError("idempotency replay conflicts with existing payment")
                return existing
            if connection.execute(
                "SELECT 1 FROM payments WHERE payment_id = ?", (payment_id,)
            ).fetchone() is not None:
                raise ValueError("payment_id already exists")
            connection.execute(
                """INSERT INTO payments
                (payment_id, intent_id, invoice_id, idempotency_key, pay_to, amount_atomic,
                 expires_at, state, created_at, updated_at, tx_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    payment_id,
                    request.intent_id,
                    request.invoice_id,
                    idempotency_key,
                    request.pay_to,
                    amount_text,
                    request.expires_at,
                    PaymentState.INVOICE_CREATED.value,
                    created_at,
                    created_at,
                ),
            )
            return PaymentView(
                payment_id=payment_id,
                intent_id=request.intent_id,
                invoice_id=request.invoice_id,
                state=PaymentState.INVOICE_CREATED,
                created_at=created_at,
                updated_at=created_at,
            )

    def transition_payment(
        self,
        *,
        payment_id: str,
        expected: PaymentState,
        target: PaymentState,
        updated_at: int,
        tx_hash: str | None = None,
    ) -> PaymentView:
        payment_id = _text(payment_id, "payment_id")
        if not isinstance(expected, PaymentState) or not isinstance(target, PaymentState):
            raise ValueError("payment states must be PaymentState values")
        updated_at = _timestamp(updated_at, "updated_at")
        if tx_hash is not None:
            tx_hash = _text(tx_hash, "tx_hash", limit=_TX_HASH_LIMIT)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)).fetchone()
            if row is None or row["state"] != expected.value:
                raise ValueError("payment state changed")
            if target not in _PAYMENT_EDGES.get(expected, set()):
                raise ValueError("illegal payment transition")
            if updated_at < row["updated_at"]:
                raise ValueError("updated_at cannot move backwards")
            persisted_hash = row["tx_hash"] if tx_hash is None else tx_hash
            updated = connection.execute(
                """UPDATE payments SET state = ?, tx_hash = ?, updated_at = ?
                WHERE payment_id = ? AND state = ?""",
                (target.value, persisted_hash, updated_at, payment_id, expected.value),
            )
            if updated.rowcount != 1:
                raise ValueError("payment state changed")
            return PaymentView(
                payment_id=row["payment_id"],
                intent_id=row["intent_id"],
                invoice_id=row["invoice_id"],
                state=target,
                created_at=row["created_at"],
                updated_at=updated_at,
                tx_hash=persisted_hash,
            )

    def get_payment(self, payment_id: str) -> PaymentView | None:
        payment_id = _text(payment_id, "payment_id")
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)).fetchone()
        finally:
            connection.close()
        return None if row is None else self._payment(row)

    def record_purchase(
        self,
        invoice_id: str,
        product_slug: str,
        amount: int | str,
        payment_method: str,
        timestamp: int,
    ) -> None:
        invoice_id = _text(invoice_id, "invoice_id")
        product_slug = _text(product_slug, "product_slug")
        _, amount_text = _amount(amount, "amount")
        payment_method = _text(payment_method, "payment_method")
        timestamp = _timestamp(timestamp, "timestamp")
        with self._transaction() as connection:
            payment = connection.execute(
                """SELECT payments.amount_atomic, intents.product_slug
                FROM payments JOIN intents ON intents.intent_id = payments.intent_id
                WHERE payments.invoice_id = ?""",
                (invoice_id,),
            ).fetchone()
            if payment is None:
                raise ValueError("invoice not found")
            if payment["amount_atomic"] != amount_text or payment["product_slug"] != product_slug:
                raise ValueError("purchase record conflicts with payment")
            existing = connection.execute(
                "SELECT * FROM purchase_log WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["product_slug"] == product_slug
                    and existing["amount"] == amount_text
                    and existing["payment_method"] == payment_method
                    and existing["timestamp"] == timestamp
                ):
                    return
                raise ValueError("purchase record conflicts with existing record")
            connection.execute(
                """INSERT INTO purchase_log
                (invoice_id, product_slug, amount, payment_method, timestamp)
                VALUES (?, ?, ?, ?, ?)""",
                (invoice_id, product_slug, amount_text, payment_method, timestamp),
            )
