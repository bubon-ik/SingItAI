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
    SIGN402_WEB_PUSH_VAPID_KEY           turns on notifications to the installed page (web_push.py)
    SIGN402_WEB_PUSH_SUBJECT             how push services reach us (default: SIGN402_WEB_URI)
    SIGN402_WEB_SURPLUS_API_KEY          a Surplus buyer key (inf_…): turns on the universal /v1 paid from the
                                         account's balance (web_api_proxy.py); without it /v1 pays per answer
    SIGN402_WEB_SURPLUS_URL              Surplus's API (https://api.surplusintelligence.ai)
    SINGIT_RECONCILIATION_PER_MINUTE     per client IP, the free public GET /v1/wallet/{address}/reconciliation (30)
plus everything SIGN402_ALLOWANCE_* the lane itself needs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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

from . import analytics
from .agent_allowance import AllowanceError, AllowanceService, AllowanceUnavailable, _usdc_atomic
from .web_accounts import (
    DEFAULT_WEB_DB, SESSION_SECONDS, WEB_DB_ENV, WebAccountStore, WebAuth, WebAuthError, chain_of_account,
)
from .web_api_keys import ApiError, ApiKeys, Stream
from .web_api_proxy import Relay
from .wallet_reconciliation import DEFAULT_DAYS, MAX_DAYS, Reconciliation
from .web_mcp import McpServer, rpc_error

logger = logging.getLogger(__name__)

PREFIX = "/web/v1"
COOKIE = "singit_session"
CSRF_HEADER = "X-SingIt-CSRF"
ACCOUNT_HEADER = "X-SingIt-Account"
MAX_BODY = 16 * 1024
RECONCILIATION_PATH = re.compile(r"/v1/wallet/([^/]{1,100})/reconciliation")  # public, before the API-key /v1
API_PREFIX = "/v1"            # the OpenAI-compatible API that API keys open (web_api_keys.py)
API_MAX_BODY = 160 * 1024     # a question may be 24,000 characters, several bytes each
MCP_PATH = "/mcp"              # SingIt's remote MCP server (web_mcp.py), on the same API keys
PROXY_MAX_BODY = 16 * 1024 * 1024  # relayed requests: a coding agent's whole context, images included
MAX_UPLOAD = 25 * 1024 * 1024      # an audio file to transcribe, OpenAI's own limit
REQUEST_READ_SECONDS = 15
MIN_UPLOAD_BYTES_PER_SECOND = 500_000
DEFAULT_PORT = 8130
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".json": "application/json",
                ".webmanifest": "application/manifest+json"}


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


def per_minute_from_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


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


SOLANA_NOT_YET = ("Spending limits on Solana are coming. Your Solana wallet can sign in, chat and look things up; "
                  "to let your agent buy now, connect a Base wallet (the same Phantom works on Base).")

