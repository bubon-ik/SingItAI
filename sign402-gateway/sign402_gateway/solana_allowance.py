"""The agent allowance on Solana, for a web account signed in with a Solana wallet.

The Base lane (agent_allowance.py) puts the limits in a contract. Solana has no such
contract here yet, so the pieces are split:

- the owner's wallet approves the agent as delegate on their own USDC account, for a
  total (an SPL ApproveChecked). That total is enforced by the chain: the agent can
  never take more than what is left of it, and one Revoke ends it;
- the daily and per-purchase limits and the expiry are enforced here, before every
  pull, and each purchase is recorded against the day;
- a purchase is paid over x402 straight from the owner's account, the agent signing as
  the approved delegate; the merchant's facilitator pays that network fee (x402's own
  facilitator checks the signer, mint, recipient and amount, not whose account it is:
  solana-x402-service/test/delegated.test.mjs).

The owner pays approval/revoke fees from their SOL. Metered Ask funding uses the
user agent's own SOL for network fees and token-account rent. The owner explicitly
funds that agent through a wallet-signed SOL transfer; there is no operator fallback. The chain work runs in the Node x402 service (solana-x402-service/src/allowance.mjs)
through the same bridge the bot's Solana chat uses.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import analytics
from .agent_allowance import AllowanceError, AllowanceUnavailable
from .secure_state import ensure_private_directory, ensure_private_file
from .solana_keys import generate_keypair
from .web_accounts import SOLANA_PREFIX

logger = logging.getLogger(__name__)

ENABLED_ENV = "SIGN402_SOLANA_ALLOWANCE_ENABLED"
DB_ENV = "SIGN402_SOLANA_ALLOWANCE_DB"
FEE_PAYER_ENV = "SIGN402_SOLANA_FEE_PAYER_KEY"   # Fernet-encrypted base58 keypair
DEFAULT_DB = "~/.sign402/solana-allowance.db"
PREPARE_SECONDS = 90   # a Solana blockhash lives about a minute
DAY = 86400
OWNER_FEE_LAMPORTS = 20_000  # enough SOL to pay one signature's fee (5,000) with room to spare
# The bridge's refusals from before anything was signed or sent (solana-x402-service): only these give a
# purchase's room back. Any other failure, a lost answer above all, may have moved the money.
NOTHING_SENT_CODES = frozenset({
    "ALREADY_ATTEMPTED", "CHALLENGE_FAILED", "EXA_CHALLENGE_FAILED", "INVALID_CHALLENGE", "EXA_INVALID_CHALLENGE",
    "INSUFFICIENT_USDC", "ALLOWANCE_TOO_LOW", "PRICE_CHANGED", "MERCHANT_CHANGED", "UNSUPPORTED_MERCHANT",
    "UNSUPPORTED_PAYMENT", "EXA_UNSUPPORTED_PAYMENT", "UNEXPECTED_FEE_PAYER", "INVALID_AMOUNT", "INVALID_INVOICE",
    "INVALID_CALL", "BUILD_FAILED", "PAYMENT_REFUSED",
    "RECIPIENT_ACCOUNT_MISSING", "INVALID_RECIPIENT", "FEE_PAYER_REQUIRED", "FEE_PAYER_LOW", "FEE_PAYER_UNAVAILABLE",
})
NOTHING_SENT_STATES = frozenset({"failed", "not_submitted"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    account TEXT PRIMARY KEY, agent_address TEXT NOT NULL UNIQUE, encrypted_key TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS limits (
    account TEXT PRIMARY KEY, owner TEXT NOT NULL, daily_cap INTEGER NOT NULL, per_purchase_cap INTEGER NOT NULL,
    expiry INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ops (
    op_id TEXT PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL,
    message_hash TEXT NOT NULL, state TEXT NOT NULL, tx TEXT, detail TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, owner_pays_fee INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS spends (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, amount INTEGER NOT NULL, purpose TEXT NOT NULL,
    pull_tx TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS metered_holds (
    hold_id TEXT PRIMARY KEY, account TEXT NOT NULL, amount INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS metered_holds_by_account ON metered_holds(account);
CREATE INDEX IF NOT EXISTS spends_by_day ON spends(account, created_at);
"""


def _usdc(value: Any, label: str) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise AllowanceError(f"{label} must be a number of USDC.") from None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.000001")):
        raise AllowanceError(f"{label} must be a positive amount of USDC, at most 6 decimals.")
    return int(amount * 1_000_000)


