"""SingIt's remote MCP server (POST /mcp, web_mcp.py): a key's own /v1, reshaped as MCP tools.

Offline: a fake shop stands for the gateway for the web-api-level tests; the gateway-side "api-tool"
action runs for real (with its payer and x402 fetch mocked), as test_web_shop.py does for tool-buy.
"""
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from eth_account import Account
from eth_account.messages import encode_defunct

from sign402_gateway import web_api as wa
from sign402_gateway.agent_allowance import AllowanceStore
from sign402_gateway.server import PAID_TOOLS
from sign402_gateway.web_accounts import WebAccountStore, WebAuth, account_id_for
from sign402_gateway.web_mcp import (
    ASK_URL, DEFAULT_ALLOWED_ORIGINS, FREE_TOOL_NAMES, OWN_TOOLS, allowed_origins_from_env, glama_json, server_card,
)
from tests.test_gateway_server import DummyServer
from tests.test_web_shop import ACCOUNT, OWNER, PAID, REQUIREMENTS, TOKEN, raw_request

NOW = 1_800_000_000
DOMAIN, URI = "app.singit.test", "https://app.singit.test"
PAID_TOOL_NAMES = {t["mcpStyleName"] for t in PAID_TOOLS.values()}


def sign(account, message):
    return Account.sign_message(encode_defunct(text=message), account.key).signature.to_0x_hex()


class Shop:
    def __init__(self, reply=(200, {"ok": True})):
        self.calls, self.reply = [], reply

    def __call__(self, action, account, body=None):
        self.calls.append((action, account, dict(body or {})))
        if action == "api-models":
            return 200, {"object": "list", "data": [{"id": "claude-opus-5.5", "object": "model"}]}
        return self.reply


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Account.create()
        self.clock = [NOW]
        self.store = WebAccountStore(Path(self.tmp.name) / "web.db")
        self.auth = WebAuth(self.store, domain=DOMAIN, uri=URI, allowed=None, now=lambda: self.clock[0])
        self.shop = Shop()
        self.api = wa.WebApi(self.auth, Mock(), shop=self.shop, now=lambda: self.clock[0], public_origin=URI)
        issued = self.auth.nonce(self.user.address)
        self.session = self.auth.verify(issued["message"], sign(self.user, issued["message"]))
        self.account = account_id_for(self.user.address)

    def call(self, method, path, body=None):
        return self.api.handle(method, path, token=self.session["token"], csrf=self.session["csrfToken"],
                               body=body or {}, client="127.0.0.1", expected_account=None)

    def make_key(self, **terms):
        _, prepared, _ = self.call("POST", "/api-keys/prepare", {"name": "my agent", **terms})
        _, made, _ = self.call("POST", "/api-keys", {"message": prepared["message"],
                                                   "signature": sign(self.user, prepared["message"])})
        return made

    def rpc(self, key, method, params=None, *, id_=1, origin=None):
        body = {"jsonrpc": "2.0", "method": method}
        if id_ is not None:
            body["id"] = id_
        if params is not None:
            body["params"] = params
        return self.api.mcp.handle(authorization=f"Bearer {key}" if key else None, api_key=None, origin=origin,
                                   protocol_version="2025-06-18", body=body)

    def call_tool(self, key, name, arguments=None, **kw):
        return self.rpc(key, "tools/call", {"name": name, "arguments": arguments or {}}, **kw)


