"""Steps 4 and 5 of docs/allowance-web-v1.md: the shop for web accounts, and linking Telegram."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from eth_account import Account

from sign402_gateway import web_api as wa
from sign402_gateway.agent_allowance import AllowanceStore
from sign402_gateway.server import Sign402GatewayHandler
from sign402_gateway.web_accounts import WebAccountStore, account_id_for
from tests.test_gateway_server import DummyServer, FakeSocket

TOKEN = "t" * 40
OWNER = "0x1111111111111111111111111111111111111111"
ACCOUNT = account_id_for(OWNER)
SELLER = "0x8AEE621035D93Deb3C0C1177fac252dC2dd501a0"
REQUIREMENTS = {
    "scheme": "exact", "network": "base-mainnet", "x402Network": "eip155:8453", "amountAtomic": "1000",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "receiver": SELLER, "paymentIntent": "news-1",
    "purpose": "x402_api_access", "extra": {"name": "USD Coin", "version": "2"},
}
PAID = {"ok": True, "status": 200, "delivered": True, "settlementTx": "0x" + "ab" * 32, "payer": "0xAGENT",
        "limiter": "0xLIMITER", "funding": "float", "fundingTx": None,
        "resourceResult": {"status": 200, "body": {"headline": "news"}}}


def raw_request(server, path, body, headers, client="127.0.0.1"):
    encoded = json.dumps(body).encode()
    raw = (f"POST {path} HTTP/1.1\r\nContent-Length: {len(encoded)}\r\nContent-Type: application/json\r\n".encode()
           + b"".join(f"{k}: {v}\r\n".encode() for k, v in headers.items()) + b"\r\n" + encoded)
    socket = FakeSocket(raw)
    with patch("sys.stderr", io.StringIO()):
        Sign402GatewayHandler(socket, (client, 5555), server)
    text = socket.wfile.getvalue().decode()
    return int(text.split()[1]), json.loads(text.split("\r\n\r\n", 1)[1])


class GatewayWebShopTests(unittest.TestCase):
    """/internal/web/*: what the web API asks of the gateway."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"SIGN402_WEB_INTERNAL_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        from sign402_gateway.web_internal import ToolQuotes

        self.server = DummyServer()
        self.server.user_event_store = Mock()
        self.server.user_event_store.summaries.return_value = []
        self.server.allowance = Mock()
        self.server.allowance.owner_lookup = lambda account: OWNER if account == ACCOUNT else None
        self.server.allowance.lane_for.return_value = {"limiter_address": "0xLIMITER"}
        self.server.allowance.pay_x402.return_value = dict(PAID)
        self.server.allowance.store = AllowanceStore(Path(self.tmp.name) / "allowance.db")
        self.server.web_tool_quotes = ToolQuotes()
        self.requirements = dict(REQUIREMENTS)
        from sign402_gateway.server import UserSpendLimitStore

        self.server.user_spend_limit_store = UserSpendLimitStore(Path(self.tmp.name) / "limits.json")
        self.server.allowance.store.record_limiter({
            "limiter_address": "0x" + "ab" * 20, "user_id": ACCOUNT, "owner_address": OWNER,
            "agent_address": "0x" + "cd" * 20, "guardian_address": "0x" + "ef" * 20, "daily_cap": 5_000_000,
            "per_purchase_cap": 1_000_000, "expiry": 2_000_000_000, "deploy_tx": "0x" + "00" * 32,
            "status": "ACTIVE", "source": "exact_match", "created_at": 1})

    def call(self, action, body=None, *, token=TOKEN, client="127.0.0.1", account=ACCOUNT):
        with (patch("sign402_gateway.server.fetch_x402_payment_required", return_value={"x402Version": 2}),
              patch("sign402_gateway.server.normalize_x402_payment_required",
                    side_effect=lambda *a, **k: dict(self.requirements))):
            return raw_request(self.server, f"/internal/web/{action}", {"account": account, **(body or {})},
                               {"X-SingIt-Internal": token}, client)

    def test_only_the_web_api_on_this_host_with_its_token_gets_in(self):
        for token, client in (("", "127.0.0.1"), ("x" * 40, "127.0.0.1"), (TOKEN, "203.0.113.5")):
            with self.subTest(token=token[:3], client=client):
                self.assertEqual(self.call("tools", token=token, client=client)[0], 403)
        with patch.dict(os.environ, {"SIGN402_WEB_INTERNAL_TOKEN": "short"}):
            self.assertEqual(self.call("tools", token="short")[0], 403)
        status, body = self.call("tools", account="1045618308")
        self.assertEqual((status, body["error"]), (403, "not_enabled"))

    def test_the_catalog_names_each_tool_and_its_seller(self):
        status, body = self.call("tools")
        self.assertEqual(status, 200)
        news = next(t for t in body["tools"] if t["id"] == "otto.crypto_news")
        self.assertEqual(news["resourceUrl"], "https://x402.ottoai.services/crypto-news")

    def test_a_quote_then_buy_pays_from_the_accounts_own_lane(self):
        status, quote = self.call("tool-quote", {"tool": "news"})
        self.assertEqual((status, quote["priceAtomic"], quote["payTo"]), (200, "1000", SELLER))
        status, bought = self.call("tool-buy", {"quoteId": quote["quoteId"]})
        self.assertEqual(status, 200)
        self.assertTrue(bought["ok"])
        args = self.server.allowance.pay_x402.call_args
        self.assertEqual(args.args[0], ACCOUNT)
        self.server.imessage_approval_service.request_purchase_approval.assert_not_called()
        self.server.user_x402_buyer.assert_not_called()
        written_for, event = self.server.user_event_store.write.call_args.args
        self.assertEqual(written_for, ACCOUNT)
        self.assertEqual(event["account"], ACCOUNT)
        # A quote buys once.
        self.assertEqual(self.call("tool-buy", {"quoteId": quote["quoteId"]})[0], 400)

    def test_a_higher_price_another_recipient_or_anothers_quote_pays_nothing(self):
        for name, spoil in (("price", lambda: self.requirements.update(amountAtomic="1001")),
                            ("recipient", lambda: self.requirements.update(receiver=Account.create().address))):
            with self.subTest(name):
                self.requirements = dict(REQUIREMENTS)
                _, quote = self.call("tool-quote", {"tool": "news"})
                spoil()
                status, body = self.call("tool-buy", {"quoteId": quote["quoteId"]})
                self.assertEqual(status, 400)
                self.assertIn("changed since your quote", body["text"])
        self.requirements = dict(REQUIREMENTS)
        _, quote = self.call("tool-quote", {"tool": "news"})
        other = account_id_for(Account.create().address)
        self.server.allowance.owner_lookup = lambda account: OWNER
        self.assertEqual(self.call("tool-buy", {"quoteId": quote["quoteId"]}, account=other)[0], 400)
        self.server.allowance.pay_x402.assert_not_called()

    def test_bitrefill_on_the_web_is_confirmed_by_the_page_not_imessage(self):
        self.server.allowance_bitrefill = Mock()
        self.server.allowance_bitrefill.quote.return_value = {
            "slug": "hediyen", "name": "Hediyen Kart", "package": "1", "packageCurrency": "TRY",
            "priceUsd": "0.02", "priceAtomic": 20_000}
        self.server.allowance_bitrefill.buy.return_value = {"ok": True, "invoiceId": "inv-9", "telegramText": "Bought."}
        status, quote = self.call("bitrefill-quote", {"productId": "hediyen", "package": "1"})
        self.assertEqual(status, 200)
        status, bought = self.call("bitrefill-buy", {"quoteId": quote["quoteId"]})
        self.assertEqual((status, bought["invoiceId"]), (200, "inv-9"))
        self.server.imessage_approval_service.request_purchase_approval.assert_not_called()
        self.server.allowance_bitrefill.buy.assert_called_once_with(ACCOUNT, "hediyen", "1", 20_000)

    def test_a_web_accounts_limits_are_its_limiters(self):
        self.server.user_spend_limit_store = Mock(wraps=self.server.user_spend_limit_store)
        _, quote = self.call("tool-quote", {"tool": "news"})
        self.call("tool-buy", {"quoteId": quote["quoteId"]})
        kwargs = self.server.user_spend_limit_store.set_limit_settings.call_args.kwargs
        self.assertEqual((kwargs["max_per_tx_atomic"], kwargs["daily_cap_atomic"]), (1_000_000, 5_000_000))
        with patch.dict(os.environ, {"SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX": "500000"}):
            _, quote = self.call("tool-quote", {"tool": "news"})
            self.call("tool-buy", {"quoteId": quote["quoteId"]})
        self.assertEqual(self.server.user_spend_limit_store.set_limit_settings.call_args.kwargs["max_per_tx_atomic"],
                         500_000)

    def test_history_and_reveal_are_the_accounts_own(self):
        self.server.user_event_store.summaries.return_value = [{"id": "p1", "title": "Crypto News"}]
        status, body = self.call("purchases")
        self.assertEqual((status, body["purchases"][0]["id"]), (200, "p1"))
        self.server.user_event_store.summaries.assert_called_with(ACCOUNT)
        self.assertEqual(self.call("purchase-reveal", {"purchaseId": "nope"})[0], 404)
        self.server.user_event_store.read.return_value = {"ok": True, "mode": "paid_tool"}
        status, body = self.call("purchase-reveal", {"purchaseId": "p1"})
        self.assertEqual((status, body["text"]), (200, "This purchase has no gift card code."))


class LinkTelegramTests(unittest.TestCase):
    """/link <code>: a Telegram chat joins a web account and uses its limiter."""

    def setUp(self):
        from sign402_gateway import server as gw

        limiter = patch.object(gw, "_USER_RATE_LIMITER", gw.SlidingWindowRateLimiter())
        limiter.start()
        self.addCleanup(limiter.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts = WebAccountStore(Path(self.tmp.name) / "web.db")
        self.accounts.ensure_account(OWNER, 1)
        self.server = DummyServer()
        self.server.user_wallet_service.resolve_telegram_user_id = Mock(return_value="1045618308")
        self.server.web_accounts = self.accounts
        self.server.allowance = Mock()
        self.server.allowance.store = AllowanceStore(Path(self.tmp.name) / "allowance.db")
        self.server.allowance.status.return_value = {"telegramText": "status"}

    def allowance(self, action, body=None):
        return raw_request(self.server, f"/agent/allowance/{action}", body or {},
                           {"Authorization": "Bearer test-wallet-token", "X-Sign402-User-Token": "user-token-1"})

    def test_a_code_links_once_and_the_bot_then_uses_the_web_accounts_limiter(self):
        code = self.accounts.new_link_code(ACCOUNT, int(__import__("time").time()))
        status, body = self.allowance("link", {"code": code})
        self.assertEqual(status, 200)
        self.assertIn(f"allowance of {OWNER}", body["telegramText"])
        self.assertEqual(self.accounts.account_for_telegram("1045618308"), ACCOUNT)
        self.assertEqual(self.accounts.telegram_for(ACCOUNT), "1045618308")

        self.allowance("status")
        self.server.allowance.status.assert_called_with(ACCOUNT)
        self.assertEqual(self.allowance("link", {"code": code})[0], 400)  # used

    def test_wrong_codes_are_refused_and_limited(self):
        for attempt in range(5):
            status, body = self.allowance("link", {"code": f"{attempt:06d}"})
            self.assertEqual(status, 400)
            self.assertIn("wrong or expired", body["telegramText"])
        self.assertEqual(self.allowance("link", {"code": "000009"})[0], 429)

    def test_a_linked_chat_buys_from_the_web_accounts_lane(self):
        self.accounts.link_telegram(self.accounts.new_link_code(ACCOUNT, 10**10 - 1000), "1045618308", 10**10 - 999)
        self.server.event_store = Mock()
        self.server.user_event_store = Mock()
        self.server.allowance.lane_for.return_value = {"limiter_address": "0xLIMITER"}
        self.server.allowance.pay_x402.return_value = dict(PAID)
        self.server.imessage_approval_service.request_purchase_approval.return_value = {"ok": True, "status": "approved"}
        with (patch("sign402_gateway.server.fetch_x402_payment_required", return_value={}),
              patch("sign402_gateway.server.normalize_x402_payment_required", return_value=dict(REQUIREMENTS))):
            status, _ = raw_request(self.server, "/agent/buy-tool", {"tool": "news", "telegramUserId": "1045618308"},
                                    {"Authorization": "Bearer test-wallet-token", "X-Sign402-User-Token": "user-token-1"})
        self.assertEqual(status, 200)
        self.server.allowance.lane_for.assert_called_with(ACCOUNT)
        self.assertEqual(self.server.allowance.pay_x402.call_args.args[0], ACCOUNT)
        self.assertEqual(self.server.user_event_store.write.call_args.args[0], "1045618308")  # the chat's history

    def test_a_new_code_kills_the_old_one_and_codes_expire(self):
        first = self.accounts.new_link_code(ACCOUNT, 100)
        second = self.accounts.new_link_code(ACCOUNT, 100)
        self.assertIsNone(self.accounts.link_telegram(first, "7", 101))
        self.assertIsNone(self.accounts.link_telegram(second, "7", 100 + 601))


class WebShopRoutesTests(unittest.TestCase):
    """The public side: /web/v1/shop/*, /purchases and /link/telegram."""

    def setUp(self):
        from tests.test_web_api import WebApiTests

        self.base = WebApiTests()
        self.base.setUp()
        self.addCleanup(self.base.tmp.cleanup)
        self.calls = []
        self.base.api.shop = lambda action, account, body: (
            self.calls.append((action, account, dict(body or {}))) or (200, {"ok": True, "telegramText": "done"}))
        self.token, self.csrf, self.me = self.base.sign_in()

    def call(self, method, path, body=None, csrf=True):
        return self.base.call(method, path, body, token=self.token, csrf=self.csrf if csrf else None)

    def test_each_shop_route_reaches_the_gateway_as_this_account(self):
        for (method, path), action in wa.SHOP_ROUTES.items():
            with self.subTest(path=path):
                status, body, _ = self.call(method, path, {"quoteId": "q", "offset": "6"})
                self.assertEqual((status, body), (200, {"ok": True, "text": "done"}))
                self.assertEqual(self.calls[-1][:2], (action, self.me["account"]))
        self.assertEqual(self.calls[-2][2], {"offset": "6"})  # GET /purchases forwards only the offset

    def test_the_shop_needs_a_session_its_csrf_token_and_a_gateway(self):
        with self.assertRaises(wa.WebAuthError):
            self.call("POST", "/shop/tools/buy", {"quoteId": "q"}, csrf=False)
        self.base.api.shop = None
        with self.assertRaises(wa.WebError) as raised:
            self.call("GET", "/shop/tools")
        self.assertEqual(raised.exception.status, 503)

    def test_linking_shows_a_code_and_the_session_says_when_it_is_linked(self):
        status, body, _ = self.call("POST", "/link/telegram")
        self.assertEqual(status, 200)
        self.assertRegex(body["code"], r"^\d{6}$")
        self.assertIn(f"/link {body['code']}", body["text"])
        self.assertFalse(self.call("GET", "/session")[1]["telegramLinked"])
        self.base.store.link_telegram(body["code"], "1045618308", int(__import__("time").time()))
        self.assertTrue(self.call("GET", "/session")[1]["telegramLinked"])
        self.call("POST", "/link/telegram/remove")
        self.assertFalse(self.call("GET", "/session")[1]["telegramLinked"])


class WebPauseTests(unittest.TestCase):
    def test_the_panic_button_pauses_through_the_guardian(self):
        from tests.test_web_api import WebApiTests

        base = WebApiTests()
        base.setUp()
        self.addCleanup(base.tmp.cleanup)
        token, csrf, _ = base.sign_in()
        base.call("POST", "/allowance/setup", {"dailyCap": "5", "perPurchaseCap": "1", "days": "30"},
                  token=token, csrf=csrf)
        with self.assertRaises(wa.WebAuthError):
            base.call("POST", "/allowance/pause", token=token)
        base.evm.send = Mock(wraps=base.evm.send)
        base.evm.wait_until = lambda read, accept: 1
        status, body, _ = base.call("POST", "/allowance/pause", token=token, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertNotIn("telegramText", body)
        self.assertEqual(base.evm.send.call_args.kwargs["data"], "0x8456cb59")  # pause()


class WebAbuseTests(unittest.TestCase):
    def setUp(self):
        from tests.test_web_api import WebApiTests

        self.base = WebApiTests()
        self.base.setUp()
        self.addCleanup(self.base.tmp.cleanup)

    def test_smart_contract_wallets_are_refused_and_7702_accounts_are_not(self):
        self.base.evm.code = lambda address: "0x6080604052"
        with self.assertRaises(wa.WebError) as raised:
            self.base.call("POST", "/auth/nonce", {"address": self.base.user.address})
        self.assertEqual(raised.exception.code, "smart_wallet_unsupported")
        self.base.evm.code = lambda address: "0xef0100" + "11" * 20
        self.assertEqual(self.base.call("POST", "/auth/nonce", {"address": self.base.user.address})[0], 200)

    def test_the_daily_deployment_budget_is_shared_by_everyone(self):
        self.base.api.max_deploys_per_day = 1
        token, csrf, _ = self.base.sign_in()
        self.base.call("POST", "/allowance/setup", {"dailyCap": "5", "perPurchaseCap": "1", "days": "30"},
                       token=token, csrf=csrf)
        self.base.user = Account.create()
        token, csrf, _ = self.base.sign_in()
        with self.assertRaises(wa.WebError) as raised:
            self.base.call("POST", "/allowance/setup", {"dailyCap": "5", "perPurchaseCap": "1", "days": "30"},
                           token=token, csrf=csrf)
        self.assertEqual((raised.exception.status, raised.exception.code), (503, "deploy_budget"))


if __name__ == "__main__":
    unittest.main()


class GatewayShopClientTests(unittest.TestCase):
    def test_it_sends_the_token_and_account_and_passes_refusals_through(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen = []

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, self.headers["X-SingIt-Internal"], body))
                status = 400 if body.get("quoteId") == "bad" else 200
                data = json.dumps({"ok": status == 200, "text": "refused" if status == 400 else "ok"}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        shop = wa.GatewayShop(f"http://127.0.0.1:{server.server_address[1]}", TOKEN)

        self.assertEqual(shop("tool-buy", ACCOUNT, {"quoteId": "q", "account": "someone-else"}), (200, {"ok": True, "text": "ok"}))
        self.assertEqual(seen[-1], ("/internal/web/tool-buy", TOKEN, {"quoteId": "q", "account": ACCOUNT}))
        self.assertEqual(shop("tool-buy", ACCOUNT, {"quoteId": "bad"})[0], 400)

        closed = wa.GatewayShop("http://127.0.0.1:9", TOKEN, timeout=2)
        with self.assertRaises(wa.WebError) as raised:
            closed("tools", ACCOUNT)
        self.assertEqual(raised.exception.status, 503)


class EndToEndShopTests(unittest.TestCase):
    """Browser → web API over HTTP → gateway over HTTP → a purchase on the account's lane."""

    def test_sign_in_quote_and_buy_across_both_processes(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer

        from eth_account.messages import encode_defunct

        from sign402_gateway.server import UserSpendLimitStore
        from sign402_gateway.web_internal import ToolQuotes
        from tests.test_web_api import WebApiTests

        base = WebApiTests()
        base.setUp()
        self.addCleanup(base.tmp.cleanup)
        env = patch.dict(os.environ, {"SIGN402_WEB_INTERNAL_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)

        class LiveGateway(DummyServer, ThreadingHTTPServer):
            daemon_threads = True

            def __init__(self):
                DummyServer.__init__(self)
                ThreadingHTTPServer.__init__(self, ("127.0.0.1", 0), Sign402GatewayHandler)

        gateway = LiveGateway()
        gateway.allowance = base.service
        base.service.lane_for = Mock(return_value={"limiter_address": "0xLIMITER"})
        base.service.pay_x402 = Mock(return_value=dict(PAID))
        gateway.user_event_store = Mock()
        gateway.user_spend_limit_store = UserSpendLimitStore(Path(base.tmp.name) / "limits.json")
        gateway.web_tool_quotes = ToolQuotes()
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        self.addCleanup(gateway.server_close)
        self.addCleanup(gateway.shutdown)

        base.api.shop = wa.GatewayShop(f"http://127.0.0.1:{gateway.server_address[1]}", TOKEN)
        web = wa.WebServer(("127.0.0.1", 0), base.api, "https://app.singit.test")
        threading.Thread(target=web.serve_forever, daemon=True).start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)
        url = f"http://127.0.0.1:{web.server_address[1]}/web/v1"
        cookie, csrf = {}, {}

        def call(method, path, body=None):
            headers = {"Content-Type": "application/json", **cookie, **csrf}
            data = json.dumps(body).encode() if body is not None else None
            with urllib.request.urlopen(urllib.request.Request(url + path, data=data, method=method,
                                                               headers=headers), timeout=30) as reply:
                return dict(reply.headers), json.loads(reply.read())

        _, issued = call("POST", "/auth/nonce", {"address": base.user.address})
        signature = Account.sign_message(encode_defunct(text=issued["message"]), base.user.key).signature.to_0x_hex()
        headers, signed = call("POST", "/auth/verify", {"message": issued["message"], "signature": signature})
        cookie["Cookie"] = headers["Set-Cookie"].split(";")[0]
        csrf[wa.CSRF_HEADER] = signed["csrfToken"]
        base.store.ensure_account(base.user.address, 1)

        with (patch("sign402_gateway.server.fetch_x402_payment_required", return_value={}),
              patch("sign402_gateway.server.normalize_x402_payment_required", return_value=dict(REQUIREMENTS))):
            _, quote = call("POST", "/shop/tools/quote", {"tool": "news"})
            self.assertEqual((quote["priceUsd"], quote["payTo"]), ("0.001", SELLER))
            _, bought = call("POST", "/shop/tools/buy", {"quoteId": quote["quoteId"]})
        self.assertTrue(bought["ok"])
        self.assertIn("text", bought)
        self.assertNotIn("telegramText", bought)
        self.assertEqual(base.service.pay_x402.call_args.args[0], signed["account"])
        self.assertEqual(gateway.user_event_store.write.call_args.args[0], signed["account"])
