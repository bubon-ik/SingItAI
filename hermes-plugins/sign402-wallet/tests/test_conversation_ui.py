"""Conversation navigation, consent and async races; no HTTP or real payments."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_plugin import FakeClient, FakeEvent, FakeGateway, load_plugin


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.client = FakeClient()
        self.gateway = FakeGateway(adapter_key="telegram")
        self.calls = []
        self.status = {"ok": True, "hasPolicy": False, "model": "test-model", "modelLabel": "Test model",
                       "inputUsdPerMTok": .2, "outputUsdPerMTok": .4, "dailyCapUsdc": "5.00",
                       "remainingWindowUsdc": "0.00", "outstandingUsdc": "4.99"}
        self.approval = {"ok": True, "approved": True}
        self.on_approve = lambda: None
        self.on_answer = lambda: None
        self.client.execute_chat = self.execute_chat
        self.plugin._client_factory = lambda: self.client
        env = patch.dict(os.environ, {"SIGN402_TELEGRAM_ALLOWED_USERS": "*", "SIGN402_AI_CHAT_ENABLED": "1", "SIGN402_TELEGRAM_SIGN402_ONLY": "1", "TELEGRAM_BOT_TOKEN": ""})
        env.start()
        self.addCleanup(env.stop)

    def execute_chat(self, operation, identity, *, payload=None, user_access_token):
        self.calls.append((operation, str(identity.user_id), payload))
        if operation == "start":
            return dict(self.status)
        if operation == "approve-policy":
            self.on_approve()
            return self.approval
        if operation == "message":
            self.on_answer()
            return {"ok": True, "text": "Your paid answer", "costAtomic": 123, "outstandingAtomic": 4_989_000}
        if operation == "models":
            if payload and payload.get("model"):
                return {"ok": True, "chosen": payload["model"]}
            return {"ok": True, "models": [{"id": "new-model", "label": "New model", "inputUsdPerMTok": 1, "outputUsdPerMTok": 2}]}
        raise AssertionError(operation)

    def press(self, text, user="1045618308", chat="chat-1", kind="dm"):
        event = FakeEvent(text, user, chat_id=chat)
        event.source.chat_type = kind
        result = self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway)
        self.assertEqual(result["action"], "skip")
        return self.gateway.adapters["telegram"].sent[-1][1]

    def operations(self):
        return [op for op, _, _ in self.calls]

    def review(self, prompt="What is x402?"):
        self.press(prompt)
        self.press("Use this model")
        return self.press("$5 / day")

    def test_first_question_needs_model_budget_review_and_explicit_approval(self):
        text = self.review()
        self.assertEqual(self.operations(), ["start"])
        for term in ["Test model", "$5.00", "30 days", "00:00 UTC", "USDC on Base", "x402", "does not move money"]:
            self.assertIn(term, text)
        self.press("Request budget approval")
        self.assertEqual(self.operations(), ["start", "approve-policy", "message"])
        self.assertEqual(self.calls[-1][2], {"text": "What is x402?"})
        self.assertEqual(self.calls[-2][2], {"dailyCapAtomic": 5_000_000, "days": 30})
        self.assertNotIn("1045618308", self.plugin._CHAT_SETUP)

    def test_no_duplicate_saved_question_after_repeat_approval(self):
        self.review()
        self.press("Request budget approval")
        self.press("Request budget approval")
        self.assertEqual(self.operations().count("approve-policy"), 1)
        self.assertEqual(self.operations().count("message"), 1)

    def test_declined_or_missing_explicit_approval_never_sends_question(self):
        for result in [{"ok": False, "approved": False}, {"ok": True, "approved": False}, {"ok": True}]:
            with self.subTest(result=result):
                self.approval = result
                self.review()
                self.press("Request budget approval")
                self.assertNotIn("message", self.operations())

    def test_cancel_discards_question_without_calling_payment_service(self):
        self.review()
        self.press("/cancel")
        self.assertNotIn("1045618308", self.plugin._CHAT_SETUP)
        self.assertEqual(self.operations(), ["start"])

    def test_navigation_while_approval_waits_cancels_only_deferred_question(self):
        self.review()
        self.on_approve = lambda: self.press("/wallet")
        text = self.press("Request budget approval")
        self.assertNotIn("message", self.operations())
        self.assertIn("saved question was cancelled", text)
        self.assertFalse(self.plugin._CHAT_INFLIGHT)

    def test_navigation_after_message_started_preserves_paid_answer(self):
        self.status.update(hasPolicy=True, policyExpiresAt=4102444800)
        self.on_answer = lambda: self.press("/wallet")
        text = self.press("hello")
        self.assertIn("Your paid answer", text)
        self.assertEqual(self.operations(), ["start", "message"])

    def test_cancel_queued_question_before_worker_runs_never_sends_it(self):
        self.status.update(hasPolicy=True, policyExpiresAt=4102444800)
        scheduled = []
        self.plugin._background_runner = scheduled.append
        self.press("hello")
        self.press("/cancel")
        scheduled.pop()()
        self.assertNotIn("message", self.operations())
        self.assertFalse(self.plugin._CHAT_INFLIGHT)

    def test_navigation_cannot_start_second_paid_message_while_first_runs(self):
        self.status.update(hasPolicy=True, policyExpiresAt=4102444800)
        scheduled = []
        self.plugin._background_runner = scheduled.append
        self.press("first")
        self.press("/wallet")
        text = self.press("second")
        self.assertIn("still running", text)
        self.assertEqual(len(scheduled), 1)
        scheduled[0]()
        self.assertNotIn("message", self.operations())

    def test_approval_runs_in_background_and_repeat_tap_does_not_duplicate(self):
        self.review()
        scheduled = []
        self.plugin._background_runner = scheduled.append
        self.press("Request budget approval")
        self.press("Request budget approval")
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(self.operations(), ["start"])
        scheduled[0]()
        self.assertEqual(self.operations(), ["start", "approve-policy", "message"])

    def test_failed_schedule_releases_chat_guard(self):
        def fail(callback):
            raise RuntimeError("offline")
        self.plugin._background_runner = fail
        self.press("hello")
        self.assertFalse(self.plugin._CHAT_INFLIGHT)

    def test_status_failure_releases_chat_guard_and_never_pays(self):
        self.status["ok"] = False
        self.press("hello")
        self.assertFalse(self.plugin._CHAT_INFLIGHT)
        self.assertEqual(self.operations(), ["start"])

    def test_setup_expires_and_cannot_launch_old_question(self):
        self.review()
        self.plugin._CHAT_SETUP["1045618308"]["expires"] = 0
        self.press("Request budget approval")
        self.assertNotIn("approve-policy", self.operations())
        self.assertNotIn("message", self.operations())

    def test_setup_is_bounded(self):
        self.plugin._TELEGRAM_OPERATION_MAX_USERS = 3
        for i in range(10):
            self.plugin._new_chat_setup(SimpleNamespace(user_id=str(i)), SimpleNamespace(chat_id="one"), self.status, "secret draft")
        self.assertEqual(len(self.plugin._CHAT_SETUP), 3)
        self.assertNotIn("0", self.plugin._CHAT_SETUP)

    def test_setup_draft_is_not_shared_between_users_or_chats(self):
        self.review("alice's question")
        self.press("another question", user="999")
        self.assertEqual(self.plugin._CHAT_SETUP["1045618308"]["prompt"], "alice's question")
        self.assertEqual(self.plugin._CHAT_SETUP["999"]["prompt"], "another question")
        self.assertIsNone(self.plugin._chat_setup("1045618308", SimpleNamespace(chat_id="different")))
        self.assertNotIn("message", self.operations())

    def test_group_and_unknown_commands_never_reach_ai(self):
        self.press("hello", kind="group")
        self.press("/unknown")
        self.press("/model", kind="group")
        self.assertEqual(self.calls, [])

    def test_old_policy_without_cached_chat_mode_answers_free_text(self):
        self.status.update(hasPolicy=True, policyExpiresAt=4102444800)
        text = self.press("hello")
        self.assertIn("Your paid answer", text)
        self.assertNotIn("approve-policy", self.operations())

    def test_expired_policy_requires_setup(self):
        self.status.update(hasPolicy=True, policyExpired=True)
        text = self.press("hello")
        self.assertIn("Set up AI chat", text)
        self.assertNotIn("message", self.operations())

    def test_paused_policy_does_not_auto_retry(self):
        self.status.update(hasPolicy=True, paused=True, pauseReason="RECONCILIATION_REQUIRED")
        text = self.press("hello")
        self.assertIn("Chat paused", text)
        self.assertNotIn("message", self.operations())
        self.assertNotIn("approve-policy", self.operations())

    def test_model_selection_is_free_and_resumes_saved_setup(self):
        self.press("original question")
        text = self.press("/model new")
        self.assertIn("Choose a daily top-up limit", text)
        self.assertNotIn("message", self.operations())
        self.assertNotIn("approve-policy", self.operations())
        flow = self.plugin._CHAT_SETUP["1045618308"]
        self.assertEqual(flow["status"]["modelLabel"], "New model")
        self.assertEqual(flow["prompt"], "original question")
        self.assertEqual(flow["status"]["outputUsdPerMTok"], 2)

    def test_settings_distinguish_credit_from_topup_allowance(self):
        text = self.press("/chat")
        self.assertIn("credit (last checked): $4.99", text)
        self.assertIn("allowance today: $0.00 of $5.00", text)
        self.assertIn("$0.2 input", text)
        self.assertIn("$0.4 output", text)
        self.assertNotIn("message", self.operations())

    def test_phone_form_is_not_sent_to_ai(self):
        self.plugin._IMESSAGE_CONNECT_SESSIONS["1045618308"] = {"stage": "awaiting-phone", "channel": "whatsapp"}
        self.press("not a phone number")
        self.assertEqual(self.calls, [])

    def test_search_form_is_not_sent_to_ai(self):
        self.press("/shop")
        self.press("Search Products")
        self.press("alza")
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.client.bitrefill_search_calls), 1)
        self.assertEqual(self.client.bitrefill_calls, [])

    def test_explicit_shopping_phrase_only_searches_catalog(self):
        self.press("Купить Alza 200 CZK")
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.client.bitrefill_search_calls), 1)
        self.assertEqual(self.client.bitrefill_search_calls[0][0], "Alza")
        self.assertEqual(self.client.bitrefill_calls, [])

    def test_question_about_shopping_remains_an_ai_question(self):
        self.press("How can I buy a gift card?")
        self.assertEqual(self.operations(), ["start"])
        self.assertEqual(self.client.bitrefill_search_calls, [])

    def test_budget_review_cannot_clear_reconciliation_pause(self):
        self.status.update(hasPolicy=True, paused=True, pauseReason="RECONCILIATION_REQUIRED")
        text = self.press("/chat_budget")
        self.assertIn("payment status check", text)
        self.assertNotIn("1045618308", self.plugin._CHAT_SETUP)
        self.assertEqual(self.operations(), ["start"])

    def test_expired_setup_controls_are_never_paid_ai_prompts(self):
        self.status.update(hasPolicy=True, policyExpiresAt=4102444800)
        for text in ["Use this model", "$5 / day", "Request budget approval", "Change budget"]:
            self.press(text)
        self.assertEqual(self.calls, [])

    def test_cancel_queued_approval_prevents_phone_request(self):
        self.review()
        scheduled = []
        self.plugin._background_runner = scheduled.append
        self.press("Request budget approval")
        self.press("/cancel")
        scheduled[0]()
        self.assertEqual(self.operations(), ["start"])
        self.assertFalse(self.plugin._CHAT_INFLIGHT)

    def test_expiry_during_external_approval_does_not_send_question(self):
        self.review()
        def expire():
            self.plugin._CHAT_SETUP["1045618308"]["expires"] = 0
        self.on_approve = expire
        self.press("Request budget approval")
        self.assertEqual(self.operations(), ["start", "approve-policy"])
