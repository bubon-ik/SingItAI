"""The Trezor allowance lane: one agent key and one on-chain limiter per user.

Design: docs/trezor-allowance-v1.md. The owner's USDC stay at the owner's Trezor
address. The owner grants an `AgentAllowance` limiter an ERC-20 allowance from
the device; the user's agent key — held here, encrypted like managed wallet keys
— spends through the limiter within caps fixed at deployment.

This module is phase 1 of the product integration: the agent key, the gas that
key needs, the limiter's deployment and the reads that describe it. Granting,
revoking, pausing and spending come in later phases.

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
GUARDIAN_ENV = "SIGN402_ALLOWANCE_GUARDIAN_ADDRESS"
MAX_DAILY_ENV = "SIGN402_ALLOWANCE_MAX_DAILY_USDC"
MAX_PER_PURCHASE_ENV = "SIGN402_ALLOWANCE_MAX_PER_PURCHASE_USDC"
MAX_DAYS_ENV = "SIGN402_ALLOWANCE_MAX_DAYS"
SOURCIFY_ENV = "SIGN402_ALLOWANCE_SOURCIFY"

DEFAULT_DB = Path.home() / ".sign402" / "allowance.db"
DEFAULT_RPC = "https://mainnet.base.org"
DEFAULT_MAX_DAILY = Decimal("100")
DEFAULT_MAX_PER_PURCHASE = Decimal("25")
DEFAULT_MAX_DAYS = 90

BASE_CHAIN_ID = 8453
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ARTIFACT_PATH = Path(__file__).resolve().parent / "contracts" / "agent_allowance.json"
SOURCIFY_API = "https://sourcify.dev/server/v2"
# Public endpoints behind Cloudflare refuse Python's default User-Agent.
USER_AGENT = "sign402-gateway/0.1 (+allowance)"

AGENT_GAS_TARGET_WEI = 200_000_000_000_000  # 0.0002 ETH: a deployment and many purchases
AGENT_GAS_MINIMUM_WEI = 50_000_000_000_000   # top up below 0.00005 ETH
MAX_GAS_TOP_UP_WEI = 2_000_000_000_000_000   # never send an agent more than 0.002 ETH at once
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

    def set_source(self, limiter: str, source: str) -> None:
        with self._db() as db:
            db.execute("UPDATE limiters SET source = ? WHERE limiter_address = ?", (source, limiter))


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
        guardian: str,
        gas_funder_key: Callable[[], str],
        artifact: Artifact,
        max_daily: int,
        max_per_purchase: int,
        max_days: int,
        publish_source: Callable[[str, str, Artifact], str] | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.store = store
        self.evm = evm
        self.fernet = fernet
        self.owners = dict(owners)
        self.guardian = to_checksum_address(guardian)
        self.gas_funder_key = gas_funder_key
        self.artifact = artifact
        self.max_daily = max_daily
        self.max_per_purchase = max_per_purchase
        self.max_days = max_days
        self.publish_source = publish_source
        self.now = now
        self._setup_lock = threading.Lock()

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
        tx = self.evm.send(self.gas_funder_key(), to=agent, value=top_up)
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

    # -- reads --

    def status(self, user_id: str) -> dict[str, Any]:
        self.owner_of(user_id)
        active = self.store.active_limiter(user_id)
        if active is None:
            return {"configured": False, "telegramText": "No Trezor allowance yet. Set one up with /allowance_setup <daily> <per purchase> <days>."}
        return {**self._describe(active), "configured": True}

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
    guardian = str(values.get(GUARDIAN_ENV, "")).strip()
    if not guardian:
        raise ValueError(f"{ENABLED_ENV}=1 needs {GUARDIAN_ENV}.")
    funder_blob = str(values.get(GAS_FUNDER_ENV, "")).strip()
    if not funder_blob:
        raise ValueError(f"{ENABLED_ENV}=1 needs {GAS_FUNDER_ENV} (Fernet-encrypted with the master key).")

    def funder_key() -> str:
        return fernet.decrypt(funder_blob.encode("ascii")).decode()

    rpc_url = str(values.get(RPC_ENV, "") or values.get("SIGN402_BASE_RPC_URL", "") or DEFAULT_RPC).strip()
    evm = EvmClient(JsonRpc(rpc_url))
    return AllowanceService(
        store=AllowanceStore(Path(str(values.get(DB_ENV, "") or DEFAULT_DB)).expanduser()),
        evm=evm,
        fernet=fernet,
        owners=owners,
        guardian=guardian,
        gas_funder_key=funder_key,
        artifact=Artifact.load(),
        max_daily=_usdc_atomic(values.get(MAX_DAILY_ENV, DEFAULT_MAX_DAILY), MAX_DAILY_ENV),
        max_per_purchase=_usdc_atomic(values.get(MAX_PER_PURCHASE_ENV, DEFAULT_MAX_PER_PURCHASE), MAX_PER_PURCHASE_ENV),
        max_days=int(values.get(MAX_DAYS_ENV, DEFAULT_MAX_DAYS)),
        publish_source=publish_to_sourcify if str(values.get(SOURCIFY_ENV, "1")) == "1" else None,
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
