"""Emails to themselves and phone calls: drafted by the agent, sent only by the user's press. Nothing leaves here."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from eth_account import Account

from sign402_gateway import web_actions, web_agent as wg
from sign402_gateway.agent_allowance import AllowanceError

BASE = "wallet:0x1111111111111111111111111111111111111111"
SOLANA = "solana:BTXXtaRQfzd7BF6zADrMtqDeDhdiiP3t2WYXHz3hCCSK"
AGENT = Account.create()


class ActionTests(unittest.TestCase):
    def setUp(self):
        web_actions._counts.clear()
        emails = Mock()
        emails.get_email.side_effect = lambda account: {BASE: "me@example.com"}.get(account)
        self.server = SimpleNamespace(buyer_email_store=emails, user_event_store=Mock(), allowance=Mock())
        self.server.allowance.agent_key.return_value = (AGENT.address, AGENT.key.to_0x_hex())
        self.pay = Mock(return_value=(20_000, {"success": True, "messageId": "m1"}, "0xtx"))
        patcher = patch.object(web_actions, "pay_once", self.pay)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_email_goes_only_to_the_saved_address_with_replies_to_them(self):
        sent = web_actions.send_email(self.server, Mock(), BASE, "Flights  to\nRome", "BER-FCO 15 Oct, 89 EUR")
        _, _, account, tool, method, url, body = self.pay.call_args.args
        self.assertEqual((account, tool.id, method, url), (BASE, "email", "POST", "https://stableemail.dev/api/send"))
        self.assertEqual((body["to"], body["replyTo"], body["subject"]), (["me@example.com"], "me@example.com", "Flights to Rome"))
        self.assertTrue(body["text"].startswith("BER-FCO 15 Oct, 89 EUR") and "Sent by your SingIt agent" in body["text"])
        self.assertEqual(sent, {"ok": True, "to": "me@example.com", "costUsd": "0.020"})
        self.assertTrue(self.pay.call_args.kwargs["record"])  # an action is a purchase in the list

    def test_no_saved_address_nothing_to_send_or_todays_emails_used_pays_nothing_more(self):
        with self.assertRaisesRegex(AllowanceError, "only send to your own address"):
            web_actions.send_email(self.server, Mock(), SOLANA, "x", "y")
        with self.assertRaisesRegex(AllowanceError, "nothing to send"):
            web_actions.send_email(self.server, Mock(), BASE, "x", "  ")
        for _ in range(web_actions.PER_DAY["email"]):
            web_actions.send_email(self.server, Mock(), BASE, "x", "y")
        with self.assertRaisesRegex(AllowanceError, "today's 10 emails"):
            web_actions.send_email(self.server, Mock(), BASE, "x", "y")
        self.assertEqual(self.pay.call_count, 10)

    def test_a_refused_payment_does_not_use_up_the_day(self):
        self.pay.side_effect = AllowanceError("over your limit")
        with self.assertRaises(AllowanceError):
            web_actions.send_email(self.server, Mock(), BASE, "x", "y")
        self.assertEqual(sum(web_actions._counts.values()), 0)

    def test_a_call_is_disclosed_as_an_ai_not_recorded_and_short(self):
        self.pay.return_value = (540_000, {"success": True, "call_id": "abc-123-def"}, "")
        started = web_actions.start_call(self.server, Mock(), BASE, "+420 123 456 789", "Book a table for 2 at 20:00 under Max.", "Czech")
        _, _, _, tool, method, url, body = self.pay.call_args.args
        self.assertEqual((tool.id, url, body["phone_number"]), ("call", "https://stablephone.dev/api/call", "+420123456789"))
        self.assertEqual((body["record"], body["max_duration"], body["voicemail_action"]), (False, 3, "hangup"))
        self.assertIn("Speak Czech", body["task"])
        self.assertIn("you are an AI assistant calling for a customer", body["task"])
        self.assertIn("Never agree to pay anything", body["task"])
        self.assertIn("Book a table for 2 at 20:00 under Max.", body["task"])
        self.assertEqual(started, {"ok": True, "callId": "abc-123-def", "phone": "+420123456789", "costUsd": "0.54"})

    def test_calls_need_a_base_wallet_and_a_full_number(self):
        with self.assertRaisesRegex(AllowanceError, "Base wallet"):
            web_actions.start_call(self.server, Mock(), SOLANA, "+420123456789", "hi")
        for number in ("123456", "911", "+0 123 456 789", "call me"):
            with self.assertRaisesRegex(AllowanceError, "country code"):
                web_actions.start_call(self.server, Mock(), BASE, number, "hi")
        self.pay.assert_not_called()

    def test_the_result_is_read_signed_in_as_the_paying_agent(self):
        seen = []

        def http(url, headers=None):
            seen.append((url, headers))
            if not headers:
                return 402, {"extensions": {"sign-in-with-x": {"info": {
                    "domain": "stablephone.dev", "uri": url, "version": "1", "chainId": "eip155:8453", "type": "eip191",
                    "nonce": "abc123def456", "issuedAt": "2026-09-29T17:42:37.907Z", "statement": "Sign in"},
                    "supportedChains": [{"chainId": "eip155:8453", "type": "eip191"}]}}}
            return 200, {"completed": True, "status": "completed", "answered_by": "human", "call_length": 1.4,
                         "summary": "Table for 2 at 20:00 booked under Max.",
                         "transcripts": [{"user": "assistant", "text": "Hello, I'm an AI assistant."},
                                         {"user": "user", "text": "Sure, 20:00 is free."}]}
        result = web_actions.call_status(self.server, BASE, "abc-123-def", http=http)
        self.assertEqual(seen[0][0], "https://stablephone.dev/api/call/abc-123-def")
        signed = json.loads(__import__("base64").b64decode(seen[1][1]["SIGN-IN-WITH-X"]))
        self.assertEqual(signed["address"], AGENT.address)
        self.assertEqual((result["summary"], result["completed"]), ("Table for 2 at 20:00 booked under Max.", True))
        self.assertEqual(result["transcript"], "Assistant: Hello, I'm an AI assistant.\nThem: Sure, 20:00 is free.")


GRANTED = {"configured": True, "state": "granted", "limiter": "0xLIM", "dailyCapAtomic": 20_000_000,
           "perPurchaseCapAtomic": 5_000_000, "remainingTodayAtomic": 20_000_000}


class AgentActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls, self.saved, self.model_reply = [], "", None

        def shop(action, account, body):
            self.calls.append((action, dict(body)))
            if action == "email-address":
                return 200, {"ok": True, "email": self.saved}
            if action == "email-address-set":
                self.saved = body["email"]
                return 200, {"ok": True, "email": self.saved}
            if action == "email-send":
                return 200, {"ok": True, "to": self.saved, "costUsd": "0.020"}
            if action == "call-start":
                return 200, {"ok": True, "callId": "abc-123-def", "phone": body["phone"], "costUsd": "0.54"}
            if action == "call-status":
                return 200, {"ok": True, "completed": True, "summary": "Booked for 20:00.", "transcript": "Them: ok",
                             "answeredBy": "human"}
            if action == "venice-chat":
                return 200, {"ok": True, "text": "Nonstop BER-FCO on 15 Oct from 89 EUR."}
            raise AssertionError(action)
        lane = Mock()
        lane.status.return_value = dict(GRANTED)
        lane.stale_allowances.return_value = []
        self.agent = wg.WebAgent(allowance=lane, shop=shop, store=wg.ChatStore(Path(self.tmp.name) / "web.db"),
                                 classify=lambda text: {"intent": self.intent},
                                 model=lambda messages, json_mode=False, max_tokens=0: self.model_reply)

    def say(self, text, chat=None):
        reply = self.agent.message(BASE, chat, text)
        return reply["chatId"], reply["messages"][1]

    def test_an_email_is_drafted_from_the_chat_and_sent_only_on_the_press(self):
        self.intent = "chat"
        chat, _ = self.say("flights Berlin to Rome?")
        self.intent = "email_me"
        _, asked = self.say("email me that", chat)
        self.assertIn("Which email", asked["text"])
        self.model_reply = json.dumps({"subject": "Flights to Rome", "body": "Nonstop BER-FCO on 15 Oct from 89 EUR."})
        _, draft = self.say("it's me@example.com", chat)
        card = draft["cards"][0]
        self.assertEqual((card["type"], card["to"], card["body"]), ("email_draft", "me@example.com", "Nonstop BER-FCO on 15 Oct from 89 EUR."))
        self.assertNotIn("email-send", [a for a, _ in self.calls])  # nothing goes out on a message
        sent = self.agent.action(BASE, chat, {"type": "send_email"})["messages"][0]
        self.assertEqual(self.calls[-1], ("email-send", {"subject": "Flights to Rome", "text": "Nonstop BER-FCO on 15 Oct from 89 EUR."}))
        self.assertIn("Sent to me@example.com", sent["text"])
        again = self.agent.action(BASE, chat, {"type": "send_email"})["messages"][0]
        self.assertIn("expired or was already sent", again["text"])  # one press, one email

    def test_a_bare_address_is_saved_by_the_agent_and_a_changed_address_is_shown_before_sending(self):
        self.intent = "chat"
        self.saved = "old@example.com"
        chat, _ = self.say("flights Berlin to Rome?")
        self.intent = "email_me"
        self.model_reply = json.dumps({"subject": "Flights", "body": "Nonstop BER-FCO."})
        _, draft = self.say("email me that", chat)
        self.assertEqual(draft["cards"][0]["to"], "old@example.com")
        _, saved = self.say("new@example.com", chat)  # no model is asked: the app saves it and says so
        self.assertEqual(self.calls[-1], ("email-address-set", {"email": "new@example.com"}))
        self.assertIn("Saved: I'll send your emails to new@example.com", saved["text"])
        self.assertEqual(saved["cards"][0]["to"], "new@example.com")  # the waiting draft, now to the new address
        self.saved = "elsewhere@example.com"  # changed behind the card's back
        changed = self.agent.action(BASE, chat, {"type": "send_email"})["messages"][0]
        self.assertEqual(changed["cards"][0]["to"], "elsewhere@example.com")
        self.assertNotIn("email-send", [a for a, _ in self.calls])
        sent = self.agent.action(BASE, chat, {"type": "send_email"})["messages"][0]
        self.assertIn("Sent to elsewhere@example.com", sent["text"])

    def test_a_call_uses_only_a_number_the_user_wrote(self):
        self.intent = "call"
        self.model_reply = json.dumps({"phone": "+420999888777", "place": "Lokal", "task": "Book a table", "language": "Czech"})
        _, invented = self.say("call Lokal and book a table for two at 8pm")
        self.assertIn("What number should I call", invented["text"])  # the model's number was not theirs
        self.model_reply = json.dumps({"phone": "+420 222 316 265", "place": "Lokal", "task": "Book a table for 2 at 20:00.",
                                       "language": "Czech"})
        chat, draft = self.say("call Lokal +420 222 316 265 and book a table for two at 8pm")
        card = draft["cards"][0]
        self.assertEqual((card["type"], card["phone"], card["language"], card["price"]), ("call_draft", "+420222316265", "Czech", "0.54"))
        self.assertNotIn("call-start", [a for a, _ in self.calls])
        started = self.agent.action(BASE, chat, {"type": "start_call"})["messages"][0]
        self.assertEqual(self.calls[-1], ("call-start", {"phone": "+420222316265", "task": "Book a table for 2 at 20:00.",
                                                          "language": "Czech"}))
        self.assertEqual(started["cards"][0]["callId"], "abc-123-def")
        done = self.agent.action(BASE, chat, {"type": "call_status", "callId": "abc-123-def", "place": "Lokal"})["messages"][0]
        self.assertIn("Booked for 20:00.", done["text"])
        self.assertEqual(done["cards"][0]["transcript"], "Them: ok")

    def test_without_a_model_the_users_own_words_make_the_draft(self):
        self.agent.model = None
        self.intent = "chat"
        chat, _ = self.say("flights?")
        self.intent = "email_me"
        self.saved = "me@example.com"
        _, draft = self.say("email me that", chat)
        self.assertEqual(draft["cards"][0]["body"], "Nonstop BER-FCO on 15 Oct from 89 EUR.")
        self.assertEqual(wg.plain("**LH400** is `late`, see [FA](https://fa.com)"), "LH400 is late, see FA (https://fa.com)")
        call = wg.plan_call("call Lokal +420 222 316 265 and book a table for two at 8pm", [], None)
        self.assertEqual((call["phone"], call["task"], call["missing"]),
                         ("+420222316265", "call Lokal and book a table for two at 8pm", ""))

    def test_calls_from_a_solana_wallet_are_not_offered_yet(self):
        self.intent = "call"
        reply = self.agent.message(SOLANA, None, "call +420222316265")["messages"][1]
        self.assertIn("Base wallet", reply["text"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