class InitializeAndListTests(Fixture):
    def test_initialize_echoes_the_version_and_names_the_server(self):
        key = self.make_key()["key"]
        status, out, _ = self.rpc(key, "initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(status, 200)
        self.assertEqual(out["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(out["result"]["serverInfo"]["name"], "singit")
        self.assertEqual(out["result"]["capabilities"]["tools"], {"listChanged": False})
        # An unsupported version falls back to ours, not the client's.
        status, out, _ = self.rpc(key, "initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(out["result"]["protocolVersion"], "2025-06-18")

    def test_tools_list_names_exactly_the_tools_this_key_can_use(self):
        key = self.make_key()["key"]
        status, out, _ = self.rpc(key, "tools/list")
        self.assertEqual(status, 200)
        tools = out["result"]["tools"]
        names = {t["name"] for t in tools}
        expected = {"get_price", "list_price_symbols", "list_models", "web_search", "read_page"} | PAID_TOOL_NAMES
        self.assertEqual(names, expected)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertFalse(any(re.search(r"bitrefill|withdraw|allowance|\bbuy\b", n, re.I) for n in names))

    def test_ping(self):
        key = self.make_key()["key"]
        status, out, _ = self.rpc(key, "ping")
        self.assertEqual((status, out["result"]), (200, {}))

    def test_every_tool_has_a_title_and_boolean_annotations_and_there_are_exactly_fourteen(self):
        key = self.make_key()["key"]
        status, out, _ = self.rpc(key, "tools/list")
        listed = out["result"]["tools"]
        self.assertEqual(len(listed), len(OWN_TOOLS) + len(PAID_TOOLS))
        self.assertEqual(len(listed), 14)
        for tool in listed:
            self.assertTrue(tool.get("title"), tool["name"])
            annotations = tool["annotations"]
            self.assertEqual(annotations["title"], tool["title"])
            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                self.assertIsInstance(annotations[hint], bool, f"{tool['name']}.{hint}")
            self.assertTrue(annotations["readOnlyHint"])
            self.assertFalse(annotations["destructiveHint"])
            if tool["name"] in FREE_TOOL_NAMES:
                self.assertTrue(annotations["idempotentHint"], tool["name"])
                self.assertFalse(annotations["openWorldHint"], tool["name"])
            else:
                self.assertFalse(annotations["idempotentHint"], tool["name"])
                self.assertTrue(annotations["openWorldHint"], tool["name"])


class AuthTests(Fixture):
    def test_no_key_is_refused_before_anything_else(self):
        status, out, headers = self.rpc(None, "initialize")
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        self.assertEqual(out["error"]["code"], -32001)
        self.assertEqual(self.shop.calls, [])

    def test_a_made_up_key_is_refused(self):
        status, out, _ = self.rpc("sk_singit_" + "x" * 43, "initialize")
        self.assertEqual(status, 401)

    def test_a_revoked_key_is_refused(self):
        made = self.make_key()
        self.call("POST", "/api-keys/revoke", {"id": made["id"]})
        status, out, _ = self.rpc(made["key"], "tools/list")
        self.assertEqual(status, 401)
        self.assertEqual(self.shop.calls, [])

    def test_an_untrusted_browser_origin_is_refused_before_auth(self):
        made = self.make_key()
        status, out, _ = self.rpc(made["key"], "initialize", origin="https://evil.test")
        self.assertEqual(status, 403)
        self.assertEqual(self.shop.calls, [])

    def test_the_literal_null_origin_is_never_trusted(self):
        made = self.make_key()
        status, out, _ = self.rpc(made["key"], "initialize", origin="null")
        self.assertEqual(status, 403)

    def test_a_trusted_scanner_origin_without_a_key_is_401_not_403(self):
        status, out, _ = self.rpc(None, "initialize", origin="https://glama.ai")
        self.assertEqual(status, 401)
        self.assertEqual(out["error"]["code"], -32001)

    def test_a_trusted_scanner_origin_with_a_valid_key_gets_cors_headers_back(self):
        made = self.make_key()
        status, out, headers = self.rpc(made["key"], "initialize", origin="https://glama.ai")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://glama.ai")
        self.assertEqual(headers["Vary"], "Origin")

    def test_a_trusted_origin_can_read_www_authenticate_on_a_401(self):
        status, _, headers = self.rpc(None, "initialize", origin="https://glama.ai")
        self.assertEqual(status, 401)
        self.assertIn("WWW-Authenticate", headers["Access-Control-Expose-Headers"])

    def test_the_mcp_hosts_own_origin_is_trusted(self):
        made = self.make_key()
        status, out, headers = self.rpc(made["key"], "initialize", origin=URI)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], URI)

    def test_no_origin_still_needs_a_key_for_initialize_and_tools_list(self):
        status, out, _ = self.rpc(None, "initialize")
        self.assertEqual(status, 401)
        status, out, _ = self.rpc(None, "tools/list")
        self.assertEqual(status, 401)


class AllowedOriginsEnvTests(unittest.TestCase):
    def test_the_default_allowlist_covers_the_known_mcp_scanners(self):
        self.assertEqual(allowed_origins_from_env({}), DEFAULT_ALLOWED_ORIGINS)
        self.assertIn("https://glama.ai", DEFAULT_ALLOWED_ORIGINS)
        self.assertIn("https://x402scan.com", DEFAULT_ALLOWED_ORIGINS)

    def test_the_env_override_replaces_the_defaults_rather_than_extending_them(self):
        origins = allowed_origins_from_env({"SINGIT_MCP_ALLOWED_ORIGINS": "https://only.example, https://also.example/"})
        self.assertEqual(origins, {"https://only.example", "https://also.example"})
        self.assertNotIn("https://glama.ai", origins)

    def test_web_api_passes_its_allowlist_to_the_mcp_server(self):
        from sign402_gateway.web_mcp import McpServer
        server = McpServer(Mock(), allowed_origins=allowed_origins_from_env(
            {"SINGIT_MCP_ALLOWED_ORIGINS": "https://only.example"}))
        self.assertTrue(server.is_trusted_origin("https://only.example"))
        self.assertFalse(server.is_trusted_origin("https://glama.ai"))


class FreeToolTests(Fixture):
    def test_get_price_asks_the_right_url_and_does_not_touch_the_shop(self):
        made = self.make_key(dailyUsd=1)
        fetch = Mock(return_value={"symbol": "BTC", "usd": 65000})
        self.api.mcp.fetch_json = fetch
        status, out, _ = self.call_tool(made["key"], "get_price", {"kind": "crypto", "symbol": "btc"})
        self.assertEqual(status, 200)
        fetch.assert_called_once_with(ASK_URL + "/v1/prices/crypto/BTC")
        self.assertEqual(out["result"]["structuredContent"], {"symbol": "BTC", "usd": 65000})
        self.assertEqual(self.shop.calls, [])
        self.assertEqual(self.store.key_spent(made["id"], NOW // 86400), (0, 0))

    def test_an_invalid_symbol_is_a_protocol_error_not_a_tool_error(self):
        made = self.make_key()
        status, out, _ = self.call_tool(made["key"], "get_price", {"kind": "crypto", "symbol": "!!!"})
        self.assertEqual(status, 200)
        self.assertEqual(out["error"]["code"], -32602)

    def test_an_fx_price_reads_base_and_quote(self):
        made = self.make_key()
        fetch = Mock(return_value={"rate": 1.08})
        self.api.mcp.fetch_json = fetch
        self.call_tool(made["key"], "get_price", {"kind": "fx", "base": "usd", "quote": "eur"})
        fetch.assert_called_once_with(ASK_URL + "/v1/prices/fx/USD/EUR")

    def test_list_price_symbols(self):
        made = self.make_key()
        self.api.mcp.fetch_json = Mock(return_value={"crypto": ["BTC"]})
        status, out, _ = self.call_tool(made["key"], "list_price_symbols")
        self.assertEqual(out["result"]["structuredContent"], {"crypto": ["BTC"]})

    def test_list_models_chat_goes_through_the_shop_without_billing_the_key(self):
        made = self.make_key()
        status, out, _ = self.call_tool(made["key"], "list_models")
        self.assertEqual(out["result"]["structuredContent"]["data"][0]["id"], "claude-opus-5.5")
        self.assertEqual(self.shop.calls, [("api-models", self.account, {})])
        self.assertEqual(self.store.key_spent(made["id"], NOW // 86400), (0, 0))


class WebSearchAndReadPageTests(Fixture):
    def test_web_search_bills_the_key_through_api_search(self):
        made = self.make_key(dailyUsd=1)
        self.shop.reply = (200, {"ok": True, "data": {"results": [{"title": "SingIt"}]}, "costAtomic": 5_000,
                                 "txId": "0x" + "ab" * 32, "network": "base"})
        status, out, _ = self.call_tool(made["key"], "web_search", {"query": "singit"})
        self.assertEqual(status, 200)
        action, account, body = self.shop.calls[-1]
        self.assertEqual((action, account, body["path"], body["body"]["query"]),
                         ("api-search", self.account, "/v1/exa/search", "singit"))
        self.assertEqual(self.store.key_spent(made["id"], NOW // 86400)[0], 5_000)
        self.assertEqual(out["result"]["structuredContent"]["results"], [{"title": "SingIt"}])

    def test_read_page_posts_exa_contents(self):
        made = self.make_key(dailyUsd=1)
        self.shop.reply = (200, {"ok": True, "data": {"results": [{"text": "hi"}]}, "costAtomic": 1_000,
                                 "txId": "0xdead", "network": "base"})
        self.call_tool(made["key"], "read_page", {"url": "https://singitai.app"})
        action, account, body = self.shop.calls[-1]
        self.assertEqual((action, body["path"], body["body"]["urls"]), ("api-search", "/v1/exa/contents",
                                                                        ["https://singitai.app"]))

    def test_a_bad_url_is_a_protocol_error(self):
        made = self.make_key()
        status, out, _ = self.call_tool(made["key"], "read_page", {"url": "not-a-url"})
        self.assertEqual(out["error"]["code"], -32602)
        self.assertEqual(self.shop.calls, [])

    def test_a_shop_refusal_is_a_tool_error_not_a_crash(self):
        made = self.make_key(dailyUsd=1)
        self.shop.reply = (403, {"ok": False, "error": "not_enabled", "text": "Set up your allowance first."})
        status, out, _ = self.call_tool(made["key"], "web_search", {"query": "singit"})
        self.assertEqual(status, 200)
        self.assertTrue(out["result"]["isError"])
        self.assertIn("Set up your allowance first.", out["result"]["content"][0]["text"])
        self.assertEqual(self.store.key_spent(made["id"], NOW // 86400), (0, 0))


class PaidDataToolTests(Fixture):
    def test_get_token_price_bills_the_key_through_api_tool(self):
        made = self.make_key(dailyUsd=1)
        self.shop.reply = (200, {"data": {"symbol": "ETH", "usd": 3_000}, "costAtomic": 3_000, "txId": "0xdef"})
        status, out, _ = self.call_tool(made["key"], "get_token_price", {"symbol": "eth"})
        self.assertEqual(status, 200)
        action, account, body = self.shop.calls[-1]
        self.assertEqual((action, body["toolId"], body["args"]), ("api-tool", "anchor.token_price", {"symbol": "eth"}))
        self.assertEqual(self.store.key_spent(made["id"], NOW // 86400)[0], 3_000)
        self.assertEqual(out["result"]["structuredContent"]["data"], {"symbol": "ETH", "usd": 3_000})

    def test_a_key_made_before_paid_tools_existed_is_refused_without_touching_the_shop(self):
        key, row = self.store.add_api_key(self.account, "legacy", 1_000_000, 250_000, NOW, NOW + 86400,
                                          scopes="answers search")
        status, out, _ = self.call_tool(key, "get_token_price", {"symbol": "eth"})
        self.assertEqual(status, 200)
        self.assertTrue(out["result"]["isError"])
        self.assertIn("Create a new key", out["result"]["content"][0]["text"])
        self.assertEqual(self.shop.calls, [])

    def test_an_unknown_tool_name_is_a_protocol_error(self):
        made = self.make_key()
        status, out, _ = self.call_tool(made["key"], "bitrefill_buy", {})
        self.assertEqual(out["error"]["code"], -32602)
        self.assertEqual(self.shop.calls, [])


class ProtocolTests(Fixture):
    def test_a_notification_gets_202_and_no_body(self):
        made = self.make_key()
        status, out, headers = self.rpc(made["key"], "notifications/initialized", id_=None)
        self.assertEqual((status, out, headers), (202, None, {}))
        status, out, _ = self.rpc(made["key"], "tools/list", id_=None)
        self.assertEqual((status, out), (202, None))

    def test_an_unknown_method_is_method_not_found(self):
        made = self.make_key()
        status, out, _ = self.rpc(made["key"], "resources/list")
        self.assertEqual(out["error"]["code"], -32601)

    def test_malformed_requests(self):
        made = self.make_key()
        for body in ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], {"id": 1, "method": "ping"}, {"jsonrpc": "1.0",
                     "id": 1, "method": "ping"}):
            with self.subTest(body):
                status, out, _ = self.api.mcp.handle(authorization=f"Bearer {made['key']}", api_key=None,
                                                     origin=None, protocol_version=None, body=body)
                self.assertEqual((status, out["error"]["code"]), (400, -32600))

    def test_bad_json_bytes(self):
        made = self.make_key()
        status, out, _ = self.api.mcp.handle(authorization=f"Bearer {made['key']}", api_key=None, origin=None,
                                             protocol_version=None, body=b"{not json")
        self.assertEqual((status, out["error"]["code"]), (400, -32700))

    def test_a_key_sends_at_most_sixty_tool_calls_a_minute(self):
        made = self.make_key(dailyUsd=1)
        self.api.mcp.fetch_json = Mock(return_value={})
        for _ in range(60):
            status, out, _ = self.call_tool(made["key"], "list_price_symbols")
            self.assertEqual(status, 200)
        status, out, _ = self.call_tool(made["key"], "list_price_symbols")
        self.assertEqual(status, 200)  # a JSON-RPC success envelope carrying a tool-level error
        self.assertTrue(out["result"]["isError"])
        self.assertIn("sends up to 60 requests a minute", out["result"]["content"][0]["text"])


class HttpTests(Fixture):
    """Over HTTP, the way an MCP client (Claude Code, Cursor, Codex) calls it. One server for the whole
    class (not one per test) so the suite doesn't pile up unclosed sqlite stores for the GC to warn about
    mid-test, as tearDownModule below already has to account for."""

    def setUp(self):
        super().setUp()
        self.server = wa.WebServer(("127.0.0.1", 0), self.api, URI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        self.root = self.base[: -len("/mcp")]
        self.key = self.make_key()["key"]

    def _options(self, origin):
        request = urllib.request.Request(self.base, method="OPTIONS", headers={"Origin": origin} if origin else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as error:
            headers = dict(error.headers)
            error.close()
            return error.code, headers

    def test_post_initialize_then_no_key_then_get(self):
        request = urllib.request.Request(self.base, data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            out = json.loads(response.read())
        self.assertEqual(out["result"]["serverInfo"]["name"], "singit")

        request = urllib.request.Request(self.base, data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode(), headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

        get_request = urllib.request.Request(self.base, method="GET")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(get_request, timeout=10)
        self.assertEqual(caught.exception.code, 405)
        self.assertEqual(caught.exception.headers.get("Allow"), "POST")
        caught.exception.close()

    def test_preflight_from_a_trusted_scanner_origin(self):
        status, headers = self._options("https://glama.ai")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://glama.ai")
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))
        self.assertIn("OPTIONS", headers.get("Access-Control-Allow-Methods", ""))
        self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers", ""))
        self.assertNotIn("Access-Control-Allow-Credentials", headers)

    def test_preflight_from_an_untrusted_origin_is_403_without_cors_headers(self):
        status, headers = self._options("https://evil.example")
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_preflight_from_the_mcp_hosts_own_origin(self):
        status, headers = self._options(URI)
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), URI)

    def test_server_card(self):
        with urllib.request.urlopen(self.root + "/.well-known/mcp/server-card.json", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIn("max-age=3600", response.headers.get("Cache-Control", ""))
            card = json.loads(response.read())
        self.assertEqual(card["transport"], {"type": "streamable-http", "url": URI + "/mcp"})
        self.assertTrue(card["authentication"]["required"])
        self.assertEqual(card["authentication"]["schemes"], ["bearer"])
        self.assertEqual(len(card["tools"]), 14)
        self.assertLessEqual(len(card["description"]), 200)
        self.assertEqual(card, server_card(URI))

    def test_glama_json(self):
        with urllib.request.urlopen(self.root + "/.well-known/glama.json", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
            data = json.loads(response.read())
        self.assertEqual(data["maintainers"], [{"email": "Exkalibur1919@proton.me"}])
        self.assertEqual(data, glama_json())

    def test_head_on_server_card_has_no_body(self):
        request = urllib.request.Request(self.root + "/.well-known/mcp/server-card.json", method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")


class GatewayApiToolTests(unittest.TestCase):
    """web_internal's "api-tool" action: one of server.PAID_TOOLS, priced and paid in one step, no quote."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"SIGN402_WEB_INTERNAL_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        from sign402_gateway.server import UserSpendLimitStore
        from sign402_gateway.web_internal import ToolQuotes

        self.server = DummyServer()
        self.server.user_event_store = Mock()
        self.server.allowance = Mock()
        self.server.allowance.owner_lookup = lambda account: OWNER if account == ACCOUNT else None
        self.server.allowance.lane_for.return_value = {"limiter_address": "0xLIMITER"}
        self.server.allowance.pay_x402.return_value = dict(PAID)
        self.server.allowance.store = AllowanceStore(Path(self.tmp.name) / "allowance.db")
        self.server.web_tool_quotes = ToolQuotes()
        self.server.user_spend_limit_store = UserSpendLimitStore(Path(self.tmp.name) / "limits.json")
        self.requirements = dict(REQUIREMENTS)
        self.server.allowance.store.record_limiter({
            "limiter_address": "0x" + "ab" * 20, "user_id": ACCOUNT, "owner_address": OWNER,
            "agent_address": "0x" + "cd" * 20, "guardian_address": "0x" + "ef" * 20, "daily_cap": 5_000_000,
            "per_purchase_cap": 1_000_000, "expiry": 2_000_000_000, "deploy_tx": "0x" + "00" * 32,
            "status": "ACTIVE", "source": "exact_match", "created_at": 1})

    def call(self, body):
        with (patch("sign402_gateway.server.fetch_x402_payment_required", return_value={"x402Version": 2}),
              patch("sign402_gateway.server.normalize_x402_payment_required",
                    side_effect=lambda *a, **k: dict(self.requirements))):
            return raw_request(self.server, "/internal/web/api-tool", {"account": ACCOUNT, **body},
                               {"X-SingIt-Internal": TOKEN})

    def test_a_price_over_the_keys_per_request_limit_pays_nothing(self):
        status, body = self.call({"toolId": "otto.crypto_news", "args": {}, "limitAtomic": 500})
        self.assertEqual(status, 400)
        self.assertIn("more than this key's per-request limit", body["text"])
        self.server.allowance.pay_x402.assert_not_called()

    def test_otherwise_it_pays_once_and_returns_the_sellers_data(self):
        status, body = self.call({"toolId": "otto.crypto_news", "args": {}, "limitAtomic": 2_000})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"], {"headline": "news"})
        self.assertEqual(body["costAtomic"], 1000)
        self.server.allowance.pay_x402.assert_called_once()

    def test_a_solana_account_is_refused(self):
        solana = "solana:" + "1" * 43
        status, body = self.call({"account": solana, "toolId": "otto.crypto_news", "args": {}})
        self.assertEqual(status, 403, "unknown web account: _account() never reaches our own Base-only check")
        self.server.allowance.pay_x402.assert_not_called()


def tearDownModule():
    # Quietly collected here: on Python 3.13+ an unclosed SQLite connection warns when it is
    # collected, and a warning printed mid-test (capturing stderr) fails whichever test runs then.
    import gc
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()


if __name__ == "__main__":
    unittest.main()