SHOP_ROUTES = {
    ("GET", "/shop/tools"): "tools",
    ("POST", "/shop/tools/quote"): "tool-quote",
    ("POST", "/shop/tools/buy"): "tool-buy",
    ("POST", "/shop/bitrefill/search"): "bitrefill-search",
    ("POST", "/shop/bitrefill/quote"): "bitrefill-quote",
    ("POST", "/shop/bitrefill/buy"): "bitrefill-buy",
    ("GET", "/purchases"): "purchases",
    ("POST", "/purchases/reveal"): "purchase-reveal",
    ("GET", "/chat/models"): "venice-models",
    ("POST", "/chat/model"): "venice-model",
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
        self.progress_by_account = RateLimit(900, 3600)  # a purchase's stage, asked every second while it runs
        self.chat_by_account = RateLimit(60, 3600)
        self.agent = None  # web_agent.WebAgent, when the chat is on
        self.solana = None  # solana_allowance.SolanaAllowanceService, when the Solana lane is on
        self.link_by_account = RateLimit(10, 3600)
        self.push = None  # web_push.WebPush, when SIGN402_WEB_PUSH_VAPID_KEY is set
        self.push_by_account = RateLimit(20, 3600)
        self.keys_by_account = RateLimit(20, 3600)
        self.keys = ApiKeys(self, now=now)
        self.mcp = McpServer(self.keys)
        self.reconciliation_by_ip = RateLimit(per_minute_from_env("SINGIT_RECONCILIATION_PER_MINUTE", 30), 60)
        self._reconciliation: Reconciliation | None = None

    def wallet_reconciliation(self, address: str, query: Mapping[str, str], client: str) -> dict[str, Any]:
        """The free public report (wallet_reconciliation.py). Needs no session or key; only the per-IP limit."""
        self.reconciliation_by_ip.hit(client)
        raw_days = query.get("days", str(DEFAULT_DAYS))
        if not re.fullmatch(r"[0-9]{1,3}", raw_days) or not 1 <= int(raw_days) <= MAX_DAYS:
            raise WebError(400, "bad_days", f"days must be a whole number from 1 to {MAX_DAYS}.")
        if self._reconciliation is None:
            self._reconciliation = Reconciliation(self.allowance.store, getattr(self.solana, "store", None), self.now)
        try:
            return self._reconciliation.report(address, int(raw_days))
        except WebAuthError:
            raise WebError(400, "bad_address", "Send a Base address (0x and 40 hex digits) or a Solana address.") from None

    def handle(self, method: str, path: str, *, token: str, csrf: str | None, body: dict[str, Any],
               client: str, expected_account: str | None = None,
               user_agent: str = "") -> tuple[int, dict[str, Any], dict[str, str]]:
        """(status, JSON body, extra headers)."""
        if method == "POST" and path == "/auth/nonce":
            self.auth_by_ip.hit(client)
            self._refuse_contract_wallets(body.get("address"))
            return 200, self.auth.nonce(body.get("address")), {}
        if method == "POST" and path == "/auth/verify":
            self.auth_by_ip.hit(client)
            signed = self.auth.verify(body.get("message"), body.get("signature"), body.get("utm"))
            # Switching wallets signs in over a live session: once the new wallet has signed, the
            # browser's previous session ends here, since its cookie is about to be replaced.
            if token:
                self.auth.logout(token)
            cookie = (f"{COOKIE}={signed['token']}; Path={PREFIX}; Max-Age={SESSION_SECONDS}; "
                      "HttpOnly; Secure; SameSite=Strict")
            out = {"account": signed["account"], "address": signed["address"], "chain": signed["chain"],
                   "csrfToken": signed["csrfToken"], "expiresAt": signed["expiresAt"],
                   "telegramLinked": bool(self.auth.store.telegram_for(signed["account"]))}
            analytics.emit("sign_in", signed["account"], chain=signed["chain"], new_account=bool(signed["newAccount"]),
                           client_ip=client, user_agent=user_agent)
            return 200, out, {"Set-Cookie": cookie}
        session = self.auth.session(token, None if method == "GET" and expected_account is None else (csrf or ""))
        account, owner = session["account_id"], session["address"]
        # The HTTP client pins the identity displayed in that tab. A cookie can
        # change independently when another tab signs in, including on GETs.
        # None is reserved for in-process callers; HTTP always supplies a string.
        if expected_account is not None:
            bootstrap = method == "GET" and path == "/session" and not expected_account
            if not bootstrap and expected_account != account:
                raise WebError(409, "session_changed", "Your account changed in another tab. Sign in again.")
        if method == "POST" and path == "/auth/logout":
            self.auth.logout(token)
            return 200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path={PREFIX}; Max-Age=0; HttpOnly; Secure; SameSite=Strict"}
        chain = chain_of_account(account)
        if method == "GET" and path == "/session":
            return 200, {"account": account, "address": owner, "chain": chain,
                         "telegramLinked": bool(self.auth.store.telegram_for(account))}, {}
        if chain == "solana" and path.startswith("/allowance"):
            if self.solana is None:  # the Solana lane is off on this server
                if method == "GET" and path == "/allowance":
                    return 200, {"configured": False, "chain": "solana", "supported": False,
                                 "text": SOLANA_NOT_YET}, {}
                raise WebError(400, "solana_not_yet", SOLANA_NOT_YET)
            return self._solana_allowance(method, path, account, body)
        if method == "POST" and path == "/link/telegram":
            self.link_by_account.hit(account)
            code = self.auth.store.new_link_code(account, int(self.now()))
            return 200, {"code": code, "expiresAt": int(self.now()) + 600,
                         "text": f"Send /link {code} to the SingIt bot within 10 minutes."}, {}
        if method == "POST" and path == "/link/telegram/remove":
            self.auth.store.unlink_telegram(account)
            return 200, {"telegramLinked": False}, {}
        if path.startswith("/push/"):
            return self._push(method, path, account, body)
        if path == "/api-keys" or path.startswith("/api-keys/"):
            return self.keys.manage(method, path, account, owner, body)
        if path == "/balance" or path.startswith("/balance/"):
            return self.keys.manage_balance(method, path, account, body)
        if path == "/chats" or path.startswith("/chats/"):
            return self._chats(method, path, account, body)
        if method == "GET" and path == "/usage":
            return 200, self._usage(account), {}
        if method == "GET" and path == "/shop/progress":
            if self.shop is None:
                raise WebError(503, "shop_unavailable", "The shop is not enabled on this server.")
            self.progress_by_account.hit(account)
            status, reply = self.shop("purchase-progress", account, {"id": str(body.get("id") or "")[:64]})
            return status, {"ok": True, "stage": str(reply.get("stage") or "working")}, {}
        if (method, path) in SHOP_ROUTES:
            if self.shop is None:
                raise WebError(503, "shop_unavailable", "The shop is not enabled on this server.")
            self.shop_by_account.hit(account)
            forwarded = dict(body)
            if method == "GET" and path == "/purchases":
                forwarded = {"offset": body.get("offset", 0)}
            elif method == "GET":
                forwarded = {}
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

    # -- notifications (web_push.py) --

    def _push(self, method: str, path: str, account: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any], dict]:
        from .web_push import PushError

        if method == "GET" and path == "/push/key":
            if self.push is None:
                return 200, {"enabled": False}, {}
            return 200, {"enabled": True, "publicKey": self.push.public_key,
                         "devices": len(self.auth.store.pushes_for(account))}, {}
        if self.push is None:
            raise WebError(503, "push_off", "Notifications are not enabled on this server.")
        if method == "POST" and path == "/push/subscribe":
            self.push_by_account.hit(account)
            try:
                devices = self.push.subscribe(account, body.get("subscription"))
            except PushError as exc:
                raise WebError(400, "push_unsupported", str(exc)) from None
            return 200, {"subscribed": True, "devices": devices}, {}
        if method == "POST" and path == "/push/unsubscribe":
            self.auth.store.remove_push(account, str(body.get("endpoint") or ""))
            return 200, {"subscribed": False}, {}
        if method == "POST" and path == "/push/test":
            self.push_by_account.hit(account)
            sent = self.push.send(account, "SingIt", "Notifications are on. You will hear from your agent here.")
            if not sent:
                raise WebError(502, "push_failed", "No device took the notification. Turn notifications off and on again.")
            return 200, {"sent": sent}, {}
        raise WebError(404, "not_found", "No such endpoint.")

    # -- allowance on Solana (solana_allowance.py) --

    def _solana_allowance(self, method: str, path: str, account: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any], dict]:
        """The same routes as Base, for a Solana wallet: limits, one approval from the wallet, revoke."""
        lane = self.solana
        if method == "GET" and path == "/allowance":
            return 200, {**lane.status(account), "alerts": [], "staleLimiters": []}, {}
        if method == "POST" and path == "/allowance/setup":
            self.setup_by_account.hit(account)
            held = int(lane.chain_state(account)["owner"]["amount"])
            if held < self.min_owner_usdc:
                raise WebError(400, "owner_needs_usdc", (
                    f"Your wallet needs at least {self.min_owner_usdc / 1_000_000:g} USDC on Solana before we "
                    "set up your agent's limits."))
            return 200, lane.setup(account, body.get("dailyCap"), body.get("perPurchaseCap"), body.get("days")), {}
        if method == "POST" and path in ("/allowance/grant/prepare", "/allowance/revoke/prepare", "/allowance/fund-gas/prepare"):
            self.prepare_by_account.hit(account)
            kind = "FUND_GAS" if path.startswith("/allowance/fund-gas") else "GRANT" if path.startswith("/allowance/grant") else "REVOKE"
            return 200, lane.prepare_wallet(account, kind, amount=body.get("amount")), {}
        if method == "POST" and path in ("/allowance/grant/submit", "/allowance/revoke/submit", "/allowance/fund-gas/submit"):
            self.permit_by_account.hit(account)  # bound signed transaction submission
            expected_kind = "FUND_GAS" if path.startswith("/allowance/fund-gas") else "GRANT" if path.startswith("/allowance/grant") else "REVOKE"
            op = lane.store.op(account, str(body.get("operation") or ""))
            if op is None or op["kind"] != expected_kind:
                raise WebError(404, "no_such_operation", "No such request.")
            return 200, lane.submit_wallet(account, str(body.get("operation") or ""), body.get("transaction")), {}
        if method == "GET" and path.startswith("/allowance/operations/"):
            return 200, lane.operation(account, path.rsplit("/", 1)[1]), {}
        if method == "POST" and path == "/allowance/pause":
            raise WebError(400, "solana_revoke", "On Solana, revoking the approval is the stop: it takes effect at once.")
        raise WebError(404, "not_found", "No such endpoint.")

    # -- allowance --

    def _public(self, described: Mapping[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in described.items() if k != "telegramText"}
        if "state" in out:
            out["state"] = _state_code(described)
        return out

    def _usage(self, account: str) -> dict[str, Any]:
        """The Usage page: today's Venice answers per model, and the chat credit and its top-ups."""
        today = int(time.time()) // 86400 * 86400  # the limiter's and Venice's day: UTC
        out: dict[str, Any] = {"since": today,
                               "today": self.agent.store.usage(account, today) if self.agent is not None else None,
                               "venice": None}
        if self.shop is not None:
            status, venice = self.shop("venice-usage", account, {})
            if status < 400 and venice.get("ok"):
                out["venice"] = {k: venice.get(k) for k in ("creditAtomic", "model", "modelLabel", "topUps")}
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
            return 200, self.agent.message(account, body.get("chatId") or None, str(body.get("text") or ""),
                                           reply_language=str(body.get("lang") or "") or None), {}
        if method == "POST" and path == "/chats/action":
            self.chat_by_account.hit(account)
            action = body.get("action")
            if not isinstance(action, dict):
                raise WebError(400, "bad_action", "Send an action object.")
            return 200, self.agent.action(account, str(body.get("chatId") or ""), action), {}
        if method == "POST" and path == "/chats/update":
            changes: dict[str, Any] = {}
            if "title" in body:
                title = " ".join(str(body.get("title") or "").split())
                if not title:
                    raise WebError(400, "bad_title", "Give the chat a name.")
                changes["title"] = title
            for name in ("pinned", "archived"):
                if name in body:
                    if not isinstance(body[name], bool):
                        raise WebError(400, f"bad_{name}", f"'{name}' is true or false.")
                    changes[name] = body[name]
            if not store.update(account, str(body.get("chatId") or ""), **changes):
                raise WebError(404, "no_such_chat", "No such chat.")
            return 200, {"ok": True, "chats": store.chats(account)}, {}
        if method == "POST" and path == "/chats/delete":
            store.delete(account, str(body.get("chatId") or ""))
            return 200, {"ok": True}, {}
        raise WebError(404, "not_found", "No such endpoint.")

    def _refuse_contract_wallets(self, address: Any) -> None:
        """Smart-contract wallets sign with ERC-1271, which v1 does not check; say so plainly.

        An EIP-7702 account (code 0xef0100…) still signs with its own key and is fine.
        """
        if not str(address or "").startswith("0x"):
            return  # a Solana wallet: no contract code to look at
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
        status["staleLimiters"] = self.allowance.stale_allowances(account)
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


# The page's own scripts and styles, as the page references them: `app.css`, `main.js`, `from "./api.js"`.
PAGE_ASSET = re.compile(r'((?:src|href)="|from\s+")(\.?/?[\w-]+\.(?:js|css))"')


def page_version(app_dir: Path) -> str:
    """A short hash of the page's files, so a new deploy is a new URL for every one of them.

    Caches in front of the page (Cloudflare sets its own browser TTL) may keep a file for
    hours; versioned references keep a browser from mixing an old script with a new one.
    """
    digest = hashlib.sha256()
    for path in sorted(app_dir.glob("*")):
        if path.is_file():
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns};".encode())
    return digest.hexdigest()[:12]


