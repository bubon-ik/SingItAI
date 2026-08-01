"""Outbound worker that keeps Trezor Suite MCP entirely on the user's computer."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .broker_client import CompanionBrokerClient
from .errors import SafeError
from .models import PurchaseIntent
from .sidecar_client import SidecarClient


_DEFAULT_TOKEN_PATH = Path("~/.config/sign402-trezor-companion/token").expanduser()
_JOB_ID = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")
_INTENT_FIELDS = {
    "intentId",
    "productSlug",
    "packageId",
    "denomination",
    "quotedTotalUsdMicros",
    "maxPaymentUsdcAtomic",
    "paymentAsset",
    "paymentNetwork",
    "recipientHash",
    "expiresAt",
}
_PAYMENT_FIELDS = {
    "intentId",
    "invoiceId",
    "payTo",
    "amountAtomic",
    "expiresAt",
    "idempotencyKey",
}


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "") or "").strip()
    if not value:
        raise ValueError(f"{name} is required when Trezor companion mode is enabled")
    return value


@dataclass(frozen=True, repr=False)
class CompanionSettings:
    enabled: bool
    broker_url: str = ""
    broker_token: str = ""
    sidecar_token: str = ""
    poll_seconds: float = 2.0

    def __repr__(self) -> str:
        return (
            "CompanionSettings("
            f"enabled={self.enabled!r}, broker_url={self.broker_url!r}, "
            f"poll_seconds={self.poll_seconds!r}, credentials='<redacted>')"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "CompanionSettings":
        if env.get("SIGN402_TREZOR_COMPANION_ENABLED") != "1":
            return cls(False)
        token = str(env.get("SIGN402_TREZOR_COMPANION_TOKEN", "") or "").strip()
        if not token:
            token_path = Path(
                str(env.get("SIGN402_TREZOR_COMPANION_TOKEN_PATH", _DEFAULT_TOKEN_PATH))
            ).expanduser()
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except OSError:
                raise ValueError("Trezor companion token is unavailable") from None
        if len(token) < 32:
            raise ValueError("Trezor companion token is invalid")
        poll_text = str(env.get("SIGN402_TREZOR_COMPANION_POLL_SECONDS", "2") or "")
        try:
            poll = float(poll_text)
        except ValueError:
            raise ValueError("SIGN402_TREZOR_COMPANION_POLL_SECONDS is invalid") from None
        if not 0.25 <= poll <= 30:
            raise ValueError("SIGN402_TREZOR_COMPANION_POLL_SECONDS is invalid")
        return cls(
            True,
            _required(env, "SIGN402_TREZOR_BROKER_URL"),
            token,
            _required(env, "SIGN402_TREZOR_SIDECAR_TOKEN"),
            poll,
        )


class CompanionWorker:
    def __init__(
        self,
        *,
        broker: CompanionBrokerClient,
        sidecar: SidecarClient,
        clock: Callable[[], int | float] = time.time,
    ):
        self.broker = broker
        self.sidecar = sidecar
        self._clock = clock

    def __repr__(self) -> str:
        return "CompanionWorker(credentials='<redacted>')"

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SafeError("invalid_clock", "Companion clock is invalid.", 503)
        return int(value)

    def run_once(self) -> bool:
        job = self.broker.claim()
        if job is None:
            return False
        job_id = job.get("jobId")
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise SafeError("broker_failed", "Trezor companion job is invalid.", 502)
        try:
            result = self._execute(job)
            self.broker.complete(job_id, result)
        except SafeError as error:
            self.broker.fail(job_id, error.code)
        except Exception:
            self.broker.fail(job_id, "companion_failed")
        return True

    def _execute(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload")
        expires_at = job.get("expiresAt")
        if not isinstance(payload, dict) or type(expires_at) is not int:
            raise SafeError("broker_failed", "Trezor companion job is invalid.", 502)
        if expires_at <= self._now():
            raise SafeError("intent_expired", "Trezor companion job has expired.", 409)
        kind = job.get("kind")
        if kind == "purchase_intent":
            if set(payload) != _INTENT_FIELDS:
                raise SafeError("broker_failed", "Purchase intent job is invalid.", 502)
            if payload.get("paymentAsset") != "USDC" or payload.get("paymentNetwork") != "Base Mainnet":
                raise SafeError("broker_failed", "Purchase intent job is invalid.", 502)
            try:
                intent = PurchaseIntent(
                    intent_id=payload["intentId"],
                    product_slug=payload["productSlug"],
                    package_id=payload["packageId"],
                    denomination=payload["denomination"],
                    quoted_total_usd_micros=payload["quotedTotalUsdMicros"],
                    max_payment_usdc_atomic=payload["maxPaymentUsdcAtomic"],
                    recipient_hash=payload["recipientHash"],
                    expires_at=payload["expiresAt"],
                )
            except (KeyError, TypeError, ValueError):
                raise SafeError("broker_failed", "Purchase intent job is invalid.", 502) from None
            return self.sidecar.approve_intent(intent)
        if kind == "usdc_payment":
            if set(payload) != _PAYMENT_FIELDS:
                raise SafeError("broker_failed", "Payment job is invalid.", 502)
            return self.sidecar.pay_invoice(
                payload["intentId"],
                payload["invoiceId"],
                payload["payTo"],
                payload["amountAtomic"],
                payload["expiresAt"],
                payload["idempotencyKey"],
            )
        raise SafeError("broker_failed", "Trezor companion job type is invalid.", 502)


def _write_token(path: Path, token: str) -> None:
    target = Path(os.path.abspath(os.fspath(path.expanduser())))
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (token + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sign402-trezor-companion")
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser("enroll", help="Pair locally and enroll this computer")
    enroll.add_argument("--code")
    enroll.add_argument("--broker-url", required=True)
    enroll.add_argument("--token-path", default=str(_DEFAULT_TOKEN_PATH))
    commands.add_parser("run", help="Poll the VPS for Trezor approval jobs")
    commands.add_parser("once", help="Process at most one approval job")
    return parser


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    arguments = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if arguments.command == "enroll":
            environment = dict(os.environ if env is None else env)
            sidecar = SidecarClient(token=_required(environment, "SIGN402_TREZOR_SIDECAR_TOKEN"))
            enrollment_code = str(arguments.code or "").strip() or getpass.getpass(
                "One-time enrollment code: "
            ).strip()
            paired = sidecar.pair()
            address = paired["pairing"]["address"]
            companion = CompanionBrokerClient(base_url=arguments.broker_url).enroll(
                enrollment_code,
                address,
            )
            _write_token(Path(arguments.token_path), companion["token"])
            print(f"Trezor companion enrolled for Base account {address}.")
            print(f"Private companion token saved to {Path(arguments.token_path).expanduser()}.")
            return 0
        settings = CompanionSettings.from_env(dict(os.environ if env is None else env))
        if not settings.enabled:
            raise ValueError("Trezor companion is disabled")
        worker = CompanionWorker(
            broker=CompanionBrokerClient(
                base_url=settings.broker_url,
                token=settings.broker_token,
            ),
            sidecar=SidecarClient(token=settings.sidecar_token),
        )
        if arguments.command == "once":
            worker.run_once()
            return 0
        while True:
            if not worker.run_once():
                time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        return 0
    except SafeError as error:
        print(f"Error: {error.message}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0
