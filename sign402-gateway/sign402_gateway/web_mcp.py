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
Every tools/list entry carries a title and MCP annotations (readOnlyHint, destructiveHint, idempotentHint,
openWorldHint): paid tools debit the key's SingIt limits but never touch external state, so they are
readOnlyHint/not destructive like the free ones, but not idempotent (each call bills again) and, except
the three free lookups that only read SingIt's own data, openWorldHint (they reach third-party data).

Every call needs a valid key (Authorization: Bearer sk_singit_…, or x-api-key) before anything else,
including initialize; this is unchanged by the origin allowlist below. The key's own 60-requests-a-minute
rate applies once per tools/call, whether the tool is free or paid, except the two Exa tools: they already
rate inside ApiKeys._search.

Browser Origin: refused with 403 unless it is trusted, same as /v1 refuses one. Trusted means either the
same origin as the MCP host itself (McpServer's public_origin, normally SIGN402_WEB_URI) or one of
DEFAULT_ALLOWED_ORIGINS / SINGIT_MCP_ALLOWED_ORIGINS below — known MCP inspectors and scanners that need to
read tools/list without a key before a human adds one. A trusted origin still needs a valid key for
anything beyond the CORS preflight; an untrusted one is refused before authentication is even checked.

Catalog metadata that other files or endpoints read from here: server_card() for
GET /.well-known/mcp/server-card.json (SEP-1649) and glama_json() for GET /.well-known/glama.json
(both served by web_api.py, public, no key).
"""

from __future__ import annotations

import functools
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping
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
# Tools billed through ApiKeys._search: it rates the key itself, so McpServer must not rate it again.
SEARCH_TOOLS = frozenset({"web_search", "read_page"})
# The only tools that read SingIt's own data for free; every other tool bills the key per call and, except
# for these three, reaches third-party data.
FREE_TOOL_NAMES = frozenset({"get_price", "list_price_symbols", "list_models"})
TOOL_TITLES: dict[str, str] = {
    "get_price": "Get price",
    "list_price_symbols": "List price symbols",
    "list_models": "List models",
    "web_search": "Web search",
    "read_page": "Read page",
    "get_weather": "Get weather",
    "get_sign402_report": "Get Sign402 report",
    "get_singit_risk_check": "SingIt risk check",
    "get_x_profile": "Get X profile",
    "get_crypto_news": "Get crypto news",
    "get_hyperliquid_market": "Get Hyperliquid market",
    "get_funding_rates": "Get funding rates",
    "resolve_ens": "Resolve ENS name",
    "get_token_price": "Get token price",
}
# Known MCP inspectors and scanners that need to read tools/list from a browser before a key is on hand;
# an exact, lowercased, no-trailing-slash match against Origin, same as the public_origin same-origin check.
DEFAULT_ALLOWED_ORIGINS = frozenset({
    "https://glama.ai",             # Glama's connector health-check and claim verification (SI-40 catalog listing)
    "https://www.glama.ai",
    "https://x402scan.com",         # x402scan's resource crawler (indexes the x402 API, may probe /mcp too)
    "https://www.x402scan.com",
})
ALLOWED_ORIGINS_ENV = "SINGIT_MCP_ALLOWED_ORIGINS"


def _normalize_origin(origin: str) -> str:
    return origin.strip().lower().rstrip("/")


def allowed_origins_from_env(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """SINGIT_MCP_ALLOWED_ORIGINS, comma-separated exact origins (scheme+host[+port], no wildcards); unset
    keeps DEFAULT_ALLOWED_ORIGINS, set replaces it entirely rather than extending it."""
    values = os.environ if env is None else env
    raw = str(values.get(ALLOWED_ORIGINS_ENV, "")).strip()
    if not raw:
        return DEFAULT_ALLOWED_ORIGINS
    return frozenset(_normalize_origin(origin) for origin in raw.split(",") if origin.strip())


def _annotated(entry: dict[str, Any]) -> dict[str, Any]:
    """Title + MCP tool annotations (SEP: title, readOnlyHint, destructiveHint, idempotentHint, openWorldHint).
    Every tool here is read-only and non-destructive: paid ones debit the key's limits but never change
    external state. Free lookups are idempotent and read SingIt's own data; paid ones bill again on every
    call (not idempotent) and, except the three free ones, reach third-party data (open world)."""
    name = entry["name"]
    free = name in FREE_TOOL_NAMES
    title = TOOL_TITLES.get(name) or name.replace("_", " ").capitalize()
    annotations = {"title": title, "readOnlyHint": True, "destructiveHint": False,
                   "idempotentHint": free, "openWorldHint": not free}
    return {**entry, "title": title, "annotations": annotations}


@functools.lru_cache(maxsize=1)
def _paid_tools() -> tuple[dict[str, str], tuple[dict[str, Any], ...]]:
    """server.PAID_TOOLS by mcpStyleName, and their MCP listing. Imported on first use, not when the web API
    starts: the gateway module is large and this process ran without it until now."""
    from . import server as gw  # noqa: PLC0415
    ids = {tool["mcpStyleName"]: tool_id for tool_id, tool in gw.PAID_TOOLS.items()}
    listed = tuple(_annotated({"name": tool["mcpStyleName"], "description": tool["description"] + " Paid per call "
                    "from your SingIt limits (Base USDC).", "inputSchema": tool["inputSchema"]})
                   for tool in gw.PAID_TOOLS.values())
    return ids, listed


class _BadArgs(Exception):
    """An unknown tool or bad arguments: a JSON-RPC protocol error (-32602), not a tool failure."""


class _MethodNotFound(Exception):
    """An unsupported JSON-RPC method (-32601)."""


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

OWN_TOOLS: list[dict[str, Any]] = [_annotated(entry) for entry in (
    {"name": "get_price", "description": "A crypto, metal or fx price. Free.", "inputSchema": GET_PRICE_SCHEMA},
    {"name": "list_price_symbols", "description": "Every symbol get_price knows. Free.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_models", "description": "Models this key can ask for chat, images or audio, with their "
     "SingIt prices. Free.", "inputSchema": LIST_MODELS_SCHEMA},
    {"name": "web_search", "description": "Search the web (Exa). Paid per call from your SingIt limits "
     "(Base USDC); needs the key's \"search\" scope.", "inputSchema": WEB_SEARCH_SCHEMA},
    {"name": "read_page", "description": "Read a web page's text (Exa). Paid per call from your SingIt limits "
     "(Base USDC); needs the key's \"search\" scope.", "inputSchema": READ_PAGE_SCHEMA},
)]


def tools() -> list[dict[str, Any]]:
    return OWN_TOOLS + list(_paid_tools()[1])


# Catalog metadata served by web_api.py's GET dispatch, public and keyless.
ICON_URL = "https://singitai.app/assets/icons/icon-512.png"
DOCS_URL = ("https://github.com/bubon-ik/SingItAI/blob/main/sign402-gateway/README.md"
            "#mcp-server-remote-for-claude-code-cursor-codex")
SERVER_CARD_DESCRIPTION = ("Free price, model and symbol lookups, plus paid web search, page reading and "
                           "market data tools, billed in Base USDC inside the API key's own SingIt limits.")
AUTH_INSTRUCTIONS = ("Create a SingIt API key in the app at https://app.singitai.app/app/, then send it as "
                     "\"Authorization: Bearer sk_singit_...\" or \"x-api-key: sk_singit_...\".")


def server_card(public_origin: str, mcp_path: str = "/mcp") -> dict[str, Any]:
    """GET /.well-known/mcp/server-card.json (SEP-1649 draft shape). public_origin is the MCP host's own
    origin (e.g. https://app.singitai.app), never a reflected request Host header."""
    return {
        "$schema": "https://static.modelcontextprotocol.io/schemas/mcp-server-card/v1.json",
        "version": "1.0",
        "protocolVersion": OUR_VERSION,
        "serverInfo": dict(SERVER_INFO),
        "description": SERVER_CARD_DESCRIPTION,
        "iconUrl": ICON_URL,
        "documentationUrl": DOCS_URL,
        "transport": {"type": "streamable-http", "url": f"{public_origin}{mcp_path}"},
        "capabilities": {"tools": {"listChanged": False}},
        "authentication": {"required": True, "schemes": ["bearer"], "headers": ["Authorization", "x-api-key"],
                           "instructions": AUTH_INSTRUCTIONS},
        "instructions": INSTRUCTIONS,
        "tools": tools(),
    }


# The support address already published in the website footer (mailto:Exkalibur1919@proton.me), reused
# here so Glama's claim can verify we control this domain.
GLAMA_MAINTAINER_EMAIL = "Exkalibur1919@proton.me"


def glama_json() -> dict[str, Any]:
    """GET /.well-known/glama.json: Glama's claim-by-well-known-file verification."""
    return {"$schema": "https://glama.ai/mcp/schemas/connector.json",
            "maintainers": [{"email": GLAMA_MAINTAINER_EMAIL}]}


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

    public_origin is this MCP server's own origin (normally SIGN402_WEB_URI); together with
    allowed_origins (default: allowed_origins_from_env()) it decides which browser Origins are trusted
    enough to even reach the auth check, below. A key is still required for anything beyond that: trust
    here only buys an untrusted scanner a 401 instead of a 403, plus the CORS headers to read it.
    """

    def __init__(self, keys: ApiKeys, *, fetch_json: Callable[[str], dict[str, Any]] | None = None,
                 public_origin: str = "", allowed_origins: frozenset[str] | None = None):
        self.keys = keys
        self.fetch_json = fetch_json or _default_fetch_json
        self.public_origin = _normalize_origin(public_origin) if public_origin else ""
        self.allowed_origins = allowed_origins_from_env() if allowed_origins is None else allowed_origins

    def is_trusted_origin(self, origin: str | None) -> bool:
        if not origin:
            return False
        normalized = _normalize_origin(origin)
        if normalized == "null":  # the sandboxed-origin literal, never a real site
            return False
        return normalized == self.public_origin or normalized in self.allowed_origins

    def handle(self, *, authorization: str | None, api_key: str | None, origin: str | None,
               protocol_version: str | None, body: bytes | dict[str, Any]) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
        if origin and not self.is_trusted_origin(origin):
            # A key is for server code; the page never holds one. Also satisfies the spec's DNS-rebinding check.
            return 403, rpc_error(None, -32600, "API keys are for server code, not browsers. Keep the key out of "
                                  "web pages."), {}
        status, result, headers = self._authenticated(authorization, api_key, protocol_version, body)
        if origin:  # trusted, checked above: let the caller read the answer across origins
            # Expose-Headers on the real answer too, not only the preflight: a 401's WWW-Authenticate is
            # otherwise unreadable to the page (Codex review).
            headers = {**headers, "Access-Control-Allow-Origin": origin, "Vary": "Origin",
                       "Access-Control-Expose-Headers": "WWW-Authenticate, Mcp-Session-Id"}
        return status, result, headers

    def _authenticated(self, authorization: str | None, api_key: str | None, protocol_version: str | None,
                        body: bytes | dict[str, Any]) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
        try:
            row = self.keys._key(authorization, api_key)
        except ApiError:
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
        params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}
        try:
            result = self._dispatch(row, method, params, protocol_version)
        except _MethodNotFound as exc:
            return 200, rpc_error(msg_id, -32601, str(exc)), {}
        except _BadArgs as exc:
            return 200, rpc_error(msg_id, -32602, str(exc)), {}
        return 200, _result(msg_id, result), {}

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
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _BadArgs("name is required.")
        args = params.get("arguments")
        args = args if isinstance(args, dict) else {}
        try:
            if name not in SEARCH_TOOLS:  # /exa/… tools already rate inside ApiKeys._search
                self.keys._rate(row["key_id"])
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
        _status, data, _headers = self.keys._search(row, "/exa/search", body)
        return data

    def _read_page(self, row, args: dict[str, Any]) -> Any:
        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise _BadArgs("url must be an http or https page.")
        _status, data, _headers = self.keys._search(row, "/exa/contents", {
            "urls": [url], "text": {"maxCharacters": 8000}})
        return data
