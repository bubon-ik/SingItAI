"""A Solana wallet on the web page: limits, the wallet's approval, and Venice paid from it."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.fernet import Fernet

from sign402_gateway import web_agent as wg
from sign402_gateway import web_venice
from sign402_gateway.agent_allowance import AllowanceError
from sign402_gateway.solana_allowance import SolanaAllowanceService, SolanaAllowanceStore
from tests.test_solana_allowance import ACCOUNT, OWNER, FakeBridge


class VeniceBridge(FakeBridge):
    """FakeBridge plus the Venice operations of the Node service."""

    def __init__(self):
        super().__init__()
        self.credit, self.paid = "0", []
        self.quotes = {"q1": {"quoteId": "q1", "amountUsdc": "5.000000", "approvalHash": "h" * 64,
                              "expiresAt": "2027-01-01T00:00:00Z", "recipient": "Venice"}}

    def run(self, user_id, payer, key, operation, fee_payer_key=None, **payload):
        if operation == "balance":
            self.calls.append((operation, payer, True, None, payload))
            return {"canConsume": float(self.credit) > 0, "balanceUsd": self.credit}
        if operation == "quote":
            self.calls.append((operation, payer, True, None, payload))
            return self.quotes["q1"]
        if operation == "status":
            return {"quote": self.quotes[payload["quoteId"]], "attempted": bool(self.paid), "state": "quoted"}
        if operation == "pay":
            self.paid.append(payload)
            self.agent_usdc -= 5_000_000
            self.credit = "5"
            return {"state": "confirmed", "transaction": "5" * 88}
        if operation == "chat":
            self.calls.append((operation, payer, True, None, payload))
            self.credit = "4.9989"
            return {"text": "Hi from Venice on Solana", "usage": {"prompt_tokens": 50, "completion_tokens": 20}}
        return super().run(user_id, payer, key, operation, fee_payer_key, **payload)


class SolanaVeniceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = VeniceBridge()
        self.lane = SolanaAllowanceService(
            store=SolanaAllowanceStore(Path(self.tmp.name) / "sol.db"), bridge=self.bridge,
            fernet=Fernet(Fernet.generate_key()), fee_payer_key=lambda: "FEE", max_daily=100_000_000,
            max_per_purchase=25_000_000, max_days=90, max_grant=300_000_000)
        self.events = Mock()
        self.events.summaries.return_value = []
        self.server = SimpleNamespace(solana_allowance=self.lane, chat_service=None, user_event_store=self.events)
        self.messages = [{"role": "system", "content": "SingIt"}, {"role": "user", "content": "hello"}]

    def grant(self, daily="20", per="5"):
        self.lane.setup(ACCOUNT, daily, per, "30")
        op = self.lane.prepare_wallet(ACCOUNT, "GRANT", amount="20")
        self.lane.submit_wallet(ACCOUNT, op["operation"], "SIGNED")

    def test_no_credit_asks_to_confirm_venices_exact_quote_and_pays_nothing(self):
        self.grant()
        status, reply = web_venice.chat_solana(self.server, ACCOUNT, self.messages)
        self.assertEqual((status, reply["error"], reply["quote"]["quoteId"]), (402, "topup_needed", "q1"))
        self.assertIn("5 USDC", reply["text"])
        self.assertEqual(self.bridge.paid, [])
        self.assertNotIn("allowance-pull", [c[0] for c in self.bridge.calls])

    def test_the_confirmed_top_up_is_pulled_within_limits_paid_and_recorded(self):
        self.grant()
        status, paid = web_venice.topup_solana(self.server, ACCOUNT, "q1", "h" * 64)
        self.assertEqual((status, paid["ok"], paid["text"]), (200, True, "Added 5 USDC of Venice credit."))
        self.assertEqual([c[0] for c in self.bridge.calls if c[0] == "allowance-pull"], ["allowance-pull"])
        self.assertEqual(self.bridge.paid, [{"quoteId": "q1", "approvalHash": "h" * 64}])
        recorded = self.events.write.call_args.args[1]
        self.assertEqual((recorded["toolName"], recorded["receipt"]["network"]), ("Venice AI credit", "Solana"))
        self.assertEqual(self.lane.status(ACCOUNT)["remainingTodayAtomic"], 15_000_000)
        status, reply = web_venice.chat_solana(self.server, ACCOUNT, self.messages)
        self.assertEqual((reply["text"], reply["promptTokens"], reply["costAtomic"]), ("Hi from Venice on Solana", 50, 1100))
        self.assertEqual(self.bridge.calls[-2][4]["conversation"], self.messages)

    def test_a_top_up_not_shown_or_over_the_limits_is_not_paid(self):
        self.grant(per="4")
        with self.assertRaisesRegex(AllowanceError, "not the top-up you were shown"):
            web_venice.topup_solana(self.server, ACCOUNT, "q1", "x" * 64)
        with self.assertRaisesRegex(AllowanceError, "per-purchase"):
            web_venice.topup_solana(self.server, ACCOUNT, "q1", "h" * 64)
        self.assertEqual(self.bridge.paid, [])

    def test_the_agent_shows_the_card_then_tops_up_and_answers(self):
        self.grant()
        shop_calls = []

        def shop(action, account, body):
            shop_calls.append(action)
            if action == "venice-chat":
                return web_venice.chat_solana(self.server, account, body["messages"])
            if action == "venice-solana-topup":
                return web_venice.topup_solana(self.server, account, body["quoteId"], body["approvalHash"])
            raise AssertionError(action)

        agent = wg.WebAgent(allowance=Mock(), shop=shop, store=wg.ChatStore(Path(self.tmp.name) / "web.db"),
                            classify=lambda text: "chat")
        agent.solana = self.lane
        first = agent.message(ACCOUNT, None, "hello")
        card = first["messages"][1]["cards"][0]
        self.assertEqual((card["type"], card["amount"], card["quoteId"]), ("venice_topup", "5", "q1"))
        reply = agent.action(ACCOUNT, first["chatId"], {"type": "venice_topup", "quoteId": card["quoteId"],
                                                         "approvalHash": card["approvalHash"]})["messages"][0]
        self.assertTrue(reply["text"].startswith("Added 5 USDC of Venice credit."))
        self.assertIn("Hi from Venice on Solana", reply["text"])
        self.assertEqual(reply["cards"][0]["type"], "usage")

    def test_the_agent_sets_solana_limits_and_asks_the_wallet_to_approve(self):
        agent = wg.WebAgent(allowance=Mock(), shop=None, store=wg.ChatStore(Path(self.tmp.name) / "web.db"),
                            classify=lambda text: "set_limits")
        agent.solana = self.lane
        agent.setup = lambda account, body: self.lane.setup(account, body["dailyCap"], body["perPurchaseCap"], body.get("days"))
        reply = agent.message(ACCOUNT, None, "Set a $20 daily limit, $5 per purchase")["messages"][1]
        self.assertEqual((reply["cards"][0]["type"], reply["cards"][0]["kind"]), ("wallet", "grant"))
        agent.classify = lambda text: "buy_tool"
        self.assertIn("sold on Base", agent.message(ACCOUNT, None, "buy crypto news")["messages"][1]["text"])


if __name__ == "__main__":
    unittest.main()


class SolanaRoutesTests(unittest.TestCase):
    """The page's allowance routes for a Solana wallet: sign in, limits, approve in the wallet."""

    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from sign402_gateway import web_api as wa
        from sign402_gateway.solana_keys import b58encode
        from sign402_gateway.web_accounts import WebAccountStore, WebAuth
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = Ed25519PrivateKey.generate()
        self.b58 = b58encode
        self.address = b58encode(self.key.public_key().public_bytes_raw())
        self.bridge = FakeBridge()
        self.lane = SolanaAllowanceService(
            store=SolanaAllowanceStore(Path(self.tmp.name) / "sol.db"), bridge=self.bridge,
            fernet=Fernet(Fernet.generate_key()), fee_payer_key=lambda: "FEE", max_daily=100_000_000,
            max_per_purchase=25_000_000, max_days=90, max_grant=300_000_000)
        store = WebAccountStore(Path(self.tmp.name) / "web.db")
        self.api = wa.WebApi(WebAuth(store, domain="app.test", uri="https://app.test", allowed=None), Mock())
        self.api.solana = self.lane

    def call(self, method, path, body=None, **auth):
        return self.api.handle(method, path, body=body or {}, client="198.51.100.7", **auth)

    def test_a_solana_wallet_sets_limits_and_approves_through_the_same_routes(self):
        cookie = self._cookie()
        status = self.call("GET", "/allowance", token=cookie["token"], csrf=None)[1]
        self.assertEqual((status["chain"], status["configured"]), ("solana", False))
        setup = self.call("POST", "/allowance/setup", {"dailyCap": "20", "perPurchaseCap": "5", "days": "30"},
                          token=cookie["token"], csrf=cookie["csrf"])[1]
        self.assertEqual(setup["limiter"], self.lane.agent_key(f"solana:{self.address}")[0])
        prepared = self.call("POST", "/allowance/grant/prepare", {"amount": "20"}, token=cookie["token"], csrf=cookie["csrf"])[1]
        self.assertEqual((prepared["chain"], prepared["transaction"]), ("solana", "BASE64TX"))
        done = self.call("POST", "/allowance/grant/submit", {"operation": prepared["operation"], "transaction": "SIGNED"},
                         token=cookie["token"], csrf=cookie["csrf"])[1]
        self.assertEqual(done["state"], "DONE")
        self.assertEqual(self.call("GET", "/allowance", token=cookie["token"], csrf=None)[1]["state"], "granted")

    def test_a_solana_wallet_short_of_usdc_is_told_before_anything_is_set(self):
        self.bridge.owner_usdc = 500_000
        cookie = self._cookie()
        from sign402_gateway.web_api import WebError
        with self.assertRaisesRegex(WebError, "at least 1 USDC on Solana"):
            self.call("POST", "/allowance/setup", {"dailyCap": "20", "perPurchaseCap": "5"}, token=cookie["token"], csrf=cookie["csrf"])

    def _cookie(self):
        issued = self.call("POST", "/auth/nonce", {"address": self.address}, token="", csrf=None)[1]
        status, body, headers = self.api.handle("POST", "/auth/verify", body={
            "message": issued["message"], "signature": self.b58(self.key.sign(issued["message"].encode()))},
            client="198.51.100.7", token="", csrf=None)
        token = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        return {"token": token, "csrf": body["csrfToken"]}