def versioned(text: str, version: str) -> str:
    return PAGE_ASSET.sub(lambda m: f'{m.group(1)}{m.group(2)}?v={version}"', text)


class WebHandler(BaseHTTPRequestHandler):
    server_version = "SingItWeb/1"
    head_only = False  # a HEAD request: the same status and headers as GET, and no body

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_READ_SECONDS)

    def _json_body(self, limit: int = MAX_BODY) -> dict[str, Any]:
        raw = self._raw_body(limit)
        try:
            body = json.loads(raw if raw else b"{}")
        except (ValueError, UnicodeError):
            raise WebError(400, "bad_json", "Send a JSON object.") from None
        if not isinstance(body, dict):
            raise WebError(400, "bad_json", "Send a JSON object.")
        return body

    def _raw_body(self, limit: int) -> bytes:
        # This server does not decode transfer encodings. Never accept ambiguous
        # framing, signed lengths, or duplicate headers, even if they agree.
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) > 1:
            raise WebError(400, "bad_length", "Ambiguous request length.")
        value = lengths[0].strip() if lengths else "0"
        if not re.fullmatch(r"[0-9]+", value):
            raise WebError(400, "bad_length", "Invalid request length.")
        digits = value.lstrip("0") or "0"
        if len(digits) > len(str(limit)) or int(digits) > limit:
            raise WebError(413, "too_large", "Request too large.")
        length = int(digits)
        # Small bodies get the usual deadline; a large one (a coding agent's context, an audio file) gets as long again
        # as it takes at MIN_UPLOAD_BYTES_PER_SECOND.
        deadline = time.monotonic() + REQUEST_READ_SECONDS + length / MIN_UPLOAD_BYTES_PER_SECOND
        raw = bytearray()
        try:
            while len(raw) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(length - len(raw))
                if not chunk:
                    raise WebError(400, "incomplete_body", "Incomplete request body.")
                raw.extend(chunk)
        except TimeoutError:
            raise WebError(408, "request_timeout", "Request body timed out.") from None
        finally:
            self.connection.settimeout(REQUEST_READ_SECONDS)
        return bytes(raw)

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
        try:
            self.end_headers()
            if not self.head_only:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # The page closed or reloaded before its answer came: no one is left to tell, and nothing failed here.
            self.close_connection = True
            logger.info("web api: %s: the page left before its answer", self.path.partition("?")[0])

    def _wallet_reconciliation(self) -> None:
        """GET/HEAD /v1/wallet/{address}/reconciliation: public read, any origin, no cookies, cached a minute."""
        path, _, query = self.path.partition("?")
        found = RECONCILIATION_PATH.fullmatch(path)
        status, headers, out = 200, {"Cache-Control": "public, max-age=60"}, {}
        try:
            out = self.server.api.wallet_reconciliation(found.group(1), dict(parse_qsl(query)), self._client())
        except WebError as error:
            status, headers = error.status, {"Cache-Control": "no-store"}
            out = {"ok": False, "error": error.code, "message": error.message}
            if error.status == 429:
                headers["Retry-After"] = "60"
        except Exception:
            logger.exception("web api: wallet reconciliation failed")
            status, headers = 500, {"Cache-Control": "no-store"}
            out = {"ok": False, "error": "internal", "message": "Something went wrong on our side."}
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        try:
            self.end_headers()
            if not self.head_only:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_relay(self, relay: Relay) -> None:
        """An upstream answer as it comes (web_api_proxy.Relay). It is read to the end even when the client left,
        because its last event says what it cost."""
        self.close_connection = True  # HTTP/1.0: the end of a stream of unknown length is the end of the connection
        gone = False
        try:
            self.send_response(relay.status)
            for key, value in relay.headers.items():
                self.send_header(key, value)
            self.send_header("X-Content-Type-Options", "nosniff")
            if relay.length is not None:
                self.send_header("Content-Length", str(relay.length))
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            gone = True
        try:
            for chunk in relay:
                if gone:
                    continue
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    gone = True
                    logger.info("web api: %s: the client left mid-answer; reading it to the end for its cost",
                                self.path.partition("?")[0])
        finally:
            relay.close()

    def _send_stream(self, status: int, data: bytes, headers: Mapping[str, str] | None = None) -> None:
        """Server-sent events, whole: the answer is complete before the first byte (web_api_keys.sse)."""
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

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
        self.send_header("Access-Control-Allow-Headers", f"Content-Type, {CSRF_HEADER}, {ACCOUNT_HEADER}")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == MCP_PATH:
            self._send(405, {"ok": False, "error": "method_not_allowed",
                             "message": "This endpoint offers no SSE stream. Send POST."}, {"Allow": "POST"})
            return
        if RECONCILIATION_PATH.fullmatch(self.path.partition("?")[0]):
            self._wallet_reconciliation()
            return
        if self.path.startswith(API_PREFIX + "/"):
            self._public("GET")
            return
        if not self.path.startswith(PREFIX + "/") and self.server.static_root is not None:
            self._static()
            return
        self._dispatch("GET")

    def do_DELETE(self) -> None:
        if self.path.split("?", 1)[0] == MCP_PATH:
            self._send(405, {"ok": False, "error": "method_not_allowed",
                             "message": "This endpoint offers no SSE stream. Send POST."}, {"Allow": "POST"})
            return
        self._send(404, {"ok": False, "error": "not_found", "message": "No such endpoint."})

    def do_HEAD(self) -> None:
        """The page answers HEAD as GET without a body; an API route is never run for one.

        Reown's wallet kit sends HEAD for the page to read its Cross-Origin-Opener-Policy.
        Without this the standard library answered 501, once per check, into the console.
        """
        self.head_only = True
        if RECONCILIATION_PATH.fullmatch(self.path.partition("?")[0]):
            self._wallet_reconciliation()
            return
        if not self.path.startswith(PREFIX + "/") and self.server.static_root is not None:
            self._static()
            return
        self._send(405, {"ok": False, "error": "method_not_allowed", "message": "Use GET or POST."},
                   {"Allow": "GET, POST"})

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
        app_dir = (root / "app").resolve()
        if target.parent == app_dir and target.suffix in (".html", ".js"):
            data = versioned(data.decode("utf-8"), page_version(app_dir)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")  # no clickjacking around wallet prompts
        # Not no-referrer: Reown's sign-in frame (email, Google, Apple) checks which site embeds it and
        # stays silent without our origin. Other sites get the origin only, never the path.
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not self.head_only:
            self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] == MCP_PATH:
            self._mcp()
            return
        if self.path.startswith(API_PREFIX + "/"):
            self._public("POST")
            return
        self._dispatch("POST")

    def _mcp(self) -> None:
        """POST /mcp: SingIt's remote MCP server, over Streamable HTTP with JSON responses only."""
        try:
            if self.headers.get_content_type() != "application/json":
                self._send(415, rpc_error(None, -32600, "Send JSON."))
                return
            raw = self._raw_body(API_MAX_BODY)
            status, out, headers = self.server.api.mcp.handle(
                authorization=self.headers.get("Authorization"), api_key=self.headers.get("x-api-key"),
                origin=self.headers.get("Origin"), protocol_version=self.headers.get("MCP-Protocol-Version"),
                body=raw)
            if out is None:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(status, out, headers)
        except WebError as error:
            self.close_connection = True
            self._send(error.status, rpc_error(None, -32600, error.message))
        except Exception:
            logger.exception("web api: POST /mcp failed")
            self._send(500, rpc_error(None, -32603, "Something went wrong on our side."))

    def _public(self, method: str) -> None:
        """/v1: code holding an API key. Errors keep OpenAI's shape so SDKs show them as they are."""
        path, _, query = self.path.partition("?")
        try:
            body: dict[str, Any] = {}
            raw = None
            if method == "POST" and path == API_PREFIX + "/audio/transcriptions":
                # A file, as OpenAI's SDK uploads it: sent on as it came; only its model is read here.
                content_type = self.headers.get("Content-Type", "")
                if self.headers.get_content_type() != "multipart/form-data":
                    raise ApiError(415, "Upload the audio as multipart/form-data.", code="multipart_only")
                try:
                    data = self._raw_body(MAX_UPLOAD)
                except WebError as error:
                    raise ApiError(error.status, error.message, code=error.code) from None
                found = re.search(rb'name="model"\r\n(?:[^\r\n]*\r\n)*?\r\n([^\r\n]{1,120})\r\n', data)
                body = {"model": found.group(1).decode("utf-8", "replace") if found else None}
                raw = (data, content_type)
            elif method == "POST":
                if self.headers.get_content_type() != "application/json":
                    raise ApiError(415, "Send JSON.", code="json_only")
                try:
                    body = self._json_body(PROXY_MAX_BODY if self.server.api.keys.proxy is not None else API_MAX_BODY)
                except WebError as error:
                    error_ = ApiError(error.status, error.message, code=error.code)
                    error_.anthropic = path.startswith(API_PREFIX + "/messages")
                    raise error_ from None
            status, out, headers = self.server.api.keys.public(
                method, path[len(API_PREFIX):], authorization=self.headers.get("Authorization"),
                origin=self.headers.get("Origin"), body=body, headers=self.headers, query=dict(parse_qsl(query)), raw=raw)
            if isinstance(out, Relay):
                self._send_relay(out)
            elif isinstance(out, Stream):
                self._send_stream(status, out, headers)
            else:
                self._send(status, out, headers)
        except ApiError as error:
            self.close_connection = True
            self._send(error.status, error.body())
        except WebError as error:
            self._send(error.status, ApiError(error.status, error.message, type_="api_error", code=error.code).body())
        except Exception:
            logger.exception("web api: %s %s failed", method, path)
            self._send(500, ApiError(500, "Something went wrong on our side.", type_="api_error").body())

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
                body = self._json_body()
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                token = cookie[COOKIE].value if COOKIE in cookie else ""
            except CookieError:
                token = ""
            status, out, headers = self.server.api.handle(
                method, path[len(PREFIX):], token=token, csrf=self.headers.get(CSRF_HEADER),
                body=body, client=self._client(), expected_account=self.headers.get(ACCOUNT_HEADER, ""),
                user_agent=self.headers.get("User-Agent", ""))
            self._send(status, out, headers)
        except WebError as error:
            self.close_connection = True
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


