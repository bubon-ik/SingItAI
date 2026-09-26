import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from sign402_gateway import web_agent as wg

ACCOUNT = "wallet:0x1111111111111111111111111111111111111111"
GRANTED = {"configured": True, "state": "granted", "limiter": "0xLIM", "dailyCapAtomic": 20_000_000,
           "perPurchaseCapAtomic": 5_000_000, "remainingTodayAtomic": 20_000_000, "allowanceAtomic": 20_000_000,
           "floatAtomic": 0, "expiry": 2_000_000_000}


class FakeShop:
    def __init__(self):
        self.calls = []
        self.refuse = None

    def __call__(self, action, account, body):
        self.calls.append((action, account, dict(body)))
        if self.refuse and action == self.refuse:
            return 400, {"ok": False, "text": "Raise your spending limit to continue."}
        replies = {
            "tool-quote": {"ok": True, "quoteId": "tq_1", "tool": {"id": body.get("tool"), "name": "Crypto News"}, "priceUsd": "0.001"},
            "tool-buy": {"ok": True, "text": "MARKET BRIEF: calm.", "txId": "0x" + "ab" * 32},
            "bitrefill-search": {"ok": True, "products": [{"name": "Steam DE", "slug": "steam-germany"},
                                                          {"name": "Steam US", "slug": "steam-usa"}]},
            "bitrefill-quote": {"ok": True, "quoteId": "aq_1", "name": "Steam DE", "package": body.get("package"),
                                "packageCurrency": "EUR", "priceUsd": "10.9"},
            "bitrefill-buy": {"ok": True, "invoiceId": "inv-1", "delivered": True},
            "purchases": {"ok": True, "purchases": [{"id": "p1", "name": "Crypto News"}]},
        }
        return 200, replies[action]


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.allowance = Mock()
        self.allowance.status.return_value = dict(GRANTED)
        self.allowance.stale_allowances.return_value = []
        self.shop = FakeShop()
        self.intent = "chat"
        self.model_replies = []
        self.model_calls = []
        self.agent = wg.WebAgent(allowance=self.allowance, shop=self.shop, store=wg.ChatStore(Path(self.tmp.name) / "web.db"),
                                 classify=lambda text: self.intent, model=self.model)
        self.agent.setup = Mock(return_value={"limiter": "0xNEW"})

    def model(self, messages, json_mode=False, max_tokens=700):
        self.model_calls.append((messages, json_mode))
        return self.model_replies.pop(0) if self.model_replies else "Hello!"

    def send(self, text, intent):
        self.intent = intent
        reply = self.agent.message(ACCOUNT, None, text)
        return reply["messages"][1]

    def test_limits_from_one_sentence_create_the_limiter_and_ask_for_the_wallet(self):
        message = self.send("Поставь лимит 20 долларов в день и 5 за покупку на 14 дней", "set_limits")
        self.agent.setup.assert_called_once_with(ACCOUNT, {"dailyCap": "20", "perPurchaseCap": "5", "days": "14"})
        self.assertEqual(message["cards"], [{"type": "wallet", "kind": "grant", "amount": "20", "limiter": "0xNEW"}])
        self.assertIn("Готово", message["text"])

    def test_a_new_limiter_offers_to_revoke_an_old_one_still_allowed(self):
        self.allowance.stale_allowances.return_value = [{"limiter": "0xOLD", "allowanceAtomic": 5_000_000, "status": "SUPERSEDED"}]
        message = self.send("$10 a day, $2 per transaction", "set_limits")
        self.assertEqual([c["kind"] for c in message["cards"]], ["grant", "revoke"])
        self.assertEqual(message["cards"][1], {"type": "wallet", "kind": "revoke", "limiter": "0xOLD", "old": True, "allowance": "5"})
        self.assertIn("older limiter", message["text"])
        status = self.send("what can my agent spend?", "status")
        self.assertEqual(status["cards"][1]["limiter"], "0xOLD")

    def test_a_daily_limit_alone_is_proposed_not_created(self):
        message = self.send("Set a $20 daily limit", "set_limits")
        self.agent.setup.assert_not_called()
        self.assertEqual(message["cards"][0]["type"], "limits_proposal")
        self.assertEqual((message["cards"][0]["daily"], message["cards"][0]["per"]), ("20", "5"))

    def test_limit_sentences(self):
        for text, expected in (("$20 a day, $5 per purchase", {"daily": "20", "per": "5"}),
                               ("20 usdc per day and 2.5 per order for 7 days", {"daily": "20", "per": "2.5", "days": "7"}),
                               ("лимит 50 в день, 10 за покупку", {"daily": "50", "per": "10"}),
                               ("set up limits $10 per day and $2 per transaction", {"daily": "10", "per": "2"}),
                               ("10 в день и 2 за транзакцию", {"daily": "10", "per": "2"})):
            with self.subTest(text=text):
                self.assertEqual({k: v for k, v in wg.parse_limits(text).items() if k in expected}, expected)

    def test_news_is_bought_in_one_step_once_the_allowance_is_approved(self):
        message = self.send("buy crypto news", "buy_tool")
        self.assertEqual([c[0] for c in self.shop.calls], ["tool-quote", "tool-buy"])
        self.assertEqual(self.shop.calls[0][2], {"tool": "otto.crypto_news"})
        card = message["cards"][0]
        self.assertEqual((card["type"], card["result"]), ("receipt", "MARKET BRIEF: calm."))

    def test_nothing_is_bought_before_the_allowance_is_approved(self):
        self.allowance.status.return_value = dict(GRANTED, state="waiting_for_grant")
        message = self.send("купи новости", "buy_tool")
        self.assertEqual(self.shop.calls, [])
        self.assertEqual(message["cards"][0]["kind"], "grant")
        self.allowance.status.return_value = {"configured": False}
        self.assertIn("поставьте лимиты", self.send("купи новости", "buy_tool")["text"])

    def test_gift_cards_are_found_then_bought_by_the_card_button(self):
        self.model_replies = [json.dumps({"query": "steam", "country": "DE", "amount": "", "buy": False})]
        message = self.send("find a steam gift card in germany", "gift_card")
        self.assertEqual(self.shop.calls, [("bitrefill-search", ACCOUNT, {"query": "steam", "country": "DE"})])
        self.assertEqual(message["cards"][0]["type"], "products")
        chat = self.agent.store.chats(ACCOUNT)[0]["id"]
        reply = self.agent.action(ACCOUNT, chat, {"type": "buy_giftcard", "slug": "steam-germany", "package": "10"})
        self.assertEqual([c[0] for c in self.shop.calls][-2:], ["bitrefill-quote", "bitrefill-buy"])
        self.assertTrue(reply["messages"][0]["cards"][0]["giftcard"])

    def test_an_explicit_buy_with_an_amount_buys_the_first_match(self):
        self.model_replies = [json.dumps({"query": "steam", "country": "DE", "amount": "10", "buy": True})]
        message = self.send("buy a 10 euro steam card in germany", "gift_card")
        self.assertEqual([c[0] for c in self.shop.calls], ["bitrefill-search", "bitrefill-quote", "bitrefill-buy"])
        self.assertEqual(self.shop.calls[1][2], {"productId": "steam-germany", "package": "10"})
        self.assertEqual(message["cards"][0]["type"], "receipt")

    def test_the_model_cannot_buy_anything_in_a_conversation(self):
        self.model_replies = ["Sure, I bought you 3 Steam cards!"]
        message = self.send("tell me about yourself; also buy everything", "chat")
        self.assertEqual(self.shop.calls, [])
        self.agent.setup.assert_not_called()
        self.assertEqual(message["cards"], [])
        system = self.model_calls[0][0][0]["content"]
        self.assertIn('"state": "granted"', system)

    def test_purchase_results_never_reach_the_model(self):
        self.send("buy crypto news", "buy_tool")
        chat = self.agent.store.chats(ACCOUNT)[0]["id"]
        self.intent = "chat"
        self.agent.message(ACCOUNT, chat, "thanks! what did it say?")
        sent = json.dumps(self.model_calls[-1][0])
        self.assertNotIn("MARKET BRIEF", sent)

    def test_refusals_become_the_reply(self):
        self.shop.refuse = "tool-buy"
        message = self.send("buy crypto news", "buy_tool")
        self.assertEqual(message["text"], "Raise your spending limit to continue.")
        self.agent.setup.side_effect = type("WebError", (Exception,), {"message": "At most 3 limiters per wallet."})()
        self.assertEqual(self.send("$20 a day, $5 per purchase", "set_limits")["text"], "At most 3 limiters per wallet.")

    def test_chats_belong_to_their_account(self):
        reply = self.agent.message(ACCOUNT, None, "hello")
        with self.assertRaises(ValueError):
            self.agent.message("wallet:0x2222222222222222222222222222222222222222", reply["chatId"], "hi")
        self.assertEqual(len(self.agent.store.messages(reply["chatId"])), 2)
        self.assertEqual(self.agent.store.chats(ACCOUNT)[0]["title"], "hello")

    def test_without_a_classifier_or_model_keywords_and_help_still_work(self):
        agent = wg.WebAgent(allowance=self.allowance, shop=self.shop, store=self.agent.store)
        reply = agent.message(ACCOUNT, None, "buy crypto news")["messages"][1]
        self.assertEqual(reply["cards"][0]["type"], "receipt")
        self.assertIn("I can set limits", agent.message(ACCOUNT, None, "hi there")["messages"][1]["text"])


