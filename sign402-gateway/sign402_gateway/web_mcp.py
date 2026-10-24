"""SingIt's own remote MCP server: POST /mcp on the public web API, authenticated with a SingIt API key.

A minimal, stateless server over MCP's Streamable HTTP transport (spec 2025-06-18, back to 2024-11-05):
JSON responses only, no SSE stream and no sessions, so one request is one answer and nothing is kept
between calls. Built on plain JSON-RPC 2.0 dicts, since this process is http.server, not ASGI.

Tools:
  * Free: get_price, list_price_symbols, list_models.
  * Paid from the key's own limits, needing the "search" scope: web_search, read_page (Exa, through
    ApiKeys._search, exactly as /v1/exa/… is billed).
  * Paid from the key's own limits, needing the "tools" scope: every entry of server.PAID_TOOLS, under
    its mcpStyleName (get_weather, resolve_ens, …), through ApiKeys.tool.

Every call needs a valid key (Authorization: Bearer sk_singit_…, or x-api-key) before anything else,
including initialize; a browser Origin is refused for the same reason /v1 refuses one. MCP has its own
per-key rate limit, separate from the /v1 one in ApiKeys._rate: heavy use of one must not starve the
other. The default is 60 requests a minute, overridden by SINGIT_MCP_REQUESTS_PER_MINUTE; it is charged
once per authenticated JSON-RPC request that carries an id (initialize, ping, tools/list, tools/call) -
notifications (202) are not rated. web_search and read_page still go through ApiKeys._search, but with
rate=False: this module already rated the request, and billing it a second time against /v1 would make
the two buckets interfere.

A client address, when the caller has one, is also limited on failed authentication (a wrong or revoked
key): too many in a short window gets 429s instead of 401s, to blunt key guessing against this endpoint.
A valid key is always served, whatever its address has been doing.
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any, Callable
from urllib.parse import quote

from .web_api_keys import ASK_URL, ApiError, ApiKeys

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
OUR_VERSION = SUPPORTED_VERSIONS[0]
SERVER_INFO = {"name": "singit", "title": "SingIt", "version": "0.1.0"}
INSTRUCTIONS = (
    "Look up prices, models, web pages and market data. Paid tools are charged per call to this API key's SingIt "
    "limits (Base USDC). get_price, list_price_symbols and list_models are free. web_search and "
    "read_page need the key's \"search\" scope; the other tools need its \"tools\" scope. Keys made "
    "before paid data tools existed need a new key for those."
)
READ_CAP = 2 * 1024 * 1024
USER_AGENT = "SingIt-MCP/0.1 (+https://singitai.app)"
SYMBOL_RE = re.compile(r"[A-Za-z0-9]{1,12}")
PRICE_KINDS = ("crypto", "metal", "fx")
MODEL_KINDS = ("chat", "images", "audio")
SEARCH_TYPES = ("auto", "fast", "neural", "keyword")
MCP_REQUESTS_PER_MINUTE_ENV = "SINGIT_MCP_REQUESTS_PER_MINUTE"
DEFAULT_MCP_REQUESTS_PER_MINUTE = 60
AUTH_FAILURES_PER_WINDOW = 30       # per client address, to blunt key guessing against this endpoint
AUTH_FAILURE_WINDOW_SECONDS = 600
AUTH_FAILURE_TABLE_MAX = 10_000     # addresses kept before stale ones are pruned


def _env_requests_per_minute() -> int:
    raw = os.environ.get(MCP_REQUESTS_PER_MINUTE_ENV)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MCP_REQUESTS_PER_MINUTE
    return value if value > 0 else DEFAULT_MCP_REQUESTS_PER_MINUTE


@functools.lru_cache(maxsize=1)
def _paid_tools() -> tuple[dict[str, str], tuple[dict[str, Any], ...]]:
    """server.PAID_TOOLS by mcpStyleName, and their MCP listing. Imported on first use, not when the web API
    starts: the gateway module is large and this process ran without it until now."""
    from . import server as gw  # noqa: PLC0415
    ids = {tool["mcpStyleName"]: tool_id for tool_id, tool in gw.PAID_TOOLS.items()}
    listed = tuple({"name": tool["mcpStyleName"], "description": tool["description"] + " Paid per call from your "
                    "SingIt limits (Base USDC).", "inputSchema": tool["inputSchema"]} for tool in gw.PAID_TOOLS.values())
    return ids, listed


class _BadArgs(Exception):
    """An unknown tool or bad arguments: a JSON-RPC protocol error (-32602), not a tool failure."""


class _MethodNotFound(Exception):
    """An unsupported JSON-RPC method (-32601)."""


class _RateLimited(Exception):
    """The key's MCP bucket is empty for this minute (-32029), with how long until it is not."""

    def __init__(self, limit: int, retry_after: int):
        self.limit, self.retry_after = limit, retry_after


