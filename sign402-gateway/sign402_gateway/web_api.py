"""The web page's API for the allowance lane: /web/v1. Design: docs/allowance-web-v1.md.

A process of its own, beside the gateway, so the only thing a reverse proxy
exposes is this: sign in with a wallet, read the allowance, create a limiter.
The gateway's internal API (and its bot token) stays on loopback.

    SIGN402_WEB_ENABLED=1 SIGN402_ALLOWANCE_ENABLED=1 \\
    SIGN402_WEB_DOMAIN=app.example SIGN402_WEB_URI=https://app.example \\
    python -m sign402_gateway.web_api

Environment:
    SIGN402_WEB_DOMAIN, SIGN402_WEB_URI   what the sign-in message names (the page's host and origin)
    SIGN402_WEB_ALLOWED_ADDRESSES        beta allowlist, comma-separated; "*" for everyone
    SIGN402_WEB_CORS_ORIGIN              the page's origin when it is not SIGN402_WEB_URI
    SIGN402_WEB_PORT                     loopback port (8130)
    SIGN402_WEB_DB                       accounts and sessions (~/.sign402/web.db)
    SIGN402_WEB_MIN_OWNER_USDC           USDC the owner must hold to create a limiter (1)
    SIGN402_WEB_MAX_LIMITERS_PER_30_DAYS (3)
plus everything SIGN402_ALLOWANCE_* the lane itself needs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent_allowance import AllowanceError, AllowanceService, AllowanceUnavailable, _usdc_atomic
from .web_accounts import (
    DEFAULT_WEB_DB, SESSION_SECONDS, WEB_DB_ENV, WebAccountStore, WebAuth, WebAuthError,
)

logger = logging.getLogger(__name__)

PREFIX = "/web/v1"
COOKIE = "singit_session"
CSRF_HEADER = "X-SingIt-CSRF"
MAX_BODY = 16 * 1024
DEFAULT_PORT = 8130


class WebError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RateLimit:
    """At most `limit` events per key in `window` seconds, in memory."""

    def __init__(self, limit: int, window: float, now: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.now = limit, window, now
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        with self._lock:
            events, now = self._events[key], self.now()
            while events and events[0] <= now - self.window:
                events.popleft()
            if len(events) >= self.limit:
                raise WebError(429, "rate_limited", "Too many requests. Wait a minute and try again.")
            events.append(now)


def _state_code(described: Mapping[str, Any]) -> str:
    state = str(described.get("state", ""))
    return state if state in ("paused", "expired", "granted") else "waiting_for_grant"


class WebApi:
    """Routing and rules, without HTTP, so tests can drive it directly."""

    def __init__(
        self,
        auth: WebAuth,
        allowance: AllowanceService,
        *,
        min_owner_usdc: int = 1_000_000,
        max_limiters_per_30_days: int = 3,
        now: Callable[[], float] = time.time,
    ):
        self.auth = auth
        self.allowance = allowance
        self.min_owner_usdc = min_owner_usdc
        self.max_limiters = max_limiters_per_30_days
        self.now = now
        self.auth_by_ip = RateLimit(30, 600)
        self.setup_by_account = RateLimit(5, 3600)

    def handle(self, method: str, path: str, *, token: str, csrf: str | None, body: dict[str, Any],
               client: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        """(status, JSON body, extra headers)."""
        if method == "POST" and path == "/auth/nonce":
            self.auth_by_ip.hit(client)
            return 200, self.auth.nonce(body.get("address")), {}
        if method == "POST" and path == "/auth/verify":
            self.auth_by_ip.hit(client)
            signed = self.auth.verify(body.get("message"), body.get("signature"))
            cookie = (f"{COOKIE}={signed['token']}; Path={PREFIX}; Max-Age={SESSION_SECONDS}; "
                      "HttpOnly; Secure; SameSite=Strict")
            return 200, {"account": signed["account"], "address": signed["address"],
                         "csrfToken": signed["csrfToken"], "expiresAt": signed["expiresAt"],
                         "telegramLinked": False}, {"Set-Cookie": cookie}
        if method == "POST" and path == "/auth/logout":
            self.auth.session(token, csrf or "")
            self.auth.logout(token)
            return 200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path={PREFIX}; Max-Age=0; HttpOnly; Secure; SameSite=Strict"}

        session = self.auth.session(token, None if method == "GET" else (csrf or ""))
        account, owner = session["account_id"], session["address"]
        if method == "GET" and path == "/session":
            return 200, {"account": account, "address": owner}, {}
        if method == "GET" and path == "/allowance":
            return 200, self._status(account, owner), {}
        if method == "POST" and path == "/allowance/setup":
            return 200, self._setup(account, owner, body), {}
        raise WebError(404, "not_found", "No such endpoint.")

    # -- allowance --

    def _public(self, described: Mapping[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in described.items() if k != "telegramText"}
        if "state" in out:
            out["state"] = _state_code(described)
        return out

    def _status(self, account: str, owner: str) -> dict[str, Any]:
        status = self._public(self.allowance.status(account))
        status["owner"] = owner
        status["ownerEthWei"] = str(self.allowance.evm.balance(owner))
        status["ownerUsdcAtomic"] = self.allowance.evm.usdc_balance(owner)
        status["alerts"] = [
            {"severity": a["severity"], "text": a["text"], "createdAt": a["created_at"]}
            for a in self.allowance.store.recent_alerts(account, 10)
        ]
        return status

    def _setup(self, account: str, owner: str, body: Mapping[str, Any]) -> dict[str, Any]:
        self.setup_by_account.hit(account)
        # Validate before spending anything on chain; setup checks again.
        for field, label in (("dailyCap", "The daily cap"), ("perPurchaseCap", "The per-purchase cap")):
            _usdc_atomic(body.get(field), label)
        if self.allowance.evm.usdc_balance(owner) < self.min_owner_usdc:
            raise WebError(400, "owner_needs_usdc", (
                f"Your wallet needs at least {self.min_owner_usdc / 1_000_000:g} USDC on Base before we "
                "create a limiter for it."))
        if self.allowance.store.limiters_since(account, int(self.now()) - 30 * 86400) >= self.max_limiters:
            raise WebError(429, "too_many_limiters", (
                f"At most {self.max_limiters} limiters per wallet in 30 days. Keep using your current one."))
        result = self.allowance.setup(account, body.get("dailyCap"), body.get("perPurchaseCap"), body.get("days"))
        return self._public(result)


class WebHandler(BaseHTTPRequestHandler):
    server_version = "SingItWeb/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # no request lines with cookies or tokens
        logger.debug("web %s", fmt % args)

    def _client(self) -> str:
        peer = self.client_address[0]
        forwarded = self.headers.get("X-Forwarded-For", "")
        # Only a reverse proxy on this host may say who the client is.
        if forwarded and ip_address(peer).is_loopback:
            return forwarded.split(",")[-1].strip()
        return peer

    def _send(self, status: int, body: dict[str, Any], headers: Mapping[str, str] | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin and origin == self.server.cors_origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin")
        if origin != self.server.cors_origin:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", f"Content-Type, {CSRF_HEADER}")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if not path.startswith(PREFIX + "/"):
                raise WebError(404, "not_found", "No such endpoint.")
            body: dict[str, Any] = {}
            if method == "POST":
                # JSON only: a cross-site form cannot send it without a preflight.
                if self.headers.get_content_type() != "application/json":
                    raise WebError(415, "json_only", "Send JSON.")
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise WebError(413, "too_large", "Request too large.")
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise WebError(400, "bad_json", "Send a JSON object.")
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                token = cookie[COOKIE].value if COOKIE in cookie else ""
            except CookieError:
                token = ""
            status, out, headers = self.server.api.handle(
                method, path[len(PREFIX):], token=token, csrf=self.headers.get(CSRF_HEADER),
                body=body, client=self._client())
            self._send(status, out, headers)
        except WebError as error:
            self._send(error.status, {"ok": False, "error": error.code, "message": error.message})
        except WebAuthError as error:
            self._send(401, {"ok": False, "error": "auth", "message": str(error)})
        except AllowanceUnavailable as error:
            self._send(403, {"ok": False, "error": "not_enabled", "message": str(error)})
        except (AllowanceError, ValueError) as error:
            self._send(400, {"ok": False, "error": "refused", "message": str(error)})
        except Exception:
            logger.exception("web api: %s %s failed", method, path)
            self._send(500, {"ok": False, "error": "internal", "message": "Something went wrong on our side."})


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], api: WebApi, cors_origin: str):
        super().__init__(address, WebHandler)
        self.api = api
        self.cors_origin = cors_origin


def build_web_api_from_env(allowance: AllowanceService, env: Mapping[str, str] | None = None) -> tuple[WebApi, str]:
    values = os.environ if env is None else env
    domain = str(values.get("SIGN402_WEB_DOMAIN", "")).strip()
    uri = str(values.get("SIGN402_WEB_URI", "")).strip().rstrip("/")
    if not domain or not uri.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise ValueError("SIGN402_WEB_DOMAIN and an https SIGN402_WEB_URI are required.")
    listed = str(values.get("SIGN402_WEB_ALLOWED_ADDRESSES", "")).strip()
    if not listed:
        raise ValueError('SIGN402_WEB_ALLOWED_ADDRESSES is required: the beta allowlist, or "*".')
    allowed = None if listed == "*" else [a.strip() for a in listed.split(",") if a.strip()]
    store = WebAccountStore(Path(str(values.get(WEB_DB_ENV, "") or DEFAULT_WEB_DB)).expanduser())
    api = WebApi(
        WebAuth(store, domain=domain, uri=uri, allowed=allowed),
        allowance,
        min_owner_usdc=_usdc_atomic(values.get("SIGN402_WEB_MIN_OWNER_USDC", "1"), "SIGN402_WEB_MIN_OWNER_USDC"),
        max_limiters_per_30_days=int(values.get("SIGN402_WEB_MAX_LIMITERS_PER_30_DAYS", "3")),
    )
    return api, str(values.get("SIGN402_WEB_CORS_ORIGIN", "") or uri).rstrip("/")


def main() -> int:
    from .agent_allowance import build_allowance_service_from_env
    from .keyring import load_master_key

    logging.basicConfig(level=logging.INFO)
    if os.environ.get("SIGN402_WEB_ENABLED") != "1":
        logger.error("web api: SIGN402_WEB_ENABLED is not 1")
        return 1
    allowance = build_allowance_service_from_env(load_master_key())
    if allowance is None:
        logger.error("web api: the allowance lane is off (SIGN402_ALLOWANCE_ENABLED != 1)")
        return 1
    api, cors_origin = build_web_api_from_env(allowance)
    port = int(os.environ.get("SIGN402_WEB_PORT", DEFAULT_PORT))
    server = WebServer(("127.0.0.1", port), api, cors_origin)
    logger.info("web api: listening on 127.0.0.1:%s for %s", port, cors_origin)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