def build_web_api_from_env(allowance: AllowanceService, env: Mapping[str, str] | None = None,
                           solana: Any = None) -> tuple[WebApi, str]:
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
    api.solana = solana
    surplus_key = str(values.get("SIGN402_WEB_SURPLUS_API_KEY", "")).strip()
    if surplus_key and shop is not None:
        from .web_api_proxy import SURPLUS_URL, Proxy, Upstream
        from .web_balance import Balances

        api.keys.proxy = Proxy(api.keys, Upstream(surplus_key, str(values.get("SIGN402_WEB_SURPLUS_URL", "")
                                                                   or SURPLUS_URL)), Balances(store.path))
    from .web_push import from_env as push_from_env

    try:
        api.push = push_from_env(store, values)
    except ValueError as exc:  # a bad key leaves notifications off; the page works without them
        logger.error("web api: notifications disabled: %s", exc)
    if str(values.get("SIGN402_WEB_CHAT_ENABLED", "1")) == "1":
        from .web_agent import ChatStore, build_agent_from_env

        agent = build_agent_from_env(allowance, shop, ChatStore(store.path), env=values)
        agent.setup = lambda account, body: (
            api._solana_allowance("POST", "/allowance/setup", account, body)[1] if account.startswith("solana:")
            else api._setup(account, store.owner_for(account), body))
        agent.solana = solana
        api.agent = agent
    return api, str(values.get("SIGN402_WEB_CORS_ORIGIN", "") or uri).rstrip("/")


