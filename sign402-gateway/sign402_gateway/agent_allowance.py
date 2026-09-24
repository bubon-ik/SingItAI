"""The Trezor allowance lane: one agent key and one on-chain limiter per user.

Design: docs/trezor-allowance-v1.md. The owner's USDC stay at the owner's Trezor
address. The owner grants an `AgentAllowance` limiter an ERC-20 allowance from
the device; the user's agent key — held here, encrypted like managed wallet keys
— spends through the limiter within caps fixed at deployment.

Phases 1 and 2 of the product integration: the agent key, the gas that key
needs, the limiter's deployment and the reads that describe it; then granting
and revoking from the owner's Trezor, through the broker and the companion on
the owner's computer, and pausing through the guardian. Spending comes later.

Nothing is trusted that can be checked. After a deployment, every immutable is
read back from the chain and the runtime code is compared with the artifact the
tests ran against (immutables masked); a limiter that fails either is recorded
as rejected and never offered for a grant.

Off unless SIGN402_ALLOWANCE_ENABLED=1. While the lane is rolled out to its
owner only, SIGN402_ALLOWANCE_OWNERS is both the allowlist and the source of
each user's Trezor address: "telegramUserId:0xAddress[,...]".
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from eth_account import Account
from eth_utils import keccak, to_checksum_address

from .base_balances import BaseBalanceError

logger = logging.getLogger(__name__)

ENABLED_ENV = "SIGN402_ALLOWANCE_ENABLED"
OWNERS_ENV = "SIGN402_ALLOWANCE_OWNERS"
DB_ENV = "SIGN402_ALLOWANCE_DB"
RPC_ENV = "SIGN402_ALLOWANCE_RPC_URL"
GAS_FUNDER_ENV = "SIGN402_ALLOWANCE_GAS_FUNDER_KEY"
GUARDIAN_KEY_ENV = "SIGN402_ALLOWANCE_GUARDIAN_KEY"
BROKER_URL_ENV = "SIGN402_ALLOWANCE_BROKER_URL"
BROKER_TOKEN_ENV = "SIGN402_ALLOWANCE_BROKER_TOKEN"
MAX_GRANT_ENV = "SIGN402_ALLOWANCE_MAX_GRANT_USDC"
FLOAT_TARGET_ENV = "SIGN402_ALLOWANCE_FLOAT_TARGET_USDC"
FLOAT_LOW_ENV = "SIGN402_ALLOWANCE_FLOAT_LOW_USDC"
EXACT_ABOVE_ENV = "SIGN402_ALLOWANCE_EXACT_ABOVE_USDC"
MAX_DAILY_ENV = "SIGN402_ALLOWANCE_MAX_DAILY_USDC"
MAX_PER_PURCHASE_ENV = "SIGN402_ALLOWANCE_MAX_PER_PURCHASE_USDC"
MAX_DAYS_ENV = "SIGN402_ALLOWANCE_MAX_DAYS"
SOURCIFY_ENV = "SIGN402_ALLOWANCE_SOURCIFY"

DEFAULT_DB = Path.home() / ".sign402" / "allowance.db"
DEFAULT_RPC = "https://mainnet.base.org"
DEFAULT_MAX_DAILY = Decimal("100")
DEFAULT_MAX_PER_PURCHASE = Decimal("25")
DEFAULT_MAX_DAYS = 90
DEFAULT_MAX_GRANT = Decimal("300")
DEFAULT_BROKER_URL = "http://127.0.0.1:8122"
DEVICE_JOB_SECONDS = 600
DEFAULT_FLOAT_TARGET = Decimal("0.20")
DEFAULT_FLOAT_LOW = Decimal("0.05")
DEFAULT_EXACT_ABOVE = Decimal("0.05")
SETTLEMENT_WAIT_SECONDS = 60
TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()

BASE_CHAIN_ID = 8453
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ARTIFACT_PATH = Path(__file__).resolve().parent / "contracts" / "agent_allowance.json"
SOURCIFY_API = "https://sourcify.dev/server/v2"
# Public endpoints behind Cloudflare refuse Python's default User-Agent.
USER_AGENT = "sign402-gateway/0.1 (+allowance)"

AGENT_GAS_TARGET_WEI = 200_000_000_000_000  # 0.0002 ETH: a deployment and many purchases
AGENT_GAS_MINIMUM_WEI = 50_000_000_000_000   # top up below 0.00005 ETH
MAX_GAS_TOP_UP_WEI = 2_000_000_000_000_000   # never send an agent more than 0.002 ETH at once
FUNDER_FEE_RESERVE_WEI = 1_000_000_000_000   # 0.000001 ETH: the funder's own fee for a top-up


def _eth_text(wei: int) -> str:
    return f"{Decimal(wei) / Decimal(10**18):.6f}".rstrip("0").rstrip(".") + " ETH"
RECEIPT_WAIT_SECONDS = 120
SETTLE_WAIT_SECONDS = 40


class AllowanceError(RuntimeError):
    """A refusal the user should read. The message is safe to show."""


class AllowanceUnavailable(AllowanceError):
    """The lane is off, or this user is not on it."""


# --- the chain ------------------------------------------------------------------

class RpcRejected(Exception):
    """The node answered and said no. Not a transport failure: never retried."""


class JsonRpc:
    """Plain JSON-RPC that keeps the node's error message.

    The gateway's balance client folds every error into "request failed", which
    is right for balances and wrong here: "insufficient funds" and "the RPC is
    down" need different answers, and only the second is worth retrying.
    """

    def __init__(self, url: str, *, timeout: float = 15.0):
        self.url = url
        self.timeout = timeout
        self._next_id = 0
        self._lock = threading.Lock()

    def call(self, method: str, params: list) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
        request = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = json.loads(response.read(1024 * 1024))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise BaseBalanceError("Base RPC is unavailable") from None
        if not isinstance(reply, dict) or reply.get("id") != request_id:
            raise BaseBalanceError("Base RPC returned an invalid response")
        if "error" in reply:
            error = reply["error"] if isinstance(reply["error"], dict) else {}
            if error.get("code") in (-32005, 429) or "rate" in str(error.get("message", "")).lower():
                raise BaseBalanceError("Base RPC is rate limiting")
            raise RpcRejected(str(error.get("message") or "rejected")[:300])
        return reply.get("result")


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def _word(value: int | str) -> str:
    if isinstance(value, str):
        return value.lower().removeprefix("0x").rjust(64, "0")
    return format(int(value), "064x")


def encode_call(signature: str, *args: int | str) -> str:
    return selector(signature) + "".join(_word(a) for a in args)


class EvmClient:
    """Base JSON-RPC with what the lane needs: reads that survive rate limits and
    lagging nodes, and EIP-1559 transactions signed here.

    Public endpoints are load-balanced; a read straight after a receipt can land
    on a node a few blocks behind (seen on mainnet in T4). Callers that need the
    effect of a transaction use `wait_until`, not a single read.
    """

    def __init__(self, rpc: Any, *, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time):
        self.rpc = rpc
        self.sleep = sleep
        self.now = now
        self._chain_confirmed = False
        self._send_lock = threading.Lock()

    def call(self, method: str, params: list) -> Any:
        for wait in (1, 2, 4, 8, None):
            try:
                return self.rpc.call(method, params)
            except RpcRejected as rejected:
                raise AllowanceError(f"Base refused {method}: {rejected}") from None
            except BaseBalanceError:
                if wait is None:
                    raise AllowanceError("Base RPC is not answering. Nothing was changed; try again shortly.") from None
                self.sleep(wait)

    def require_base(self) -> None:
        if not self._chain_confirmed:
            if int(self.call("eth_chainId", []), 16) != BASE_CHAIN_ID:
                raise AllowanceError("The configured RPC is not Base mainnet.")
            self._chain_confirmed = True

    def call_word(self, to: str, data: str) -> int:
        self.require_base()
        result = self.call("eth_call", [{"to": to, "data": data}, "latest"])
        if not isinstance(result, str) or len(result) != 66:
            raise AllowanceError(f"{to} did not answer a read as expected.")
        return int(result, 16)

    def code(self, address: str) -> str:
        self.require_base()
        return str(self.call("eth_getCode", [address, "latest"]))

    def balance(self, address: str) -> int:
        self.require_base()
        return int(self.call("eth_getBalance", [address, "latest"]), 16)

    def usdc_balance(self, address: str) -> int:
        return self.call_word(USDC, encode_call("balanceOf(address)", address))

    def quote(self, sender: str, *, to: str | None, data: str = "0x", value: int = 0) -> dict[str, int]:
        """Gas, fees and the most the transaction can cost at current prices."""
        self.require_base()
        priority = int(self.call("eth_maxPriorityFeePerGas", []), 16)
        base_fee = int(self.call("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
        estimate_request = {"from": sender, "data": data, "value": hex(value)}
        if to:
            estimate_request["to"] = to
        gas = math.ceil(int(self.call("eth_estimateGas", [estimate_request]), 16) * 1.2)
        max_fee = 2 * base_fee + priority
        return {"gas": gas, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority, "maxCostWei": gas * max_fee + value}

    def send(self, private_key: str, *, to: str | None, data: str = "0x", value: int = 0) -> str:
        """Sign and broadcast one transaction; returns its hash."""
        self.require_base()
        account = Account.from_key(private_key)
        with self._send_lock:
            nonce = int(self.call("eth_getTransactionCount", [account.address, "pending"]), 16)
            fees = self.quote(account.address, to=to, data=data, value=value)
            transaction = {
                "type": 2,
                "chainId": BASE_CHAIN_ID,
                "nonce": nonce,
                "maxPriorityFeePerGas": fees["maxPriorityFeePerGas"],
                "maxFeePerGas": fees["maxFeePerGas"],
                "gas": fees["gas"],
                "value": value,
                "data": data,
            }
            if to:
                transaction["to"] = to_checksum_address(to)
            signed = Account.sign_transaction(transaction, private_key)
            raw = signed.raw_transaction.to_0x_hex()
            local_hash = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
            try:
                reported = self.call("eth_sendRawTransaction", [raw])
            except AllowanceError as error:
                # A broadcast retried after a dropped connection can find the
                # node already holding it: that is success, not a refusal.
                if "already known" not in str(error).lower():
                    raise
                reported = local_hash
            if str(reported).lower() != local_hash:
                raise AllowanceError(f"The RPC reported {reported} for transaction {local_hash}. Check both before retrying.")
            return local_hash

    def wait_receipt(self, tx_hash: str) -> dict[str, Any]:
        deadline = self.now() + RECEIPT_WAIT_SECONDS
        while self.now() < deadline:
            try:
                receipt = self.rpc.call("eth_getTransactionReceipt", [tx_hash])
            except BaseBalanceError:
                receipt = None
            if isinstance(receipt, dict):
                if int(receipt.get("status", "0x0"), 16) != 1:
                    raise AllowanceError(f"Transaction {tx_hash} reverted.")
                return receipt
            self.sleep(2)
        raise AllowanceError(f"Transaction {tx_hash} was sent but not mined within {RECEIPT_WAIT_SECONDS}s. Do not retry; check it first.")

    def wait_until(self, read: Callable[[], Any], accept: Callable[[Any], bool]) -> Any:
        """Read until the value shows the mined effect, or give up and return it."""
        deadline = self.now() + SETTLE_WAIT_SECONDS
        value = read()
        while not accept(value) and self.now() < deadline:
            self.sleep(2)
            value = read()
        return value


# --- the contract artifact ------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    bytecode: str
    deployed_bytecode: str
    immutable_ranges: tuple[tuple[int, int], ...]
    compiler_version: str
    standard_json_input: dict
    source_sha256: str

    @classmethod
    def load(cls, path: Path = ARTIFACT_PATH) -> "Artifact":
        data = json.loads(path.read_text())
        return cls(
            bytecode=data["bytecode"],
            deployed_bytecode=data["deployedBytecode"],
            immutable_ranges=tuple((int(s), int(n)) for s, n in data["immutableRanges"]),
            compiler_version=data["compilerVersion"],
            standard_json_input=data["standardJsonInput"],
            source_sha256=data["sourceSha256"],
        )

    def creation_data(self, owner: str, agent: str, guardian: str, daily: int, per_purchase: int, expiry: int) -> str:
        return self.bytecode + "".join(_word(v) for v in (USDC, owner, agent, guardian, daily, per_purchase, expiry))

    def masked(self, code_hex: str) -> bytes:
        code = bytearray(bytes.fromhex(code_hex.removeprefix("0x")))
        for start, length in self.immutable_ranges:
            code[start:start + length] = b"\x00" * length
        return bytes(code)

    def runs(self, code_hex: str) -> bool:
        return self.masked(code_hex) == self.masked(self.deployed_bytecode)


# --- storage ---------------------------------------------------------------------------

class AllowanceStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    user_id TEXT PRIMARY KEY,
                    agent_address TEXT NOT NULL UNIQUE,
                    encrypted_key TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS limiters (
                    limiter_address TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    owner_address TEXT NOT NULL,
                    agent_address TEXT NOT NULL,
                    guardian_address TEXT NOT NULL,
                    daily_cap INTEGER NOT NULL,
                    per_purchase_cap INTEGER NOT NULL,
                    expiry INTEGER NOT NULL,
                    deploy_tx TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'SUPERSEDED', 'REJECTED')),
                    source TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS limiters_by_user ON limiters(user_id, created_at);
                CREATE TABLE IF NOT EXISTS operations (
                    op_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('GRANT', 'REVOKE', 'PAUSE')),
                    limiter_address TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    job_id TEXT,
                    tx_hash TEXT,
                    state TEXT NOT NULL CHECK(state IN ('WAITING_DEVICE', 'BROADCAST', 'DONE', 'FAILED')),
                    detail TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operations_by_user ON operations(user_id, created_at);
                CREATE TABLE IF NOT EXISTS settlements (
                    tx_hash TEXT NOT NULL,
                    log_index TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    pay_to TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (tx_hash, log_index)
                );
                CREATE TABLE IF NOT EXISTS bitrefill_quotes (
                    quote_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    package TEXT NOT NULL,
                    name TEXT NOT NULL,
                    price_atomic INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS bitrefill_reveals (
                    invoice_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    revealed_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watch_cursors (
                    subject TEXT PRIMARY KEY,
                    block INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK(severity IN ('INFO', 'ALARM')),
                    text TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_outflows (
                    tx_hash TEXT NOT NULL,
                    log_index TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    first_seen INTEGER NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tx_hash, log_index)
                );
                """
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def agent(self, user_id: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM agents WHERE user_id = ?", (user_id,)).fetchone()

    def insert_agent(self, user_id: str, address: str, encrypted_key: str, now: int) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO agents (user_id, agent_address, encrypted_key, created_at) VALUES (?, ?, ?, ?)",
                (user_id, address, encrypted_key, now),
            )

    def active_limiter(self, user_id: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute(
                "SELECT * FROM limiters WHERE user_id = ? AND status = 'ACTIVE' ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()

    def record_limiter(self, row: Mapping[str, Any]) -> None:
        with self._db() as db:
            if row["status"] == "ACTIVE":
                db.execute("UPDATE limiters SET status = 'SUPERSEDED' WHERE user_id = ? AND status = 'ACTIVE'", (row["user_id"],))
            db.execute(
                """INSERT INTO limiters (limiter_address, user_id, owner_address, agent_address, guardian_address,
                   daily_cap, per_purchase_cap, expiry, deploy_tx, status, source, created_at)
                   VALUES (:limiter_address, :user_id, :owner_address, :agent_address, :guardian_address,
                   :daily_cap, :per_purchase_cap, :expiry, :deploy_tx, :status, :source, :created_at)""",
                dict(row),
            )

    def limiter(self, user_id: str, address: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute(
                "SELECT * FROM limiters WHERE user_id = ? AND lower(limiter_address) = lower(?)",
                (user_id, address),
            ).fetchone()

    def insert_op(self, row: Mapping[str, Any]) -> None:
        with self._db() as db:
            db.execute(
                """INSERT INTO operations (op_id, user_id, kind, limiter_address, amount, job_id, tx_hash,
                   state, detail, created_at, updated_at)
                   VALUES (:op_id, :user_id, :kind, :limiter_address, :amount, :job_id, :tx_hash,
                   :state, :detail, :created_at, :updated_at)""",
                dict(row),
            )

    def update_op(self, op_id: str, now: int, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with self._db() as db:
            db.execute(
                f"UPDATE operations SET {assignments}, updated_at = :updated_at WHERE op_id = :op_id",
                {**fields, "updated_at": now, "op_id": op_id},
            )

    def open_ops(self, user_id: str) -> list[sqlite3.Row]:
        with self._db() as db:
            return db.execute(
                "SELECT * FROM operations WHERE user_id = ? AND state IN ('WAITING_DEVICE', 'BROADCAST') ORDER BY created_at",
                (user_id,),
            ).fetchall()

    def recent_ops(self, user_id: str, limit: int = 3) -> list[sqlite3.Row]:
        with self._db() as db:
            return db.execute(
                "SELECT * FROM operations WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

    def counted_settlements(self, user_id: str) -> set[tuple[str, str]]:
        with self._db() as db:
            rows = db.execute("SELECT tx_hash, log_index FROM settlements WHERE user_id = ?", (user_id,)).fetchall()
        return {(row["tx_hash"].lower(), row["log_index"]) for row in rows}

    def count_settlement(self, tx_hash: str, log_index: str, user_id: str, pay_to: str, amount: int,
                         resource: str, now: int) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO settlements (tx_hash, log_index, user_id, pay_to, amount, resource, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tx_hash.lower(), log_index, user_id, pay_to, amount, resource, now),
            )

    def save_bitrefill_quote(self, row: Mapping[str, Any]) -> None:
        with self._db() as db:
            db.execute(
                """INSERT INTO bitrefill_quotes (quote_id, user_id, slug, package, name, price_atomic, created_at, expires_at)
                   VALUES (:quote_id, :user_id, :slug, :package, :name, :price_atomic, :created_at, :expires_at)""",
                dict(row),
            )

    def take_bitrefill_quote(self, user_id: str, quote_id: str, now: int) -> sqlite3.Row:
        """The quote, once: a confirmed price buys one order."""
        with self._db() as db:
            row = db.execute("SELECT * FROM bitrefill_quotes WHERE quote_id = ? AND user_id = ?",
                             (quote_id, user_id)).fetchone()
            if row is None:
                raise AllowanceError("That quote is not yours or does not exist. Quote again.")
            if row["used_at"] is not None:
                raise AllowanceError("That quote was already used. Quote again for another order.")
            if row["expires_at"] <= now:
                raise AllowanceError("That quote has expired. Quote again to see today's price.")
            db.execute("UPDATE bitrefill_quotes SET used_at = ? WHERE quote_id = ? AND used_at IS NULL", (now, quote_id))
            return row

    def reveal_once(self, user_id: str, invoice_id: str, now: int) -> bool:
        """True the first time a code is shown for this order, False after."""
        with self._db() as db:
            try:
                db.execute("INSERT INTO bitrefill_reveals (invoice_id, user_id, revealed_at) VALUES (?, ?, ?)",
                           (invoice_id, user_id, now))
            except sqlite3.IntegrityError:
                return False
        return True

    def unreveal(self, invoice_id: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM bitrefill_reveals WHERE invoice_id = ?", (invoice_id,))

    def active_limiters(self) -> list[sqlite3.Row]:
        with self._db() as db:
            return db.execute("SELECT * FROM limiters WHERE status = 'ACTIVE' ORDER BY created_at").fetchall()

    def cursor(self, subject: str) -> int | None:
        with self._db() as db:
            row = db.execute("SELECT block FROM watch_cursors WHERE subject = ?", (subject,)).fetchone()
        return None if row is None else int(row["block"])

    def set_cursor(self, subject: str, block: int) -> None:
        with self._db() as db:
            db.execute("INSERT INTO watch_cursors (subject, block) VALUES (?, ?) "
                       "ON CONFLICT(subject) DO UPDATE SET block = excluded.block", (subject, block))

    def add_alert(self, user_id: str, severity: str, text: str, now: int) -> None:
        with self._db() as db:
            db.execute("INSERT INTO alerts (user_id, severity, text, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, severity, text, now))

    def recent_alerts(self, user_id: str, limit: int = 3) -> list[sqlite3.Row]:
        with self._db() as db:
            return db.execute("SELECT * FROM alerts WHERE user_id = ? ORDER BY alert_id DESC LIMIT ?",
                              (user_id, limit)).fetchall()

    def note_outflow(self, tx_hash: str, log_index: str, user_id: str, recipient: str, amount: int, now: int) -> None:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO agent_outflows (tx_hash, log_index, user_id, recipient, amount, first_seen) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (tx_hash.lower(), log_index, user_id, recipient, amount, now))

    def open_outflows(self, user_id: str) -> list[sqlite3.Row]:
        with self._db() as db:
            return db.execute("SELECT * FROM agent_outflows WHERE user_id = ? AND resolved = 0", (user_id,)).fetchall()

    def resolve_outflow(self, tx_hash: str, log_index: str) -> None:
        with self._db() as db:
            db.execute("UPDATE agent_outflows SET resolved = 1 WHERE tx_hash = ? AND log_index = ?",
                       (tx_hash.lower(), log_index))

    def set_source(self, limiter: str, source: str) -> None:
        with self._db() as db:
            db.execute("UPDATE limiters SET source = ? WHERE limiter_address = ?", (source, limiter))


# --- the owner's device, through the broker -------------------------------------------------

class BrokerClient:
    """The loopback broker that hands device jobs to the owner's companion."""

    def __init__(self, url: str, token: str, *, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read() or b"{}")
            except ValueError:
                return error.code, {}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise AllowanceError("The Trezor link on the server is not answering. Nothing was changed.") from None

    def companion(self, user_id: str) -> dict | None:
        status, body = self._request("GET", f"/v1/internal/companions/{user_id}")
        if status == 404:
            return None
        if status != 200:
            raise AllowanceError("The Trezor link on the server refused the request. Nothing was changed.")
        return body.get("companion")

    def create_job(self, user_id: str, kind: str, key: str, payload: dict, expires_at: int) -> dict:
        status, body = self._request("POST", "/v1/internal/jobs", {
            "userId": user_id, "kind": kind, "idempotencyKey": key, "payload": payload, "expiresAt": expires_at,
        })
        if status != 202 or not isinstance(body.get("job"), dict):
            raise AllowanceError("Could not reach your Trezor companion. Is it running on your computer?")
        return body["job"]

    def job(self, job_id: str) -> dict:
        status, body = self._request("GET", f"/v1/internal/jobs/{job_id}")
        if status != 200 or not isinstance(body.get("job"), dict):
            raise AllowanceError("The Trezor link on the server did not return the request.")
        return body["job"]


def verify_signed_approve(raw_tx: str, owner: str, spender: str, amount_atomic: int) -> str:
    """The transaction hash, if and only if raw_tx is exactly this approve from owner.

    The owner's computer already checked it before returning it; this checks
    again, here, because this is where it is broadcast.
    """
    import rlp

    try:
        raw = bytes.fromhex(raw_tx.removeprefix("0x"))
        if not raw or raw[0] != 2:
            raise ValueError("not an EIP-1559 transaction")
        fields = rlp.decode(raw[1:])
        chain_id, _nonce, _priority, _max_fee, _gas, to, value, data, access_list = fields[:9]
        expected = bytes.fromhex(encode_call("approve(address,uint256)", spender, amount_atomic)[2:])
        if (
            len(fields) != 12
            or int.from_bytes(chain_id, "big") != BASE_CHAIN_ID
            or to != bytes.fromhex(USDC[2:])
            or int.from_bytes(value, "big") != 0
            or data != expected
            or access_list != []
            or Account.recover_transaction(raw_tx).lower() != owner.lower()
        ):
            raise ValueError("not the requested approve")
    except Exception:
        raise AllowanceError(
            "What came back from your Trezor is not the approve that was asked for. It was not sent."
        ) from None
    return "0x" + keccak(raw).hex()


DEVICE_ERRORS = {
    "device_rejected": "You cancelled it on the Trezor. Nothing changed.",
    "device_timeout": "The Trezor was not confirmed in time. Nothing changed.",
    "limiter_invalid": (
        "Your computer refused to show this on the Trezor: the spender is not your verified limiter. "
        "Nothing was signed. If you did not change anything yourself, treat this as an attack."
    ),
    "limit_exceeded": "The amount is above the limit set on your computer (SIGN402_TREZOR_POC_MAX_USD). Nothing changed.",
    "not_paired": "Your computer has no paired Trezor. Pair it again, then retry. Nothing changed.",
    "pairing_mismatch": "The Trezor paired on your computer does not match. Nothing changed.",
    "device_busy": "The Trezor was busy with another request. Nothing changed; try again.",
}


# --- the service ---------------------------------------------------------------------------

def _usdc_atomic(value: Any, name: str) -> int:
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise AllowanceError(f"{name} must be a USDC amount such as 10 or 2.50.") from None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.000001")):
        raise AllowanceError(f"{name} must be a positive USDC amount with at most six decimals.")
    return int(amount * 1_000_000)


def _usdc_text(atomic: int) -> str:
    text = f"{Decimal(atomic) / Decimal(1_000_000):.6f}".rstrip("0").rstrip(".")
    return f"{text} USDC"


def parse_owners(raw: str) -> dict[str, str]:
    owners = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        user_id, _, address = item.partition(":")
        if not user_id.strip() or not address.strip():
            raise ValueError(f"{OWNERS_ENV} entries are telegramUserId:0xAddress, got {item!r}")
        owners[user_id.strip()] = to_checksum_address(address.strip())
    return owners


class AllowanceService:
    def __init__(
        self,
        *,
        store: AllowanceStore,
        evm: EvmClient,
        fernet: Any,
        owners: Mapping[str, str],
        guardian_key: Callable[[], str],
        gas_funder_key: Callable[[], str],
        artifact: Artifact,
        max_daily: int,
        max_per_purchase: int,
        max_days: int,
        publish_source: Callable[[str, str, Artifact], str] | None = None,
        broker: BrokerClient | None = None,
        max_grant: int = int(DEFAULT_MAX_GRANT * 1_000_000),
        float_target: int = int(DEFAULT_FLOAT_TARGET * 1_000_000),
        float_low: int = int(DEFAULT_FLOAT_LOW * 1_000_000),
        exact_above: int = int(DEFAULT_EXACT_ABOVE * 1_000_000),
        now: Callable[[], float] = time.time,
    ):
        self.store = store
        self.evm = evm
        self.fernet = fernet
        self.owners = dict(owners)
        self.guardian_key = guardian_key
        self.guardian = Account.from_key(guardian_key()).address
        self.gas_funder_key = gas_funder_key
        self.artifact = artifact
        self.max_daily = max_daily
        self.max_per_purchase = max_per_purchase
        self.max_days = max_days
        self.publish_source = publish_source
        self.broker = broker
        self.max_grant = max_grant
        self.float_target = float_target
        self.float_low = float_low
        self.exact_above = exact_above
        self.now = now
        self._spend_lock = threading.Lock()
        self._setup_lock = threading.Lock()
        self._ops_lock = threading.Lock()

    # -- who --

    def owner_of(self, user_id: str) -> str:
        owner = self.owners.get(str(user_id))
        if not owner:
            raise AllowanceUnavailable("The Trezor allowance is not enabled for this account.")
        return owner

    def agent_key(self, user_id: str) -> tuple[str, str]:
        """(address, private key) for this user's agent, created on first use."""
        row = self.store.agent(user_id)
        if row is None:
            account = Account.create()
            self.store.insert_agent(
                user_id,
                account.address,
                self.fernet.encrypt(account.key.to_0x_hex().encode()).decode("ascii"),
                int(self.now()),
            )
            logger.info("allowance: created agent %s for user %s", account.address, user_id)
            row = self.store.agent(user_id)
        key = self.fernet.decrypt(row["encrypted_key"].encode("ascii")).decode()
        if Account.from_key(key).address != row["agent_address"]:
            raise AllowanceError("The stored agent key does not match its address.")
        return row["agent_address"], key

    # -- setup --

    def setup(self, user_id: str, daily: Any, per_purchase: Any, days: Any) -> dict[str, Any]:
        owner = self.owner_of(user_id)
        daily_cap = _usdc_atomic(daily, "The daily cap")
        per_purchase_cap = _usdc_atomic(per_purchase, "The per-purchase cap")
        try:
            lifetime = int(str(days).strip())
        except ValueError:
            raise AllowanceError("The number of days must be a whole number.") from None
        if per_purchase_cap > daily_cap:
            raise AllowanceError("The per-purchase cap cannot be above the daily cap.")
        if daily_cap > self.max_daily or per_purchase_cap > self.max_per_purchase:
            raise AllowanceError(
                f"Caps are limited to {_usdc_text(self.max_daily)} a day and "
                f"{_usdc_text(self.max_per_purchase)} a purchase while the lane is in testing."
            )
        if not 1 <= lifetime <= self.max_days:
            raise AllowanceError(f"The allowance can last 1 to {self.max_days} days.")

        with self._setup_lock:
            active = self.store.active_limiter(user_id)
            if (active is not None and active["daily_cap"] == daily_cap
                    and active["per_purchase_cap"] == per_purchase_cap and active["expiry"] > self.now() + 86400):
                return {**self._describe(active), "created": False}

            agent, agent_key = self.agent_key(user_id)
            previous = active
            expiry = int(self.now()) + lifetime * 86400
            data = self.artifact.creation_data(owner, agent, self.guardian, daily_cap, per_purchase_cap, expiry)
            self.ensure_gas(agent, self.evm.quote(agent, to=None, data=data)["maxCostWei"])
            tx = self.evm.send(agent_key, to=None, data=data)
            receipt = self.evm.wait_receipt(tx)
            limiter = to_checksum_address(receipt["contractAddress"])
            row = {
                "limiter_address": limiter, "user_id": user_id, "owner_address": owner,
                "agent_address": agent, "guardian_address": self.guardian,
                "daily_cap": daily_cap, "per_purchase_cap": per_purchase_cap, "expiry": expiry,
                "deploy_tx": tx, "status": "ACTIVE", "source": "pending", "created_at": int(self.now()),
            }
            problem = self._verify_deployment(row)
            if problem:
                self.store.record_limiter({**row, "status": "REJECTED"})
                raise AllowanceError(f"The deployed limiter {limiter} failed verification: {problem}. It will not be used.")
            self.store.record_limiter(row)
            logger.info("allowance: deployed %s for user %s in %s", limiter, user_id, tx)

        if self.publish_source is not None:
            try:
                self.store.set_source(limiter, self.publish_source(limiter, tx, self.artifact))
            except Exception as exc:  # publication must not undo a verified deployment
                logger.warning("allowance: source publication for %s failed: %s", limiter, exc)
                self.store.set_source(limiter, "failed")
        result = {**self._describe(self.store.active_limiter(user_id)), "created": True}
        if previous is not None:
            # Replacing a limiter does not touch what the Trezor granted the old
            # one: that allowance is on chain until the owner revokes it.
            left = self.evm.call_word(USDC, encode_call(
                "allowance(address,address)", previous["owner_address"], previous["limiter_address"]))
            if left:
                result["previousLimiter"] = previous["limiter_address"]
                result["previousAllowanceAtomic"] = left
                result["telegramText"] += (
                    f"\n\nYour previous limiter {previous['limiter_address']} still holds an allowance of "
                    f"{_usdc_text(left)} from your Trezor. Revoke it from the device; replacing a limiter does not."
                )
        return result

    def ensure_gas(self, agent: str, next_cost_wei: int) -> None:
        """Make sure the agent can pay for its next transaction at current prices.

        Tops up to the larger of the usual target and twice that cost, so a quiet
        day refills rarely and a busy one still clears. A top-up above
        MAX_GAS_TOP_UP_WEI is refused: a fee spike must not drain the operator's
        gas wallet into one agent.
        """
        balance = self.evm.balance(agent)
        if balance >= max(AGENT_GAS_MINIMUM_WEI, next_cost_wei):
            return
        top_up = max(AGENT_GAS_TARGET_WEI, 2 * next_cost_wei) - balance
        if top_up > MAX_GAS_TOP_UP_WEI:
            raise AllowanceError("Gas on Base is unusually expensive right now. Nothing was changed; try again later.")
        funder_key = self.gas_funder_key()
        funder = Account.from_key(funder_key).address
        available = self.evm.balance(funder)
        if available < top_up + FUNDER_FEE_RESERVE_WEI:
            logger.warning("allowance: gas funder %s has %s wei, needs %s", funder, available, top_up)
            raise AllowanceError(
                f"The operator's gas wallet {funder} has {_eth_text(available)} on Base, not enough to fund "
                f"your agent ({_eth_text(top_up + FUNDER_FEE_RESERVE_WEI)}). Nothing was changed; "
                "send ETH on Base to that address and try again."
            )
        tx = self.evm.send(funder_key, to=agent, value=top_up)
        self.evm.wait_receipt(tx)
        self.evm.wait_until(lambda: self.evm.balance(agent), lambda wei: wei >= balance + top_up)
        logger.info("allowance: funded agent %s with %s wei in %s", agent, top_up, tx)

    def _verify_deployment(self, row: Mapping[str, Any]) -> str | None:
        limiter = row["limiter_address"]
        code = self.evm.wait_until(lambda: self.evm.code(limiter), lambda c: len(c) > 2)
        if not self.artifact.runs(code):
            return "its code is not the tested AgentAllowance"
        expected = {
            "token()": int(USDC, 16), "owner()": int(row["owner_address"], 16),
            "agent()": int(row["agent_address"], 16), "guardian()": int(row["guardian_address"], 16),
            "dailyCap()": row["daily_cap"], "perPurchaseCap()": row["per_purchase_cap"],
            "expiry()": row["expiry"], "paused()": 0,
        }
        for getter, value in expected.items():
            if self.evm.call_word(limiter, selector(getter)) != value:
                return f"{getter} does not read back as deployed"
        return None

    # -- the owner's device: grant and revoke --

    def _verified_owner(self, user_id: str) -> str:
        owner = self.owner_of(user_id)
        if self.broker is None:
            raise AllowanceUnavailable("Granting from the Trezor is not set up on this server.")
        companion = self.broker.companion(user_id)
        if companion is None:
            raise AllowanceError("No Trezor companion is paired for your account. Pair it first.")
        if str(companion.get("walletAddress", "")).lower() != owner.lower():
            raise AllowanceError(
                "The Trezor paired through the companion is not the address on file for you. Nothing was changed."
            )
        return owner

    def grant(self, user_id: str, amount: Any) -> dict[str, Any]:
        owner = self._verified_owner(user_id)
        amount_atomic = _usdc_atomic(amount, "The allowance")
        if amount_atomic > self.max_grant:
            raise AllowanceError(f"Allowances are limited to {_usdc_text(self.max_grant)} while the lane is in testing.")
        active = self.store.active_limiter(user_id)
        if active is None:
            raise AllowanceError("Set up a limiter first with /allowance_setup <daily> <per purchase> <days>.")
        if active["expiry"] <= self.now() or self.evm.call_word(active["limiter_address"], selector("paused()")):
            raise AllowanceError("Your limiter is expired or paused. Set up a new one first.")
        return self._device_op(user_id, "GRANT", active["limiter_address"], amount_atomic, owner)

    def revoke(self, user_id: str, limiter: str | None = None) -> dict[str, Any]:
        owner = self._verified_owner(user_id)
        if limiter:
            row = self.store.limiter(user_id, str(limiter).strip())
            if row is None:
                raise AllowanceError("That limiter is not one of yours.")
        else:
            row = self.store.active_limiter(user_id)
            if row is None:
                raise AllowanceError("You have no limiter to revoke.")
        return self._device_op(user_id, "REVOKE", row["limiter_address"], 0, owner)

    def _device_op(self, user_id: str, kind: str, limiter: str, amount: int, owner: str) -> dict[str, Any]:
        with self._ops_lock:
            if self.store.open_ops(user_id):
                raise AllowanceError("A request is already waiting for your Trezor. Finish or let it expire first.")
            now = int(self.now())
            op_id = "op_" + keccak(text=f"{user_id}:{kind}:{limiter}:{amount}:{now}:{os.urandom(8).hex()}").hex()[:24]
            job = self.broker.create_job(
                user_id, "usdc_approve", f"allowance:{op_id}",
                {"spender": to_checksum_address(limiter), "amountAtomic": amount}, now + DEVICE_JOB_SECONDS,
            )
            self.store.insert_op({
                "op_id": op_id, "user_id": user_id, "kind": kind, "limiter_address": limiter, "amount": amount,
                "job_id": job["jobId"], "tx_hash": None, "state": "WAITING_DEVICE", "detail": "",
                "created_at": now, "updated_at": now,
            })
        what = f"an approve of {_usdc_text(amount)}" if amount else "an approve of 0 (revoke)"
        return {
            "operation": op_id, "state": "WAITING_DEVICE",
            "telegramText": (
                f"Confirm on your Trezor: {what} to\n{limiter}\n"
                "Your computer checks the limiter before the device shows anything. "
                "Then send /allowance_status."
            ),
        }

    def advance(self, user_id: str) -> None:
        """Move this user's pending grants and revokes forward, once each."""
        with self._ops_lock:
            for op in self.store.open_ops(user_id):
                try:
                    self._advance_op(op)
                except AllowanceError as error:
                    self.store.update_op(op["op_id"], int(self.now()), state="FAILED", detail=str(error))

    def _advance_op(self, op: sqlite3.Row) -> None:
        """One step for one operation. A transient failure leaves it where it is.

        WAITING_DEVICE -> the broker job finished: failed on the owner's side
        (FAILED, with the reason in words), or signed (checked here, then
        BROADCAST before sending, so a lost reply cannot send it twice).
        BROADCAST -> a receipt: DONE once the allowance reads back, FAILED if it
        reverted. No receipt yet: the same signed bytes are offered again, which
        a node that already has them answers with "already known".
        """
        now = int(self.now())
        if op["state"] == "WAITING_DEVICE":
            try:
                job = self.broker.job(op["job_id"])
            except AllowanceError:
                return
            state = job.get("state")
            if state in ("QUEUED", "LEASED"):
                return
            if state == "EXPIRED":
                self.store.update_op(op["op_id"], now, state="FAILED",
                                     detail="Nobody confirmed on the Trezor in time (is the companion running?). Nothing changed.")
                return
            if state != "SUCCEEDED":
                code = str(job.get("errorCode") or "failed")
                self.store.update_op(op["op_id"], now, state="FAILED", detail=DEVICE_ERRORS.get(
                    code, f"It failed on your computer ({code}). Nothing changed."))
                return
            raw = str((job.get("result") or {}).get("signedTransaction", ""))
            tx_hash = verify_signed_approve(raw, self.owners.get(op["user_id"], ""), op["limiter_address"], op["amount"])
            self.store.update_op(op["op_id"], now, state="BROADCAST", tx_hash=tx_hash)
            self._broadcast(raw)
            return

        # BROADCAST
        try:
            receipt = self.evm.rpc.call("eth_getTransactionReceipt", [op["tx_hash"]])
        except Exception:
            return
        if isinstance(receipt, dict):
            if int(receipt.get("status", "0x0"), 16) != 1:
                raise AllowanceError(f"The approve {op['tx_hash']} reverted on Base.")
            owner = self.owners.get(op["user_id"], "")
            left = self.evm.wait_until(
                lambda: self.evm.call_word(USDC, encode_call("allowance(address,address)", owner, op["limiter_address"])),
                lambda value: value == op["amount"],
            )
            self.store.update_op(op["op_id"], now, state="DONE", detail=f"Allowance now {_usdc_text(left)}.")
            return
        if now - op["updated_at"] > DEVICE_JOB_SECONDS:
            raise AllowanceError(f"The approve {op['tx_hash']} was sent but not mined. Check it on BaseScan before retrying.")
        try:
            job = self.broker.job(op["job_id"])
        except AllowanceError:
            return
        self._broadcast(str((job.get("result") or {}).get("signedTransaction", "")))

    def _broadcast(self, raw: str) -> None:
        try:
            self.evm.call("eth_sendRawTransaction", [raw])
        except AllowanceError as error:
            message = str(error).lower()
            if "already known" in message or "nonce too low" in message:
                return  # sent before; the receipt decides
            if "not answering" in message:
                return  # transport: stays BROADCAST, offered again next time
            if "insufficient funds" in message:
                raise AllowanceError(
                    "Your Trezor address needs a little ETH on Base to pay for the approve's gas. It was not sent."
                ) from None
            raise

    def _op_text(self, op: sqlite3.Row) -> str:
        when = time.strftime("%H:%M UTC", time.gmtime(op["created_at"]))
        what = {"GRANT": f"grant {_usdc_text(op['amount'])}", "REVOKE": "revoke", "PAUSE": "pause"}[op["kind"]]
        state = {"WAITING_DEVICE": "waiting for your Trezor", "BROADCAST": f"sent, {op['tx_hash']}",
                 "DONE": "done", "FAILED": "failed"}[op["state"]]
        detail = f" — {op['detail']}" if op["detail"] else ""
        return f"{when} {what}: {state}{detail}"

    # -- spending: the agent pays, the limiter funds it --

    def lane_for(self, user_id: str) -> sqlite3.Row | None:
        """The user's usable limiter, None if they are not on this lane at all.

        Listed with a limiter but unable to spend (no grant, paused, expired) is a
        refusal, not a fallback: an owner who set up the Trezor lane must never be
        paid for from a custodial wallet without knowing it.
        """
        if str(user_id) not in self.owners:
            return None
        active = self.store.active_limiter(str(user_id))
        if active is None:
            return None
        limiter = active["limiter_address"]
        if active["expiry"] <= self.now():
            raise AllowanceError("Your Trezor allowance has expired. Set up a new limiter with /allowance_setup.")
        if self.evm.call_word(limiter, selector("paused()")):
            raise AllowanceError("Your Trezor allowance is paused. Set up a new limiter to spend again.")
        owner = active["owner_address"]
        if not self.evm.call_word(USDC, encode_call("allowance(address,address)", owner, limiter)):
            raise AllowanceError("Nothing is granted from your Trezor yet. Grant an allowance with /allowance_grant <amount>.")
        return active

    def _fund(self, active: Mapping[str, Any], agent: str, agent_key: str, amount: int, ref_text: str) -> dict[str, Any]:
        """Make sure the agent holds `amount`: from its float, a refill, or exactly this."""
        limiter, owner = active["limiter_address"], active["owner_address"]
        float_now = self.evm.usdc_balance(agent)
        room = min(
            active["per_purchase_cap"],
            self.evm.call_word(limiter, selector("remainingToday()")),
            self.evm.call_word(USDC, encode_call("allowance(address,address)", owner, limiter)),
            self.evm.usdc_balance(owner),
        )
        if amount > self.exact_above:
            size, kind = amount, "exact"
        elif float_now - amount >= self.float_low:
            return {"funding": "float", "fundingTx": None, "floatBefore": float_now}
        else:
            size, kind = max(amount - float_now, min(self.float_target - float_now, room)), "refill"
            if size <= 0:
                if float_now >= amount:
                    return {"funding": "float", "fundingTx": None, "floatBefore": float_now}
                size = amount - float_now
        if size > room:
            raise AllowanceError(
                f"Your limiter cannot fund {_usdc_text(size)} now: it allows {_usdc_text(room)} "
                "(per purchase, left today, the allowance and your Trezor balance). Nothing was paid."
            )
        data = encode_call("spend(address,uint256,bytes32)", agent, size, keccak(text=ref_text).hex())
        self.ensure_gas(agent, self.evm.quote(agent, to=limiter, data=data)["maxCostWei"])
        tx = self.evm.send(agent_key, to=limiter, data=data)
        self.evm.wait_receipt(tx)
        self.evm.wait_until(lambda: self.evm.usdc_balance(agent), lambda held: held >= float_now + size)
        return {"funding": f"{kind} {_usdc_text(size)}", "fundingTx": tx, "floatBefore": float_now}

    def find_settlement(self, agent: str, pay_to: str, amount: int, from_block: int,
                        seen: set[tuple[str, str]] = frozenset()) -> tuple[str, str] | None:
        """(tx hash, log index) of a USDC Transfer agent -> pay_to of exactly `amount`,
        mined from `from_block` on and not already counted for another purchase.

        Two identical micro-payments to one seller are ordinary; without `seen`, the
        second would find the first's transfer and pass as paid.
        """
        deadline = self.now() + SETTLEMENT_WAIT_SECONDS
        while True:
            latest = int(self.evm.call("eth_blockNumber", []), 16)
            logs = self.evm.call("eth_getLogs", [{
                "fromBlock": hex(from_block), "toBlock": hex(latest), "address": USDC,
                "topics": [TRANSFER_TOPIC, "0x" + _word(agent), "0x" + _word(pay_to)],
            }]) or []
            for entry in logs:
                key = (str(entry["transactionHash"]).lower(), str(entry.get("logIndex", "0x0")))
                if int(entry["data"], 16) == amount and key not in seen:
                    return key
            if self.now() >= deadline:
                return None
            self.evm.sleep(3)

    def pay_x402(
        self,
        user_id: str,
        resource_url: str,
        requirements: Mapping[str, Any],
        x402_client: Callable[..., dict[str, Any]],
        *,
        method: str = "GET",
        request_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pay one x402 resource from the agent key, funded by the limiter.

        The caller has already decided the purchase may happen (limits, spending
        memory, the owner's approval when asked). This moves the money and proves
        it: paid means delivered (2xx) and a matching settlement on chain.
        """
        active = self.lane_for(user_id)
        if active is None:
            raise AllowanceUnavailable("The Trezor allowance is not set up for this account.")
        amount = int(requirements["amountAtomic"])
        pay_to = to_checksum_address(str(requirements["receiver"]))
        if str(requirements["asset"]).lower() != USDC.lower():
            raise AllowanceError("Only USDC on Base can be paid from the Trezor allowance.")
        agent, agent_key = self.agent_key(user_id)
        with self._spend_lock:
            funding = self._fund(active, agent, agent_key, amount,
                                 f"x402:{resource_url}:{amount}:{self.now()}:{os.urandom(4).hex()}")
            start = int(self.evm.call("eth_blockNumber", []), 16)
            kwargs = {"private_key": agent_key, "max_atomic": str(amount), "expected_receiver": pay_to,
                      "expected_asset": USDC}
            if method != "GET" or request_body is not None:
                kwargs.update(method=method, request_body=request_body)
            result = x402_client(resource_url, **kwargs)
            if "insufficient_funds" in json.dumps(result, default=str):
                # The facilitator's node can be a few blocks behind the funding.
                self.evm.sleep(6)
                result = x402_client(resource_url, **kwargs)
            status = int(result.get("status") or 0)
            found = self.find_settlement(agent, pay_to, amount, start, self.store.counted_settlements(user_id))
            settlement = None
            if found is not None:
                settlement = found[0]
                self.store.count_settlement(found[0], found[1], user_id, pay_to, amount, resource_url, int(self.now()))
        delivered = 200 <= status < 300
        return {
            "ok": delivered and settlement is not None,
            "status": status,
            "delivered": delivered,
            "settlementTx": settlement,
            "payer": agent,
            "limiter": active["limiter_address"],
            **funding,
            "resourceResult": result,
        }

    # -- the guardian: pause without the device --

    def pause(self, user_id: str) -> dict[str, Any]:
        self.owner_of(user_id)
        active = self.store.active_limiter(user_id)
        if active is None:
            raise AllowanceError("You have no limiter to pause.")
        limiter = active["limiter_address"]
        if self.evm.call_word(limiter, selector("paused()")):
            return {**self._describe(active), "paused": True}
        data = selector("pause()")
        self.ensure_gas(self.guardian, self.evm.quote(self.guardian, to=limiter, data=data)["maxCostWei"])
        tx = self.evm.send(self.guardian_key(), to=limiter, data=data)
        self.evm.wait_receipt(tx)
        self.evm.wait_until(lambda: self.evm.call_word(limiter, selector("paused()")), lambda flag: flag == 1)
        now = int(self.now())
        self.store.insert_op({
            "op_id": "op_" + tx[2:26], "user_id": user_id, "kind": "PAUSE", "limiter_address": limiter,
            "amount": 0, "job_id": None, "tx_hash": tx, "state": "DONE", "detail": "Paused for good.",
            "created_at": now, "updated_at": now,
        })
        logger.info("allowance: guardian paused %s for user %s in %s", limiter, user_id, tx)
        described = self._describe(active)
        described["telegramText"] = (
            f"Paused. The limiter {limiter} can no longer move anything, permanently (tx {tx}).\n"
            "To stop the allowance on chain as well, revoke it from your Trezor with /allowance_revoke.\n\n"
            + described["telegramText"]
        )
        return described

    # -- reads --

    def status(self, user_id: str) -> dict[str, Any]:
        self.owner_of(user_id)
        self.advance(user_id)
        active = self.store.active_limiter(user_id)
        if active is None:
            return {"configured": False, "telegramText": "No Trezor allowance yet. Set one up with /allowance_setup <daily> <per purchase> <days>."}
        described = self._describe(active)
        alerts = self.store.recent_alerts(user_id)
        if alerts:
            described["telegramText"] += "\n\nWatcher:\n" + "\n".join(
                f"{time.strftime('%m-%d %H:%M UTC', time.gmtime(a['created_at']))} "
                f"{'ALARM ' if a['severity'] == 'ALARM' else ''}{a['text']}" for a in alerts)
        operations = [self._op_text(op) for op in self.store.recent_ops(user_id)]
        if operations:
            described["telegramText"] += "\n\nRecent requests:\n" + "\n".join(operations)
        return {**described, "configured": True, "operations": operations}

    def _describe(self, row: Mapping[str, Any]) -> dict[str, Any]:
        limiter, owner, agent = row["limiter_address"], row["owner_address"], row["agent_address"]
        remaining = self.evm.call_word(limiter, selector("remainingToday()"))
        allowance = self.evm.call_word(USDC, encode_call("allowance(address,address)", owner, limiter))
        paused = bool(self.evm.call_word(limiter, selector("paused()")))
        float_usdc = self.evm.usdc_balance(agent)
        owner_usdc = self.evm.usdc_balance(owner)
        expires = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(row["expiry"]))
        state = "paused" if paused else ("expired" if row["expiry"] <= self.now() else
                                         ("granted" if allowance > 0 else "waiting for a grant from your Trezor"))
        text = "\n".join([
            "Trezor allowance",
            f"Limiter: {limiter}",
            f"Caps: {_usdc_text(row['daily_cap'])} a day, {_usdc_text(row['per_purchase_cap'])} a purchase, until {expires}",
            f"Left today: {_usdc_text(remaining)}",
            f"Allowance from your Trezor: {_usdc_text(allowance)}",
            f"Agent float: {_usdc_text(float_usdc)}",
            f"Your Trezor address holds: {_usdc_text(owner_usdc)}",
            f"State: {state}",
            f"Check the contract from your phone: https://base.blockscout.com/address/{limiter}?tab=contract",
        ])
        return {
            "limiter": limiter, "owner": owner, "agent": agent, "guardian": row["guardian_address"],
            "dailyCapAtomic": row["daily_cap"], "perPurchaseCapAtomic": row["per_purchase_cap"],
            "expiry": row["expiry"], "remainingTodayAtomic": remaining, "allowanceAtomic": allowance,
            "paused": paused, "floatAtomic": float_usdc, "ownerUsdcAtomic": owner_usdc,
            "deployTx": row["deploy_tx"], "source": row["source"], "state": state, "telegramText": text,
        }


# --- source publication ---------------------------------------------------------------

def publish_to_sourcify(limiter: str, deploy_tx: str, artifact: Artifact, *, sleep=time.sleep) -> str:
    """Submit one deployment to Sourcify and wait for the match. Returns the match."""
    body = json.dumps({
        "stdJsonInput": artifact.standard_json_input,
        "compilerVersion": artifact.compiler_version.removeprefix("v"),
        "contractIdentifier": "src/AgentAllowance.sol:AgentAllowance",
        "creationTransactionHash": deploy_tx,
    }).encode()
    request = urllib.request.Request(
        f"{SOURCIFY_API}/verify/{BASE_CHAIN_ID}/{limiter}", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            job = json.load(response)["verificationId"]
    except urllib.error.HTTPError as error:
        # The same source already published for this address is the goal met.
        if error.code == 409 and b"already_verified" in error.read():
            return "exact_match"
        raise
    for _ in range(20):
        sleep(3)
        poll = urllib.request.Request(f"{SOURCIFY_API}/verify/{job}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(poll, timeout=30) as response:
            status = json.load(response)
        if status.get("isJobCompleted"):
            match = ((status.get("contract") or {}).get("match")) or "failed"
            return str(match)
    return "pending"


# --- wiring ---------------------------------------------------------------------------------

def build_allowance_service_from_env(
    master_key: str,
    env: Mapping[str, str] | None = None,
) -> AllowanceService | None:
    values = os.environ if env is None else env
    if str(values.get(ENABLED_ENV, "0")).strip() != "1":
        return None
    from cryptography.fernet import Fernet

    if not master_key:
        raise ValueError(f"{ENABLED_ENV}=1 needs the wallet master key to encrypt agent keys.")
    fernet = Fernet(master_key.encode("ascii"))
    owners = parse_owners(str(values.get(OWNERS_ENV, "")))
    if not owners:
        raise ValueError(f"{ENABLED_ENV}=1 needs {OWNERS_ENV}.")
    guardian_blob = str(values.get(GUARDIAN_KEY_ENV, "")).strip()
    if not guardian_blob:
        raise ValueError(f"{ENABLED_ENV}=1 needs {GUARDIAN_KEY_ENV} (Fernet-encrypted with the master key).")
    funder_blob = str(values.get(GAS_FUNDER_ENV, "")).strip()
    if not funder_blob:
        raise ValueError(f"{ENABLED_ENV}=1 needs {GAS_FUNDER_ENV} (Fernet-encrypted with the master key).")

    def funder_key() -> str:
        return fernet.decrypt(funder_blob.encode("ascii")).decode()

    def guardian_key() -> str:
        return fernet.decrypt(guardian_blob.encode("ascii")).decode()

    broker_token = str(values.get(BROKER_TOKEN_ENV, "")).strip()
    broker = (
        BrokerClient(str(values.get(BROKER_URL_ENV, "") or DEFAULT_BROKER_URL), broker_token)
        if broker_token else None
    )

    rpc_url = str(values.get(RPC_ENV, "") or values.get("SIGN402_BASE_RPC_URL", "") or DEFAULT_RPC).strip()
    evm = EvmClient(JsonRpc(rpc_url))
    return AllowanceService(
        store=AllowanceStore(Path(str(values.get(DB_ENV, "") or DEFAULT_DB)).expanduser()),
        evm=evm,
        fernet=fernet,
        owners=owners,
        guardian_key=guardian_key,
        gas_funder_key=funder_key,
        artifact=Artifact.load(),
        max_daily=_usdc_atomic(values.get(MAX_DAILY_ENV, DEFAULT_MAX_DAILY), MAX_DAILY_ENV),
        max_per_purchase=_usdc_atomic(values.get(MAX_PER_PURCHASE_ENV, DEFAULT_MAX_PER_PURCHASE), MAX_PER_PURCHASE_ENV),
        max_days=int(values.get(MAX_DAYS_ENV, DEFAULT_MAX_DAYS)),
        publish_source=publish_to_sourcify if str(values.get(SOURCIFY_ENV, "1")) == "1" else None,
        broker=broker,
        max_grant=_usdc_atomic(values.get(MAX_GRANT_ENV, DEFAULT_MAX_GRANT), MAX_GRANT_ENV),
        float_target=_usdc_atomic(values.get(FLOAT_TARGET_ENV, DEFAULT_FLOAT_TARGET), FLOAT_TARGET_ENV),
        float_low=_usdc_atomic(values.get(FLOAT_LOW_ENV, DEFAULT_FLOAT_LOW), FLOAT_LOW_ENV),
        exact_above=_usdc_atomic(values.get(EXACT_ABOVE_ENV, DEFAULT_EXACT_ABOVE), EXACT_ABOVE_ENV),
    )


def encrypt_operator_key(master_key: str) -> tuple[str, str]:
    """A new operator key (gas funder or guardian): (address, encrypted blob).

    The plain key is never printed. The blob goes into the gateway environment;
    fund the address with a little ETH on Base.
    """
    from cryptography.fernet import Fernet

    account = Account.create()
    blob = Fernet(master_key.encode("ascii")).encrypt(account.key.to_0x_hex().encode()).decode("ascii")
    return account.address, blob


if __name__ == "__main__":
    import sys

    from .keyring import load_master_key

    if sys.argv[1:] != ["new-operator-key"]:
        print("usage: python -m sign402_gateway.agent_allowance new-operator-key", file=sys.stderr)
        sys.exit(2)
    address, blob = encrypt_operator_key(load_master_key())
    print(f"address {address}")
    print(f"encrypted {blob}")