def _default_fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(READ_CAP)
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read(READ_CAP) or b"{}")
        except ValueError:
            body = {}
        text = str(body.get("text") or body.get("message") or body.get("error") or "The request failed.")[:500]
        raise ApiError(502, text, type_="api_error", code="upstream_error") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiError(503, "Try again shortly.", type_="api_error", code="unavailable") from None
    try:
        return json.loads(data)
    except ValueError:
        raise ApiError(502, "The upstream answer could not be read.", type_="api_error", code="upstream_error") from None


def _symbol(value: Any) -> str:
    text = str(value or "").strip()
    if not SYMBOL_RE.fullmatch(text):
        raise _BadArgs("Symbols are 1 to 12 letters or digits.")
    return quote(text.upper(), safe="")


GET_PRICE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(PRICE_KINDS), "description": "What kind of price to look up."},
        "symbol": {"type": "string", "description": "Ticker for crypto or metal, e.g. BTC, XAU."},
        "base": {"type": "string", "description": "Base currency for fx, e.g. USD."},
        "quote": {"type": "string", "description": "Quote currency for fx, e.g. EUR."},
    },
    "required": ["kind"],
}
LIST_MODELS_SCHEMA = {
    "type": "object",
    "properties": {"kind": {"type": "string", "enum": list(MODEL_KINDS), "description": "Defaults to chat."}},
}
WEB_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to search the web for."},
        "numResults": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Defaults to a few."},
        "type": {"type": "string", "enum": list(SEARCH_TYPES)},
    },
    "required": ["query"],
}
READ_PAGE_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "The http(s) page to read."}},
    "required": ["url"],
}

OWN_TOOLS: list[dict[str, Any]] = [
    {"name": "get_price", "description": "A crypto, metal or fx price. Free.", "inputSchema": GET_PRICE_SCHEMA},
    {"name": "list_price_symbols", "description": "Every symbol get_price knows. Free.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_models", "description": "Models this key can ask for chat, images or audio, with their "
     "SingIt prices. Free.", "inputSchema": LIST_MODELS_SCHEMA},
    {"name": "web_search", "description": "Search the web (Exa). Paid per call from your SingIt limits "
     "(Base USDC); needs the key's \"search\" scope.", "inputSchema": WEB_SEARCH_SCHEMA},
    {"name": "read_page", "description": "Read a web page's text (Exa). Paid per call from your SingIt limits "
     "(Base USDC); needs the key's \"search\" scope.", "inputSchema": READ_PAGE_SCHEMA},
]


def tools() -> list[dict[str, Any]]:
    return OWN_TOOLS + list(_paid_tools()[1])


