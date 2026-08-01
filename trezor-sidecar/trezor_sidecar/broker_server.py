"""Opt-in HTTP broker for outbound per-user Trezor companions."""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .broker_store import BrokerStore


_MAX_BODY_BYTES = 65_536
_JOB_PATH = re.compile(r"/v1/internal/jobs/([A-Za-z0-9._:-]{8,128})\Z")
_FINISH_PATH = re.compile(r"/v1/companion/jobs/([A-Za-z0-9._:-]{8,128})/(complete|fail)\Z")
_COMPANION_PATH = re.compile(r"/v1/internal/companions/([0-9]{1,32})\Z")


def _clock_timestamp(clock: Callable[[], int | float]) -> int:
    value = clock()
    if isinstance(value, bool):
        raise ValueError("invalid clock")
    if isinstance(value, int):
        timestamp = value
    elif isinstance(value, float) and math.isfinite(value):
        timestamp = int(value)
    else:
        raise ValueError("invalid clock")
    if not 0 < timestamp <= (1 << 63) - 1:
        raise ValueError("invalid clock")
    return timestamp


@dataclass(frozen=True, repr=False)
class BrokerSettings:
    enabled: bool
    internal_token: str = ""
    state_path: Path = Path("~/.sign402-trezor-broker/state.db").expanduser()
    host: str = "127.0.0.1"
    port: int = 8122

    def __repr__(self) -> str:
        return (
            "BrokerSettings("
            f"enabled={self.enabled!r}, state_path={self.state_path!r}, "
            f"host={self.host!r}, port={self.port!r}, credentials='<redacted>')"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "BrokerSettings":
        if env.get("SIGN402_TREZOR_BROKER_ENABLED") != "1":
            return cls(False)
        token = str(env.get("SIGN402_TREZOR_BROKER_INTERNAL_TOKEN", "") or "").strip()
        if len(token) < 32:
            raise ValueError("SIGN402_TREZOR_BROKER_INTERNAL_TOKEN must contain at least 32 characters")
        path = Path(
            str(
                env.get(
                    "SIGN402_TREZOR_BROKER_STATE_PATH",
                    "~/.sign402-trezor-broker/state.db",
                )
            )
        ).expanduser()
        port_text = str(env.get("SIGN402_TREZOR_BROKER_PORT", "8122") or "")
        if not port_text.isascii() or not port_text.isdecimal() or not 1 <= int(port_text) <= 65535:
            raise ValueError("SIGN402_TREZOR_BROKER_PORT is invalid")
        host = str(env.get("SIGN402_TREZOR_BROKER_HOST", "127.0.0.1") or "").strip()
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("broker must bind to loopback and be exposed only through an HTTPS reverse proxy")
        return cls(True, token, path, host, int(port_text))


class BrokerHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        settings: BrokerSettings,
        store: BrokerStore,
        clock: Callable[[], int | float] = time.time,
    ):
        self.settings = settings
        self.store = store
        self.clock = clock
        super().__init__(address, BrokerHandler)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(5.0)
        return request, client_address

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


