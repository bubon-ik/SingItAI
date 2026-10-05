"""Web accounts: Sign-In with Ethereum or Solana, sessions, and the owner behind each account.

Design: docs/allowance-web-v1.md, "Accounts and identity". The server writes the
exact sign-in message when it hands out a nonce and keeps it; signing in means
returning that same text with a signature from the address it names (EIP-4361
recovered for an EVM wallet, an ed25519 signature for a Solana one). Nothing in
the submitted message is parsed for trust — it is compared byte for byte with
what was issued.

An account is `wallet:<checksummed address>` for an EVM wallet and
`solana:<base58 address>` for a Solana one. Its owner address is that address;
the Base allowance lane reads EVM owners through `WebAccountStore.owner_for`.

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
from .solana_keys import b58decode, b58encode

WEB_DB_ENV = "SIGN402_WEB_DB"
DEFAULT_WEB_DB = "~/.sign402/web.db"
CHAIN_ID = 8453
NONCE_SECONDS = 300
LINK_CODE_SECONDS = 600
SESSION_SECONDS = 12 * 3600
STATEMENT = "Sign in to SingIt. This signature does not move funds or approve any spending."
ACCOUNT_PREFIX = "wallet:"         # an EVM wallet (Base)
SOLANA_PREFIX = "solana:"          # a Solana wallet


class WebAuthError(Exception):
    """A sign-in or session refused; the message is safe to show."""


def solana_address(value: str) -> str | None:
    """The canonical base58 form of a Solana public key, or None."""
    try:
        raw = b58decode(value)
    except ValueError:
        return None
    return b58encode(raw) if len(raw) == 32 and b58encode(raw) == value else None


def chain_of(address: Any) -> str:
    """ "base" for an EVM address, "solana" for a Solana one."""
    text = str(address or "").strip()
    if is_address(text):
        return "base"
    if solana_address(text):
        return "solana"
    raise WebAuthError("That is not an Ethereum or Solana address.")


def normalized(address: str) -> str:
    return to_checksum_address(address) if chain_of(address) == "base" else address


def account_id_for(address: str) -> str:
    if chain_of(address) == "solana":
        return SOLANA_PREFIX + address
    return ACCOUNT_PREFIX + to_checksum_address(address)


def chain_of_account(account_id: str) -> str:
    return "solana" if str(account_id).startswith(SOLANA_PREFIX) else "base"


def _allow_key(address: str) -> str:
    """EVM addresses compare case-insensitively; Solana's base58 is case-sensitive."""
    return address.lower() if address.startswith("0x") else address


def siws_message(domain: str, uri: str, address: str, nonce: str, issued: str, expires: str) -> str:
    """Sign In With Solana (CAIP-122), the text Phantom, Solflare and Backpack show and sign."""
    return (f"{domain} wants you to sign in with your Solana account:\n{address}\n\n{STATEMENT}\n\n"
            f"URI: {uri}\nVersion: 1\nChain ID: mainnet\nNonce: {nonce}\nIssued At: {issued}\n"
            f"Expiration Time: {expires}")


