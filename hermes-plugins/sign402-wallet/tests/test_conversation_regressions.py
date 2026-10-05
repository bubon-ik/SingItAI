"""Conversation transitions, cancellation and privacy boundaries, with fake I/O."""
import io
import json
import os
import time
import unittest
from unittest.mock import patch

import test_assistant as fixtures


class ConversationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AssistantTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.api = self.case.plugin
        self.assistant = self.api._natural_assistant
        self.Intent = self.case.router.Intent
        self.uid = "1045618308"

    def pending(self, stage="alternative", **kwargs):
        defaults = dict(action="food", country="US", category="food")
        defaults.update(kwargs)
        self.assistant.remember(self.uid, self.Intent(**defaults), stage)

    def reply(self, text, action="clarify", **kwargs):
        with self.case.decision(action, **kwargs):
            self.case.dispatch(text)

    def test_every_browsing_stage_allows_new_wallet_tasks(self):
        stages = ("menu", "select-category", "select-product", "select-package", "awaiting-country",
                  "awaiting-search", "loading-catalog", "loading-search", "loading-product")
        for stage in stages:
            for network in ("base", "solana"):
                with self.subTest(stage=stage, network=network):
                    self.api._BITREFILL_SESSIONS[self.uid] = {"stage": stage, "country": "US"}
                    self.reply("my balance", "balance", network=network, reply="new_task")
                    self.assertEqual(self.case.client.calls[-1][2], network)
                    self.assertNotIn(self.uid, self.api._BITREFILL_SESSIONS)
                    self.assistant.attempts.clear()
        self.assertFalse(self.case.client.bitrefill_calls)

    def test_every_pending_stage_allows_new_task(self):
        for stage in ("country", "alternative", "confirm-intent", "network"):
            for action in ("balance", "order_status", "limits", "unsupported", "food"):
                with self.subTest(stage=stage, action=action):
                    self.pending(stage, action="gift_card", country=None)
                    self.reply("another task", action, network="solana", country="DE", reply="new_task")
                    if action == "food":
                        current = self.assistant.pending[self.uid]
                        self.assertEqual((current[1].action, current[1].country), ("food", "DE"))
                    else:
                        self.assertNotIn(self.uid, self.assistant.pending)
                    self.assistant.attempts.clear()
        self.assertFalse(self.case.client.bitrefill_calls)
        self.assertFalse(self.case.client.withdraw_calls)

    def test_natural_acceptance_continues_alternative(self):
        for phrase in ("yes please", "sure, show them", "да, покажи", "давай посмотрим"):
            with self.subTest(phrase=phrase):
                self.api._BITREFILL_SESSIONS.clear()
                self.pending()
                self.reply(phrase, reply="accept")
                self.assertEqual(self.case.client.bitrefill_list_calls[-1][:2], ("US", "food"))
        self.assertFalse(self.case.client.bitrefill_calls)

    def test_category_all_can_explicitly_replace_food(self):
        self.pending()
        self.reply("all gift cards please", "gift_card", reply="continue", category="all", category_explicit=True)
        self.assertEqual(self.case.client.bitrefill_list_calls[-1][:2], ("US", "all"))

    def test_country_correction_does_not_approve_alternative(self):
        self.pending()
        self.reply("actually Germany", reply="continue", country="DE")
        self.assertFalse(self.case.client.bitrefill_list_calls)
        self.assertEqual(self.assistant.pending[self.uid][1].country, "DE")
        self.case.dispatch("Yes")
        self.assertEqual(self.case.client.bitrefill_list_calls[-1][:2], ("DE", "food"))

    def test_unclear_reply_keeps_question_and_original_expiry(self):
        for stage in ("country", "alternative", "confirm-intent", "network"):
            with self.subTest(stage=stage):
                self.pending(stage, action="balance" if stage == "network" else "food" if stage == "alternative" else "gift_card",
                             country=None, network="other" if stage == "network" else "unspecified")
                before = self.assistant.pending[self.uid][0]
                self.reply("hmm?", reply="unclear")
                self.assertEqual(self.assistant.pending[self.uid][0], before)
                self.assertEqual(self.assistant.pending[self.uid][2], stage)
        self.assertFalse(self.case.client.bitrefill_list_calls)

    def test_balance_network_question_accepts_both_network_buttons(self):
        for network in ("Base", "Solana"):
            self.reply("which balance?", "balance", network="other")
            with patch.object(self.case.router, "classify") as classify:
                self.case.dispatch(network)
            classify.assert_not_called()
            self.assertEqual(self.case.client.calls[-1][2], network.lower())

    def test_network_question_accepts_natural_reply(self):
        self.reply("wallet balance", "balance", network="other")
        self.reply("on Solana please", reply="continue", network="solana")
        self.assertEqual(self.case.client.calls[-1][2], "solana")

    def test_search_words_and_long_merchant_names_stay_search_queries(self):
        for query in ("Wolt", "The Coffee Bean and Tea Leaf"):
            self.api._BITREFILL_SESSIONS[self.uid] = {"stage": "awaiting-search", "country": "US"}
            self.api._BITREFILL_USER_COUNTRIES[self.uid] = "US"
            self.reply(query, reply="continue")
            self.assertEqual(self.case.client.bitrefill_search_calls[-1][:2], (query, "US"))

    def test_manual_country_prompt_accepts_country_name(self):
        self.api._BITREFILL_SESSIONS[self.uid] = {"stage": "awaiting-country"}
        self.reply("Germany", reply="continue", country="DE")
        self.assertEqual(self.api._BITREFILL_USER_COUNTRIES[self.uid], "DE")
        self.assertFalse(self.case.client.bitrefill_calls)

    def test_catalog_country_followup_retains_category(self):
        self.api._BITREFILL_SESSIONS[self.uid] = {"stage": "select-product", "country": "US", "category": "food"}
        self.reply("and in Germany?", reply="continue", country="DE")
        self.assertEqual(self.case.client.bitrefill_list_calls[-1][:2], ("DE", "food"))

    def test_checkout_values_never_reach_classifier(self):
        for stage in ("awaiting-recipient", "awaiting-buyer-email", "select-payment-token",
                      "loading-payment-tokens", "review-purchase", "purchasing"):
            with self.subTest(stage=stage):
                self.api._BITREFILL_SESSIONS[self.uid] = {"stage": stage}
                event = fixtures.FakeEvent("private checkout input", self.uid)
                with patch.object(self.case.router, "classify") as classify:
                    self.assistant.handle(event=event, source=event.source, gateway=self.case.gateway, api=self.api)
                classify.assert_not_called()

    def test_context_to_classifier_contains_no_session_secrets(self):
        self.api._BITREFILL_SESSIONS[self.uid] = {
            "stage": "select-product", "country": "US", "category": "food",
            "recipient": {"email": "private@example.invalid"}, "products": [{"secret": "redemption"}],
        }
        with self.case.decision("balance", network="base") as classify:
            self.case.dispatch("my balance")
        self.assertEqual(classify.call_args.kwargs["context"],
                         {"stage": "select-product", "country": "US", "category": "food", "action": "catalog"})

    def test_duplicate_inflight_message_does_not_use_rate_limit(self):
        jobs = []
        self.api._background_runner = jobs.append
        for _ in range(15):
            self.case.dispatch("my balance")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(self.assistant.attempts), 1)
        self.api._background_runner = lambda work: work()
        with self.case.decision("balance", network="base"):
            jobs[0]()
        self.assertEqual(len(self.case.client.calls), 1)

    def test_rate_limited_new_task_cancels_old_response(self):
        jobs = []
        self.api._background_runner = jobs.append
        self.case.dispatch("show US food cards")
        self.assistant.attempts = [(time.monotonic(), self.uid)] * 12
        self.case.dispatch("my balance")
        with self.case.decision("gift_card", country="US"):
            jobs[0]()
        self.assertFalse(self.case.client.bitrefill_list_calls)

    def test_failed_classifier_does_not_leave_cancelled_catalog_loading(self):
        for stage, restored in (("loading-catalog", "select-category"), ("loading-search", "awaiting-search"),
                                ("loading-product", "menu")):
            with self.subTest(stage=stage):
                self.api._BITREFILL_SESSIONS[self.uid] = {"stage": stage, "country": "US"}
                with patch.object(self.case.router, "classify", side_effect=self.case.router.RouterUnavailable()):
                    self.case.dispatch("my balance")
                self.assertEqual(self.api._BITREFILL_SESSIONS[self.uid]['stage'], restored)

    def test_semantic_cancel_and_decline_clear_context_without_payment(self):
        for reply in ("cancel", "decline"):
            self.pending()
            self.reply("never mind" if reply == "cancel" else "no thanks", reply=reply)
            self.assertFalse(self.assistant.pending)
            self.assertFalse(self.case.client.bitrefill_list_calls)
            self.assertFalse(self.case.client.bitrefill_calls)

    def test_recognized_different_task_wins_over_continue_label(self):
        for action in ("balance", "unsupported"):
            self.pending("country", action="gift_card", country=None)
            self.reply("different request", action, country="DE", network="base", reply="continue")
            self.assertNotIn(self.uid, self.assistant.pending)
        self.assertEqual(len(self.case.client.calls), 1)
        self.assertFalse(self.case.client.bitrefill_list_calls)

    def test_confident_wallet_task_wins_over_uncertain_context_relation(self):
        self.pending("country", action="gift_card", country=None)
        self.reply("my balance", "balance", network="base", reply="unclear")
        self.assertEqual(self.case.client.calls[-1][2], "base")
        self.assertFalse(self.assistant.pending)

    def test_explicit_gift_card_request_does_not_repeat_offer(self):
        self.pending()
        self.reply("show all gift card categories instead", "gift_card", category="all",
                   category_explicit=True, reply="unclear")
        self.assertEqual(self.case.client.bitrefill_list_calls[-1][:2], ("US", "all"))

    def test_merchant_food_classification_does_not_override_search_reply(self):
        self.api._BITREFILL_USER_COUNTRIES[self.uid] = "US"
        self.api._BITREFILL_SESSIONS[self.uid] = {"stage": "awaiting-search", "country": "US"}
        self.reply("Wolt", "food", reply="continue")
        self.assertEqual(self.case.client.bitrefill_search_calls[-1][:2], ("Wolt", "US"))
        self.assertFalse(self.assistant.pending)

    def test_expired_inflight_acceptance_does_not_resume_task(self):
        self.pending()
        jobs = []
        self.api._background_runner = jobs.append
        self.case.dispatch("yes please")
        with patch('time.monotonic', return_value=time.monotonic() + 1000):
            with self.case.decision("clarify", reply="accept"):
                jobs[0]()
        self.assertFalse(self.case.client.bitrefill_list_calls)

    def test_provider_context_is_enum_only_and_replies_are_validated(self):
        answers = {name: {"type": "choice", "choice": value, "confidence": .99}
                   for name, value in dict(intent="clarify", country="unknown", category="unspecified",
                                           network="unspecified", language="en", reply="accept").items()}
        payloads = []
        def opener(request, timeout):
            payloads.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"answers": answers}).encode())
        context = dict(stage="alternative", action="food", country="US", category="food",
                       recipient="private", network="secret-invalid-network")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            result = self.case.router.classify("yes please", context=context, opener=opener)
            self.assertEqual(result.reply, "accept")
            self.assertFalse(result.category_explicit)
            self.assertEqual(payloads[-1]["state"]["pending_task"],
                             dict(stage="alternative", action="food", country="US", category="food"))
            answers['reply']['confidence'] = .1
            self.assertEqual(self.case.router.classify("yes", context=context, opener=opener).reply, "unclear")
            answers['reply']['choice'] = 'buy-products'
            with self.assertRaises(self.case.router.RouterUnavailable):
                self.case.router.classify("yes", context=context, opener=opener)
