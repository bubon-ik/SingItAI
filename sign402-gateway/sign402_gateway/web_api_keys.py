"""API keys: an OpenAI-compatible /v1 for code, paid from the key owner's own limits.

Design: docs/singit-api-keys-plan.md. A key (`sk_singit_…`) is made in the web app and stands for one
account. Any OpenAI SDK pointed at /v1 with the key as its Bearer token asks SingIt Ask for an answer;
the gateway pays for it from that account's own limiter (Base) or delegated USDC (Solana), exactly as
the web chat does (web_ask._paid_answer). A key can do nothing else: it cannot change limits, move
funds or buy goods.

Each key has a daily cap and a per-answer cap of its own, below the account's limits. The daily cap
is held strictly: before an answer, the key's spend today plus the most every answer in flight may
cost must fit under it. Creating a key takes a signature from the account's wallet over the exact
terms, like signing in; revoking one takes nothing but the session.

The key itself is shown once and stored only as a hash (web_accounts.WebAccountStore).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping

from . import analytics
from .web_accounts import NONCE_SECONDS, WebAuthError, chain_of_account

logger = logging.getLogger(__name__)

DEFAULT_DAILY_ATOMIC = 5_000_000          # $5 a day
DEFAULT_PER_REQUEST_ATOMIC = 250_000      # $0.25 an answer
DEFAULT_DAYS = 90
MAX_DAILY_ATOMIC = 1_000_000_000          # $1,000 a day; the account's own limits hold on top
MAX_PER_REQUEST_ATOMIC = 50_000_000       # $50 an answer
MAX_DAYS = 365
REQUESTS_PER_MINUTE = 60
SEARCH_PATHS = ("/exa/search", "/exa/contents", "/exa/answer", "/video/generations")
SEARCH_HOLD_ATOMIC = 67_000  # the most one search can cost (web_data.SEARCH_CAP_ATOMIC), held against the daily cap
VIDEO_HOLD_ATOMIC = 1_302_000  # the most one video job can cost (web_data.VIDEO_CAP_ATOMIC)
VIDEO_JOB = re.compile(r"/video/generations/([A-Za-z0-9_-]{6,80})(/content)?")
ASK_URL = "https://ask.singitai.app"
NAME = re.compile(r"[\w .,:()'-]{1,40}")


class ApiError(Exception):
    """An error in OpenAI's shape, so SDKs show its message as it is."""

    def __init__(self, status: int, message: str, *, type_: str = "invalid_request_error", code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.type, self.code = status, message, type_, code
        self.anthropic = False  # set for /v1/messages: Anthropic's SDK and Claude Code read their own shape

    def body(self) -> dict[str, Any]:
        if self.anthropic:
            return {"type": "error", "error": {"type": ANTHROPIC_TYPES.get(self.status, "api_error"),
                                               "message": self.message}}
        return {"error": {"message": self.message, "type": self.type, "code": self.code}}


ANTHROPIC_TYPES = {400: "invalid_request_error", 401: "authentication_error", 402: "billing_error",
                   403: "permission_error", 404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error",
                   503: "overloaded_error"}


def _usd_atomic(value: Any, default: int, *, low: int, high: int, what: str) -> int:
    if value in (None, ""):
        return default
    try:
        atomic = round(float(value) * 1_000_000)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number of dollars.") from None
    if not low <= atomic <= high:
        raise ValueError(f"{what} must be between ${low / 1e6:g} and ${high / 1e6:g}.")
    return atomic


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def key_message(address: str, name: str, daily: int, per_request: int, until: int, nonce: str, issued: int) -> str:
    """What the owner signs to make a key: the exact terms, in words."""
    return (
        "SingIt: create an API key\n\n"
        f"Name: {name}\n"
        f"Spends up to {daily / 1e6:g} USDC a day and {per_request / 1e6:g} USDC per request, "
        "from your SingIt limits.\n"
        f"Valid until: {_date(until)}\n"
        f"Account: {address}\n\n"
        "This key can only pay for model answers, web search (Exa) and paid data lookups (weather, market data, "
        "ENS and similar) in SingIt. It cannot move your funds or change your limits.\n\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {_date(issued)}"
    )


def public_key(row: Mapping[str, Any], spent: tuple[int, int], now: int) -> dict[str, Any]:
    state = "revoked" if row["revoked_at"] is not None else ("expired" if row["expires_at"] <= now else "active")
    return {"id": row["key_id"], "name": row["name"], "prefix": row["prefix"], "state": state,
            "dailyCapUsd": row["daily_cap_atomic"] / 1e6, "perRequestCapUsd": row["per_request_cap_atomic"] / 1e6,
            "spentTodayUsd": spent[0] / 1e6, "answersToday": spent[1], "scopes": str(row["scopes"]).split(),
            "createdAt": row["created_at"], "expiresAt": row["expires_at"], "lastUsedAt": row["last_used_at"]}


class ApiKeys:
    """The web app's key management (/web/v1/api-keys…) and the public /v1 the keys open."""

    def __init__(self, api: Any, now=time.time):
        self.api = api  # web_api.WebApi: its auth, shop, push and rate limits
        self.now = now
        self._lock = threading.Lock()
        self._reserved: dict[str, int] = defaultdict(int)  # key_id -> most the answers in flight may cost
        self._calls: dict[str, list[float]] = defaultdict(list)
        self.proxy = None  # web_api_proxy.Proxy when a Surplus key is set: every format, paid from the balance

    @property
    def store(self):
        return self.api.auth.store

    def _day(self) -> int:
        return int(self.now()) // 86400

    # -- the web app (session + CSRF, behind WebApi.handle) --

    def manage(self, method: str, path: str, account: str, owner: str, body: Mapping[str, Any]) -> tuple[int, dict, dict]:
        now = int(self.now())
        if method == "GET" and path == "/api-keys":
            day = self._day()
            keys = [public_key(r, self.store.key_spent(r["key_id"], day), now) for r in self.store.api_keys(account)]
            return 200, {"keys": keys, "baseUrl": "/v1"}, {}
        self.api.keys_by_account.hit(account)
        if method == "POST" and path == "/api-keys/prepare":
            name = str(body.get("name") or "").strip()
            if not NAME.fullmatch(name):
                raise ValueError("Name the key in up to 40 letters, digits, spaces or simple punctuation.")
            daily = _usd_atomic(body.get("dailyUsd"), DEFAULT_DAILY_ATOMIC, low=10_000, high=MAX_DAILY_ATOMIC,
                                what="The daily limit")
            per = _usd_atomic(body.get("perRequestUsd"), min(DEFAULT_PER_REQUEST_ATOMIC, daily), low=1_000,
                              high=min(MAX_PER_REQUEST_ATOMIC, daily), what="The per-answer limit")
            days = body.get("days", DEFAULT_DAYS)
            if type(days) is not int or not 1 <= days <= MAX_DAYS:
                raise ValueError(f"A key lasts 1 to {MAX_DAYS} days.")
            nonce, until = secrets.token_hex(16), now + days * 86400
            message = key_message(owner, name, daily, per, until, nonce, now)
            self.store.add_nonce(nonce, owner, message, now + NONCE_SECONDS, now)
            self.store.add_key_request(nonce, account, name, daily, per, until)
            return 200, {"message": message, "chain": chain_of_account(account), "expiresAt": now + NONCE_SECONDS}, {}
        if method == "POST" and path == "/api-keys":
            message, signature = str(body.get("message") or ""), str(body.get("signature") or "")
            nonce = next((line[len("Nonce: "):] for line in message.splitlines() if line.startswith("Nonce: ")), "")
            issued = self.store.take_nonce(nonce, now) if nonce else None
            request = self.store.take_key_request(nonce) if issued is not None else None
            if issued is None or request is None or issued["message"] != message or request["account_id"] != account:
                raise WebAuthError("This key request is unknown, used or expired. Start again.")
            if issued["address"] != owner or not self.api.auth._signed_by(owner, message, signature):
                raise WebAuthError("The signature is not from this account's wallet.")
            key, row = self.store.add_api_key(account, request["name"], request["daily_cap_atomic"],
                                              request["per_request_cap_atomic"], now, request["expires_at"])
            analytics.emit("api_key_created", account, scopes=row["scopes"])
            if self.api.push is not None:
                try:
                    self.api.push.send(account, "SingIt", f'API key "{row["name"]}" was created. '
                                       "Revoke it in SingIt if this was not you.", url="/app/")
                except Exception:  # noqa: BLE001 - a notice that fails never undoes the key
                    pass
            return 200, {"key": key, **public_key(row, (0, 0), now)}, {}
        if method == "POST" and path == "/api-keys/revoke":
            if not self.store.revoke_api_key(account, str(body.get("id") or "")[:40], now):
                raise ValueError("No such live key.")
            return 200, {"ok": True, "id": body.get("id"), "state": "revoked"}, {}
        raise ValueError("No such key action.")

    # -- the balance API keys spend (session + CSRF, behind WebApi.handle) --

    def manage_balance(self, method: str, path: str, account: str, body: Mapping[str, Any]) -> tuple[int, dict, dict]:
        if self.proxy is None:
            raise ValueError("The SingIt balance is not enabled on this server.")
        from .web_balance import MAX_TOPUP, MIN_TOPUP, TOPUP_STEP, new_topup_id  # noqa: PLC0415
        balances, now = self.proxy.balances, int(self.now())
        if method == "GET" and path == "/balance":
            lines = [{"id": r["id"], "at": r["at"], "kind": r["kind"], "amountUsd": r["amount_atomic"] / 1e6,
                      "balanceUsd": r["balance_after"] / 1e6, "model": r["model"], "key": r["key_id"],
                      "inputTokens": r["input_tokens"], "outputTokens": r["output_tokens"], "txId": r["tx_id"],
                      "network": r["network"]} for r in balances.ledger(account, limit=50)]
            return 200, {**self._balance(account), "ledger": lines}, {}
        self.api.keys_by_account.hit(account)
        if method == "POST" and path == "/balance/settings":
            topup = _usd_atomic(body.get("topupUsd"), 0, low=MIN_TOPUP, high=MAX_TOPUP, what="The top-up")
            threshold = _usd_atomic(body.get("thresholdUsd"), 0, low=0, high=MAX_TOPUP, what="The threshold")
            if not topup or topup % TOPUP_STEP or threshold % TOPUP_STEP:
                raise ValueError("Give the top-up and the threshold in whole cents.")
            if threshold >= topup:
                raise ValueError("The threshold must be below the top-up.")
            balances.set_terms(account, topup, threshold, body.get("autoTopup") is True, now)
            return 200, self._balance(account), {}
        if method == "POST" and path == "/balance/topup":
            amount = _usd_atomic(body.get("amountUsd"), 0, low=MIN_TOPUP, high=MAX_TOPUP, what="The top-up")
            if amount % TOPUP_STEP:
                raise ValueError("Give the top-up in whole cents.")
            status, reply = self._shop("credit-topup", account, {"topupId": new_topup_id(), "amountAtomic": amount,
                                                                 "minAtomic": amount})
            if status != 200:
                raise ValueError(str(reply.get("text") or "The balance could not be topped up. Nothing was paid."))
            return 200, {**self._balance(account), "topup": {k: reply.get(k) for k in (
                "creditedAtomic", "paidAtomic", "feeAtomic", "txId", "network")}}, {}
        if method == "POST" and path == "/balance/refund":
            state = balances.state(account)
            if state["balanceAtomic"] <= 0:
                raise ValueError("There is nothing to refund.")
            if balances.request_refund(account, now):
                logger.warning("web api: refund requested by %s: %s USDC", account, state["balanceAtomic"] / 1e6)
            return 200, self._balance(account), {}
        raise ValueError("No such balance action.")

    # -- the public /v1, for code holding a key --

    def _key(self, authorization: str | None, api_key: str | None = None):
        scheme, _, token = str(authorization or "").partition(" ")
        token = token.strip() if scheme.lower() == "bearer" else ""
        token = token or str(api_key or "").strip()  # x-api-key: the Anthropic SDK's and Claude Code's header
        row = self.store.api_key(token, int(self.now())) if token else None
        if row is None:
            raise ApiError(401, "Invalid or revoked API key. Create one in the SingIt app.",
                           type_="authentication_error", code="invalid_api_key")
        return row

    def _rate(self, key_id: str) -> None:
        now = self.now()
        with self._lock:
            recent = [t for t in self._calls[key_id] if t > now - 60]
            if len(recent) >= REQUESTS_PER_MINUTE:
                raise ApiError(429, f"This key sends up to {REQUESTS_PER_MINUTE} requests a minute. Slow down.",
                               type_="rate_limit_error", code="rate_limit_exceeded")
            recent.append(now)
            self._calls[key_id] = recent

    def _shop(self, action: str, account: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.api.shop is None:
            raise ApiError(503, "Answers are not enabled on this server.", type_="api_error", code="unavailable")
        return self.api.shop(action, account, body)

    def public(self, method: str, path: str, *, authorization: str | None, origin: str | None,
               body: Mapping[str, Any], headers: Mapping[str, Any] | None = None,
               query: Mapping[str, str] | None = None, raw: tuple[bytes, str] | None = None) -> tuple[int, Any, dict[str, str]]:
        try:
            return self._public(method, path, authorization=authorization, origin=origin, body=body,
                                headers=headers or {}, query=query or {}, raw=raw)
        except ApiError as error:
            error.anthropic = path.startswith("/messages")
            raise

    def _public(self, method: str, path: str, *, authorization: str | None, origin: str | None,
                body: Mapping[str, Any], headers: Mapping[str, Any],
                query: Mapping[str, str], raw: tuple[bytes, str] | None = None) -> tuple[int, Any, dict[str, str]]:
        if origin:
            # A key belongs in server code. A page that holds one gives it to everyone who opens it.
            raise ApiError(403, "API keys are for server code, not browsers. Keep the key out of web pages.",
                           code="browser_request")
        row = self._key(authorization, headers.get("x-api-key"))
        if method == "POST" and path in SEARCH_PATHS:
            return self._search(row, path, body)
        job = VIDEO_JOB.fullmatch(path) if method == "GET" else None
        if job:
            return self._video_job(job.group(1), bool(job.group(2)), headers, query)
        if self.proxy is not None:
            from .web_api_proxy import ROUTES  # noqa: PLC0415
            if method == "POST" and path in ROUTES:
                self._rate(row["key_id"])
                relay = self.proxy.relay(row, path, body, headers, ApiError, lambda amount: self._reserve(row, amount),
                                         raw=raw)
                return relay.status, relay, {}
            if method == "GET" and path == "/balance":
                return 200, self._balance(row["account_id"]), {}
            if method == "GET" and path == "/usage":
                return 200, self._usage(row["account_id"], query.get("days")), {}
        if method == "GET" and path == "/models":
            status, reply = self._shop("api-models", row["account_id"], {})
            if status != 200:
                raise ApiError(502, "The model list is unavailable right now.", type_="api_error")
            return 200, {"object": "list", "data": reply.get("data") or []}, {}
        if method == "POST" and path == "/chat/completions":
            return self._chat(row, body)
        if self.proxy is not None:
            raise ApiError(404, "No such endpoint. This API answers POST /v1/chat/completions, /v1/responses, "
                           "/v1/messages, /v1/images/generations, /v1/audio/speech and /transcriptions, and "
                           "/v1/exa/search, /contents and /answer, and GET /v1/models, /v1/balance and /v1/usage.",
                           code="not_found")
        raise ApiError(404, "No such endpoint. This API answers POST /v1/chat/completions and GET /v1/models.",
                       code="not_found")

    def _search(self, row, path: str, body: Mapping[str, Any], *, rate: bool = True) -> tuple[int, Any, dict[str, str]]:
        """Web search or page contents from Exa, or a video job, bought at SingIt Ask's x402 price from the owner's
        wallet within its limits, per request: no balance is involved.

        `rate=False` is for the MCP server (web_mcp.py): it already rated the request in its own
        per-key bucket, so this must not also charge it against the /v1 bucket."""
        video = path == "/video/generations"
        hold = VIDEO_HOLD_ATOMIC if video else SEARCH_HOLD_ATOMIC
        if not video and "search" not in str(row["scopes"]).split():
            raise ApiError(403, "This key was made for model answers only. Create a new key in the SingIt app to use "
                           "search.", type_="permission_error", code="scope_missing")
        if rate:
            self._rate(row["key_id"])
        per = int(row["per_request_cap_atomic"])
        done = self._reserve(row, min(per, hold))
        try:
            status, reply = self._shop("api-search", row["account_id"], {
                "path": "/v1" + path, "body": dict(body), "limitAtomic": per})
        except BaseException:
            done(0)
            raise
        if status != 200:
            done(0)
            raise _api_error(status, reply)
        cost = reply.get("costAtomic")
        cost = cost if type(cost) is int and cost >= 0 else min(per, hold)
        done(cost)
        data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
        data["billing"] = {**(data.get("billing") or {}), "chargedAtomic": cost, "txId": reply.get("txId") or None,
                           "network": reply.get("network"), "paidFrom": "your wallet, within your SingIt limits"}
        if video and data.get("id"):  # poll here, with the same key and the job's token
            data["poll"] = f"/v1/video/generations/{data['id']}"
            data["results"] = [{**r, "url": str(r.get("url", "")).replace(ASK_URL, "")} for r in data.get("results") or []
                               if isinstance(r, dict)]
        return 200, data, {"X-SingIt-Charged-Atomic": str(cost)}

    def tool(self, row, tool_id: str, args: Mapping[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        """One of server.PAID_TOOLS, bought at its x402 price from the owner's wallet within its limits, per
        request: the MCP server (web_mcp.py) calls this; it does not rate the key itself (the caller does, once
        per tools/call, since this is not reached through _search)."""
        if "tools" not in str(row["scopes"]).split():
            raise ApiError(403, "This key was made before paid data tools existed. Create a new key in the SingIt "
                           "app to use them.", type_="permission_error", code="scope_missing")
        if chain_of_account(row["account_id"]) != "base":
            raise ApiError(400, "Paid data tools are on Base only for now.", code="base_only")
        per = int(row["per_request_cap_atomic"])
        done = self._reserve(row, per)
        try:
            status, reply = self._shop("api-tool", row["account_id"], {
                "toolId": tool_id, "args": dict(args), "limitAtomic": per})
        except BaseException:
            done(0)
            raise
        if status != 200:
            done(0)
            raise _api_error(status, reply)
        cost = reply.get("costAtomic")
        cost = cost if type(cost) is int and cost >= 0 else per
        done(cost)
        data = {"data": reply.get("data"), "billing": {"chargedAtomic": cost, "txId": reply.get("txId") or None,
                "network": "base", "paidFrom": "your wallet, within your SingIt limits"}}
        return 200, data, {"X-SingIt-Charged-Atomic": str(cost)}

    def _video_job(self, job_id: str, content: bool, headers: Mapping[str, Any],
                   query: Mapping[str, str]) -> tuple[int, Any, dict[str, str]]:
        """A video job's status, or its file: SingIt Ask's free routes, opened with the job's own token."""
        from urllib.parse import quote  # noqa: PLC0415
        from .web_api_proxy import Relay  # noqa: PLC0415
        token = str(headers.get("x-job-token") or query.get("token") or "")[:400]
        if not token:
            raise ApiError(400, "Send the job's token in the X-Job-Token header.", code="token_missing")
        index = str(query.get("index") or "0")
        url = f"{ASK_URL}/v1/video/generations/{job_id}" + (f"/content?index={quote(index)}" if content else "")
        request = urllib.request.Request(url, headers={"X-Job-Token": token, "User-Agent": "SingIt-App/1.0 (+https://singitai.app)"})
        try:
            response = urllib.request.urlopen(request, timeout=120 if content else 20)
        except urllib.error.HTTPError as error:
            response = error
        except OSError:
            raise ApiError(503, "Try again shortly.", type_="api_error", code="unavailable") from None
        with response:
            data = response.read(512 * 1024 * 1024 if content else 1_000_000)
            kind = response.headers.get("Content-Type", "application/json")
            status = response.status if hasattr(response, "status") else response.code
        if not content and status in (200, 202):
            job = json.loads(data)
            job["results"] = [{**r, "url": str(r.get("url", "")).replace(ASK_URL, "")} for r in job.get("results") or []]
            data, kind = json.dumps(job).encode(), "application/json"
        return status, Relay(status, {"Content-Type": kind}, [data], len(data)), {}

    def _reserve(self, row, amount: int):
        """Counts `amount` against the key's daily cap while its request runs; the returned function records what
        was charged in the end and lets the reservation go."""
        key_id, daily, day = row["key_id"], int(row["daily_cap_atomic"]), self._day()
        with self._lock:
            spent, _ = self.store.key_spent(key_id, day)
            if spent + self._reserved[key_id] + amount > daily:
                raise ApiError(429, f"This key's daily limit of {daily / 1e6:g} USDC is used up, counting requests "
                               "still running. It starts again at 00:00 UTC, or raise it with a new key.",
                               type_="insufficient_quota", code="insufficient_quota")
            self._reserved[key_id] += amount
        settled = []

        def done(charged: int) -> None:
            if settled:
                return
            settled.append(charged)
            with self._lock:
                self._reserved[key_id] -= amount
            if charged:
                self.store.record_key_spend(key_id, day, charged, int(self.now()))

        return done

    def _balance(self, account: str) -> dict[str, Any]:
        state = self.proxy.balances.state(account)
        return {"object": "balance", "currency": "USDC", "balanceUsd": state["balanceAtomic"] / 1e6,
                "availableUsd": state["availableAtomic"] / 1e6, "heldUsd": state["heldAtomic"] / 1e6,
                "topupUsd": state["topupAtomic"] / 1e6, "thresholdUsd": state["thresholdAtomic"] / 1e6,
                "autoTopup": state["autoTopup"],
                "refundRequested": state["refundRequestedAt"] is not None,
                "pendingTopups": [{"id": p["id"], "state": p["state"], "amountUsd": p["amountAtomic"] / 1e6}
                                  for p in state["pending"]]}

    def _usage(self, account: str, days: Any) -> dict[str, Any]:
        try:
            days = max(1, min(int(days or 1), 90))
        except (TypeError, ValueError):
            raise ApiError(400, "days must be a whole number from 1 to 90.", code="bad_days") from None
        since = (self._day() - days + 1) * 86400
        rows = self.proxy.balances.usage(account, since)
        names = {r["key_id"]: r["name"] for r in self.store.api_keys(account)}
        data = [{"date": datetime.fromtimestamp(r["day"] * 86400, tz=timezone.utc).strftime("%Y-%m-%d"),
                 "key": r["key_id"], "keyName": names.get(r["key_id"]), "model": r["model"], "requests": r["calls"],
                 "chargedUsd": (r["charged_atomic"] or 0) / 1e6, "inputTokens": r["input_tokens"] or 0,
                 "outputTokens": r["output_tokens"] or 0} for r in rows]
        return {"object": "list", "days": days, "totalUsd": sum(d["chargedUsd"] for d in data), "data": data}

    def _chat(self, row, body: Mapping[str, Any]) -> tuple[int, Any, dict[str, str]]:
        if body.get("tools") or body.get("functions"):
            raise ApiError(400, "Tools are not offered yet.", code="tools_unsupported")
        key_id, account = row["key_id"], row["account_id"]
        per, daily = int(row["per_request_cap_atomic"]), int(row["daily_cap_atomic"])
        self._rate(key_id)
        day = self._day()
        with self._lock:
            spent, _ = self.store.key_spent(key_id, day)
            if spent + self._reserved[key_id] + per > daily:
                raise ApiError(429, f"This key's daily limit of {daily / 1e6:g} USDC is used up, counting answers "
                               "still running. It starts again at 00:00 UTC, or raise it with a new key.",
                               type_="insufficient_quota", code="insufficient_quota")
            self._reserved[key_id] += per
        try:
            tokens = body.get("max_tokens", body.get("max_completion_tokens"))
            status, reply = self._shop("api-chat", account, {
                "model": body.get("model"), "messages": body.get("messages"),
                "maxTokens": tokens if type(tokens) is int else None, "limitAtomic": per})
        finally:
            with self._lock:
                self._reserved[key_id] -= per
        if status != 200:
            raise _api_error(status, reply)
        cost = reply.get("costAtomic")
        cost = cost if type(cost) is int and cost >= 0 else per  # an answer whose cost is unreadable counts in full
        self.store.record_key_spend(key_id, day, cost, int(self.now()))
        prompt, completion = int(reply.get("promptTokens") or 0), int(reply.get("completionTokens") or 0)
        model = body.get("model") if isinstance(body.get("model"), str) and body.get("model") else "singit-ask"
        tx = str(reply.get("txId") or "")
        out = {
            "id": "chatcmpl-singit-" + (tx[2:18] if tx.startswith("0x") else secrets.token_hex(8)),
            "object": "chat.completion", "created": int(self.now()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply.get("text") or ""},
                         "finish_reason": reply.get("finishReason") or "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
            "billing": {**(reply.get("billing") or {}), "chargedAtomic": cost, "txId": tx or None,
                        "paidFrom": "your SingIt limits"},
        }
        headers = {"X-SingIt-Charged-Atomic": str(cost)}
        if body.get("stream"):
            options = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
            return 200, Stream(sse(out, include_usage=bool(options.get("include_usage")))), headers
        return 200, out, headers


class Stream(bytes):
    """A finished answer, sent as OpenAI's server-sent events (text/event-stream)."""


def sse(answer: Mapping[str, Any], *, include_usage: bool = False) -> bytes:
    """One answer as the chunks OpenAI streams: the role and text, then why it stopped, then (asked for) the usage,
    then [DONE]. The answer is whole before the first chunk: SingIt Ask pays for an answer once it came back, so
    clients that only speak streaming get it all at once rather than word by word."""
    base = {"id": answer["id"], "object": "chat.completion.chunk", "created": answer["created"], "model": answer["model"]}
    choice = answer["choices"][0]
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": choice["message"]["content"]},
                              "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
         "billing": answer["billing"]},
    ]
    if include_usage:
        chunks.append({**base, "choices": [], "usage": answer["usage"]})
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"


def _api_error(status: int, reply: Mapping[str, Any]) -> ApiError:
    """The gateway's refusal, in OpenAI's shape and in the gateway's own words."""
    text = str(reply.get("text") or reply.get("message") or "The answer could not be bought. Nothing was paid.")[:500]
    error = str(reply.get("error") or "")
    if status == 404 or error == "model_not_found":
        return ApiError(404, text, code="model_not_found")
    if status == 403:  # the account's limits are not set, approved or active
        return ApiError(403, text, type_="insufficient_quota", code="payment_not_ready")
    if status == 429:
        return ApiError(429, text, type_="rate_limit_error", code="rate_limit_exceeded")
    if status == 503:
        return ApiError(503, text, type_="api_error", code="payments_paused")
    if status == 400:
        return ApiError(400, text, code=error or "refused")
    return ApiError(502, text, type_="api_error", code=error or "upstream_error")
