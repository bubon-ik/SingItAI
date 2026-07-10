import hashlib
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator

from cryptography.fernet import Fernet, InvalidToken
from eth_account import Account

from .base_balances import (
    BASE_USDC_ADDRESS,
    DEFAULT_SINGIT_TOKEN_ADDRESS,
    build_base_balance_provider_from_env,
)


DEFAULT_USER_WALLET_STORE_PATH = Path.home() / ".sign402" / "user-wallets.db"
ACCESS_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_ACTIVE_ACCESS_TOKENS_PER_USER = 8
BASE_NATIVE_ETH_ASSET_ID = "native"


class WalletEncryptionError(RuntimeError):
    pass


class UserWalletStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _best_effort_chmod(self.path.parent, 0o700)
        self._init_db()
        _best_effort_chmod(self.path, 0o600)

    def get_wallet_by_telegram_user_id(
        self, telegram_user_id: str
    ) -> dict[str, Any] | None:
        with self.lock, self._database() as db:
            row = db.execute(
                """
                SELECT telegram_user_id, telegram_username, chain, wallet_address,
                       encrypted_private_key, status, created_at, updated_at
                FROM user_wallets
                WHERE telegram_user_id = ?
                """,
                (str(telegram_user_id),),
            ).fetchone()
        return _row_to_dict(row)

    def insert_wallet(
        self,
        *,
        telegram_user_id: str,
        telegram_username: str,
        chain: str,
        wallet_address: str,
        encrypted_private_key: str,
        status: str,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO user_wallets (
                    telegram_user_id, telegram_username, chain, wallet_address,
                    encrypted_private_key, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(telegram_user_id),
                    str(telegram_username or ""),
                    chain,
                    wallet_address,
                    encrypted_private_key,
                    status,
                    now,
                    now,
                ),
            )
        wallet = self.get_wallet_by_telegram_user_id(telegram_user_id)
        if wallet is None:
            raise RuntimeError("wallet insert failed")
        return wallet

    def record_audit_event(
        self,
        *,
        telegram_user_id: str,
        event_type: str,
        wallet_address: str = "",
        metadata_json: str = "{}",
    ) -> None:
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO user_wallet_audit (
                    telegram_user_id, event_type, wallet_address, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(telegram_user_id),
                    str(event_type),
                    str(wallet_address or ""),
                    str(metadata_json or "{}"),
                    int(time.time()),
                ),
            )

    def _init_db(self) -> None:
        with self._database() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_wallets (
                    telegram_user_id TEXT PRIMARY KEY,
                    telegram_username TEXT NOT NULL DEFAULT '',
                    chain TEXT NOT NULL,
                    wallet_address TEXT NOT NULL UNIQUE,
                    encrypted_private_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_wallet_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    wallet_address TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_access_tokens (
                    telegram_user_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL
                )
                """
            )
            # The original token table stores one token per user. Keeping
            # several short-lived hashes avoids a race where /wallet rotates a
            # token while another authenticated command is already in flight.
            # Leave the old table in place for a smooth upgrade from existing
            # deployments; a fresh v2 token revokes its legacy predecessor.
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_access_tokens_v2 (
                    token_hash TEXT PRIMARY KEY,
                    telegram_user_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS user_access_tokens_v2_user_created
                ON user_access_tokens_v2 (telegram_user_id, created_at DESC)
                """
            )

    def issue_access_token(self, telegram_user_id: str) -> str:
        """Mint a fresh per-user API token, storing only its hash.

        Binds a caller to one specific user so a leaked token compromises that
        user alone rather than authorizing action as any user. Multiple recent
        tokens are allowed briefly so concurrent Telegram commands cannot
        invalidate one another midway through a request.
        """
        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(token)
        now = int(time.time())
        expires_at = now + ACCESS_TOKEN_TTL_SECONDS
        with self.lock, self._database() as db:
            db.execute(
                "DELETE FROM user_access_tokens_v2 WHERE expires_at <= ?",
                (now,),
            )
            db.execute(
                """
                INSERT INTO user_access_tokens_v2 (
                    token_hash, telegram_user_id, created_at, expires_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (token_hash, str(telegram_user_id), now, expires_at),
            )
            stale_rows = db.execute(
                """
                SELECT token_hash
                FROM user_access_tokens_v2
                WHERE telegram_user_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT -1 OFFSET ?
                """,
                (str(telegram_user_id), MAX_ACTIVE_ACCESS_TOKENS_PER_USER),
            ).fetchall()
            if stale_rows:
                db.executemany(
                    "DELETE FROM user_access_tokens_v2 WHERE token_hash = ?",
                    [(str(row["token_hash"]),) for row in stale_rows],
                )
            # A v2 token has now been issued to the trusted plugin, so the
            # one-token legacy record is no longer needed for this user.
            db.execute(
                "DELETE FROM user_access_tokens WHERE telegram_user_id = ?",
                (str(telegram_user_id),),
            )
        return token

    def resolve_telegram_user_id(self, access_token: str) -> str | None:
        token = str(access_token or "")
        if not token:
            return None
        token_hash = _token_hash(token)
        now = int(time.time())
        with self.lock, self._database() as db:
            row = db.execute(
                """
                SELECT telegram_user_id
                FROM user_access_tokens_v2
                WHERE token_hash = ? AND expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
            if row is None:
                # Compatibility for a running Hermes process during the first
                # deployment of this migration. The next token mint removes it.
                row = db.execute(
                    "SELECT telegram_user_id FROM user_access_tokens WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone()
        return str(row["telegram_user_id"]) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
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


class ManagedBaseWalletService:
    def __init__(
        self,
        *,
        store: UserWalletStore,
        master_key: str,
        balance_provider: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.store = store
        self.master_key = str(master_key or "")
        self.balance_provider = balance_provider

    def create_wallet(
        self,
        telegram_user_id: str,
        telegram_username: str = "",
    ) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        # Mint a fresh per-user access token on every create/start so the caller
        # (Telegram plugin) can authenticate as this user on later requests.
        access_token = self.store.issue_access_token(user_id)
        existing = self.store.get_wallet_by_telegram_user_id(user_id)
        if existing is not None:
            self.store.record_audit_event(
                telegram_user_id=user_id,
                event_type="wallet_create_idempotent",
                wallet_address=existing["wallet_address"],
            )
            return {**_wallet_response(existing, created=False), "accessToken": access_token}

        fernet = self._fernet()
        account = Account.create()
        private_key = account.key.hex()
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        encrypted_private_key = fernet.encrypt(private_key.encode("utf-8")).decode(
            "ascii"
        )
        try:
            wallet = self.store.insert_wallet(
                telegram_user_id=user_id,
                telegram_username=telegram_username,
                chain="base",
                wallet_address=account.address,
                encrypted_private_key=encrypted_private_key,
                status="created",
            )
        except sqlite3.IntegrityError:
            existing = self.store.get_wallet_by_telegram_user_id(user_id)
            if existing is None:
                raise
            self.store.record_audit_event(
                telegram_user_id=user_id,
                event_type="wallet_create_idempotent",
                wallet_address=existing["wallet_address"],
            )
            return {**_wallet_response(existing, created=False), "accessToken": access_token}
        self.store.record_audit_event(
            telegram_user_id=user_id,
            event_type="wallet_created",
            wallet_address=wallet["wallet_address"],
        )
        return {**_wallet_response(wallet, created=True), "accessToken": access_token}

    def issue_access_token(self, telegram_user_id: str) -> str:
        return self.store.issue_access_token(_require_telegram_user_id(telegram_user_id))

    def resolve_telegram_user_id(self, access_token: str) -> str | None:
        return self.store.resolve_telegram_user_id(access_token)

    def wallet_status(self, telegram_user_id: str) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id)
        if wallet is None:
            return {
                "ok": False,
                "wallet": None,
                "telegramText": (
                    "No Base agent wallet yet. Send /wallet to create one."
                ),
            }
        self.store.record_audit_event(
            telegram_user_id=user_id,
            event_type="wallet_status_read",
            wallet_address=wallet["wallet_address"],
        )
        return _wallet_response(wallet, created=False)

    def wallet_balance(self, telegram_user_id: str) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id)
        if wallet is None:
            return {
                "ok": False,
                "wallet": None,
                "balanceUnavailable": True,
                "telegramText": (
                    "No Base agent wallet yet. Send /wallet to create one."
                ),
            }

        safe_wallet = _safe_wallet(wallet)
        self.store.record_audit_event(
            telegram_user_id=user_id,
            event_type="wallet_balance_read",
            wallet_address=wallet["wallet_address"],
        )
        if self.balance_provider is None:
            return {
                "ok": True,
                "wallet": safe_wallet,
                "balanceUnavailable": True,
                "telegramText": (
                    f"Base agent wallet: {safe_wallet['address']}\n\n"
                    "Balance lookup is not configured yet. Spending is disabled until "
                    "iMessage approval is configured."
                ),
            }

        try:
            provider_result = self.balance_provider(wallet["wallet_address"])
            balances, unverified_tokens = _normalize_balance_provider_result(
                provider_result
            )
        except Exception:
            return {
                "ok": True,
                "wallet": safe_wallet,
                "balanceUnavailable": True,
                "telegramText": (
                    f"Base agent wallet: {safe_wallet['address']}\n\n"
                    "Balance lookup is unavailable right now. Spending is disabled "
                    "until iMessage approval is configured."
                ),
            }
        response = {
            "ok": True,
            "wallet": safe_wallet,
            "balanceUnavailable": False,
            "balances": balances,
            "telegramText": _balance_text(
                safe_wallet["address"],
                balances,
                unverified_tokens,
            ),
        }
        if unverified_tokens:
            response["unverifiedTokens"] = unverified_tokens
        return response

    def withdrawable_tokens(self, telegram_user_id: str) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id)
        if wallet is None:
            return {
                "ok": False,
                "wallet": None,
                "balanceUnavailable": True,
                "tokens": [],
                "telegramText": (
                    "No Base agent wallet yet. Send /wallet to create one."
                ),
            }

        safe_wallet = _safe_wallet(wallet)
        if self.balance_provider is None:
            return {
                "ok": True,
                "wallet": safe_wallet,
                "balanceUnavailable": True,
                "tokens": [],
                "telegramText": (
                    f"Base agent wallet: {safe_wallet['address']}\n\n"
                    "Token lookup is not configured yet."
                ),
            }

        try:
            provider_result = self.balance_provider(wallet["wallet_address"])
            balances, unverified_tokens = _normalize_balance_provider_result(
                provider_result
            )
        except Exception:
            return {
                "ok": True,
                "wallet": safe_wallet,
                "balanceUnavailable": True,
                "tokens": [],
                "telegramText": (
                    f"Base agent wallet: {safe_wallet['address']}\n\n"
                    "Token lookup is unavailable right now."
                ),
            }

        tokens = _withdrawable_token_list(balances, unverified_tokens)
        return {
            "ok": True,
            "wallet": safe_wallet,
            "balanceUnavailable": False,
            "tokens": tokens,
            "telegramText": _withdrawable_tokens_text(tokens),
        }

    def decrypt_private_key_for_future_signing(self, telegram_user_id: str) -> str:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id)
        if wallet is None:
            raise ValueError("wallet not found")
        try:
            return self._fernet().decrypt(
                wallet["encrypted_private_key"].encode("ascii")
            ).decode("utf-8")
        except InvalidToken as exc:
            raise WalletEncryptionError(
                "wallet private key could not be decrypted"
            ) from exc

    def _fernet(self) -> Fernet:
        if not self.master_key:
            raise WalletEncryptionError(
                "SIGN402_WALLET_MASTER_KEY is required to create managed wallets"
            )
        try:
            return Fernet(self.master_key.encode("ascii"))
        except Exception as exc:
            raise WalletEncryptionError(
                "SIGN402_WALLET_MASTER_KEY must be a valid Fernet key"
            ) from exc