class BrokerHandler(BaseHTTPRequestHandler):
    server: BrokerHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, value: dict[str, Any] | None = None) -> None:
        body = b"" if value is None else json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"ok": False, "code": code, "message": message})

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise ValueError("request body is invalid")
        length = int(raw_length)
        if not 0 < length <= _MAX_BODY_BYTES:
            raise ValueError("request body is invalid")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("request body is invalid")
        decoded = json.loads(body.decode("utf-8"), parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        if not isinstance(decoded, dict):
            raise ValueError("request body is invalid")
        return decoded

    def _bearer(self) -> str:
        value = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not value.startswith(prefix):
            raise PermissionError("authorization is required")
        return value[len(prefix) :]

    def _internal(self) -> None:
        if not hmac.compare_digest(self._bearer(), self.server.settings.internal_token):
            raise PermissionError("invalid authorization")

    def do_GET(self) -> None:
        try:
            if self.path == "/health":
                self._send(200, {"ok": True, "status": "ready" if self.server.settings.enabled else "disabled"})
                return
            match = _JOB_PATH.fullmatch(self.path)
            if match is not None:
                self._internal()
                job = self.server.store.job(match.group(1), now=_clock_timestamp(self.server.clock))
                self._send(200, {"ok": True, "job": job})
                return
            match = _COMPANION_PATH.fullmatch(self.path)
            if match is not None:
                self._internal()
                companion = self.server.store.companion(match.group(1))
                if companion is None:
                    self._error(404, "not_paired", "No active Trezor companion is enrolled.")
                else:
                    self._send(200, {"ok": True, "companion": companion})
                return
            self._error(404, "not_found", "Resource was not found.")
        except PermissionError:
            self._error(401, "unauthorized", "Authorization failed.")
        except ValueError:
            self._error(400, "invalid_request", "Request is invalid.")
        except Exception:
            self._error(500, "broker_failed", "Broker request failed safely.")

    def do_POST(self) -> None:
        try:
            if not self.server.settings.enabled:
                self._error(503, "disabled", "Trezor companion broker is disabled.")
                return
            now = _clock_timestamp(self.server.clock)
            if self.path == "/v1/internal/enrollments":
                self._internal()
                payload = self._read_json()
                if set(payload) != {"userId"}:
                    raise ValueError
                code = self.server.store.create_enrollment(str(payload["userId"]), now=now)
                self._send(201, {"ok": True, "enrollmentCode": code, "expiresAt": now + 600})
                return
            if self.path == "/v1/enroll":
                payload = self._read_json()
                if set(payload) != {"enrollmentCode", "walletAddress"}:
                    raise ValueError
                companion = self.server.store.enroll(
                    str(payload["enrollmentCode"]),
                    str(payload["walletAddress"]),
                    now=now,
                )
                self._send(201, {"ok": True, "companion": companion})
                return
            if self.path == "/v1/internal/jobs":
                self._internal()
                payload = self._read_json()
                if set(payload) != {"userId", "kind", "idempotencyKey", "payload", "expiresAt"}:
                    raise ValueError
                job = self.server.store.create_job(
                    user_id=str(payload["userId"]),
                    kind=str(payload["kind"]),
                    idempotency_key=str(payload["idempotencyKey"]),
                    payload=payload["payload"],
                    expires_at=payload["expiresAt"],
                    now=now,
                )
                self._send(202, {"ok": True, "job": job})
                return
            if self.path == "/v1/companion/jobs/claim":
                token = self._bearer()
                payload = self._read_json()
                if payload != {}:
                    raise ValueError
                job = self.server.store.claim(token, now=now)
                if job is None:
                    self._send(204)
                else:
                    self._send(200, {"ok": True, "job": job})
                return
            finish = _FINISH_PATH.fullmatch(self.path)
            if finish is not None:
                token = self._bearer()
                payload = self._read_json()
                if finish.group(2) == "complete":
                    if set(payload) != {"result"} or not isinstance(payload["result"], dict):
                        raise ValueError
                    job = self.server.store.finish(
                        token,
                        finish.group(1),
                        result=payload["result"],
                        error_code=None,
                        now=now,
                    )
                else:
                    if set(payload) != {"errorCode"}:
                        raise ValueError
                    job = self.server.store.finish(
                        token,
                        finish.group(1),
                        result=None,
                        error_code=str(payload["errorCode"]),
                        now=now,
                    )
                self._send(200, {"ok": True, "job": job})
                return
            self._error(404, "not_found", "Resource was not found.")
        except PermissionError:
            self._error(401, "unauthorized", "Authorization failed.")
        except ValueError:
            self._error(400, "invalid_request", "Request is invalid.")
        except Exception:
            self._error(500, "broker_failed", "Broker request failed safely.")

    def _method_not_allowed(self) -> None:
        self._error(405, "method_not_allowed", "Method is not allowed.")

    do_DELETE = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_PUT = _method_not_allowed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sign402-trezor-broker")
    parser.add_argument("--check", action="store_true", help="Validate configuration without starting")
    return parser


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    arguments = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        settings = BrokerSettings.from_env(dict(os.environ if env is None else env))
        if not settings.enabled:
            raise ValueError("Trezor companion broker is disabled")
        store = BrokerStore(settings.state_path)
        if arguments.check:
            print("Trezor companion broker configuration is valid.")
            return 0
        server = BrokerHttpServer(
            (settings.host, settings.port),
            settings=settings,
            store=store,
        )
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
