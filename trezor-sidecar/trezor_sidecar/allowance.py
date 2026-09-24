"""Grant, revoke and inspect a USDC allowance for an AgentAllowance limiter.

Design: docs/trezor-allowance-v1.md. The limiter source is in
``agent-allowance/`` at the repository root.

    python -m trezor_sidecar.allowance status <limiter>
    python -m trezor_sidecar.allowance grant  <limiter> <amount-usdc>
    python -m trezor_sidecar.allowance revoke <limiter>

``grant`` and ``revoke`` are the only on-chain actions the Trezor takes in this
design, and both are a plain USDC ``approve``, which the device renders
readably (check T2). Each one is signed on the device, then the signed bytes are
checked here against exactly the ``approve`` that was requested — right token,
right spender, right amount, right signer, Base — and only then broadcast.

``grant`` refuses a limiter it cannot vouch for: no code, a different owner, a
different token, paused, or expired. It cannot prove the code is the audited
limiter; that check is done by the owner on a second device (see the printed
BaseScan link). ``revoke`` inspects nothing on purpose: setting an allowance to
zero must work for any spender, including a broken or hostile one.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping

from eth_utils import keccak, to_checksum_address

from .base import (
    BASE_USDC_ADDRESS,
    BaseRpcClient,
    encode_usdc_approve,
    verify_signed_usdc_approve,
)
from .errors import SafeError
from .mcp_client import McpToolCaller, TrezorMcpClient
from .service import TrezorSidecarService
from .transfer_preview import _paired_address, _settings


_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_AMOUNT = re.compile(r"[0-9]+(\.[0-9]{1,6})?\Z")
RECEIPT_WAIT_SECONDS = 90
RECEIPT_POLL_SECONDS = 3
SETTLE_WAIT_SECONDS = 30
"""How long to wait for the RPC to show a mined approve. Public endpoints are
load-balanced; the node answering the next read can be a few blocks behind the
one that reported the receipt."""
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


def _usdc(atomic: int) -> str:
    return f"{Decimal(atomic) / Decimal(1_000_000):.6f}".rstrip("0").rstrip(".") + " USDC"


def _address(value: str) -> str:
    if not isinstance(value, str) or _ADDRESS.fullmatch(value) is None:
        raise SafeError("invalid_request", "Limiter must be a 0x-prefixed 20-byte address.")
    if int(value, 16) == 0:
        raise SafeError("invalid_request", "Limiter cannot be the zero address.")
    return to_checksum_address(value)


def _word_address(word: int) -> str:
    if word >> 160:
        raise SafeError("limiter_invalid", "Limiter returned a malformed address.", 409)
    return to_checksum_address(word.to_bytes(20, "big"))


def parse_amount(text: str, max_usd: Decimal) -> int:
    """USDC amount as atomic units: positive, at most six decimals, capped."""
    if not isinstance(text, str) or _AMOUNT.fullmatch(text) is None:
        raise SafeError("invalid_request", "Amount must be a plain decimal such as 1.00.")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise SafeError("invalid_request", "Amount must be a plain decimal such as 1.00.") from None
    if value <= 0:
        raise SafeError("invalid_request", "Amount must be greater than zero.")
    if value > max_usd:
        raise SafeError(
            "limit_exceeded",
            f"Amount exceeds SIGN402_TREZOR_POC_MAX_USD ({max_usd}). The allowance is "
            "the owner's worst-case loss; raise that setting deliberately if intended.",
        )
    return int(value * 1_000_000)


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


def _describe(limiter: Limiter, out: Callable[[str], None]) -> None:
    out(f"  limiter          {limiter.address}")
    out(f"  owner            {limiter.owner}")
    out(f"  token            {limiter.token}")
    out(f"  agent            {limiter.agent}")
    out(f"  guardian         {limiter.guardian}")
    out(f"  daily cap        {_usdc(limiter.daily_cap)}")
    out(f"  per purchase     {_usdc(limiter.per_purchase_cap)}")
    out(f"  expires          {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(limiter.expiry))}")
    out(f"  paused           {'yes' if limiter.paused else 'no'}")
    out(f"  left today       {_usdc(limiter.remaining_today)}")


@dataclass
class Deps:
    trezor: TrezorMcpClient
    rpc: BaseRpcClient
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    out: Callable[[str], None] = print


def _deps(settings, **overrides) -> Deps:
    trezor = overrides.pop("trezor", None) or TrezorMcpClient(McpToolCaller(settings.mcp_token))
    rpc = overrides.pop("rpc", None) or BaseRpcClient(settings.base_rpc_url)
    return Deps(trezor=trezor, rpc=rpc, **overrides)


def _approve(settings, deps: Deps, owner: str, spender: str, amount_atomic: int) -> str:
    """Sign on the device, verify the exact bytes, broadcast, wait for the receipt."""
    result = deps.trezor.sign_base_transaction(
        settings.derivation_path,
        BASE_USDC_ADDRESS,
        encode_usdc_approve(spender, amount_atomic),
    )
    raw = TrezorSidecarService._signed_transaction(result)
    local_hash = verify_signed_usdc_approve(raw, owner, spender, amount_atomic)
    deps.out("Signed transaction matches the requested approve. Broadcasting.")
    pushed = deps.trezor.push_base_transaction(raw)
    tx_hash = TrezorSidecarService._broadcast_hash(pushed)
    if tx_hash != local_hash:
        raise SafeError(
            "broadcast_ambiguous",
            f"Suite reported {tx_hash}, but the signed transaction is {local_hash}. "
            "Check both on BaseScan before retrying.",
            409,
        )
    deps.out(f"  tx               https://basescan.org/tx/{tx_hash}")
    deadline = deps.now() + RECEIPT_WAIT_SECONDS
    while deps.now() < deadline:
        try:
            status = deps.rpc.receipt_status(tx_hash)
        except SafeError as error:
            if error.code != "base_rpc_unavailable":
                raise
            status = None
        if status == 1:
            return tx_hash
        if status == 0:
            raise SafeError("transaction_failed", f"Transaction {tx_hash} reverted.", 409)
        deps.sleep(RECEIPT_POLL_SECONDS)
    raise SafeError(
        "receipt_pending",
        f"Broadcast, not mined within {RECEIPT_WAIT_SECONDS}s: {tx_hash}. "
        "Do not sign again; check it on BaseScan.",
        504,
    )


def _settled_allowance(deps: Deps, owner: str, spender: str, expected: int) -> int:
    """The allowance once the RPC shows the mined value, or the last value read."""
    deadline = deps.now() + SETTLE_WAIT_SECONDS
    while True:
        left = _read(lambda: deps.rpc.usdc_allowance(owner, spender), deps.sleep)
        if left == expected or deps.now() >= deadline:
            return left
        deps.sleep(2)


def _report_allowance(deps: Deps, verb: str, left: int, expected: int) -> None:
    if left == expected:
        deps.out(f"{verb}. Allowance now {_usdc(left)}.")
    else:
        deps.out(
            f"{verb} and mined, but the RPC still reports {_usdc(left)} instead of "
            f"{_usdc(expected)} — a node behind the chain. Check the transaction "
            "on BaseScan; do not sign again."
        )


def status(limiter_text: str, env: Mapping[str, str] | None = None, **overrides) -> Limiter:
    settings = _settings(os.environ if env is None else env)
    deps = _deps(settings, **overrides)
    limiter = inspect_limiter(deps.rpc, _address(limiter_text), deps.sleep)
    _describe(limiter, deps.out)
    left = _read(lambda: deps.rpc.usdc_allowance(limiter.owner, limiter.address), deps.sleep)
    balance = _read(lambda: deps.rpc.get_balances(limiter.owner).usdc_atomic, deps.sleep)
    deps.out(f"  allowance left   {_usdc(left)}")
    deps.out(f"  owner balance    {_usdc(balance)}")
    return limiter


def grant(
    limiter_text: str, amount_text: str, env: Mapping[str, str] | None = None, **overrides
) -> str:
    settings = _settings(os.environ if env is None else env)
    deps = _deps(settings, **overrides)
    owner = _paired_address(settings)
    spender = _address(limiter_text)
    amount = parse_amount(amount_text, settings.max_usd)

    limiter = inspect_limiter(deps.rpc, spender, deps.sleep)
    if limiter.owner.lower() != owner.lower():
        raise SafeError("limiter_invalid", f"Limiter owner is {limiter.owner}, not your paired account {owner}.", 409)
    if limiter.token.lower() != BASE_USDC_ADDRESS.lower():
        raise SafeError("limiter_invalid", f"Limiter token is {limiter.token}, not USDC on Base.", 409)
    if limiter.paused:
        raise SafeError("limiter_invalid", "Limiter is paused permanently; deploy a new one.", 409)
    if limiter.expiry <= deps.now():
        raise SafeError("limiter_invalid", "Limiter has expired; deploy a new one.", 409)

    deps.out("Grant an allowance. This moves no money: it lets the limiter pull up to")
    deps.out("the amount from your address, only through its caps, until revoked.")
    _describe(limiter, deps.out)
    deps.out(f"  ALLOWANCE        {_usdc(amount)}  (your worst-case loss)")
    deps.out("")
    deps.out("Before confirming, check on a second device that this address is a")
    deps.out("verified AgentAllowance with the values above:")
    deps.out(f"  https://base.blockscout.com/address/{spender}?tab=contract  (Sourcify)")
    deps.out(f"  https://basescan.org/address/{spender}#code")
    deps.out("")
    deps.out("On the Trezor, confirm only if it shows an approve, this spender in")
    deps.out(f"full, and {_usdc(amount)}.")
    deps.out("")

    tx_hash = _approve(settings, deps, owner, spender, amount)
    _report_allowance(deps, "Granted", _settled_allowance(deps, owner, spender, amount), amount)
    return tx_hash


def revoke(limiter_text: str, env: Mapping[str, str] | None = None, **overrides) -> str:
    settings = _settings(os.environ if env is None else env)
    deps = _deps(settings, **overrides)
    owner = _paired_address(settings)
    spender = _address(limiter_text)

    deps.out("Revoke: set the allowance for this spender to zero. Nothing about the")
    deps.out("spender is checked first, so this works for any contract, broken or not.")
    deps.out(f"  spender          {spender}")
    deps.out("On the Trezor, confirm only if it shows an approve of 0 to this spender.")
    deps.out("")

    tx_hash = _approve(settings, deps, owner, spender, 0)
    _report_allowance(deps, "Revoked", _settled_allowance(deps, owner, spender, 0), 0)
    return tx_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trezor_sidecar.allowance")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status").add_argument("limiter")
    granting = commands.add_parser("grant")
    granting.add_argument("limiter")
    granting.add_argument("amount", help="USDC, e.g. 1.00")
    commands.add_parser("revoke").add_argument("limiter")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            status(args.limiter)
        elif args.command == "grant":
            grant(args.limiter, args.amount)
        else:
            revoke(args.limiter)
    except SafeError as error:
        print(f"Error: {error.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