def _ed25519_signature(signature: str) -> bytes:
    """A Solana wallet's 64-byte signature, as base58 (the norm), 0x-hex or base64."""
    import base64
    text = signature.strip()
    candidates = []
    if text.startswith("0x"):
        candidates.append(lambda: bytes.fromhex(text[2:]))
    candidates += [lambda: b58decode(text), lambda: base64.b64decode(text, validate=True)]
    for decode in candidates:
        try:
            raw = decode()
        except Exception:
            continue
        if len(raw) == 64:
            return raw
    raise WebAuthError("The signature could not be read.")


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
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    endpoint TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS push_by_account ON push_subscriptions(account_id);
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
                       (account_id, normalized(address), now))
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

    def account_for_user(self, user_id: str) -> str | None:
        """The web account behind a lane user: itself, or the one a Telegram user linked."""
        if str(user_id).isdigit():
            return self.account_for_telegram(user_id)
        return user_id if self.account(user_id) is not None else None

    # -- push subscriptions (web_push.py) --

    def add_push(self, account_id: str, endpoint: str, p256dh: str, auth: str, now: int, keep: int) -> None:
        """One row per device; a device that signs in to another account moves to it."""
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO push_subscriptions(endpoint, account_id, p256dh, auth, created_at)"
                       " VALUES (?, ?, ?, ?, ?)", (endpoint, account_id, p256dh, auth, now))
            db.execute("DELETE FROM push_subscriptions WHERE account_id = ? AND endpoint NOT IN (SELECT endpoint"
                       " FROM push_subscriptions WHERE account_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                       (account_id, account_id, keep))

    def pushes_for(self, account_id: str) -> list[sqlite3.Row]:
        with self._db() as db:
            return db.execute("SELECT * FROM push_subscriptions WHERE account_id = ? ORDER BY created_at",
                              (account_id,)).fetchall()

    def remove_push(self, account_id: str, endpoint: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM push_subscriptions WHERE account_id = ? AND endpoint = ?", (account_id, endpoint))

    def drop_push(self, endpoint: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

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
        self.allowed = None if allowed is None else {_allow_key(a.strip()) for a in allowed}
        self.now = now

    def _check_allowed(self, address: str) -> None:
        if self.allowed is not None and _allow_key(address) not in self.allowed:
            raise WebAuthError("This address is not in the beta yet.")

    def nonce(self, address: Any) -> dict[str, Any]:
        address = str(address or "").strip()
        chain = chain_of(address)
        address = normalized(address)
        self._check_allowed(address)
        now = self.now()
        nonce = secrets.token_hex(16)
        if chain == "solana":
            message = siws_message(self.domain, self.uri, address, nonce, _iso(now), _iso(now + NONCE_SECONDS))
        else:
            message = siwe_message({
                "domain": self.domain, "uri": self.uri, "version": "1", "statement": STATEMENT,
                "nonce": nonce, "issuedAt": _iso(now), "expirationTime": _iso(now + NONCE_SECONDS),
            }, address, CHAIN_ID)
        self.store.add_nonce(nonce, address, message, int(now) + NONCE_SECONDS, int(now))
        return {"nonce": nonce, "message": message, "chain": chain, "expiresAt": int(now) + NONCE_SECONDS}

    def _signed_by(self, address: str, message: str, signature: str) -> bool:
        if chain_of(address) == "solana":
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            try:
                Ed25519PublicKey.from_public_bytes(b58decode(address)).verify(
                    _ed25519_signature(signature), message.encode())
                return True
            except InvalidSignature:
                return False
        try:
            signer = Account.recover_message(encode_defunct(text=message), signature=signature)
        except Exception:
            raise WebAuthError("The signature could not be read.") from None
        return signer.lower() == address.lower()

    def verify(self, message: Any, signature: Any) -> dict[str, Any]:
        message, signature = str(message or ""), str(signature or "")
        nonce = next((line[len("Nonce: "):] for line in message.splitlines() if line.startswith("Nonce: ")), "")
        now = int(self.now())
        issued = self.store.take_nonce(nonce, now) if nonce else None
        if issued is None or issued["message"] != message:
            raise WebAuthError("This sign-in request is unknown, used or expired. Start again.")
        if not self._signed_by(issued["address"], message, signature):
            raise WebAuthError("The signature is not from the address that asked to sign in.")
        self._check_allowed(issued["address"])
        account_id = self.store.ensure_account(issued["address"], now)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.store.add_session(_hash(token), _hash(csrf), account_id, issued["address"], now, now + SESSION_SECONDS)
        return {"token": token, "csrfToken": csrf, "account": account_id, "address": issued["address"],
                "chain": chain_of_account(account_id), "expiresAt": now + SESSION_SECONDS}

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