def build_wallet_service_from_env(
    *,
    env: dict[str, str],
    store_path: Path | None = None,
) -> ManagedBaseWalletService:
    return ManagedBaseWalletService(
        store=UserWalletStore(store_path or DEFAULT_USER_WALLET_STORE_PATH),
        master_key=env.get("SIGN402_WALLET_MASTER_KEY", ""),
        balance_provider=build_base_balance_provider_from_env(env),
    )


def _require_telegram_user_id(telegram_user_id: str) -> str:
    user_id = str(telegram_user_id or "").strip()
    if not user_id:
        raise ValueError("telegramUserId is required")
    return user_id


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _safe_wallet(wallet: dict[str, Any]) -> dict[str, Any]:
    return {
        "chain": str(wallet["chain"]),
        "address": str(wallet["wallet_address"]),
        "status": str(wallet["status"]),
        "spendingEnabled": False,
    }


def _wallet_response(wallet: dict[str, Any], *, created: bool) -> dict[str, Any]:
    safe_wallet = _safe_wallet(wallet)
    if created:
        text = (
            f"Your Base agent wallet is ready:\n{safe_wallet['address']}\n\n"
            "Fund this wallet with a small amount only.\n"
            "Spending is disabled until iMessage approval is configured."
        )
    else:
        text = (
            f"Your Base agent wallet:\n{safe_wallet['address']}\n\n"
            "Spending is disabled until iMessage approval is configured."
        )
    return {
        "ok": True,
        "created": created,
        "wallet": safe_wallet,
        "telegramText": text,
    }


