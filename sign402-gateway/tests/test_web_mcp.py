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
from sign402_gateway import web_mcp as wm
from sign402_gateway.agent_allowance import AllowanceStore
from sign402_gateway.server import PAID_TOOLS
from sign402_gateway.web_accounts import WebAccountStore, WebAuth, account_id_for
from sign402_gateway.web_mcp import ASK_URL
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
        self.api = wa.WebApi(self.auth, Mock(), shop=self.shop, now=lambda: self.clock[0])
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

    def rpc(self, key, method, params=None, *, id_=1, origin=None, client_ip=None):
        body = {"jsonrpc": "2.0", "method": method}
        if id_ is not None:
            body["id"] = id_
        if params is not None:
            body["params"] = params
        return self.api.mcp.handle(authorization=f"Bearer {key}" if key else None, api_key=None, origin=origin,
                                   protocol_version="2025-06-18", body=body, client_ip=client_ip)

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

    def test_a_browser_origin_is_refused(self):
        made = self.make_key()
        status, out, _ = self.rpc(made["key"], "initialize", origin="https://evil.test")
        self.assertEqual(status, 403)
        self.assertEqual(self.shop.calls, [])

    def test_no_key_is_still_401_when_a_client_ip_is_given(self):
        status, out, headers = self.rpc(None, "initialize", client_ip="203.0.113.5")
        self.assertEqual(status, 401)
        self.assertEqual(out["error"]["code"], -32001)


