"""Operator-only check: does the Trezor screen render a readable purchase?

EIP-712 typed data is signed blind on a Safe 3 through Trezor Suite MCP — the
device shows the domain name and the signing account, never the message
fields. So an intent approval today proves a button was pressed, not that a
human read what they were buying.

A plain signed message (`ethereumSignMessage`) may render its text instead.
This signs one, with no purchase attached, so the screen can be judged before
any of the intent format is rewritten around it.

Nothing here can move funds: a message signature is not a transaction, and no
invoice, payment or broadcast is involved.

Run it with the sidecar environment loaded:

    python -m trezor_sidecar.message_preview
"""

from __future__ import annotations

import os
import sys
from typing import Mapping

from .config import SidecarSettings
from .errors import SafeError
from .mcp_client import McpToolCaller


PREVIEW_MESSAGE = "\n".join(
    (
        "Sign402 purchase",
        "ALZA.CZ - 100 CZK",
        "Pay up to 4.76 USDC on Base",
        "Expires in 10 minutes",
    )
)


def _settings(env: Mapping[str, str]) -> SidecarSettings:
    settings = SidecarSettings.from_env(dict(env))
    if not settings.enabled:
        raise SafeError(
            "disabled",
            "Set SIGN402_TREZOR_POC_ENABLED=1 and load the sidecar environment.",
            503,
        )
    return settings


def preview(env: Mapping[str, str] | None = None) -> dict[str, object]:
    settings = _settings(os.environ if env is None else env)

    print("Preview only. A message signature cannot move funds.")
    print("This exact text is being sent to the device:")
    print()
    for line in PREVIEW_MESSAGE.splitlines():
        print(f"    {line}")
    print()
    print("On the Trezor, check whether those lines appear at all:")
    print("  - is the product name readable?")
    print("  - is the amount readable?")
    print("  - or is it only a hash and the account address again?")
    print()

    # A preview must not widen what the sidecar itself is allowed to call, so
    # it carries its own single-tool allow-list.
    call = McpToolCaller(
        settings.mcp_token,
        allowed_tools=frozenset({"trezor_sign_message"}),
    )
    result = call(
        "trezor_sign_message",
        {
            "coin": "base",
            "path": settings.derivation_path,
            "message": PREVIEW_MESSAGE,
        },
    )

    print("Signed on device.")
    print(f"  response fields: {sorted(result)}")
    return result


def main(argv: list[str] | None = None) -> int:
    if argv:
        print("usage: python -m trezor_sidecar.message_preview", file=sys.stderr)
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