def _text(atomic: int) -> str:
    return f"{Decimal(atomic) / 1_000_000:.6f}".rstrip("0").rstrip(".") + " USDC"


class SolanaAllowanceStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        ensure_private_directory(self.path.parent)
        # Pre-create privately: chmod after sqlite.connect leaves a readable window.
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self._lock = threading.RLock()
        with self._db() as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")  # web and gateway can migrate the shared store together
            columns = {row["name"] for row in db.execute("PRAGMA table_info(ops)")}
            if "owner_pays_fee" not in columns:
                db.execute("ALTER TABLE ops ADD COLUMN owner_pays_fee INTEGER NOT NULL DEFAULT 0")
            if "tx_blockhash" not in columns:
                db.execute("ALTER TABLE ops ADD COLUMN tx_blockhash TEXT")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        ensure_private_file(self.path)
        for suffix in ("-journal", "-wal", "-shm"):
            # Repair sidecars from an older deployment before opening SQLite.
            try:
                ensure_private_file(Path(str(self.path) + suffix))
            except FileNotFoundError:
                pass  # another SQLite connection can remove its journal meanwhile
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def agent(self, account: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM agents WHERE account = ?", (account,)).fetchone()

    def add_agent(self, account: str, address: str, encrypted_key: str, now: int) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO agents VALUES (?, ?, ?, ?)", (account, address, encrypted_key, now))

    def limits(self, account: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM limits WHERE account = ?", (account,)).fetchone()

    def set_limits(self, account: str, owner: str, daily: int, per: int, expiry: int, now: int) -> None:
        with self._db() as db:
            db.execute("""INSERT INTO limits VALUES (?, ?, ?, ?, ?, ?, ?)
                          ON CONFLICT(account) DO UPDATE SET owner = excluded.owner, daily_cap = excluded.daily_cap,
                          per_purchase_cap = excluded.per_purchase_cap, expiry = excluded.expiry, updated_at = excluded.updated_at""",
                       (account, owner, daily, per, expiry, now, now))

    def add_op(self, op: Mapping[str, Any]) -> None:
        with self._db() as db:
            db.execute("""INSERT INTO ops(op_id, account, kind, amount, message_hash, state, tx, detail, created_at, updated_at,
                          owner_pays_fee) VALUES (:op_id, :account, :kind, :amount, :message_hash, :state, NULL, '', :now, :now,
                          :owner_pays_fee)""", {"owner_pays_fee": 1, **dict(op)})

    def op(self, account: str, op_id: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM ops WHERE op_id = ? AND account = ?", (op_id, account)).fetchone()

    def update_op(self, op_id: str, state: str, now: int, tx: str | None = None, detail: str = "") -> None:
        with self._db() as db:
            db.execute("UPDATE ops SET state = ?, tx = COALESCE(?, tx), detail = ?, updated_at = ? WHERE op_id = ?",
                       (state, tx, detail, now, op_id))

    def spent_since(self, account: str, since: int) -> int:
        with self._db() as db:
            row = db.execute("SELECT COALESCE(SUM(amount), 0) FROM spends WHERE account = ? AND created_at >= ?",
                             (account, since)).fetchone()
            held = db.execute("SELECT COALESCE(SUM(amount), 0) FROM metered_holds WHERE account = ?", (account,)).fetchone()
        return int(row[0]) + int(held[0])

    def reserve_metered(self, hold_id: str, account: str, ceiling: int, daily_cap: int, now: int) -> None:
        """A durable ceiling counts alongside other Solana purchases until reconciled."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_budget(db, account, ceiling, now, daily_ceiling=daily_cap)
            db.execute("INSERT INTO metered_holds VALUES (?, ?, ?, ?)", (hold_id, account, ceiling, now))

    @staticmethod
    def _check_budget(db: sqlite3.Connection, account: str, amount: int, now: int,
                      *, daily_ceiling: int | None = None, per_ceiling: int | None = None) -> None:
        """Called inside the same write transaction that reserves the debit.

        setup() writes this database too. A completed change therefore applies to
        every new reservation, including workers in another process. Already
        reserved/in-flight payments remain counted; they cannot safely be recalled.
        """
        limits = db.execute("SELECT * FROM limits WHERE account = ?", (account,)).fetchone()
        if limits is None:
            raise AllowanceUnavailable("Set your limits and approve them first.")
        if type(amount) is not int or amount <= 0:
            raise AllowanceError("The payment must be a positive integer amount of atomic USDC.")
        if limits["expiry"] <= now:
            raise AllowanceError("Your Solana allowance has expired. Set new limits to continue.")
        daily = min(limits["daily_cap"], daily_ceiling) if daily_ceiling is not None else limits["daily_cap"]
        per = min(limits["per_purchase_cap"], per_ceiling) if per_ceiling is not None else limits["per_purchase_cap"]
        if amount > per:
            raise AllowanceError(f"{_text(amount)} is above your {_text(per)} per-purchase limit. Nothing was paid.")
        spent = db.execute("SELECT COALESCE(SUM(amount), 0) FROM spends WHERE account = ? AND created_at >= ?",
                           (account, now // DAY * DAY)).fetchone()[0]
        held = db.execute("SELECT COALESCE(SUM(amount), 0) FROM metered_holds WHERE account = ?", (account,)).fetchone()[0]
        used = int(spent) + int(held)
        if used + amount > daily:
            raise AllowanceError(f"That would pass today's {_text(daily)} limit "
                                 f"({_text(max(0, daily - used))} left). Nothing was paid.")

    def reserve_spend(self, account: str, amount: int, purpose: str, now: int,
                      *, daily_ceiling: int, per_ceiling: int) -> int:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_budget(db, account, amount, now, daily_ceiling=daily_ceiling, per_ceiling=per_ceiling)
            return int(db.execute("INSERT INTO spends(account, amount, purpose, pull_tx, created_at) VALUES (?, ?, ?, NULL, ?)",
                                  (account, amount, purpose[:200], now)).lastrowid)

    def claim_gas_submission(self, account: str, op_id: str, now: int) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT 1 FROM ops WHERE account=? AND kind='FUND_GAS' "
                                 "AND state IN ('VALIDATING','SUBMITTING','UNCERTAIN')", (account,)).fetchone()
            if pending:
                raise AllowanceError("A previous SOL transfer needs confirmation. Nothing more was sent.")
            return db.execute("UPDATE ops SET state='VALIDATING', updated_at=? WHERE account=? AND op_id=? AND kind='FUND_GAS' AND state='PREPARED'",
                              (now, account, op_id)).rowcount == 1

    def checkpoint_gas(self, account: str, op_id: str, tx: str, blockhash: str, now: int) -> bool:
        with self._db() as db:
            return db.execute("UPDATE ops SET state='SUBMITTING', tx=?, tx_blockhash=?, updated_at=? "
                              "WHERE account=? AND op_id=? AND state='VALIDATING'",
                              (tx, blockhash, now, account, op_id)).rowcount == 1

    def finish_pending_op(self, account: str, op_id: str, state: str, detail: str, now: int,
                          *, expected_state: str | None = None) -> None:
        with self._db() as db:
            db.execute("UPDATE ops SET state=?, detail=?, updated_at=? WHERE account=? AND op_id=? "
                       "AND state IN ('VALIDATING','SUBMITTING','UNCERTAIN') AND (? IS NULL OR state=?)",
                       (state, detail, now, account, op_id, expected_state, expected_state))

    def expire_prepared_op(self, account: str, op_id: str, now: int) -> None:
        with self._db() as db:
            db.execute("UPDATE ops SET state='EXPIRED', updated_at=?, detail=? WHERE account=? AND op_id=? AND state='PREPARED'",
                       (now, "Too late for this transaction; prepare it again.", account, op_id))

    def pending_gas_ops(self, account: str) -> list[sqlite3.Row]:
        with self._db() as db:
            return db.execute("SELECT * FROM ops WHERE account=? AND kind='FUND_GAS' "
                              "AND state IN ('VALIDATING','SUBMITTING','UNCERTAIN')", (account,)).fetchall()

    def pending_gas(self, account: str) -> bool:
        return bool(self.pending_gas_ops(account))

    def release_metered(self, hold_id: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM metered_holds WHERE hold_id = ?", (hold_id,))

    def settle_metered(self, hold_id: str, amount: int, tx: str, now: int) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            held = db.execute("SELECT * FROM metered_holds WHERE hold_id = ?", (hold_id,)).fetchone()
            if held is None or type(amount) is not int or not 0 <= amount <= held["amount"]:
                raise AllowanceError("Metered settlement has no matching reservation. Do not retry payment.")
            db.execute("INSERT INTO spends(account, amount, purpose, pull_tx, created_at) VALUES (?, ?, ?, ?, ?)",
                       (held["account"], amount, "SingIt Ask · actual usage", tx, now))
            db.execute("DELETE FROM metered_holds WHERE hold_id = ?", (hold_id,))

    def add_spend(self, account: str, amount: int, purpose: str, pull_tx: str | None, now: int) -> int:
        with self._db() as db:
            return int(db.execute("INSERT INTO spends(account, amount, purpose, pull_tx, created_at) VALUES (?, ?, ?, ?, ?)",
                                  (account, amount, purpose[:200], pull_tx, now)).lastrowid)

    def confirm_spend(self, spend_id: int, pull_tx: str | None) -> None:
        with self._db() as db:
            db.execute("UPDATE spends SET pull_tx = COALESCE(?, pull_tx) WHERE id = ?", (pull_tx, spend_id))

    def settled_spend_count(self, account: str) -> int:
        with self._db() as db:
            return int(db.execute("SELECT COUNT(*) FROM spends WHERE account = ? AND pull_tx IS NOT NULL",
                                  (account,)).fetchone()[0])

    def settled_spends_between(self, account: str, since: int, until: int) -> list[tuple[int, str, int]]:
        """(amount, purpose, created_at) of settled spends (pull_tx set) in [since, until]; for the public report."""
        with self._db() as db:
            rows = db.execute("SELECT amount, purpose, created_at FROM spends WHERE account = ? AND pull_tx IS NOT NULL "
                              "AND created_at >= ? AND created_at <= ? ORDER BY created_at",
                              (account, since, until)).fetchall()
        return [(int(r["amount"]), str(r["purpose"]), int(r["created_at"])) for r in rows]

    def last_approval(self, account: str) -> str | None:
        """'GRANT' or 'REVOKE': the wallet's latest finished approval change, from our own records."""
        with self._db() as db:
            row = db.execute("SELECT kind FROM ops WHERE account = ? AND kind IN ('GRANT', 'REVOKE') AND state = 'DONE' "
                             "ORDER BY updated_at DESC, created_at DESC LIMIT 1", (account,)).fetchone()
        return row["kind"] if row else None

    def drop_spend(self, spend_id: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM spends WHERE id = ?", (spend_id,))


class SolanaAllowanceService:
    """Limits, the wallet's approval and revoke, and funding a purchase, for `solana:` accounts."""

    def __init__(self, *, store: SolanaAllowanceStore, bridge: Any, fernet: Any, fee_payer_key: Callable[[], str] | None,
                 max_daily: int, max_per_purchase: int, max_days: int, max_grant: int,
                 now: Callable[[], float] = time.time):
        self.store, self.bridge, self.fernet, self.fee_payer_key = store, bridge, fernet, fee_payer_key
        self.max_daily, self.max_per_purchase, self.max_days, self.max_grant = max_daily, max_per_purchase, max_days, max_grant
        self.now = now
        self._spend_lock = threading.Lock()
        # What the chain said a moment ago, for showing and routing: one chat message asks several times, and
        # the public RPC refuses bursts. A payment always reads it fresh; paying or signing forgets it.
        self._seen: dict[str, tuple[float, dict[str, Any]]] = {}

    # -- identity --

    @staticmethod
    def owner(account: str) -> str:
        if not str(account).startswith(SOLANA_PREFIX):
            raise AllowanceUnavailable("Not a Solana account.")
        return str(account)[len(SOLANA_PREFIX):]

    def agent_key(self, account: str) -> tuple[str, str]:
        """The account's agent keypair (address, base58 secret), created on first use."""
        row = self.store.agent(account)
        if row is None:
            address, secret = generate_keypair()
            self.store.add_agent(account, address, self.fernet.encrypt(secret.encode()).decode(), int(self.now()))
            row = self.store.agent(account)
        return row["agent_address"], self.fernet.decrypt(row["encrypted_key"].encode()).decode()

    def _call(self, account: str, operation: str, *, fee_payer: bool = False, **payload: Any) -> dict[str, Any]:
        from .solana_chat import SolanaChatError
        agent, key = self.agent_key(account)
        started = time.monotonic()
        try:
            return self.bridge.run(account, agent, key, operation,
                                   fee_payer_key=self.fee_payer_key() if fee_payer and self.fee_payer_key else None, **payload)
        except SolanaChatError as exc:
            refusal = AllowanceError(str(exc.text))  # already a sentence for the user
            refusal.code = exc.code  # whether the money may have moved: see spend()
            raise refusal from None
        finally:
            key = None
            logger.info("solana allowance: %s took %.1fs", operation, time.monotonic() - started)

    # -- reads --

    SEEN_SECONDS = 10

    def chain_state(self, account: str, *, fresh: bool = True) -> dict[str, Any]:
        seen = self._seen.get(account)
        if not fresh and seen is not None and self.now() - seen[0] < self.SEEN_SECONDS:
            return seen[1]
        state = self._call(account, "allowance-state", owner=self.owner(account))
        self._seen[account] = (self.now(), state)
        return state

    def status(self, account: str) -> dict[str, Any]:
        owner = self.owner(account)
        limits = self.store.limits(account)
        if limits is None:
            return {"configured": False, "chain": "solana", "owner": owner}
        chain = self.chain_state(account, fresh=False)
        allowed = int(chain["owner"]["delegatedToAgent"])
        spent = self.store.spent_since(account, int(self.now()) // DAY * DAY)
        expired = limits["expiry"] <= self.now()
        state = "expired" if expired else "granted" if allowed > 0 else "waiting_for_grant"
        return {
            "configured": True, "chain": "solana", "state": state, "owner": owner,
            "limiter": self.agent_key(account)[0],  # the delegate plays the limiter's part on Solana
            "dailyCapAtomic": limits["daily_cap"], "perPurchaseCapAtomic": limits["per_purchase_cap"],
            "remainingTodayAtomic": max(0, limits["daily_cap"] - spent), "allowanceAtomic": allowed,
            "floatAtomic": int(chain["agent"]["usdcAtomic"]), "ownerUsdcAtomic": int(chain["owner"]["amount"]),
            "agentSolLamports": str(chain["agent"].get("solLamports") or "0"),
            "ownerSolLamports": str(chain["owner"].get("solLamports") or "0"),
            "expiry": limits["expiry"],
        }

    # -- limits and the wallet's approval --

    def setup(self, account: str, daily: Any, per: Any, days: Any = 30) -> dict[str, Any]:
        owner = self.owner(account)
        daily_cap, per_cap = _usdc(daily, "The daily cap"), _usdc(per, "The per-purchase cap")
        try:
            days = int(days or 30)
        except (TypeError, ValueError):
            raise AllowanceError("Days must be a whole number.") from None
        if per_cap > daily_cap:
            raise AllowanceError("The per-purchase cap cannot be above the daily cap.")
        if daily_cap > self.max_daily or per_cap > self.max_per_purchase or not 1 <= days <= self.max_days:
            raise AllowanceError(f"Limits must stay within {_text(self.max_daily)} a day, {_text(self.max_per_purchase)} "
                                 f"per purchase and {self.max_days} days.")
        agent, _ = self.agent_key(account)
        now = int(self.now())
        first_time = self.store.limits(account) is None
        self.store.set_limits(account, owner, daily_cap, per_cap, now + days * DAY, now)
        analytics.emit("limits_set", account, chain="solana", daily_cap_usd=analytics.usdc_to_usd_string(daily_cap),
                       per_purchase_cap_usd=analytics.usdc_to_usd_string(per_cap), expiry_days=days,
                       first_time=first_time)
        return {"limiter": agent, "dailyCapAtomic": daily_cap, "perPurchaseCapAtomic": per_cap, "expiry": now + days * DAY}

    def prepare_wallet(self, account: str, kind: str, *, amount: Any = None, **_: Any) -> dict[str, Any]:
        """The transaction the owner's wallet signs: approve the agent for a total, or revoke it."""
        owner = self.owner(account)
        kind = str(kind).upper()
        if kind == "GRANT":
            if self.store.limits(account) is None:
                raise AllowanceError("Set your limits first.")
            atomic = _usdc(amount, "The amount")
            if atomic > self.max_grant:
                raise AllowanceError(f"Allow at most {_text(self.max_grant)} at a time.")
        elif kind == "FUND_GAS":
            for pending in self.store.pending_gas_ops(account):
                self.operation(account, pending["op_id"])
            if self.store.pending_gas(account):
                raise AllowanceError("A previous SOL transfer needs confirmation. It was not repeated; check its transaction before funding again.")
            try:
                value = Decimal(str(amount)) * 1_000_000_000
                if not value.is_finite() or value != value.to_integral_value() or not 0 < value <= 100_000_000:
                    raise ValueError()
                atomic = int(value)
            except (ValueError, InvalidOperation):
                raise AllowanceError("Enter a SOL amount above zero and at most 0.1, with at most 9 decimal places.") from None
        elif kind == "REVOKE":
            atomic = 0
        else:
            raise AllowanceError("Grant or revoke only.")
        owner_sol = int(self.chain_state(account)["owner"].get("solLamports") or 0)
        if owner_sol < OWNER_FEE_LAMPORTS + (atomic if kind == "FUND_GAS" else 0):
            raise AllowanceError("Your wallet needs SOL for this transfer and its network fee. SingIt does not sponsor it.")
        owner_pays = True
        prepared = self._call(account, "allowance-prepare", owner=owner, ownerPaysFee=True,
                              kind={"GRANT": "approve", "REVOKE": "revoke", "FUND_GAS": "fund-gas"}[kind], amount=str(atomic))
        op_id = "sop_" + secrets.token_urlsafe(12)
        now = int(self.now())
        self.store.add_op({"op_id": op_id, "account": account, "kind": kind, "amount": atomic, "owner_pays_fee": int(owner_pays),
                           "message_hash": prepared["messageHash"], "state": "PREPARED", "now": now})
        agent = self.agent_key(account)[0]
        shows = (f"Your wallet will ask you to let {agent[:4]}…{agent[-4:]} spend up to {_text(atomic)} of your USDC."
                 if kind == "GRANT" else "Your wallet will ask you to revoke your agent's permission to spend your USDC.")
        if kind == "FUND_GAS":
            shows = f"Transfer {Decimal(atomic) / 1_000_000_000} SOL to your own agent {agent} for network fees and account rent. Your wallet also pays the network fee."
        return {"operation": op_id, "chain": "solana", "kind": kind, "transaction": prepared["transaction"],
                "walletShows": shows, "expiresAt": now + PREPARE_SECONDS}

    def submit_wallet(self, account: str, op_id: str, transaction: Any) -> dict[str, Any]:
        self._seen.pop(account, None)
        op = self.store.op(account, str(op_id))
        if op is None:
            raise AllowanceError("No such request.")
        if op["state"] != "PREPARED":
            return self.operation(account, op_id)
        now = int(self.now())
        if op["created_at"] + PREPARE_SECONDS < now:
            self.store.expire_prepared_op(account, op_id, now)
            return self.operation(account, op_id)
        if not op["owner_pays_fee"]:
            raise AllowanceError("This old sponsored request is disabled. Prepare a new transaction paid by your wallet.")
        owner_pays = True
        # Wallets add their own fee settings and guards before signing; the bridge accepts those only
        # around exactly this approve or revoke (solana-x402-service/src/allowance.mjs, doesOnly).
        if op["kind"] == "FUND_GAS" and not self.store.claim_gas_submission(account, op_id, now):
            return self.operation(account, op_id)
        payload = dict(owner=self.owner(account), ownerPaysFee=owner_pays, transaction=str(transaction or ""),
                       messageHash=op["message_hash"], kind={"GRANT": "approve", "REVOKE": "revoke", "FUND_GAS": "fund-gas"}[op["kind"]],
                       amount=str(op["amount"]))
        if op["kind"] == "FUND_GAS":
            try:
                inspected = self._call(account, "allowance-inspect", **payload)
                if not inspected.get("transaction") or not inspected.get("blockhash"):
                    raise AllowanceError("The signed transfer could not be checked. Nothing was sent.")
            except Exception:
                # inspect is read-only, so even a timeout here proves we did not submit.
                self.store.finish_pending_op(account, op_id, "FAILED", "The transfer could not be validated. Nothing was sent; prepare it again.", int(self.now()), expected_state="VALIDATING")
                raise
            if not self.store.checkpoint_gas(account, op_id, inspected["transaction"], inspected["blockhash"], int(self.now())):
                return self.operation(account, op_id)
        try:
            result = self._call(account, "allowance-submit", fee_payer=not owner_pays, **payload)
        except Exception as exc:
            if op["kind"] == "FUND_GAS":
                # These checks happen before send() in TokenAllowance. Every other
                # exception may have sent; the checkpoint permits read-only recovery.
                refused = getattr(exc, "code", None) in {"INVALID_TRANSACTION", "TRANSACTION_MISMATCH", "SIGNATURE_REQUIRED"}
                self.store.finish_pending_op(account, op_id, "FAILED" if refused else "UNCERTAIN",
                                             "Nothing was sent; prepare it again." if refused else "The transfer outcome is unknown. Check its status; it was not repeated.", int(self.now()))
            raise
        state = {"confirmed": "DONE", "failed": "FAILED", "rejected": "FAILED"}.get(result.get("state"), "UNCERTAIN")
        detail = {
            "DONE": (f"Allowance now {_text(op['amount'])}." if op["kind"] == "GRANT" else "Revoked: your agent can no longer spend."),
            "FAILED": "Solana refused the transaction. Nothing changed.",
            "UNCERTAIN": "Sent; Solana has not confirmed it yet. Check again in a moment.",
        }[state]
        if op["kind"] == "FUND_GAS" and state == "DONE":
            detail = f"Added {Decimal(op['amount']) / 1_000_000_000} SOL to your agent for network fees."
        if op["kind"] == "FUND_GAS":
            if result.get("transaction") != inspected["transaction"]:
                state, detail = "UNCERTAIN", "The transfer receipt needs confirmation. It was not repeated."
            self.store.finish_pending_op(account, op_id, state, detail, int(self.now()))
        else:
            self.store.update_op(op_id, state, int(self.now()), tx=result.get("transaction"), detail=detail)
            self._emit_op_event(op, state)
        return self.operation(account, op_id)

    def _emit_op_event(self, op: Mapping[str, Any], state: str) -> None:
        """approval_signed for a GRANT, revoke for a REVOKE; FUND_GAS is not a user-facing allowance event."""
        if op["kind"] not in ("GRANT", "REVOKE"):
            return
        name = "approval_signed" if op["kind"] == "GRANT" else "revoke"
        analytics.emit(name, op["account"], chain="solana", state=state,
                       amount_usd=analytics.usdc_to_usd_string(op["amount"]),
                       seconds_from_prepare=int(self.now()) - op["created_at"])

    def operation(self, account: str, op_id: str) -> dict[str, Any]:
        op = self.store.op(account, str(op_id))
        if op is None:
            raise AllowanceError("No such request.")
        if op["state"] == "VALIDATING" and op["updated_at"] + PREPARE_SECONDS < self.now():
            self.store.finish_pending_op(account, op_id, "FAILED", "Validation timed out before submission. Prepare the transfer again.", int(self.now()), expected_state="VALIDATING")
        elif op["state"] in ("SUBMITTING", "UNCERTAIN") and op["tx"]:
            try:
                result = self._call(account, "allowance-reconcile", transaction=op["tx"], blockhash=op["tx_blockhash"])
            except Exception:
                result = {}  # RPC/bridge failures never release an uncertain transfer
            state = {"confirmed": "DONE", "failed": "FAILED", "expired": "EXPIRED"}.get(result.get("state"))
            if state and result.get("transaction") == op["tx"]:
                detail = (f"Added {Decimal(op['amount']) / 1_000_000_000} SOL to your agent for network fees."
                          if state == "DONE" and op["kind"] == "FUND_GAS" else
                          "Confirmed on Solana." if state == "DONE" else
                          "The transfer expired without confirmation. Prepare it again." if state == "EXPIRED" else
                          "Solana refused the transaction. Nothing changed.")
                self.store.finish_pending_op(account, op_id, state, detail, int(self.now()))
                self._seen.pop(account, None)
        op = self.store.op(account, str(op_id))
        detail = op["detail"]
        if op["state"] in ("SUBMITTING", "UNCERTAIN") and not op["tx"]:
            detail = "This older transfer has no recovery signature. Contact support before sending another transfer."
        return {"operation": op["op_id"], "kind": op["kind"], "state": op["state"], "chain": "solana",
                "amountAtomic": str(op["amount"]), "currency": "SOL" if op["kind"] == "FUND_GAS" else "USDC", "txHash": op["tx"], "detail": detail,
                "explorer": f"https://solscan.io/tx/{op['tx']}" if op["tx"] else None}

    def stale_allowances(self, account: str) -> list[dict[str, Any]]:
        return []  # one delegate per token account: a new approval replaces the old one

    # -- paying --

    def spend(self, account: str, amount: int, purpose: str, pay: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Pay `amount` from the owner's account within the limits, and count it against today.

        `pay` makes the x402 payment (the agent as delegate, the merchant paying the fee). The
        limits are checked first and the purchase counted before `pay` runs; one purchase at a
        time, so two cannot both fit the same room. The count is taken back only when the
        payment surely did not leave: a lost answer may still have moved the money.
        """
        with self._spend_lock:
            if type(amount) is not int or amount <= 0:
                raise AllowanceError("The payment must be a positive integer amount of atomic USDC.")
            if self.store.limits(account) is None:
                raise AllowanceUnavailable("Set your limits and approve them first.")
            owner = self.chain_state(account)["owner"]
            if amount > int(owner["delegatedToAgent"]):
                raise AllowanceError("Your wallet's approval is used up or revoked. Approve a new allowance to continue. Nothing was paid.")
            if amount > int(owner["amount"]):
                raise AllowanceError(f"Your Solana wallet holds {_text(int(owner['amount']))}; this costs {_text(amount)}. Nothing was paid.")
            self._seen.pop(account, None)
            # After the potentially slow chain read, re-read current limits and
            # reserve atomically with every other worker and metered hold.
            spend_id = self.store.reserve_spend(account, amount, purpose, int(self.now()),
                                                daily_ceiling=self.max_daily, per_ceiling=self.max_per_purchase)
            try:
                result = pay()
            except Exception as exc:
                if getattr(exc, "code", None) in NOTHING_SENT_CODES:
                    self.store.drop_spend(spend_id)
                else:
                    logger.warning("solana allowance: %s payment of %s has no clear outcome; it stays counted",
                                   account, amount)
                raise
            state = result.get("state")
            category = "bitrefill" if purpose.startswith("Bitrefill") else "data_tool"
            if state in ("accepted", "confirmed"):
                before = self.store.settled_spend_count(account)
                self.store.confirm_spend(spend_id, result.get("transaction"))
                analytics.emit("purchase_completed", account, chain="solana", category=category,
                               amount_usd=analytics.usdc_to_usd_string(amount),
                               first_purchase=before == 0, purchase_n=before + 1)
            elif state in NOTHING_SENT_STATES:
                self.store.drop_spend(spend_id)
                analytics.emit("purchase_failed", account, chain="solana", category=category, reason="seller_error")
            else:
                logger.warning("solana allowance: %s payment of %s ended %r; it stays counted", account, amount, state)
        logger.info("solana allowance: %s paid %s for %s (%s)", account, amount, purpose[:60], result.get("state"))
        return result


def build_solana_allowance_from_env(master_key: str, bridge: Any = None,
                                    env: Mapping[str, str] | None = None) -> SolanaAllowanceService | None:
    values = os.environ if env is None else env
    if str(values.get(ENABLED_ENV, "0")).strip() != "1":
        return None
    from cryptography.fernet import Fernet

    from .agent_allowance import (DEFAULT_MAX_DAILY, DEFAULT_MAX_DAYS, DEFAULT_MAX_GRANT, DEFAULT_MAX_PER_PURCHASE,
                                  MAX_DAILY_ENV, MAX_DAYS_ENV, MAX_GRANT_ENV, MAX_PER_PURCHASE_ENV, _usdc_atomic)

    fernet = Fernet(master_key.encode("ascii"))
    blob = str(values.get(FEE_PAYER_ENV, "")).strip()  # pays gift-card transfers' SOL fee, paid back in USDC
    if bridge is None:
        from .solana_chat import SolanaBridge
        bridge = SolanaBridge(None, Path(str(values.get("SIGN402_SOLANA_CHAT_STATE_DIR", "") or
                                             Path.home() / ".sign402" / "solana-chat")) / "web-operations")
    return SolanaAllowanceService(
        store=SolanaAllowanceStore(Path(str(values.get(DB_ENV, "") or DEFAULT_DB)).expanduser()),
        bridge=bridge, fernet=fernet,
        fee_payer_key=(lambda: fernet.decrypt(blob.encode("ascii")).decode()) if blob else None,
        max_daily=_usdc_atomic(values.get(MAX_DAILY_ENV, DEFAULT_MAX_DAILY), MAX_DAILY_ENV),
        max_per_purchase=_usdc_atomic(values.get(MAX_PER_PURCHASE_ENV, DEFAULT_MAX_PER_PURCHASE), MAX_PER_PURCHASE_ENV),
        max_days=int(values.get(MAX_DAYS_ENV, DEFAULT_MAX_DAYS)),
        max_grant=_usdc_atomic(values.get(MAX_GRANT_ENV, DEFAULT_MAX_GRANT), MAX_GRANT_ENV),
    )


def encrypt_fee_payer_key(master_key: str) -> tuple[str, str]:
    """A new Solana keypair for our fee payer: its address, and the keypair encrypted with the master key."""
    from cryptography.fernet import Fernet

    address, secret = generate_keypair()
    return address, Fernet(master_key.encode("ascii")).encrypt(secret.encode()).decode()


if __name__ == "__main__":
    import sys

    from .keyring import load_master_key

    if sys.argv[1:] != ["new-fee-payer"]:
        print("usage: python -m sign402_gateway.solana_allowance new-fee-payer", file=sys.stderr)
        sys.exit(2)
    fee_address, fee_blob = encrypt_fee_payer_key(load_master_key())
    print(f"address {fee_address}")
    print(f"encrypted {fee_blob}")
