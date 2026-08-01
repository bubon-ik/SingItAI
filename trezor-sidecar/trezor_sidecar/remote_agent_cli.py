"""Operator client for testing the separate VPS Trezor agent before Hermes rollout."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Mapping, Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sign402-trezor-remote-cli")
    parser.add_argument("--user-id", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("test")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--product-id", required=True)
    prepare.add_argument("--package-id", required=True)
    prepare.add_argument("--country", required=True)
    confirm = commands.add_parser("confirm")
    confirm.add_argument("--code", required=True)
    commands.add_parser("cancel")
    return parser


def _call(env: Mapping[str, str], operation: str, payload: dict) -> str:
    url = str(env.get("SIGN402_TREZOR_REMOTE_AGENT_URL", "http://127.0.0.1:8123")).rstrip("/")
    token = str(env.get("SIGN402_TREZOR_REMOTE_AGENT_TOKEN", "") or "").strip()
    if len(token) < 32:
        raise ValueError("SIGN402_TREZOR_REMOTE_AGENT_TOKEN is required")
    body = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url + "/v1/" + operation,
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=700) as response:
            raw = response.read(16_385)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(16_385)
        finally:
            error.close()
        decoded = json.loads(raw.decode("utf-8"))
        raise ValueError(str(decoded.get("message") or "Trezor request failed safely")) from None
    if len(raw) > 16_384:
        raise ValueError("Trezor response is too large")
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict) or decoded.get("ok") is not True or not isinstance(decoded.get("text"), str):
        raise ValueError("Trezor response is invalid")
    return decoded["text"]


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    arguments = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        payload = {"userId": arguments.user_id}
        if arguments.command == "prepare":
            payload.update(
                {
                    "productId": arguments.product_id,
                    "packageId": arguments.package_id,
                    "country": arguments.country.upper(),
                }
            )
        elif arguments.command == "confirm":
            payload["confirmationCode"] = arguments.code.upper()
        print(_call(dict(os.environ if env is None else env), arguments.command, payload))
        return 0
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
