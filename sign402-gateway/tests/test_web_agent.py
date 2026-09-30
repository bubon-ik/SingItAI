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
        self.venice = {"ok": True, "text": "Venice: hello!", "costAtomic": 900, "creditAtomic": 4_999_100}
        self.catalog = {"ok": True, "products": [
            {"slug": "isimo-colombia", "name": "Isimo Colombia", "type": "gift_card", "country": "CO", "categories": ["retail"]},
            {"slug": "steam-germany", "name": "Steam DE", "type": "gift_card", "country": "DE", "categories": ["games"]},
            {"slug": "vodafone-germany", "name": "Vodafone DE", "type": "phone_refill", "country": "DE",
             "categories": ["refill"], "needsRecipient": True}]}

    def __call__(self, action, account, body):
        self.calls.append((action, account, dict(body)))
        if self.refuse and action == self.refuse:
            return 400, {"ok": False, "text": getattr(self, "refusal", "Raise your spending limit to continue.")}
        if action == "venice-chat":
            return (200 if self.venice.get("ok") else 400), dict(self.venice)
        if action == "catalog-search":
            return 200, json.loads(json.dumps(self.catalog))
        replies = {
            "tool-quote": {"ok": True, "quoteId": "tq_1", "tool": {"id": body.get("tool"), "name": "Crypto News"}, "priceUsd": "0.001"},
            "tool-buy": {"ok": True, "text": "MARKET BRIEF: calm.", "txId": "0x" + "ab" * 32},
            "bitrefill-search": {"ok": True, "products": [{"name": "Steam DE", "slug": "steam-germany"},
                                                          {"name": "Steam US", "slug": "steam-usa"}]},
            "bitrefill-packages": {"ok": True, "slug": body.get("productId"), "recipientRequired": False,
                                   "packages": [{"value": "10", "currency": "EUR", "priceUsd": "10.9"},
                                                {"value": "25", "currency": "EUR", "priceUsd": "27.1"}]},
            "bitrefill-quote": {"ok": True, "quoteId": "aq_1", "name": "Steam DE", "package": body.get("package"),
                                "packageCurrency": "EUR", "priceUsd": "10.9"},
            "bitrefill-buy": {"ok": True, "invoiceId": "inv-1", "delivered": True,
                              "howToUse": "Show the barcode to the cashier."},
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
        self.hints = {}
        self.fit = None  # Jev's ranking: slug -> probability
        self.agent = wg.WebAgent(allowance=self.allowance, shop=self.shop, store=wg.ChatStore(Path(self.tmp.name) / "web.db"),
                                 classify=lambda text: {"intent": self.intent, **self.hints}, model=self.model,
                                 rank=self.rank)
        self.agent.setup = Mock(return_value={"limiter": "0xNEW"})

    def rank(self, text, options):
        self.ranked = dict(options)
        if self.fit is None:
            raise wg.AgentUnavailable("classifier")
        return self.fit

    def model(self, messages, json_mode=False, max_tokens=700):
        self.model_calls.append((messages, json_mode))
        return self.model_replies.pop(0) if self.model_replies else "Hello!"

    def send(self, text, intent):
        self.intent = intent
        reply = self.agent.message(ACCOUNT, None, text)
        return reply["messages"][1]

    def test_replacing_a_working_limiter_is_confirmed_on_a_card(self):
        message = self.send("Set a $50 daily limit, $10 per purchase", "set_limits")
        self.agent.setup.assert_not_called()
        card = message["cards"][0]
        self.assertEqual((card["type"], card["daily"], card["per"], card["replaces"]), ("limits_proposal", "50", "10", "0xLIM"))
        self.assertIn("Replace it?", message["text"])

    def test_limits_from_one_sentence_create_the_limiter_and_ask_for_the_wallet(self):
        self.allowance.status.return_value = {"configured": False}
        message = self.send("Поставь лимит 20 долларов в день и 5 за покупку на 14 дней", "set_limits")
        self.agent.setup.assert_called_once_with(ACCOUNT, {"dailyCap": "20", "perPurchaseCap": "5", "days": "14"})
        self.assertEqual(message["cards"], [{"type": "wallet", "kind": "grant", "amount": "20", "limiter": "0xNEW"}])
        self.assertIn("Готово", message["text"])

    def test_a_new_limiter_offers_to_revoke_an_old_one_still_allowed(self):
        self.allowance.stale_allowances.return_value = [{"limiter": "0xOLD", "allowanceAtomic": 5_000_000, "status": "SUPERSEDED"}]
        self.allowance.status.return_value = dict(GRANTED, state="paused")
        message = self.send("$10 a day, $2 per transaction", "set_limits")
        self.allowance.status.return_value = dict(GRANTED)
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
                               ("10 в день и 2 за транзакцию", {"daily": "10", "per": "2"}),
                               # What "Raise it to $24" under a product sends: the same number for both.
                               ("Set a $24 daily limit, $24 per purchase", {"daily": "24", "per": "24"}),
                               ("30 дневной лимит, 10 за покупку", {"daily": "30", "per": "10"})):
            with self.subTest(text=text):
                self.assertEqual({k: v for k, v in wg.parse_limits(text).items() if k in expected}, expected)
        self.assertNotIn("days", wg.parse_limits("30 дневной лимит, 10 за покупку"))  # "дневной" is not a number of days

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
        self.assertEqual(self.shop.calls[0], ("catalog-search", ACCOUNT,
                                              {"query": "steam", "country": "DE", "category": "", "productType": "gift_card"}))
        card = message["cards"][0]
        self.assertEqual(card["type"], "products")
        self.assertEqual(card["items"][1]["packages"][0], {"value": "10", "currency": "EUR", "priceUsd": "10.9"})
        self.assertEqual(card["items"][2], {"name": "Vodafone DE", "slug": "vodafone-germany", "needsRecipient": True})
        chat = self.agent.store.chats(ACCOUNT)[0]["id"]
        reply = self.agent.action(ACCOUNT, chat, {"type": "buy_giftcard", "slug": "steam-germany", "package": "10"})
        self.assertEqual([c[0] for c in self.shop.calls][-2:], ["bitrefill-quote", "bitrefill-buy"])
        self.assertTrue(reply["messages"][0]["cards"][0]["giftcard"])
        self.assertEqual(reply["messages"][0]["cards"][0]["howToUse"], "Show the barcode to the cashier.")

    def test_an_esim_is_searched_among_esims_by_the_place(self):
        self.model_replies = [json.dumps({"query": "eSIM", "country": "DE", "place": "Germany", "amount": "", "buy": False})]
        message = self.send("i wanna buy eSim for Germany", "esim")
        self.assertEqual(self.shop.calls[0][2], {"query": "", "country": "DE", "category": "", "productType": "esim"})
        self.assertEqual(message["cards"][0]["kind"], "esim")
        self.assertIn("eSIMs", message["text"])
        self.model_replies = [json.dumps({"query": "eSIM", "country": "", "place": "Europe", "amount": "", "buy": False})]
        self.send("esim for europe", "esim")
        searched = [body for action, _, body in self.shop.calls if action == "catalog-search"]
        self.assertEqual(searched[-1]["query"], "Europe")  # a region is searched by name
        self.assertEqual(wg.ESIM_WORDS.sub("", "eSIM data plan Europe").strip(), "Europe")

    def test_jev_keeps_what_fits_and_drops_what_does_not(self):
        self.model_replies = [json.dumps({"query": "steam", "country": "", "amount": "", "buy": False})]
        self.hints = {"country": "DE"}  # Jev read the country; the model missed it
        self.fit = {"steam-germany": 0.9, "isimo-colombia": 0.001, "vodafone-germany": 0.02, "none": 0.08}
        message = self.send("steam card for my nephew in berlin", "gift_card")
        self.assertEqual(self.shop.calls[0][2]["country"], "DE")
        self.assertIn("Isimo Colombia", self.ranked["isimo-colombia"])
        self.assertEqual([i["slug"] for i in message["cards"][0]["items"]], ["steam-germany"])

    def test_food_is_offered_as_gift_cards_for_food_in_that_country(self):
        self.hints = {"country": "CZ"}
        message = self.send("я хочу заказать еду в Чехии", "food")
        self.assertEqual(self.shop.calls[0][2], {"query": "", "country": "CZ", "category": "food", "productType": "gift_card"})
        self.assertIn("доставку еды", message["text"])
        self.assertNotIn("не могу", message["text"])  # what can be done comes first
        self.assertEqual(message["cards"][0]["places"], {"country": "CZ", "place": ""})
        self.hints = {}
        self.assertIn("В какой стране", self.send("хочу заказать еду", "food")["text"])
        # A city they named is where places to eat are looked for, not the whole country.
        self.model_replies = [json.dumps({"query": "", "country": "DE", "place": "Germany", "city": "Berlin"})]
        message = self.send("I'm hungry in Berlin", "food")
        self.assertEqual(message["cards"][0]["places"], {"country": "DE", "place": "Berlin"})
        # Nothing sold for food there: no dead end, places to eat are still offered.
        self.shop.catalog = {"ok": True, "products": []}
        self.model_replies = [json.dumps({"query": "", "country": "TH", "place": "Thailand", "city": "Bangkok"})]
        message = self.send("I'm hungry in Bangkok", "food")
        self.assertIn("places to eat", message["text"])
        self.assertEqual((message["cards"][0]["items"], message["cards"][0]["places"]), ([], {"country": "TH", "place": "Bangkok"}))

    def test_without_the_catalog_bitrefills_own_search_is_used(self):
        self.shop.catalog = {"ok": False, "error": "catalog_off", "products": []}
        self.model_replies = [json.dumps({"query": "steam", "country": "DE", "amount": "", "buy": False})]
        self.send("find a steam gift card in germany", "gift_card")
        self.assertEqual(self.shop.calls[1], ("bitrefill-search", ACCOUNT, {"query": "steam", "country": "DE", "kind": "gift-cards"}))

    def test_a_named_value_that_is_not_offered_is_shown_not_bought(self):
        self.model_replies = [json.dumps({"query": "steam", "country": "DE", "amount": "13", "buy": True})]
        message = self.send("buy a 13 euro steam card in germany", "gift_card")
        self.assertNotIn("bitrefill-buy", [c[0] for c in self.shop.calls])
        self.assertEqual(message["cards"][0]["type"], "products")

    def test_an_explicit_buy_with_an_amount_buys_the_first_match(self):
        self.model_replies = [json.dumps({"query": "steam", "country": "DE", "amount": "10", "buy": True})]
        message = self.send("buy a 10 euro steam card in germany", "gift_card")
        self.assertNotIn("bitrefill-buy", [c[0] for c in self.shop.calls])  # unranked: shown, not bought
        self.model_replies = [json.dumps({"query": "steam", "country": "DE", "amount": "10", "buy": True})]
        self.fit = {"steam-germany": 0.93, "isimo-colombia": 0.01, "none": 0.06}
        message = self.send("buy a 10 euro steam card in germany", "gift_card")
        self.assertEqual([c[0] for c in self.shop.calls if c[0] != "bitrefill-packages"][-3:],
                         ["catalog-search", "bitrefill-quote", "bitrefill-buy"])
        self.assertEqual(self.shop.calls[-2][2], {"productId": "steam-germany", "package": "10"})
        self.assertEqual(message["cards"][0]["type"], "receipt")

    def test_conversation_runs_on_venice_once_the_allowance_is_approved(self):
        self.shop.venice = {"ok": True, "text": "Sure, I bought you 3 Steam cards!", "topUpUsd": "5.00", "model": "zai-org-glm-5-2",
                            "modelLabel": "GLM 5.2", "promptTokens": 900, "completionTokens": 340, "costAtomic": 1100}
        message = self.send("tell me about yourself; also buy everything", "chat")
        self.assertEqual([c[0] for c in self.shop.calls], ["venice-chat"])  # talking buys nothing but its credit
        self.agent.setup.assert_not_called()
        self.assertEqual(self.model_calls, [])
        self.assertEqual(message["cards"], [{"type": "usage", "model": "GLM 5.2", "tokens": 1240, "costUsd": "0.0011"}])
        today = self.agent.store.usage(ACCOUNT, 0)
        self.assertEqual((today["messages"], today["tokens"], today["costAtomic"]), (1, 1240, 1100))
        self.assertEqual(today["models"][0]["label"], "GLM 5.2")
        sent = self.shop.calls[0][2]["messages"]
        self.assertIn('"state": "granted"', sent[0]["content"])
        self.assertEqual(sent[-1], {"role": "user", "content": "tell me about yourself; also buy everything"})

    def test_the_chosen_reply_language_wins_over_the_messages(self):
        self.agent.message(ACCOUNT, None, "привет", reply_language="en")
        self.assertIn("Always reply in English.", self.shop.calls[-1][2]["messages"][0]["content"])
        self.intent = "status"
        reply = self.agent.message(ACCOUNT, None, "what can my agent spend?", reply_language="ru")["messages"][1]
        self.assertIn("Вот что сейчас", reply["text"])

    def test_a_venice_refusal_is_the_reply_and_an_off_switch_falls_back(self):
        self.shop.venice = {"ok": False, "error": "chat_refused", "text": "Venice sells chat credit in 5.00 USDC top-ups."}
        self.assertEqual(self.send("hi", "chat")["text"], "Venice sells chat credit in 5.00 USDC top-ups.")
        self.shop.venice = {"ok": False, "error": "chat_off", "text": "off"}
        self.model_replies = ["Hello from the concierge"]
        self.assertEqual(self.send("hi", "chat")["text"], "Hello from the concierge")

    def test_before_the_allowance_the_concierge_talks_and_nothing_is_paid(self):
        self.allowance.status.return_value = {"configured": False}
        self.send("who are you?", "chat")
        self.assertEqual(self.shop.calls, [])
        self.assertIn("opens once their limits are approved", self.model_calls[0][0][0]["content"])

    def test_purchase_results_never_reach_the_model(self):
        self.send("buy crypto news", "buy_tool")
        chat = self.agent.store.chats(ACCOUNT)[0]["id"]
        self.intent = "chat"
        self.agent.message(ACCOUNT, chat, "thanks! what did it say?")
        sent = json.dumps(self.shop.calls[-1][2])
        self.assertIn("what did it say", sent)
        self.assertNotIn("MARKET BRIEF", sent)

    def test_refusals_become_the_reply(self):
        self.shop.refuse = "tool-buy"
        message = self.send("buy crypto news", "buy_tool")
        self.assertEqual(message["text"], "Raise your spending limit to continue.")
        self.agent.setup.side_effect = type("WebError", (Exception,), {"message": "At most 3 limiters per wallet."})()
        self.allowance.status.return_value = {"configured": False}
        self.assertEqual(self.send("$20 a day, $5 per purchase", "set_limits")["text"], "At most 3 limiters per wallet.")

    def test_a_solana_account_chats_and_looks_but_does_not_set_limits_or_buy(self):
        solana = "solana:7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        self.intent = "set_limits"
        reply = self.agent.message(solana, None, "Set a $20 daily limit, $5 per purchase")["messages"][1]
        self.assertIn("Solana", reply["text"])
        self.agent.setup.assert_not_called()
        self.allowance.status.assert_not_called()
        self.intent = "gift_card"
        self.model_replies = [json.dumps({"query": "steam", "country": "DE", "amount": "10", "buy": True})]
        self.fit = {"steam-germany": 0.95}
        reply = self.agent.message(solana, None, "buy a 10 euro steam card in germany")["messages"][1]
        self.assertTrue(reply["cards"][0]["readOnly"])
        self.assertNotIn("bitrefill-buy", [c[0] for c in self.shop.calls])

    def test_short_of_usdc_the_reply_offers_to_add_funds(self):
        self.allowance.status.return_value = {"configured": False}
        self.agent.setup.side_effect = type("WebError", (Exception,), {
            "message": "Your wallet needs at least 1 USDC on Base before we create a limiter for it.",
            "code": "owner_needs_usdc"})()
        reply = self.send("$20 a day, $5 per purchase", "set_limits")
        self.assertEqual(reply["cards"], [{"type": "add_funds"}])
        self.allowance.status.return_value = dict(GRANTED)
        self.shop.refuse = "tool-buy"
        self.shop.refusal = "Your limiter cannot fund 0.001 USDC now: it allows 0 USDC. Nothing was paid."
        self.assertEqual(self.send("buy crypto news", "buy_tool")["cards"], [{"type": "add_funds"}])

    def test_chats_belong_to_their_account(self):
        reply = self.agent.message(ACCOUNT, None, "hello")
        with self.assertRaises(ValueError):
            self.agent.message("wallet:0x2222222222222222222222222222222222222222", reply["chatId"], "hi")
        self.assertEqual(len(self.agent.store.messages(reply["chatId"])), 2)
        self.assertEqual(self.agent.store.chats(ACCOUNT)[0]["title"], "hello")

    def test_chats_are_pinned_renamed_and_archived_by_their_owner(self):
        first = self.agent.message(ACCOUNT, None, "first")["chatId"]
        second = self.agent.message(ACCOUNT, None, "second")["chatId"]
        store = self.agent.store
        self.assertTrue(store.update(ACCOUNT, first, pinned=True, title="Steam cards"))
        self.assertEqual([(c["id"], c["title"], c["pinned"]) for c in store.chats(ACCOUNT)],
                         [(first, "Steam cards", True), (second, "second", False)])
        self.assertFalse(store.update("wallet:0x2222222222222222222222222222222222222222", second, archived=True))
        store.update(ACCOUNT, first, archived=True)
        chat = store.chats(ACCOUNT)[0]
        self.assertEqual((chat["pinned"], chat["archived"]), (False, True))
        self.agent.message(ACCOUNT, first, "back again")
        self.assertFalse(next(c for c in store.chats(ACCOUNT) if c["id"] == first)["archived"])

    def test_without_a_classifier_or_model_keywords_and_help_still_work(self):
        agent = wg.WebAgent(allowance=self.allowance, shop=self.shop, store=self.agent.store)
        reply = agent.message(ACCOUNT, None, "buy crypto news")["messages"][1]
        self.assertEqual(reply["cards"][0]["type"], "receipt")
        self.allowance.status.return_value = {"configured": False}
        self.assertIn("I can set limits", agent.message(ACCOUNT, None, "hi there")["messages"][1]["text"])


