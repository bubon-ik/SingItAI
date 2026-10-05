"""Loopback boundary between the working Hermes process and remote Trezor flow."""

from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Sequence

from .errors import SafeError
from .remote_agent import build_remote_agent_controller


_MAX_BODY_BYTES = 16_384
_OPERATIONS = frozenset({"status", "test", "prepare", "confirm", "cancel"})


class RemoteAgentHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, *, token: str, controller: Any):
        self.api_token = token
        self.controller = controller
        super().__init__(address, RemoteAgentHandler)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(5.0)
        return request, client_address

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


class RemoteAgentHandler(BaseHTTPRequestHandler):
    server: RemoteAgentHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"ok": False, "code": code, "message": message})

    def _authorize(self) -> bool:
        value = self.headers.get("Authorization", "")
        return value.startswith("Bearer ") and hmac.compare_digest(
            value[7:], self.server.api_token
        )

    def _read(self) -> dict[str, Any]:
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or not 0 < int(length) <= _MAX_BODY_BYTES:
            raise ValueError
        decoded = json.loads(self.rfile.read(int(length)).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError
        return decoded

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True, "status": "ready"})
        else:
            self._error(404, "not_found", "Route not found.")

    def do_POST(self) -> None:
        operation = self.path.removeprefix("/v1/") if self.path.startswith("/v1/") else ""
        if operation not in _OPERATIONS:
            self._error(404, "not_found", "Route not found.")
            return
        if not self._authorize():
            self._error(401, "unauthorized", "Authorization failed.")
            return
        try:
            payload = self._read()
            user_id = str(payload.get("userId", ""))
            if operation in {"status", "test", "cancel"}:
                if set(payload) != {"userId"}:
                    raise ValueError
                method = {
                    "status": self.server.controller.pair,
                    "test": self.server.controller.intent_test,
                    "cancel": self.server.controller.cancel,
                }[operation]
                text = method(user_id)
            elif operation == "prepare":
                if set(payload) != {"userId", "productId", "packageId", "country"}:
                    raise ValueError
                text = self.server.controller.prepare(
                    user_id,
                    payload["productId"],
                    payload["packageId"],
                    payload["country"],
                )
            else:
                if set(payload) != {"userId", "confirmationCode"}:
                    raise ValueError
                text = self.server.controller.confirm(user_id, payload["confirmationCode"])
            if not isinstance(text, str) or not text or len(text.encode("utf-8")) > _MAX_BODY_BYTES:
                raise ValueError
            self._send(200, {"ok": True, "text": text})
        except SafeError as error:
            self._error(error.status, error.code, error.message)
        except ValueError:
            self._error(400, "invalid_request", "Request is invalid.")
        except Exception:
            self._error(500, "agent_failed", "Trezor payment request failed safely.")

    def _unsupported(self) -> None:
        self._error(405, "method_not_allowed", "Method not allowed.")

    do_DELETE = _unsupported
    do_HEAD = _unsupported
    do_OPTIONS = _unsupported
    do_PATCH = _unsupported
    do_PUT = _unsupported


def build_server(env: Mapping[str, str]) -> RemoteAgentHttpServer:
    if env.get("SIGN402_TREZOR_REMOTE_AGENT_ENABLED") != "1":
        raise ValueError("remote Trezor agent is disabled")
    token = str(env.get("SIGN402_TREZOR_REMOTE_AGENT_TOKEN", "") or "").strip()
    if len(token) < 32:
        raise ValueError("SIGN402_TREZOR_REMOTE_AGENT_TOKEN must contain at least 32 characters")
    port_text = str(env.get("SIGN402_TREZOR_REMOTE_AGENT_PORT", "8123") or "")
    if not port_text.isascii() or not port_text.isdecimal() or not 1 <= int(port_text) <= 65535:
        raise ValueError("SIGN402_TREZOR_REMOTE_AGENT_PORT is invalid")
    controller = build_remote_agent_controller(env)
    return RemoteAgentHttpServer(("127.0.0.1", int(port_text)), token=token, controller=controller)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    del argv
    try:
        server = build_server(dict(os.environ if env is None else env))
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
