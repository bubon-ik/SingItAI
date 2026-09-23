"""Operator-only preview of the Base USDC approve confirmation screen.

This signs one USDC ``approve`` on the Trezor and stops. It never broadcasts,
and it never calls ``trezor_push_transaction``, so nothing can change on chain.
The spender is always the burn address ``0x…dEaD``, whose key nobody holds, so
even a signed transaction that escaped this process and was mined would grant
an allowance that no one can ever use.

It answers the two questions that gate the allowance design
(docs/trezor-allowance-v1.md, checks T1 and T2) before any contract exists:

- T1: does Trezor Suite MCP accept ``approve`` calldata and let the device
  sign it at all?
- T2: does the screen show the spender and the USDC amount, or only the token
  contract and raw data?

Run it with the sidecar environment loaded:

    python -m trezor_sidecar.approve_preview
"""

from __future__ import annotations

import os
import sys
from typing import Mapping

from .base import BASE_USDC_ADDRESS, encode_usdc_approve
from .errors import SafeError
from .mcp_client import McpToolCaller, TrezorMcpClient
from .transfer_preview import _describe, _paired_address, _settings


BURN_SPENDER = "0x000000000000000000000000000000000000dEaD"
PREVIEW_AMOUNT_ATOMIC = 1_000_000  # 1.00 USDC, shown on screen only.


def _preview_amount(env: Mapping[str, str]) -> int:
    """Allowance shown on the device screen. Defaults to 1.00 USDC.

    ``approve`` needs no balance, so an unfunded account can preview any
    amount. The override exists to see how a realistic grant (say 300.00)
    renders, not because the default fails.
    """
    raw = str(env.get("SIGN402_TREZOR_PREVIEW_AMOUNT_ATOMIC", "") or "").strip()
    if not raw:
        return PREVIEW_AMOUNT_ATOMIC
    if not raw.isascii() or not raw.isdecimal():
        raise SafeError(
            "invalid_request",
            "SIGN402_TREZOR_PREVIEW_AMOUNT_ATOMIC must be a non-negative integer.",
        )
    return int(raw)


def preview(
    env: Mapping[str, str] | None = None,
    *,
    client: TrezorMcpClient | None = None,
) -> dict[str, object]:
    values = os.environ if env is None else env
    settings = _settings(values)
    amount_atomic = _preview_amount(values)
    address = _paired_address(settings)
    calldata = encode_usdc_approve(BURN_SPENDER, amount_atomic)

    print("Preview only. Nothing will be broadcast and no funds can move.")
    print(f"  signer     {address}  (your paired account)")
    print(f"  token      USDC on Base ({BASE_USDC_ADDRESS})")
    print(f"  action     approve (permission, not a transfer)")
    print(f"  spender    {BURN_SPENDER}  (burn address, nobody holds its key)")
    print(f"  allowance  {amount_atomic / 1_000_000:.2f} USDC")
    print()
    print("Read the device screen before deciding, and write down what it shows:")
    print("  - does it say this is an approval / allowance, or a plain call?")
    print("  - is the spender address shown, in full?")
    print("  - is the allowance shown as USDC, or as a raw integer?")
    print("  - or does it only show the contract and raw data (blind signing)?")
    print("Confirming or rejecting on the device are both valid results.")
    print()

    if client is None:
        client = TrezorMcpClient(McpToolCaller(settings.mcp_token))
    result = client.sign_base_transaction(
        settings.derivation_path,
        BASE_USDC_ADDRESS,
        calldata,
    )

    print("Signed on device. Not broadcast.")
    print()
    print("Response shape (keys and value types only — never the signature):")
    _describe(result)
    return result


def main(argv: list[str] | None = None) -> int:
    if argv:
        print("usage: python -m trezor_sidecar.approve_preview", file=sys.stderr)
        return 2
    try:
        preview()
    except SafeError as error:
        print(f"Error: {error.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