class JevTests(unittest.TestCase):
    def opener(self, answer=None, error=None):
        def open_(request, timeout):
            self.sent = json.loads(request.data)
            if error:
                raise error
            answers = answer if isinstance(answer, dict) and "intent" in answer else {"intent": answer}
            return io.BytesIO(json.dumps({"answers": answers}).encode())
        return open_

    def test_a_confident_choice_is_the_intent_and_a_weak_one_asks(self):
        jev = wg.Jev("key", opener=self.opener({"type": "choice", "choice": "buy_tool", "confidence": 0.93}))
        self.assertEqual(jev("gimme news"), {"intent": "buy_tool", "country": "", "category": ""})
        self.assertEqual(self.sent["model"], "jev-latest")
        self.assertEqual(set(self.sent["questions"]["intent"]["criteria"]), set(wg.INTENTS))
        self.assertLessEqual(len(self.sent["questions"]["country"]["criteria"]), 255)  # Jev's limit per choice
        jev = wg.Jev("key", opener=self.opener({"type": "choice", "choice": "buy_tool", "confidence": 0.4}))
        self.assertEqual(jev("hmm")["intent"], "clarify")

    def test_country_and_kind_of_shop_come_with_the_intent_and_products_are_ranked(self):
        choice = lambda c, p: {"type": "choice", "choice": c, "confidence": p}
        jev = wg.Jev("key", opener=self.opener({"intent": choice("esim", 0.95), "country": choice("DE", 0.9),
                                                "category": choice("mobile", 0.5)}))
        self.assertEqual(jev("internet in berlin"), {"intent": "esim", "country": "DE", "category": ""})
        jev = wg.Jev("key", opener=self.opener({"intent": choice("esim", 0.95), "best": {
            "type": "choice", "choice": "a", "confidence": 0.8, "probabilities": {"a": 0.8, "b": 0.05, "none": 0.15}}}))
        self.assertEqual(jev.rank("x", {"a": "A", "b": "B"}), {"a": 0.8, "b": 0.05, "none": 0.15})
        self.assertIn("none", self.sent["questions"]["best"]["criteria"])

    def test_failures_are_unavailable_and_the_agent_falls_back_to_keywords(self):
        jev = wg.Jev("key", opener=self.opener(error=OSError("down")))
        with self.assertRaises(wg.AgentUnavailable):
            jev("buy crypto news")
        self.assertEqual(wg.keyword_intent("купи криптоновости"), "buy_tool")
        self.assertEqual(wg.keyword_intent("поставь лимит 20 в день"), "set_limits")
        self.assertEqual(wg.keyword_intent("отзови разрешение"), "revoke")


if __name__ == "__main__":
    unittest.main()
