"""Bitrefill from a Solana wallet on the web: an MCP invoice in USDC on Solana, paid from the allowance."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.fernet import Fernet

from sign402_gateway import solana_bitrefill as sb
from sign402_gateway import web_agent as wg
from sign402_gateway.agent_allowance import AllowanceError
from sign402_gateway.solana_allowance import SolanaAllowanceService, SolanaAllowanceStore
from tests.test_solana_allowance import ACCOUNT, OWNER, FakeBridge

INVOICE = "c2b27180-610e-4132-af77-ad42fc0ac444"


class Bridge(FakeBridge):
    def run(self, user_id, payer, key, operation, fee_payer_key=None, **payload):
        if operation == "bitrefill-invoice-pay":
            self.calls.append((operation, payer, True, None, payload))
            self.charge(int(payload["maxAmount"]))
            return {"invoiceId": payload["invoiceId"], "state": "accepted", "transaction": "4" * 88}
        return super().run(user_id, payer, key, operation, fee_payer_key, **payload)


class FakeMcp:
    checkout_mode = "guest"

    def __init__(self, amount="9.44", status="complete"):
        self.calls, self.amount, self.status = [], amount, status

    def get_product_details(self, *, product_id, country):
        return {"productId": product_id, "name": "Alza CZ", "currency": "CZK", "requiredRecipientFields": [],
                "packages": [{"value": "200", "priceUsd": "9.44", "packageId": "200"}]}

    def quote_product(self, *, product_id, package_id, country, recipient):
        return {"productId": product_id, "name": "Alza CZ", "packageValue": package_id, "currency": "CZK",
                "priceUsd": "9.44", "requiredRecipientFields": []}

    def _call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"response": {"invoice_id": INVOICE, "invoice_access_token": "tok-secret", "payment_info": {"amount": self.amount},
                             "x402_payment_url": "https://api.bitrefill.com/x402/invoice/pay"}}

    def _normalize_invoice(self, payload):
        return payload["response"]

    _invoice_id = staticmethod(lambda invoice: invoice.get("invoice_id"))
    _invoice_access_token = staticmethod(lambda invoice: invoice.get("invoice_access_token", ""))
    _invoice_status = staticmethod(lambda invoice: invoice.get("invoice_status", ""))
    _orders = staticmethod(lambda invoice: invoice.get("orders", []))

    def invoice_status(self, *, invoice_id, invoice_access_token):
        self.calls.append(("get-invoice-by-id", {"invoice_id": invoice_id, "token": invoice_access_token}))
        return {"invoice_id": invoice_id, "invoice_status": self.status, "orders": [{"status": "delivered", "redemption_info": {
            "code": "ALZA-9999-8888", "instructions": "Enter the code ALZA-9999-8888 at checkout on alza.cz."}}]}


class SolanaBitrefillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = Bridge()
        self.lane = SolanaAllowanceService(
            store=SolanaAllowanceStore(Path(self.tmp.name) / "sol.db"), bridge=self.bridge,
            fernet=Fernet(Fernet.generate_key()), fee_payer_key=lambda: "FEE", max_daily=100_000_000,
            max_per_purchase=25_000_000, max_days=90, max_grant=300_000_000)
        self.lane.setup(ACCOUNT, "20", "10", "30")
        op = self.lane.prepare_wallet(ACCOUNT, "GRANT", amount="20")
        self.lane.submit_wallet(ACCOUNT, op["operation"], "SIGNED")
        self.mcp = FakeMcp()
        self.emails = {}
        self.events = Mock()
        self.server = SimpleNamespace(
            solana_allowance=self.lane, bitrefill_search_service=SimpleNamespace(bitrefill_client=self.mcp),
            user_event_store=self.events,
            buyer_email_store=SimpleNamespace(get_email=self.emails.get,
                                              set_email=lambda k, v: self.emails.__setitem__(k, v) or v))

    def buy(self):
        return sb.buy(self.server, ACCOUNT, "alza-czech-republic", "200", sleep=lambda s: None)

    def test_the_wallet_is_read_once_and_the_code_asked_for_often_at_first(self):
        self.emails[ACCOUNT] = "me@example.com"
        self.bridge.calls.clear()
        self.lane._seen.clear()
        answers = iter(["pending", "pending", "pending", "complete"])
        ready = self.mcp.invoice_status
        def status(**kw):
            if next(answers, "complete") == "pending":  # paid, the code not ready yet
                return {"invoice_id": kw["invoice_id"], "invoice_status": "pending", "orders": [{"status": "processing"}]}
            return ready(**kw)
        self.mcp.invoice_status = status
        clock, waits = [1000.0], []
        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds
        result = sb.buy(self.server, ACCOUNT, "alza-czech-republic", "200", sleep=sleep, now=lambda: clock[0])
        self.assertTrue(result["delivered"])
        self.assertEqual(waits, [1.5, 1.5, 1.5])  # a code ready after 4.5 s is seen then, not at 6 or 12 s
        reads = [c[0] for c in self.bridge.calls if c[0] == "allowance-state"]
        self.assertEqual(len(reads), 1)  # in spend(); no status() read before the order
        self.assertEqual(sb._poll_wait(19.9), 1.5)
        self.assertEqual(sb._poll_wait(20.0), 5)

    def test_without_an_email_nothing_is_ordered(self):
        with self.assertRaises(sb.NeedsEmail):
            self.buy()
        self.assertEqual(self.mcp.calls, [])

    def test_an_invoice_in_usdc_on_solana_is_pulled_within_limits_and_paid(self):
        self.emails[ACCOUNT] = "me@example.com"
        bought = self.buy()
        order = self.mcp.calls[0]
        self.assertEqual(order[0], "buy-products")
        self.assertEqual((order[1]["payment_method"], order[1]["email"]), ("usdc_solana", "me@example.com"))
        pay = [c for c in self.bridge.calls if c[0] == "bitrefill-invoice-pay"][0][4]
        self.assertEqual(pay, {"url": sb.PAY_URL, "invoiceId": INVOICE, "maxAmount": "9440000", "owner": OWNER})
        self.assertEqual((bought["priceUsd"], bought["delivered"]), ("9.44", True))
        self.assertEqual(len(bought["purchaseId"]), 24)  # the chat's receipt opens its code by this
        self.assertEqual(bought["howToUse"], "Enter the code … at checkout on alza.cz.")
        recorded = self.events.write.call_args.args[1]
        self.assertEqual((recorded["mode"], recorded["fulfillmentToken"]), ("bitrefill_mcp_solana", "tok-secret"))
        self.assertNotIn("ALZA-9999-8888", json.dumps(bought) + json.dumps(recorded))
        self.assertEqual(self.lane.status(ACCOUNT)["remainingTodayAtomic"], 20_000_000 - 9_440_000)

    def test_an_invoice_above_the_quote_or_the_limits_is_not_paid(self):
        self.emails[ACCOUNT] = "me@example.com"
        self.mcp.amount = "12.00"
        with self.assertRaisesRegex(AllowanceError, "above the price you were shown"):
            self.buy()
        self.lane.setup(ACCOUNT, "20", "5", "30")
        with self.assertRaisesRegex(AllowanceError, "does not fit your limits"):
            self.buy()
        self.assertEqual([c for c in self.bridge.calls if c[0] == "bitrefill-invoice-pay"], [])

    def test_the_code_is_shown_once_from_the_encrypted_token(self):
        event = {"mode": "bitrefill_mcp_solana", "invoiceId": INVOICE, "productName": "Alza CZ", "fulfillmentToken": "tok-secret",
                 "quoteId": INVOICE, "bitrefill": {}}
        shown = sb.reveal(self.server, event, ACCOUNT)
        self.assertIn("ALZA-9999-8888", shown["telegramText"])
        self.assertEqual(shown["fields"], [{"label": "Code", "value": "ALZA-9999-8888", "kind": "code"}])
        self.assertIn("alza.cz", shown["howToUse"])
        self.events.clear_fulfillment_token.assert_called_once()
        self.assertIn("already shown once", sb.reveal(self.server, {**event, "fulfillmentToken": ""}, ACCOUNT)["telegramText"])

    def test_the_agent_asks_the_email_once_then_finishes_the_purchase(self):
        def shop(action, account, body):
            if action in ("bitrefill-solana-buy", "buyer-email-set"):
                try:
                    if action == "buyer-email-set":
                        sb.set_email(self.server, account, body["email"])
                        return 200, {"ok": True}
                    return 200, sb.buy(self.server, account, body["productId"], body["package"], sleep=lambda s: None)
                except sb.NeedsEmail as exc:
                    return 409, {"ok": False, "error": "email_needed", "text": str(exc)}
            if action == "venice-chat":
                return 503, {"ok": False, "error": "chat_off"}
            raise AssertionError(action)

        agent = wg.WebAgent(allowance=Mock(), shop=shop, store=wg.ChatStore(Path(self.tmp.name) / "web.db"),
                            classify=lambda text: "chat")
        agent.solana = self.lane
        first = agent.message(ACCOUNT, None, "hi")  # a chat to hold the purchase
        asked = agent.action(ACCOUNT, first["chatId"], {"type": "buy_giftcard", "slug": "alza-czech-republic",
                                                         "package": "200", "name": "Alza CZ"})["messages"][0]
        self.assertIn("What email should I use?", asked["text"])
        done = agent.message(ACCOUNT, first["chatId"], "sure, me@example.com")["messages"][1]
        self.assertTrue(done["text"].startswith("Saved your email. Bought Alza CZ 200 CZK for 9.44 USDC on Solana."))
        self.assertEqual(self.emails[ACCOUNT], "me@example.com")
        self.assertEqual(done["cards"][0]["type"], "receipt")


if __name__ == "__main__":
    unittest.main()


class SolanaInternalRoutesTests(SolanaBitrefillTests):
    def test_the_gateway_routes_a_solana_accounts_bitrefill_calls_to_solana(self):
        from sign402_gateway import web_internal
        self.server.allowance = Mock()
        self.server.web_accounts = SimpleNamespace(account=lambda account: {"account_id": account})
        status, offered = web_internal.handle(self.server, "bitrefill-packages", {"account": ACCOUNT, "productId": "alza-czech-republic"})
        self.assertEqual((status, offered["packages"][0]["priceUsd"]), (200, "9.44"))
        status, reply = web_internal.handle(self.server, "bitrefill-solana-buy", {"account": ACCOUNT, "productId": "x", "package": "200"})
        self.assertEqual((status, reply["error"]), (409, "email_needed"))
        web_internal.handle(self.server, "buyer-email-set", {"account": ACCOUNT, "email": "me@example.com"})
        self.assertTrue(web_internal.handle(self.server, "buyer-email", {"account": ACCOUNT})[1]["hasEmail"])
        self.server.allowance.lane_for.assert_not_called()  # the Base lane is never asked
