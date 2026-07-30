"""Per-buyer email addresses for Bitrefill guest checkout.

Guest invoices require an address: it is where the redemption codes land if
the buyer ever loses the chat, which is the only other place they appear. That
makes this personal data we are obliged to hold, so it is encrypted at rest and
only ever leaves here masked.
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .secure_state import (
    SensitiveStateCipher,
    SensitiveStateError,
    ensure_private_directory,
    ensure_private_file,
)


def normalize_email(value: str) -> str:
    """Validate an address without ever quoting it back.

    A rejection travels into logs and user-facing messages, so it says what is
    wrong and nothing about the value itself.
    """
    email = str(value or "").strip().lower()
    if not email:
        raise ValueError("an email address is required")
    local, separator, domain = email.partition("@")
    if (
        not separator
        or not local
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or any(character.isspace() for character in email)
    ):
        raise ValueError("that is not a valid email address")
    return email


def mask_email(value: str) -> str:
    """Show enough to recognise the address, not enough to disclose it."""
    email = str(value or "").strip()
    if not email:
        return ""
    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    return f"{local[0]}***@{domain}" if len(local) > 1 else f"***@{domain}"


class BuyerEmailStore:
    def __init__(self, path: Path, *, cipher: SensitiveStateCipher | None = None):
        self.path = Path(path)
        self.cipher = cipher
        self.lock = threading.Lock()
        ensure_private_directory(self.path.parent)
        self._init_db()
        ensure_private_file(self.path)

    def set_email(self, telegram_user_id: str, email: str) -> str:
        address = normalize_email(email)
        if self.cipher is None:
            raise SensitiveStateError(
                "SIGN402_WALLET_MASTER_KEY is required to store a buyer email"
            )
        encrypted = self.cipher.encrypt_json({"email": address})
        now = int(time.time())
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO buyer_emails (
                    telegram_user_id, encrypted_email, created_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    encrypted_email = excluded.encrypted_email,
                    updated_at = excluded.updated_at
                """,
                (str(telegram_user_id), encrypted, now, now),
            )
        return address

    def get_email(self, telegram_user_id: str) -> str | None:
        with self.lock, self._database() as db:
            row = db.execute(
                "SELECT encrypted_email FROM buyer_emails"
                " WHERE telegram_user_id = ?",
                (str(telegram_user_id),),
            ).fetchone()
        if row is None:
            return None
        if self.cipher is None:
            raise SensitiveStateError(
                "SIGN402_WALLET_MASTER_KEY is required to read a buyer email"
            )
        decoded = self.cipher.decrypt_json(str(row[0]))
        address = decoded.get("email") if isinstance(decoded, dict) else None
        return str(address) if address else None

    def forget_email(self, telegram_user_id: str) -> bool:
        with self.lock, self._database() as db:
            cursor = db.execute(
                "DELETE FROM buyer_emails WHERE telegram_user_id = ?",
                (str(telegram_user_id),),
            )
        return cursor.rowcount > 0

    def _init_db(self) -> None:
        with self._database() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS buyer_emails (
                    telegram_user_id TEXT PRIMARY KEY,
                    encrypted_email TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=5.0)

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()
