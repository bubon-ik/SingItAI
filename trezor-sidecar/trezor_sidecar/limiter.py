"""Is this address the tested limiter, owned by this Trezor? Asked before the device.

Design: docs/trezor-allowance-v1.md. A grant is the one moment of consent for
everything the agent spends afterwards, and the device shows the spender only as
an address. A compromised server could ask this machine to approve a contract
that looks like a limiter and is not — no caps, or a different owner — and the
screen would look the same.

So this machine checks first, from the chain, with nothing from the server taken
on trust: the spender's runtime code is the AgentAllowance the tests and the
mutation check ran against (immutables masked, against `agent_allowance.json`,
kept identical to the gateway's copy by a test), its owner is this Trezor's
address, its token is USDC, it is not paused and not expired. Only then is the
device asked. Revoking checks nothing: setting an allowance to zero must work
for any spender, broken or hostile.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from eth_utils import keccak, to_checksum_address

from .base import BASE_USDC_ADDRESS, BaseRpcClient
from .errors import SafeError

ARTIFACT_PATH = Path(__file__).with_name("agent_allowance.json")

READ_BACKOFF_SECONDS = (1, 2, 4, 8)
"""Waits between retries of a read. Reads are idempotent, and public Base
endpoints refuse short bursts; a refused read is retried, never guessed."""


def _selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


_GETTERS = {
    name: _selector(f"{name}()")
    for name in (
        "owner", "token", "agent", "guardian", "dailyCap",
        "perPurchaseCap", "expiry", "paused", "remainingToday",
    )
}


def _word_address(word: int) -> str:
    if word >> 160:
        raise SafeError("limiter_invalid", "Limiter returned a malformed address.", 409)
    return to_checksum_address(word.to_bytes(20, "big"))


@dataclass(frozen=True)
class Limiter:
    address: str
    owner: str
    token: str
    agent: str
    guardian: str
    daily_cap: int
    per_purchase_cap: int
    expiry: int
    paused: bool
    remaining_today: int


def _read(read: Callable[[], object], sleep: Callable[[float], None]):
    """One chain read, retried with backoff while the RPC refuses it."""
    for wait in READ_BACKOFF_SECONDS:
        try:
            return read()
        except SafeError as error:
            if error.code != "base_rpc_unavailable":
                raise
        sleep(wait)
    try:
        return read()
    except SafeError as error:
        if error.code != "base_rpc_unavailable":
            raise
        raise SafeError(
            "base_rpc_unavailable",
            "Could not read from Base: the RPC kept refusing (rate limit or outage), "
            "or the address is not an AgentAllowance. Nothing was signed. Wait a "
            "minute and run it again.",
            503,
        ) from None


def inspect_limiter(
    rpc: BaseRpcClient, limiter: str, sleep: Callable[[float], None] = time.sleep
) -> Limiter:
    if not _read(lambda: rpc.has_code(limiter), sleep):
        raise SafeError("limiter_invalid", "There is no contract at that address on Base.", 409)
    words = {
        name: _read(lambda data=data: rpc.call_word(limiter, data), sleep)
        for name, data in _GETTERS.items()
    }
    if words["paused"] not in (0, 1):
        raise SafeError("limiter_invalid", "Limiter returned a malformed flag.", 409)
    return Limiter(
        address=limiter,
        owner=_word_address(words["owner"]),
        token=_word_address(words["token"]),
        agent=_word_address(words["agent"]),
        guardian=_word_address(words["guardian"]),
        daily_cap=words["dailyCap"],
        per_purchase_cap=words["perPurchaseCap"],
        expiry=words["expiry"],
        paused=bool(words["paused"]),
        remaining_today=words["remainingToday"],
    )


class LimiterArtifact:
    """The tested runtime code, with the byte ranges immutables occupy."""

    def __init__(self, path: Path = ARTIFACT_PATH):
        data = json.loads(path.read_text())
        self.deployed_bytecode = data["deployedBytecode"]
        self.ranges = [(int(start), int(length)) for start, length in data["immutableRanges"]]

    def _masked(self, code_hex: str) -> bytes:
        code = bytearray(bytes.fromhex(code_hex.removeprefix("0x")))
        for start, length in self.ranges:
            code[start:start + length] = b"\x00" * length
        return bytes(code)

    def runs(self, code_hex: str) -> bool:
        try:
            return self._masked(code_hex) == self._masked(self.deployed_bytecode)
        except ValueError:
            return False


def verify_for_grant(
    rpc: BaseRpcClient,
    spender: str,
    owner: str,
    *,
    now: int,
    artifact: LimiterArtifact | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Limiter:
    """Refuse, before the device is asked, any spender that is not our limiter."""
    code = _read(lambda: rpc.code(spender), sleep)
    if not (artifact or LimiterArtifact()).runs(code):
        raise SafeError(
            "limiter_invalid",
            "That spender is not the tested AgentAllowance limiter. Nothing was shown on the Trezor.",
            409,
        )
    limiter = inspect_limiter(rpc, spender, sleep)
    if limiter.owner.lower() != owner.lower():
        raise SafeError("limiter_invalid", f"Limiter owner is {limiter.owner}, not this Trezor {owner}.", 409)
    if limiter.token.lower() != BASE_USDC_ADDRESS.lower():
        raise SafeError("limiter_invalid", f"Limiter token is {limiter.token}, not USDC on Base.", 409)
    if limiter.paused:
        raise SafeError("limiter_invalid", "Limiter is paused permanently; set up a new one.", 409)
    if limiter.expiry <= now:
        raise SafeError("limiter_invalid", "Limiter has expired; set up a new one.", 409)
    return limiter