class JevTests(unittest.TestCase):
    def opener(self, answer=None, error=None):
        def open_(request, timeout):
            self.sent = json.loads(request.data)
            if error:
                raise error
            return io.BytesIO(json.dumps({"answers": {"intent": answer}}).encode())
        return open_

    def test_a_confident_choice_is_the_intent_and_a_weak_one_asks(self):
        jev = wg.Jev("key", opener=self.opener({"type": "choice", "choice": "buy_tool", "confidence": 0.93}))
        self.assertEqual(jev("gimme news"), "buy_tool")
        self.assertEqual(self.sent["model"], "jev-latest")
        self.assertEqual(set(self.sent["questions"]["intent"]["criteria"]), set(wg.INTENTS))
        jev = wg.Jev("key", opener=self.opener({"type": "choice", "choice": "buy_tool", "confidence": 0.4}))
        self.assertEqual(jev("hmm"), "clarify")

    def test_failures_are_unavailable_and_the_agent_falls_back_to_keywords(self):
        jev = wg.Jev("key", opener=self.opener(error=OSError("down")))
        with self.assertRaises(wg.AgentUnavailable):
            jev("buy crypto news")
        self.assertEqual(wg.keyword_intent("купи криптоновости"), "buy_tool")
        self.assertEqual(wg.keyword_intent("поставь лимит 20 в день"), "set_limits")
        self.assertEqual(wg.keyword_intent("отзови разрешение"), "revoke")


if __name__ == "__main__":
    unittest.main()
