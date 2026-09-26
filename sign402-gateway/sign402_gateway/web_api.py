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
    SIGN402_WEB_MAX_DEPLOYS_PER_DAY      limiters we pay to deploy for everyone together in 24 h (50)
    SIGN402_WEB_INTERNAL_TOKEN           shared with the gateway for the shop (32+ characters)
    SIGN402_WEB_GATEWAY_URL              the gateway on loopback (http://127.0.0.1:8099)
    SIGN402_WEB_STATIC_DIR               the site's website/ folder: serves /app/ and /assets/ too
plus everything SIGN402_ALLOWANCE_* the lane itself needs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl

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
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".json": "application/json"}


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


class GatewayShop:
    """The gateway's /internal/web/ routes (sign402_gateway.web_internal), over loopback."""

    def __init__(self, url: str, token: str, timeout: float = 180.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def __call__(self, action: str, account: str, body: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        data = json.dumps({**dict(body or {}), "account": account}).encode()
        request = urllib.request.Request(
            f"{self.url}/internal/web/{action}", data=data, method="POST",
            headers={"Content-Type": "application/json", "X-SingIt-Internal": self.token})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read() or b"{}")
            except ValueError:
                return error.code, {"ok": False, "error": "gateway", "text": "The shop is unavailable right now."}
        except (urllib.error.URLError, TimeoutError, OSError):
            raise WebError(503, "shop_unavailable", "The shop is unavailable right now. Nothing was paid.") from None


def _public_text(body: Mapping[str, Any]) -> dict[str, Any]:
    """Gateway replies use telegramText for the bot; the page reads `text`."""
    out = {k: v for k, v in body.items() if k != "telegramText"}
    if "telegramText" in body and "text" not in out:
        out["text"] = body["telegramText"]
    return out


SHOP_ROUTES = {
    ("GET", "/shop/tools"): "tools",
    ("POST", "/shop/tools/quote"): "tool-quote",
    ("POST", "/shop/tools/buy"): "tool-buy",
    ("POST", "/shop/bitrefill/search"): "bitrefill-search",
    ("POST", "/shop/bitrefill/quote"): "bitrefill-quote",
    ("POST", "/shop/bitrefill/buy"): "bitrefill-buy",
    ("GET", "/purchases"): "purchases",
    ("POST", "/purchases/reveal"): "purchase-reveal",
}


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
        max_deploys_per_day: int = 50,
        shop: Callable[[str, str, Mapping[str, Any] | None], tuple[int, dict[str, Any]]] | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.auth = auth
        self.allowance = allowance
        self.min_owner_usdc = min_owner_usdc
        self.max_limiters = max_limiters_per_30_days
        self.max_deploys_per_day = max_deploys_per_day
        self.shop = shop
        self.now = now
        self.auth_by_ip = RateLimit(30, 600)
        self.setup_by_account = RateLimit(5, 3600)
        self.prepare_by_account = RateLimit(20, 3600)
        self.permit_by_account = RateLimit(6, 86400)
        self.shop_by_account = RateLimit(60, 3600)
        self.chat_by_account = RateLimit(60, 3600)
        self.agent = None  # web_agent.WebAgent, when the chat is on
        self.link_by_account = RateLimit(10, 3600)

    def handle(self, method: str, path: str, *, token: str, csrf: str | None, body: dict[str, Any],
               client: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        """(status, JSON body, extra headers)."""
        if method == "POST" and path == "/auth/nonce":
            self.auth_by_ip.hit(client)
            self._refuse_contract_wallets(body.get("address"))
            return 200, self.auth.nonce(body.get("address")), {}
        if method == "POST" and path == "/auth/verify":
            self.auth_by_ip.hit(client)
            signed = self.auth.verify(body.get("message"), body.get("signature"))
            cookie = (f"{COOKIE}={signed['token']}; Path={PREFIX}; Max-Age={SESSION_SECONDS}; "
                      "HttpOnly; Secure; SameSite=Strict")
            out = {"account": signed["account"], "address": signed["address"],
                   "csrfToken": signed["csrfToken"], "expiresAt": signed["expiresAt"],
                   "telegramLinked": bool(self.auth.store.telegram_for(signed["account"]))}
            return 200, out, {"Set-Cookie": cookie}
        if method == "POST" and path == "/auth/logout":
            self.auth.session(token, csrf or "")
            self.auth.logout(token)
            return 200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path={PREFIX}; Max-Age=0; HttpOnly; Secure; SameSite=Strict"}

        session = self.auth.session(token, None if method == "GET" else (csrf or ""))
        account, owner = session["account_id"], session["address"]
        if method == "GET" and path == "/session":
            return 200, {"account": account, "address": owner,
                         "telegramLinked": bool(self.auth.store.telegram_for(account))}, {}
        if method == "POST" and path == "/link/telegram":
            self.link_by_account.hit(account)
            code = self.auth.store.new_link_code(account, int(self.now()))
            return 200, {"code": code, "expiresAt": int(self.now()) + 600,
                         "text": f"Send /link {code} to the SingIt bot within 10 minutes."}, {}
        if method == "POST" and path == "/link/telegram/remove":
            self.auth.store.unlink_telegram(account)
            return 200, {"telegramLinked": False}, {}
        if path == "/chats" or path.startswith("/chats/"):
            return self._chats(method, path, account, body)
        if (method, path) in SHOP_ROUTES:
            if self.shop is None:
                raise WebError(503, "shop_unavailable", "The shop is not enabled on this server.")
            self.shop_by_account.hit(account)
            forwarded = dict(body)
            if method == "GET" and path == "/purchases":
                forwarded = {"offset": body.get("offset", 0)}
            status, reply = self.shop(SHOP_ROUTES[(method, path)], account, forwarded)
            return status, _public_text(reply), {}
        if method == "GET" and path == "/allowance":
            return 200, self._status(account, owner), {}
        if method == "POST" and path == "/allowance/setup":
            return 200, self._setup(account, owner, body), {}
        if method == "POST" and path in ("/allowance/grant/prepare", "/allowance/revoke/prepare"):
            self.prepare_by_account.hit(account)
            kind = "GRANT" if path.startswith("/allowance/grant") else "REVOKE"
            return 200, self.allowance.prepare_wallet(
                account, kind, amount=body.get("amount"), limiter=body.get("limiter"),
                method=str(body.get("method") or "approve")), {}
        if method == "POST" and path in ("/allowance/grant/submit", "/allowance/revoke/submit"):
            op = self.allowance.store.op(account, str(body.get("operation") or ""))
            kind = "GRANT" if path.startswith("/allowance/grant") else "REVOKE"
            if op is None or op["kind"] != kind:
                raise WebError(404, "no_such_operation", "No such request.")
            if op["method"] == "permit":
                self.permit_by_account.hit(account)  # each one costs us gas
                return 200, self.allowance.submit_permit(account, op["op_id"], body.get("signature")), {}
            return 200, self.allowance.submit_wallet(account, op["op_id"], body.get("txHash")), {}
        if method == "POST" and path == "/allowance/pause":
            # The panic button: our guardian pauses the limiter for good, no wallet needed.
            self.setup_by_account.hit(account)
            return 200, self._public(self.allowance.pause(account)), {}
        if method == "GET" and path.startswith("/allowance/operations/"):
            return 200, self.allowance.operation(account, path.rsplit("/", 1)[1]), {}
        raise WebError(404, "not_found", "No such endpoint.")

    # -- allowance --

    def _public(self, described: Mapping[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in described.items() if k != "telegramText"}
        if "state" in out:
            out["state"] = _state_code(described)
        return out

    def _chats(self, method: str, path: str, account: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any], dict]:
        """The chat: talk to the agent; it sets limits, finds and buys (web_agent.py)."""
        if self.agent is None:
            raise WebError(503, "chat_unavailable", "The chat is not enabled on this server.")
        store = self.agent.store
        if method == "GET" and path == "/chats":
            return 200, {"chats": store.chats(account)}, {}
        if method == "GET" and path.startswith("/chats/"):
            chat_id = path.rsplit("/", 1)[1]
            chat = store.chat(account, chat_id)
            if chat is None:
                raise WebError(404, "no_such_chat", "No such chat.")
            return 200, {"chatId": chat_id, "title": chat["title"], "messages": store.messages(chat_id)}, {}
        if method == "POST" and path == "/chats/message":
            self.chat_by_account.hit(account)
            return 200, self.agent.message(account, body.get("chatId") or None, str(body.get("text") or "")), {}
        if method == "POST" and path == "/chats/action":
            self.chat_by_account.hit(account)
            action = body.get("action")
            if not isinstance(action, dict):
                raise WebError(400, "bad_action", "Send an action object.")
            return 200, self.agent.action(account, str(body.get("chatId") or ""), action), {}
        if method == "POST" and path == "/chats/delete":
            store.delete(account, str(body.get("chatId") or ""))
            return 200, {"ok": True}, {}
        raise WebError(404, "not_found", "No such endpoint.")

    def _refuse_contract_wallets(self, address: Any) -> None:
        """Smart-contract wallets sign with ERC-1271, which v1 does not check; say so plainly.

        An EIP-7702 account (code 0xef0100…) still signs with its own key and is fine.
        """
        try:
            code = self.allowance.evm.code(str(address or ""))
        except Exception:
            return  # the sign-in itself will say what is wrong with the address
        if code not in ("0x", "") and not code.lower().startswith("0xef0100"):
            raise WebError(400, "smart_wallet_unsupported",
                           "Smart-contract wallets are not supported yet. Connect a regular wallet (Rabby, MetaMask, Phantom).")

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
        if self.allowance.store.limiters_since(None, int(self.now()) - 86400) >= self.max_deploys_per_day:
            logger.warning("web api: the daily limiter deployment budget (%s) is used up", self.max_deploys_per_day)
            raise WebError(503, "deploy_budget", "We have created as many limiters as we can today. Try again tomorrow.")
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
        # Only a proxy on this host (cloudflared, or a reverse proxy) may say who the client is.
        if not ip_address(peer).is_loopback:
            return peer
        cloudflare = self.headers.get("Cf-Connecting-Ip", "").strip()
        if cloudflare:
            return cloudflare
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[-1].strip() if forwarded else peer

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
        if not self.path.startswith(PREFIX + "/") and self.server.static_root is not None:
            self._static()
            return
        self._dispatch("GET")

    def _static(self) -> None:
        """The page itself (website/app and its assets), so page and API share one origin."""
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/app/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.endswith("/"):
            path += "index.html"
        root = self.server.static_root
        target = (root / path.lstrip("/")).resolve()
        allowed = [(root / "app").resolve(), (root / "assets").resolve()]
        if not any(target.is_relative_to(base) for base in allowed) or not target.is_file():
            self._send(404, {"ok": False, "error": "not_found", "message": "No such page."})
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")  # no clickjacking around wallet prompts
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path, _, query = self.path.partition("?")
        try:
            if not path.startswith(PREFIX + "/"):
                raise WebError(404, "not_found", "No such endpoint.")
            body: dict[str, Any] = dict(parse_qsl(query)) if method == "GET" else {}
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

    def __init__(self, address: tuple[str, int], api: WebApi, cors_origin: str, static_root: Path | None = None):
        super().__init__(address, WebHandler)
        self.api = api
        self.cors_origin = cors_origin
        # website/: /app/ and /assets/ are served from here when set.
        self.static_root = Path(static_root).resolve() if static_root else None


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
    token = str(values.get("SIGN402_WEB_INTERNAL_TOKEN", "")).strip()
    shop = None
    if token:
        if len(token) < 32:
            raise ValueError("SIGN402_WEB_INTERNAL_TOKEN must have at least 32 characters.")
        shop = GatewayShop(str(values.get("SIGN402_WEB_GATEWAY_URL", "") or "http://127.0.0.1:8099"), token)
    api = WebApi(
        WebAuth(store, domain=domain, uri=uri, allowed=allowed),
        allowance,
        min_owner_usdc=_usdc_atomic(values.get("SIGN402_WEB_MIN_OWNER_USDC", "1"), "SIGN402_WEB_MIN_OWNER_USDC"),
        max_limiters_per_30_days=int(values.get("SIGN402_WEB_MAX_LIMITERS_PER_30_DAYS", "3")),
        max_deploys_per_day=int(values.get("SIGN402_WEB_MAX_DEPLOYS_PER_DAY", "50")),
        shop=shop,
    )
    if str(values.get("SIGN402_WEB_CHAT_ENABLED", "1")) == "1":
        from .web_agent import ChatStore, build_agent_from_env

        agent = build_agent_from_env(allowance, shop, ChatStore(store.path), env=values)
        agent.setup = lambda account, body: api._setup(account, store.owner_for(account), body)
        api.agent = agent
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
    static = os.environ.get("SIGN402_WEB_STATIC_DIR", "")
    server = WebServer(("127.0.0.1", port), api, cors_origin, Path(static).expanduser() if static else None)
    logger.info("web api: listening on 127.0.0.1:%s for %s", port, cors_origin)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
