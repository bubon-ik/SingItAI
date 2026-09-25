"""Web accounts: Sign-In with Ethereum, sessions, and the owner behind each account.

Design: docs/allowance-web-v1.md, "Accounts and identity". The server writes the
exact EIP-4361 message when it hands out a nonce and keeps it; signing in means
returning that same text with a signature that recovers to the address it
names. Nothing in the submitted message is parsed for trust — it is compared
byte for byte with what was issued.

An account is `wallet:<checksummed address>`. Its owner address is that address;
the allowance lane reads it through `WebAccountStore.owner_for`.

Session and CSRF tokens are random, shown once, and stored only as hashes.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address, to_checksum_address

from .allowance_bitrefill import siwe_message

WEB_DB_ENV = "SIGN402_WEB_DB"
DEFAULT_WEB_DB = "~/.sign402/web.db"
CHAIN_ID = 8453
NONCE_SECONDS = 300
LINK_CODE_SECONDS = 600
SESSION_SECONDS = 12 * 3600
STATEMENT = "Sign in to SingIt. This signature does not move funds or approve any spending."
ACCOUNT_PREFIX = "wallet:"


class WebAuthError(Exception):
    """A sign-in or session refused; the message is safe to show."""


def account_id_for(address: str) -> str:
    return ACCOUNT_PREFIX + to_checksum_address(address)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class WebAccountStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    owner_address TEXT NOT NULL UNIQUE,
                    telegram_user_id TEXT UNIQUE,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_nonces (
                    nonce TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    message TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    address TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_by_expiry ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS link_codes (
                    code_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # -- nonces --

    def add_nonce(self, nonce: str, address: str, message: str, expires_at: int, now: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM auth_nonces WHERE expires_at < ?", (now - 86400,))
            db.execute("INSERT INTO auth_nonces(nonce, address, message, expires_at) VALUES (?, ?, ?, ?)",
                       (nonce, address, message, expires_at))

    def take_nonce(self, nonce: str, now: int) -> sqlite3.Row | None:
        """The issued nonce, marked used; None if unknown, used or expired."""
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM auth_nonces WHERE nonce = ?", (nonce,)).fetchone()
            if row is None or row["used_at"] is not None or row["expires_at"] < now:
                return None
            db.execute("UPDATE auth_nonces SET used_at = ? WHERE nonce = ?", (now, nonce))
            return row

    # -- accounts --

    def ensure_account(self, address: str, now: int) -> str:
        account_id = account_id_for(address)
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO accounts(account_id, owner_address, created_at) VALUES (?, ?, ?)",
                       (account_id, to_checksum_address(address), now))
        return account_id

    def account(self, account_id: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()

    def owner_for(self, user_id: str) -> str | None:
        """The allowance lane's owner lookup: the address of a web account, or None."""
        if not str(user_id).startswith(ACCOUNT_PREFIX):
            return None
        row = self.account(str(user_id))
        return row["owner_address"] if row else None

    # -- linking a Telegram account --

    def new_link_code(self, account_id: str, now: int) -> str:
        """A 6-digit one-time code for /link in the bot; earlier unused codes of this account die."""
        with self._lock, self._db() as db:
            db.execute("DELETE FROM link_codes WHERE expires_at < ? OR (account_id = ? AND used_at IS NULL)",
                       (now, account_id))
            while True:
                code = f"{secrets.randbelow(1_000_000):06d}"
                if db.execute("SELECT 1 FROM link_codes WHERE code_hash = ?", (_hash(code),)).fetchone() is None:
                    break
            db.execute("INSERT INTO link_codes(code_hash, account_id, expires_at) VALUES (?, ?, ?)",
                       (_hash(code), account_id, now + LINK_CODE_SECONDS))
        return code

    def link_telegram(self, code: str, telegram_user_id: str, now: int) -> str | None:
        """Use a code: the Telegram id joins its account (leaving any other). None if unknown, used or expired."""
        code = str(code or "").strip()
        if len(code) != 6 or not code.isdigit():
            return None
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM link_codes WHERE code_hash = ? AND used_at IS NULL AND expires_at >= ?",
                             (_hash(code), now)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE link_codes SET used_at = ? WHERE code_hash = ?", (now, row["code_hash"]))
            db.execute("UPDATE accounts SET telegram_user_id = NULL WHERE telegram_user_id = ?", (str(telegram_user_id),))
            db.execute("UPDATE accounts SET telegram_user_id = ? WHERE account_id = ?",
                       (str(telegram_user_id), row["account_id"]))
            return row["account_id"]

    def unlink_telegram(self, account_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE accounts SET telegram_user_id = NULL WHERE account_id = ?", (account_id,))

    def account_for_telegram(self, telegram_user_id: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT account_id FROM accounts WHERE telegram_user_id = ?",
                             (str(telegram_user_id),)).fetchone()
        return row["account_id"] if row else None

    def telegram_for(self, account_id: str) -> str | None:
        row = self.account(account_id)
        return row["telegram_user_id"] if row else None

    # -- sessions --

    def add_session(self, session_hash: str, csrf_hash: str, account_id: str, address: str,
                    now: int, expires_at: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            db.execute(
                "INSERT INTO sessions(session_hash, csrf_hash, account_id, address, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)", (session_hash, csrf_hash, account_id, address, now, expires_at))

    def session(self, session_hash: str, now: int) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM sessions WHERE session_hash = ? AND expires_at > ?",
                              (session_hash, now)).fetchone()

    def drop_session(self, session_hash: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE session_hash = ?", (session_hash,))


class WebAuth:
    """Nonce → signed message → session, for addresses on the allowlist."""

    def __init__(
        self,
        store: WebAccountStore,
        *,
        domain: str,
        uri: str,
        allowed: Iterable[str] | None,
        now: Callable[[], float] = time.time,
    ):
        self.store = store
        self.domain = domain
        self.uri = uri
        # None: everyone (general availability). Otherwise the beta allowlist.
        self.allowed = None if allowed is None else {a.lower() for a in allowed}
        self.now = now

    def _check_allowed(self, address: str) -> None:
        if self.allowed is not None and address.lower() not in self.allowed:
            raise WebAuthError("This address is not in the beta yet.")

    def nonce(self, address: Any) -> dict[str, Any]:
        address = str(address or "").strip()
        if not is_address(address):
            raise WebAuthError("That is not an Ethereum address.")
        address = to_checksum_address(address)
        self._check_allowed(address)
        now = self.now()
        nonce = secrets.token_hex(16)
        message = siwe_message({
            "domain": self.domain, "uri": self.uri, "version": "1", "statement": STATEMENT,
            "nonce": nonce, "issuedAt": _iso(now), "expirationTime": _iso(now + NONCE_SECONDS),
        }, address, CHAIN_ID)
        self.store.add_nonce(nonce, address, message, int(now) + NONCE_SECONDS, int(now))
        return {"nonce": nonce, "message": message, "expiresAt": int(now) + NONCE_SECONDS}

    def verify(self, message: Any, signature: Any) -> dict[str, Any]:
        message, signature = str(message or ""), str(signature or "")
        nonce = next((line[len("Nonce: "):] for line in message.splitlines() if line.startswith("Nonce: ")), "")
        now = int(self.now())
        issued = self.store.take_nonce(nonce, now) if nonce else None
        if issued is None or issued["message"] != message:
            raise WebAuthError("This sign-in request is unknown, used or expired. Start again.")
        try:
            signer = Account.recover_message(encode_defunct(text=message), signature=signature)
        except Exception:
            raise WebAuthError("The signature could not be read.") from None
        if signer.lower() != issued["address"].lower():
            raise WebAuthError("The signature is not from the address that asked to sign in.")
        self._check_allowed(issued["address"])
        account_id = self.store.ensure_account(issued["address"], now)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.store.add_session(_hash(token), _hash(csrf), account_id, issued["address"], now, now + SESSION_SECONDS)
        return {"token": token, "csrfToken": csrf, "account": account_id, "address": issued["address"],
                "expiresAt": now + SESSION_SECONDS}

    def session(self, token: str, csrf: str | None = None) -> sqlite3.Row:
        """The live session for this cookie; with `csrf`, also the matching CSRF token."""
        row = self.store.session(_hash(token), int(self.now())) if token else None
        if row is None:
            raise WebAuthError("Sign in first.")
        if csrf is not None and not secrets.compare_digest(_hash(csrf), row["csrf_hash"]):
            raise WebAuthError("This request is missing its CSRF token.")
        self._check_allowed(row["address"])
        return row

    def logout(self, token: str) -> None:
        if token:
            self.store.drop_session(_hash(token))
