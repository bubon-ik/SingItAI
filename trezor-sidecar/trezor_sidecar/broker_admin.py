"""Local VPS administration for one-time companion enrollment."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Mapping, Sequence

from .broker_server import BrokerSettings
from .broker_store import BrokerStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sign402-trezor-broker-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser("create-enrollment")
    enroll.add_argument("--user-id", required=True)
    status = commands.add_parser("status")
    status.add_argument("--user-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    arguments = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        settings = BrokerSettings.from_env(dict(os.environ if env is None else env))
        if not settings.enabled:
            raise ValueError("Trezor companion broker is disabled")
        store = BrokerStore(settings.state_path)
        if arguments.command == "create-enrollment":
            code = store.create_enrollment(arguments.user_id, now=int(time.time()))
            print("One-time Trezor enrollment code (valid 10 minutes):")
            print(code)
            return 0
        companion = store.companion(arguments.user_id)
        if companion is None:
            print("No active Trezor companion is enrolled.")
            return 1
        print(f"Trezor companion active for {companion['walletAddress']}.")
        return 0
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