def _result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def rpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_success(data: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(data)}]}
    if isinstance(data, dict):
        out["structuredContent"] = data
    return out


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class McpServer:
    """One request, one answer: no session, no SSE. Construct with the web app's ApiKeys.

    `requests_per_minute` fixes the MCP bucket's size, overriding SINGIT_MCP_REQUESTS_PER_MINUTE; tests
    pass it directly instead of setting the environment. The env is otherwise read once here, at
    construction, not at import time (the web API builds one of these for the life of the process)."""

    def __init__(self, keys: ApiKeys, *, fetch_json: Callable[[str], dict[str, Any]] | None = None,
                 requests_per_minute: int | None = None):
        self.keys = keys
        self.fetch_json = fetch_json or _default_fetch_json
        self.requests_per_minute = requests_per_minute if requests_per_minute is not None else _env_requests_per_minute()
        self._lock = threading.Lock()
        self._calls: dict[str, list[float]] = defaultdict(list)
        self._auth_failures: dict[str, list[float]] = defaultdict(list)

    def handle(self, *, authorization: str | None, api_key: str | None, origin: str | None,
               protocol_version: str | None, body: bytes | dict[str, Any],
               client_ip: str | None = None) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
        if origin:
            # A key is for server code; the page never holds one. Also satisfies the spec's DNS-rebinding check.
            return 403, rpc_error(None, -32600, "API keys are for server code, not browsers. Keep the key out of "
                                  "web pages."), {}
        try:
            row = self.keys._key(authorization, api_key)
        except ApiError:
            # Checked only after the key failed, so a valid key behind a shared NAT or proxy is never locked
            # out by someone else's bad requests from the same address (Codex review).
            if client_ip is not None and self._failed_auth(client_ip):
                return (429, rpc_error(None, -32029, "Too many failed sign-ins from this address. Try again in a "
                                       "few minutes."), {"Retry-After": str(AUTH_FAILURE_WINDOW_SECONDS)})
            return (401, rpc_error(None, -32001, "Invalid or revoked API key. Create one in the SingIt app."),
                   {"WWW-Authenticate": 'Bearer realm="singit", error="invalid_token"'})
        if isinstance(body, (bytes, bytearray)):
            try:
                parsed = json.loads(body) if body else {}
            except (ValueError, UnicodeError):
                return 400, rpc_error(None, -32700, "Parse error."), {}
        else:
            parsed = body
        if isinstance(parsed, list):
            return 400, rpc_error(None, -32600, "Batch requests are not supported."), {}
        if not isinstance(parsed, dict) or parsed.get("jsonrpc") != "2.0" or not isinstance(parsed.get("method"), str):
            return 400, rpc_error(None, -32600, "Invalid Request."), {}
        method, msg_id, has_id = parsed["method"], parsed.get("id"), "id" in parsed
        if not has_id or method.startswith("notifications/"):
            return 202, None, {}
        try:
            self._rate(row["key_id"])
        except _RateLimited as exc:
            return (429, rpc_error(msg_id, -32029, f"This key sends up to {exc.limit} requests a minute to MCP. "
                                   "Slow down."), {"Retry-After": str(exc.retry_after)})
        params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}
        try:
            result = self._dispatch(row, method, params, protocol_version)
        except _MethodNotFound as exc:
            return 200, rpc_error(msg_id, -32601, str(exc)), {}
        except _BadArgs as exc:
            return 200, rpc_error(msg_id, -32602, str(exc)), {}
        return 200, _result(msg_id, result), {}

    def _rate(self, key_id: str) -> None:
        """MCP's own per-key bucket: independent of ApiKeys._rate (the /v1 one), so heavy use of one
        endpoint never starves the other."""
        now = self.keys.now()
        with self._lock:
            recent = [t for t in self._calls[key_id] if t > now - 60]
            if len(recent) >= self.requests_per_minute:
                raise _RateLimited(self.requests_per_minute, max(1, round(60 - (now - recent[0]))))
            recent.append(now)
            self._calls[key_id] = recent

    def _failed_auth(self, client_ip: str) -> bool:
        """Records one failed authentication from `client_ip`; True once it is over the limit for the window.
        Stale addresses are dropped whenever the table grows large, so it cannot grow without bound."""
        now = self.keys.now()
        cutoff = now - AUTH_FAILURE_WINDOW_SECONDS
        with self._lock:
            if len(self._auth_failures) > AUTH_FAILURE_TABLE_MAX:
                for ip in [ip for ip, times in self._auth_failures.items() if not times or times[-1] <= cutoff]:
                    del self._auth_failures[ip]
            recent = [t for t in self._auth_failures[client_ip] if t > cutoff]
            over = len(recent) >= AUTH_FAILURES_PER_WINDOW
            if not over:
                recent.append(now)
            self._auth_failures[client_ip] = recent
            return over

    def _dispatch(self, row, method: str, params: dict[str, Any], protocol_version: str | None) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools()}
        if method == "tools/call":
            return self._tools_call(row, params)
        raise _MethodNotFound(f"Unknown method '{method}'.")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else OUR_VERSION
        return {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": dict(SERVER_INFO), "instructions": INSTRUCTIONS}

    def _tools_call(self, row, params: dict[str, Any]) -> dict[str, Any]:
        # handle() already rated this call once in the MCP bucket; web_search and read_page must not
        # also be rated in ApiKeys._search (the /v1 bucket) - see their rate=False below.
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _BadArgs("name is required.")
        args = params.get("arguments")
        args = args if isinstance(args, dict) else {}
        try:
            data = self._call_tool(row, name, args)
        except ApiError as exc:
            return _tool_error(exc.message)
        return _tool_success(data)

    def _call_tool(self, row, name: str, args: dict[str, Any]) -> Any:
        if name == "get_price":
            return self._get_price(args)
        if name == "list_price_symbols":
            return self.fetch_json(f"{ASK_URL}/v1/prices")
        if name == "list_models":
            return self._list_models(row, args)
        if name == "web_search":
            return self._web_search(row, args)
        if name == "read_page":
            return self._read_page(row, args)
        tool_id = _paid_tools()[0].get(name)
        if tool_id is None:
            raise _BadArgs(f"Unknown tool '{name}'.")
        _status, data, _headers = self.keys.tool(row, tool_id, args)
        return data

    def _get_price(self, args: dict[str, Any]) -> Any:
        kind = args.get("kind")
        if kind not in PRICE_KINDS:
            raise _BadArgs("kind must be crypto, metal or fx.")
        if kind == "fx":
            url = f"{ASK_URL}/v1/prices/fx/{_symbol(args.get('base'))}/{_symbol(args.get('quote'))}"
        else:
            url = f"{ASK_URL}/v1/prices/{kind}/{_symbol(args.get('symbol'))}"
        return self.fetch_json(url)

    def _list_models(self, row, args: dict[str, Any]) -> Any:
        kind = args.get("kind") or "chat"
        if kind not in MODEL_KINDS:
            raise _BadArgs("kind must be chat, images or audio.")
        if kind != "chat":
            return self.fetch_json(f"{ASK_URL}/v1/{kind}/models")
        status, reply = self.keys._shop("api-models", row["account_id"], {})
        if status != 200:
            raise ApiError(502, "The model list is unavailable right now.", type_="api_error")
        return {"object": "list", "data": reply.get("data") or []}

    def _web_search(self, row, args: dict[str, Any]) -> Any:
        query = str(args.get("query") or "").strip()
        if not query:
            raise _BadArgs("query is required.")
        body: dict[str, Any] = {"query": query}
        num = args.get("numResults")
        if num is not None:
            if type(num) is not int or not 1 <= num <= 10:
                raise _BadArgs("numResults must be a whole number from 1 to 10.")
            body["numResults"] = num
        type_ = args.get("type")
        if type_ is not None:
            if type_ not in SEARCH_TYPES:
                raise _BadArgs("type must be auto, fast, neural or keyword.")
            body["type"] = type_
        _status, data, _headers = self.keys._search(row, "/exa/search", body, rate=False)
        return data

    def _read_page(self, row, args: dict[str, Any]) -> Any:
        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise _BadArgs("url must be an http or https page.")
        _status, data, _headers = self.keys._search(row, "/exa/contents", {
            "urls": [url], "text": {"maxCharacters": 8000}}, rate=False)
        return data