def main() -> int:
    from .agent_allowance import build_allowance_service_from_env
    from .keyring import load_master_key

    logging.basicConfig(level=logging.INFO)
    if os.environ.get("SIGN402_WEB_ENABLED") != "1":
        logger.error("web api: SIGN402_WEB_ENABLED is not 1")
        return 1
    from .solana_allowance import build_solana_allowance_from_env

    master = load_master_key()
    allowance = build_allowance_service_from_env(master)
    try:
        solana = build_solana_allowance_from_env(master)
    except (ValueError, OSError) as exc:  # a misconfigured Solana lane stays off; Base keeps working
        logger.error("web api: solana allowance lane disabled: %s", exc)
        solana = None
    if allowance is None:
        logger.error("web api: the allowance lane is off (SIGN402_ALLOWANCE_ENABLED != 1)")
        return 1
    api, cors_origin = build_web_api_from_env(allowance, solana=solana)
    port = int(os.environ.get("SIGN402_WEB_PORT", DEFAULT_PORT))
    static = os.environ.get("SIGN402_WEB_STATIC_DIR", "")
    server = WebServer(("127.0.0.1", port), api, cors_origin, Path(static).expanduser() if static else None)
    logger.info("web api: listening on 127.0.0.1:%s for %s", port, cors_origin)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
