"""Operator-only preview of the Base USDC transfer confirmation screen.

This signs one USDC transfer on the Trezor and stops. It never broadcasts, and
it never calls ``trezor_push_transaction``, so nothing can move on chain. The
recipient is always the paired account itself, so even a signed transaction
that escaped this process would only pay the sender.

It exists to answer one question before any real purchase: does the Safe 3
screen show the recipient and the USDC amount, or only the token contract and
raw calldata?

Run it with the sidecar environment loaded:

    python -m trezor_sidecar.transfer_preview
"""

from __future__ import annotations

import os
import sys
from typing import Mapping

from .base import encode_usdc_transfer
from .config import SidecarSettings
from .errors import SafeError
from .mcp_client import McpToolCaller, TrezorMcpClient
from .store import SidecarStore


BASE_USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PREVIEW_AMOUNT_ATOMIC = 1_000_000  # 1.00 USDC, shown on screen only.


def _preview_amount(env: Mapping[str, str]) -> int:
    """Amount shown on the device screen.

    Defaults to 1.00 USDC. Gas estimation can reject an amount the account
    cannot cover, so an unfunded account can fall back to ``0`` and still see
    the recipient and token on screen.
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


def _settings(env: Mapping[str, str]) -> SidecarSettings:
    settings = SidecarSettings.from_env(dict(env))
    if not settings.enabled:
        raise SafeError(
            "disabled",
            "Set SIGN402_TREZOR_POC_ENABLED=1 and load the sidecar environment.",
            503,
        )
    return settings


def _paired_address(settings: SidecarSettings) -> str:
    pairing = SidecarStore(settings.state_path).get_pairing()
    if pairing is None:
        raise SafeError(
            "not_paired",
            "Pair the Trezor first with: sign402-trezor-poc pair",
            409,
        )
    return pairing.address


def preview(env: Mapping[str, str] | None = None) -> dict[str, object]:
    values = os.environ if env is None else env
    settings = _settings(values)
    amount_atomic = _preview_amount(values)
    address = _paired_address(settings)
    calldata = encode_usdc_transfer(address, amount_atomic)

    print("Preview only. Nothing will be broadcast and no funds can move.")
    print(f"  token      USDC on Base ({BASE_USDC_CONTRACT})")
    print(f"  amount     {amount_atomic / 1_000_000:.2f} USDC")
    print(f"  recipient  {address}  (your own paired account)")
    print()
    print("Approve on the Trezor, then read the device screen carefully:")
    print("  - is the recipient address shown?")
    print("  - is the amount shown as USDC, or as a raw integer?")
    print("  - or does it only show the contract and raw data?")
    print()

    client = TrezorMcpClient(McpToolCaller(settings.mcp_token))
    result = client.sign_base_transaction(
        settings.derivation_path,
        BASE_USDC_CONTRACT,
        calldata,
    )

    # Never print or persist the signed transaction itself.
    signed = result.get("serializedTx") or result.get("signedTransaction")
    print("Signed on device. Not broadcast.")
    print(f"  signed payload present: {bool(signed)}")
    print(f"  response fields: {sorted(result)}")
    return result


def main(argv: list[str] | None = None) -> int:
    if argv:
        print("usage: python -m trezor_sidecar.transfer_preview", file=sys.stderr)
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
