import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from eth_account import Account
from eth_account.messages import encode_defunct

from sign402_gateway import goplausible, web_internal, web_venice
from sign402_gateway.chat_store import ChatStore
from sign402_gateway.server import _validate_base_usdc_x402_requirement
from sign402_gateway.venice_chat import ChatService, UnknownModel, VeniceConfig, VeniceModelCatalogue

ACCOUNT = "wallet:0x1111111111111111111111111111111111111111"
VENICE_PAY_TO = "0x2670B922ef37C7Df47158725C0CC407b5382293F"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
AGENT = Account.create()
ROW = {"limiter_address": "0xLIM", "daily_cap": 20_000_000, "per_purchase_cap": 5_000_000, "expiry": 4_000_000_000}


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


class FakeVenice:
    """Venice's x402 top-up, balance and chat endpoints, as the web chat meets them."""

    def __init__(self, balance=0):
        self.balance, self.requests, self.signers = balance, [], []

    def __call__(self, method, url, *, headers=None, json_body=None):
        self.requests.append((method, url, json_body))
        envelope = json.loads(base64.b64decode(headers["X-Sign-In-With-X"]))
        self.signers.append(Account.recover_message(encode_defunct(text=envelope["message"]),
                                                    signature=envelope["signature"]))
        if "/x402/balance/" in url:
            return FakeResponse(200, {"data": {"balanceUsd": self.balance / 1e6, "canConsume": self.balance >= 100_000}})
        if url.endswith("/x402/top-up"):
            return FakeResponse(402, {"x402Version": 2, "accepts": [{
                "scheme": "exact", "network": "eip155:8453", "amount": "5000000", "asset": USDC,
                "payTo": VENICE_PAY_TO, "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}}]})
        if url.endswith("/chat/completions"):
            self.balance -= 1_000
            return FakeResponse(200, {"choices": [{"message": {"content": "Hi! Venice here."}}],
                                      "usage": {"prompt_tokens": 120, "completion_tokens": 30}},
                                {"X-Balance-Remaining": str(self.balance / 1e6)})
        raise AssertionError(url)


class WebVeniceTests(unittest.TestCase):
    def setUp(self):
        store = ChatStore(":memory:")
        self.addCleanup(store.close)
        config = VeniceConfig(bound_pay_to=VENICE_PAY_TO.lower(), network="eip155:8453", asset=USDC,
                              chunk_atomic=5_000_000, max_outstanding_atomic=10_000_000, daily_cap_atomic=5_000_000)
        self.allowance = Mock()
        self.allowance.lane_for.return_value = dict(ROW)
        self.allowance.agent_key.return_value = (AGENT.address, AGENT.key.to_0x_hex())
        self.server = SimpleNamespace(
            allowance=self.allowance, user_event_store=Mock(),
            chat_service=ChatService(
                store=store, client=SimpleNamespace(config=config, purchases_paused=lambda: False), wallet_service=None,
                daily_cap_atomic=5_000_000, catalogue=VeniceModelCatalogue(fetch=Mock(side_effect=OSError("offline")))))
        self.gw = SimpleNamespace(normalize_x402_payment_required=goplausible.normalize_x402_payment_required,
                                  _validate_base_usdc_x402_requirement=_validate_base_usdc_x402_requirement,
                                  _enforce_user_purchase_rate=Mock())
        self.venice = FakeVenice()
        self.pay = Mock(return_value={"ok": True, "txId": "0xabc"})
        self.patches = [patch.object(web_venice, "transport", self.venice),
                        patch.object(web_internal, "pay_from_allowance", self.pay),
                        patch.object(web_internal, "_limits_from_limiter", Mock())]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def chat(self, *texts):
        messages = [{"role": "system", "content": "You are SingIt."}]
        messages += [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(texts)]
        return web_venice.chat(self.server, self.gw, ACCOUNT, messages)

    def test_the_first_message_buys_venice_credit_from_the_limiter_then_answers(self):
        def paid(*args, **kwargs):
            self.venice.balance = 5_000_000  # the top-up landed
            return {"ok": True, "txId": "0xabc"}
        self.pay.side_effect = paid
        status, reply = self.chat("hello")
        self.assertEqual((status, reply["text"], reply["topUpUsd"]), (200, "Hi! Venice here.", "5.00"))
        _, _, account, tool, url, requirements = self.pay.call_args.args
        self.assertEqual((account, tool["id"], url), (ACCOUNT, "venice.credit", web_venice.TOPUP_URL))
        self.assertEqual((requirements["amountAtomic"], requirements["receiver"]), ("5000000", VENICE_PAY_TO))
        self.assertEqual(self.pay.call_args.kwargs["request_body"], {})
        self.assertEqual(set(self.venice.signers), {AGENT.address})  # Venice meters the agent, never a custodial wallet
        self.assertEqual((reply["modelLabel"], reply["promptTokens"], reply["completionTokens"]),
                         ("Venice Uncensored 1.2", 120, 30))
        self.server.user_event_store.summaries.return_value = [
            {"name": "Venice AI credit", "paid": "5 USDC", "recordedAt": "2026-09-27T10:00:00Z", "transactionUrl": "u"},
            {"name": "Crypto News", "paid": "0.001 USDC", "recordedAt": "x", "transactionUrl": ""}]
        status, usage = web_venice.usage(self.server, ACCOUNT)
        self.assertEqual(usage["topUps"], [{"at": "2026-09-27T10:00:00Z", "paid": "5 USDC", "transactionUrl": "u"}])
        self.assertEqual(usage["modelLabel"], "Venice Uncensored 1.2")

    def test_the_conversation_goes_to_venice_whole_and_credit_is_reused(self):
        self.venice.balance = 3_000_000
        status, reply = self.chat("what is x402?", "A payment protocol.", "and who uses it?")
        self.assertEqual(status, 200)
        self.assertNotIn("topUpUsd", reply)
        self.pay.assert_not_called()
        sent = next(body for method, url, body in self.venice.requests if url.endswith("/chat/completions"))
        self.assertEqual([m["role"] for m in sent["messages"]], ["system", "user", "assistant", "user"])

    def test_a_top_up_above_the_per_purchase_limit_is_refused_with_the_reason(self):
        self.allowance.lane_for.return_value = dict(ROW, per_purchase_cap=2_000_000)
        status, reply = self.chat("hello")
        self.assertEqual(status, 400)
        self.assertIn("5.00 USDC top-ups", reply["text"])
        self.pay.assert_not_called()

    def test_no_chat_before_the_allowance_and_none_when_the_feature_is_off(self):
        self.allowance.lane_for.return_value = None
        with self.assertRaises(web_venice.AllowanceUnavailable):
            self.chat("hello")
        self.server.chat_service = None
        self.assertEqual(self.chat("hello")[1]["error"], "chat_off")

    def test_the_account_picks_its_model_from_venices_list(self):
        status, listing = web_venice.models(self.server, ACCOUNT)
        self.assertEqual((status, listing["chosen"], listing["chosenLabel"]),
                         (200, "venice-uncensored-1-2", "Venice Uncensored 1.2"))
        prices = [m["outputUsdPerMTok"] for m in listing["models"]]
        self.assertEqual(prices, sorted(prices))  # cheapest first
        web_venice.choose_model(self.server, ACCOUNT, "grok-4-6")
        self.assertEqual(web_venice.models(self.server, ACCOUNT)[1]["chosenLabel"], "Grok 4.6")
        with self.assertRaises(UnknownModel):
            web_venice.choose_model(self.server, ACCOUNT, "gpt-9-imaginary")
        self.venice.balance = 3_000_000
        self.chat("hi")
        sent = next(body for method, url, body in self.venice.requests if url.endswith("/chat/completions"))
        self.assertEqual(sent["model"], "grok-4-6")

    def test_only_a_conversation_ending_with_the_user_is_sent(self):
        with self.assertRaises(ValueError):
            web_venice.chat(self.server, self.gw, ACCOUNT, [{"role": "tool", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