class FailedAuthIpLimitTests(Fixture):
    def test_too_many_failed_auths_from_one_address_get_429_instead_of_401(self):
        for _ in range(wm.AUTH_FAILURES_PER_WINDOW):
            status, _, _ = self.rpc(None, "initialize", client_ip="198.51.100.9")
            self.assertEqual(status, 401)
        status, out, headers = self.rpc(None, "initialize", client_ip="198.51.100.9")
        self.assertEqual(status, 429)
        self.assertEqual(out["error"]["code"], -32029)
        self.assertEqual(int(headers["Retry-After"]), wm.AUTH_FAILURE_WINDOW_SECONDS)

    def test_a_different_address_is_unaffected(self):
        for _ in range(wm.AUTH_FAILURES_PER_WINDOW):
            self.rpc(None, "initialize", client_ip="198.51.100.9")
        status, _, _ = self.rpc(None, "initialize", client_ip="198.51.100.10")
        self.assertEqual(status, 401)

    def test_a_successful_auth_does_not_count_as_a_failure(self):
        made = self.make_key()
        for _ in range(wm.AUTH_FAILURES_PER_WINDOW):
            status, _, _ = self.rpc(made["key"], "ping", client_ip="198.51.100.9")
            self.assertEqual(status, 200)
        status, _, _ = self.rpc(made["key"], "ping", client_ip="198.51.100.9")
        self.assertEqual(status, 200)

    def test_a_valid_key_is_served_even_from_a_throttled_address(self):
        made = self.make_key()
        for _ in range(wm.AUTH_FAILURES_PER_WINDOW + 1):
            self.rpc(None, "initialize", client_ip="198.51.100.9")
        status, _, _ = self.rpc(None, "initialize", client_ip="198.51.100.9")
        self.assertEqual(status, 429)
        status, _, _ = self.rpc(made["key"], "ping", client_ip="198.51.100.9")
        self.assertEqual(status, 200)

    def test_no_client_ip_means_no_limiting(self):
        for _ in range(wm.AUTH_FAILURES_PER_WINDOW + 5):
            status, _, _ = self.rpc(None, "initialize")
            self.assertEqual(status, 401)


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

    def test_a_key_sends_at_most_sixty_mcp_requests_a_minute(self):
        made = self.make_key(dailyUsd=1)
        self.api.mcp.fetch_json = Mock(return_value={})
        for _ in range(60):
            status, out, _ = self.call_tool(made["key"], "list_price_symbols")
            self.assertEqual(status, 200)
        status, out, headers = self.call_tool(made["key"], "list_price_symbols")
        self.assertEqual(status, 429)
        self.assertEqual(out["error"]["code"], -32029)
        self.assertIn("60 requests a minute", out["error"]["message"])
        self.assertTrue(int(headers["Retry-After"]) > 0)

    def test_the_mcp_bucket_is_separate_from_v1s(self):
        """Exhausting /v1's own bucket must not touch MCP's, and the other way round."""
        made = self.make_key(dailyUsd=1)
        self.shop.reply = (200, {"ok": True, "data": {}, "costAtomic": 0})
        with patch("sign402_gateway.web_api_keys.REQUESTS_PER_MINUTE", 2):
            for _ in range(2):
                self.api.keys.public("POST", "/exa/search", authorization=f"Bearer {made['key']}",
                                     origin=None, body={"query": "x"})
            with self.assertRaises(wa.ApiError) as caught:
                self.api.keys.public("POST", "/exa/search", authorization=f"Bearer {made['key']}",
                                     origin=None, body={"query": "x"})
            self.assertEqual(caught.exception.status, 429)
            # /v1's bucket is now empty, but MCP's own bucket is untouched.
            status, out, _ = self.rpc(made["key"], "ping")
            self.assertEqual(status, 200)

        # The ping above already used one of this key's two MCP slots.
        self.api.mcp.requests_per_minute = 2
        status, _, _ = self.rpc(made["key"], "ping")
        self.assertEqual(status, 200)
        status, out, headers = self.rpc(made["key"], "ping")
        self.assertEqual(status, 429)
        self.assertEqual(out["error"]["code"], -32029)
        # /v1 still has its own, unrelated bucket (REQUESTS_PER_MINUTE is back to 60 outside the patch).
        status, _, _ = self.api.keys.public("POST", "/exa/search", authorization=f"Bearer {made['key']}",
                                            origin=None, body={"query": "x"})
        self.assertEqual(status, 200)

    def test_mcp_requests_per_minute_env_override(self):
        with patch.dict(os.environ, {"SINGIT_MCP_REQUESTS_PER_MINUTE": "2"}):
            mcp = wa.McpServer(self.api.keys, fetch_json=Mock(return_value={}))
        self.assertEqual(mcp.requests_per_minute, 2)
        made = self.make_key(dailyUsd=1)
        for _ in range(2):
            status, _, _ = mcp.handle(authorization=f"Bearer {made['key']}", api_key=None, origin=None,
                                      protocol_version=None, body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            self.assertEqual(status, 200)
        status, out, headers = mcp.handle(authorization=f"Bearer {made['key']}", api_key=None, origin=None,
                                          protocol_version=None, body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], str(60))

    def test_an_invalid_env_override_falls_back_to_the_default(self):
        with patch.dict(os.environ, {"SINGIT_MCP_REQUESTS_PER_MINUTE": "not-a-number"}):
            mcp = wa.McpServer(self.api.keys)
        self.assertEqual(mcp.requests_per_minute, 60)
        with patch.dict(os.environ, {"SINGIT_MCP_REQUESTS_PER_MINUTE": "0"}):
            mcp = wa.McpServer(self.api.keys)
        self.assertEqual(mcp.requests_per_minute, 60)

    def test_search_tools_bill_only_the_mcp_bucket_not_v1s(self):
        made = self.make_key(dailyUsd=1)
        self.shop.reply = (200, {"ok": True, "data": {"results": []}, "costAtomic": 0})
        self.call_tool(made["key"], "web_search", {"query": "x"})
        with patch("sign402_gateway.web_api_keys.REQUESTS_PER_MINUTE", 1):
            # /v1's own bucket is still empty: web_search did not touch it.
            status, _, _ = self.api.keys.public("POST", "/exa/search", authorization=f"Bearer {made['key']}",
                                                origin=None, body={"query": "x"})
            self.assertEqual(status, 200)


class HttpTests(Fixture):
    """Over HTTP, the way an MCP client (Claude Code, Cursor, Codex) calls it."""

    def setUp(self):
        super().setUp()
        self.server = wa.WebServer(("127.0.0.1", 0), self.api, URI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        self.key = self.make_key()["key"]

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

    def test_too_many_failed_auths_from_the_same_peer_eventually_429s(self):
        no_key = urllib.request.Request(self.base, data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode(),
            headers={"Content-Type": "application/json"})
        for _ in range(wm.AUTH_FAILURES_PER_WINDOW):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(no_key, timeout=10)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(no_key, timeout=10)
        self.assertEqual(caught.exception.code, 429)
        self.assertTrue(caught.exception.headers.get("Retry-After"))
        caught.exception.close()


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