def _normalize_balance_provider_result(
    provider_result: object,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(provider_result, dict):
        raise ValueError("balance provider must return an object")
    structured_balances = provider_result.get("balances")
    if isinstance(structured_balances, dict):
        unverified = provider_result.get("unverifiedTokens", [])
        if not isinstance(unverified, list):
            raise ValueError("unverifiedTokens must be a list")
        return structured_balances, [
            token for token in unverified if isinstance(token, dict)
        ]
    return provider_result, []


def _balance_text(
    address: str,
    balances: dict[str, Any],
    unverified_tokens: list[dict[str, Any]] | None = None,
) -> str:
    if not balances:
        return f"Base agent wallet: {address}\n\nNo balances found."
    lines = [f"Base agent wallet: {address}", "", "Balances:"]
    trusted_order = ("ETH", "USDC", "SINGIT")
    ordered_symbols = [symbol for symbol in trusted_order if symbol in balances]
    ordered_symbols.extend(
        sorted(symbol for symbol in balances if symbol not in trusted_order)
    )
    for symbol in ordered_symbols:
        value = balances[symbol]
        lines.append(f"- {symbol}: {value}")
    if unverified_tokens:
        lines.extend(["", "Unverified tokens (not enabled for spending):"])
        for token in unverified_tokens:
            symbol = str(token.get("symbol", "ERC20"))
            value = str(token.get("balance", "0"))
            contract = str(token.get("contractAddress", ""))
            lines.append(f"- {symbol}: {value} ({_short_address(contract)})")
    lines.append("")
    lines.append("Spending is disabled until iMessage approval is configured.")
    return "\n".join(lines)


def _withdrawable_token_list(
    balances: dict[str, Any],
    unverified_tokens: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    eth_balance = str(balances.get("ETH", "0"))
    if _positive_decimal(eth_balance):
        tokens.append(
            {
                "symbol": "ETH",
                "contractAddress": BASE_NATIVE_ETH_ASSET_ID,
                "balance": eth_balance,
                "decimals": 18,
                "verified": True,
                "native": True,
            }
        )
    trusted = (
        ("USDC", BASE_USDC_ADDRESS, 6),
        ("SINGIT", DEFAULT_SINGIT_TOKEN_ADDRESS, 18),
    )
    for symbol, contract_address, decimals in trusted:
        balance = str(balances.get(symbol, "0"))
        if _positive_decimal(balance):
            tokens.append(
                {
                    "symbol": symbol,
                    "contractAddress": contract_address,
                    "balance": balance,
                    "decimals": decimals,
                    "verified": True,
                }
            )

    seen = {str(token["contractAddress"]).lower() for token in tokens}
    for token in unverified_tokens or []:
        contract_address = str(token.get("contractAddress", "") or "").strip()
        if not contract_address or contract_address.lower() in seen:
            continue
        balance = str(token.get("balance", "0") or "0")
        if not _positive_decimal(balance):
            continue
        decimals = token.get("decimals")
        if isinstance(decimals, bool) or not isinstance(decimals, int):
            continue
        if decimals < 0 or decimals > 36:
            continue
        symbol = str(token.get("symbol", "ERC20") or "ERC20").strip()[:16] or "ERC20"
        tokens.append(
            {
                "symbol": symbol,
                "contractAddress": contract_address,
                "balance": balance,
                "decimals": decimals,
                "verified": False,
            }
        )
        seen.add(contract_address.lower())
    return tokens


def _withdrawable_tokens_text(tokens: list[dict[str, Any]]) -> str:
    if not tokens:
        return "No Base asset balances are available to withdraw yet."
    lines = ["Choose an asset to withdraw:"]
    for index, token in enumerate(tokens, start=1):
        symbol = str(token.get("symbol") or "ERC20")
        balance = str(token.get("balance") or "0")
        line = f"{index}. {symbol}: {balance}"
        if token.get("native"):
            line += " (leave ETH for gas)"
        if not token.get("verified"):
            line += f" ({_short_address(str(token.get('contractAddress') or ''))})"
        lines.append(line)
    return "\n".join(lines)


def _positive_decimal(value: object) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, ValueError):
        return False


def _short_address(address: str) -> str:
    text = str(address or "")
    if len(text) < 12:
        return text
    return f"{text[:8]}...{text[-4:]}"
