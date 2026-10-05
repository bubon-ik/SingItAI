"""The calling pilot through our own Bland account: listed numbers only, written down first, never retried.

Nothing here reaches Bland: its HTTP answers are faked.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sign402_gateway import bland_calls, web_actions, web_agent as wg
from sign402_gateway.agent_allowance import AllowanceError

BASE = "wallet:0x1111111111111111111111111111111111111111"
SOLANA = "solana:BTXXtaRQfzd7BF6zADrMtqDeDhdiiP3t2WYXHz3hCCSK"
MINE = "+420773173967"


class FakeBland:
    def __init__(self):
        self.requests, self.fail, self.reply = [], None, {"status": "success", "call_id": "b1f2c3d4-e5f6", "message": "ok"}
        self.details = {"completed": True, "status": "completed", "answered_by": "human", "call_length": 0.9,
                        "summary": "They said it is sunny.", "transcripts": [
                            {"user": "assistant", "text": "Dobrý den, tady je asistent."},
                            {"user": "user", "text": "Je slunečno."}, {"user": "agent-action", "text": "ended"}]}

    def __call__(self, method, url, headers, body=None):
        self.requests.append((method, url, dict(headers), body))
        if self.fail:
            raise self.fail
        return (200, dict(self.reply)) if method == "POST" else (200, dict(self.details))


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = [1_790_700_000]
        self.bland = FakeBland()
        self.calls = bland_calls.BlandCalls(api_key="KEY", allowed={MINE}, store=bland_calls.CallStore(Path(self.tmp.name) / "c.db"),
                                            http=self.bland, now=lambda: self.clock[0])

    def start(self, account=BASE, phone=MINE, language="Czech"):
        return self.calls.start(account, phone, "Ask what the weather is like.", language, "INSTRUCTIONS")

    def test_a_listed_number_is_called_once_disclosed_unrecorded_and_short(self):
        started = self.start()
        method, url, headers, body = self.bland.requests[0]
        self.assertEqual((method, url, headers), ("POST", "https://api.bland.ai/v1/calls", {"authorization": "KEY"}))
        self.assertEqual((body["phone_number"], body["language"], body["max_duration"], body["record"]), (MINE, "cs", 3, False))
        self.assertEqual(body["voicemail"], {"action": "hangup"})
        self.assertIn("asistent s umělou inteligencí", body["first_sentence"])
        self.assertEqual(body["task"], "INSTRUCTIONS")
        self.assertNotIn("retry", body)  # Bland can redial on voicemail; a pilot call never does
        self.assertNotIn("from", body)   # Bland's own numbers, without our Twilio
        self.assertTrue(started["callId"].startswith("bland:call_") and started["pilot"])
        self.assertEqual(body["metadata"], {"order_id": started["callId"][len("bland:"):]})

    def test_our_twilio_is_used_when_configured(self):
        self.calls.twilio_key, self.calls.from_number = "ENC", "+420222000111"
        self.start()
        _, _, headers, body = self.bland.requests[0]
        self.assertEqual((headers["encrypted_key"], body["from"]), ("ENC", "+420222000111"))

    def test_an_unlisted_number_is_never_called(self):
        with self.assertRaisesRegex(AllowanceError, "not in the calling pilot"):
            self.start(phone="+420222316265")
        self.assertEqual(self.bland.requests, [])

    def test_a_lost_answer_is_not_repeated_and_blocks_the_next_call_for_a_while(self):
        self.bland.fail = ConnectionError()
        with self.assertRaisesRegex(AllowanceError, "may or may not ring. It was not repeated"):
            self.start()
        self.bland.fail = None
        with self.assertRaisesRegex(AllowanceError, "still going"):
            self.start()
        self.assertEqual(len(self.bland.requests), 1)
        self.clock[0] += bland_calls.ACTIVE_SECONDS + 1
        self.start()  # later, a new call may be asked for

    def test_a_refusal_says_nothing_rang_and_three_a_day(self):
        self.bland.reply = {"status": "error", "message": "Insufficient balance for international calls"}
        with self.assertRaisesRegex(AllowanceError, "Insufficient balance.*Nothing rang"):
            self.start()
        self.bland.reply = {"status": "success", "call_id": "b1f2c3d4-e5f6"}
        for _ in range(2):
            started = self.start()
            self.calls.status(BASE, started["callId"])  # completed: the next may start
        with self.assertRaisesRegex(AllowanceError, "today's 3 calls"):
            self.start()

    def test_the_result_is_read_only_by_the_account_that_asked(self):
        started = self.start()
        result = self.calls.status(BASE, started["callId"])
        self.assertEqual(self.bland.requests[-1][:2], ("GET", "https://api.bland.ai/v1/calls/b1f2c3d4-e5f6"))
        self.assertEqual((result["completed"], result["answeredBy"], result["summary"]), (True, "human", "They said it is sunny."))
        self.assertEqual(result["transcript"], "Assistant: Dobrý den, tady je asistent.\nThem: Je slunečno.")
        with self.assertRaisesRegex(AllowanceError, "Unknown call"):
            self.calls.status(SOLANA, started["callId"])

    def test_the_pilot_is_on_only_with_its_switch_key_and_numbers(self):
        env = {bland_calls.ENABLED_ENV: "1", bland_calls.KEY_ENV: "K", bland_calls.ALLOWED_ENV: "+420 773 173 967, junk",
               bland_calls.DB_ENV: str(Path(self.tmp.name) / "e.db")}
        self.assertEqual(bland_calls.from_env(env).allowed, {MINE})
        self.assertTrue(bland_calls.pilot_accepts("+420 773 173 967", env))
        self.assertFalse(bland_calls.pilot_accepts("+420222316265", env))
        self.assertIsNone(bland_calls.from_env({**env, bland_calls.KEY_ENV: ""}))
        self.assertIsNone(bland_calls.from_env({**env, bland_calls.ENABLED_ENV: "0"}))
        self.assertFalse(bland_calls.pilot_accepts(MINE, {**env, bland_calls.ENABLED_ENV: "0"}))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pilot = Mock()
        self.pilot.accepts.side_effect = lambda phone: phone == MINE
        self.pilot.start.return_value = {"ok": True, "callId": "bland:call_x", "phone": MINE, "costUsd": "0", "pilot": True}
        patcher = patch.object(bland_calls, "from_env", return_value=self.pilot)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_pilot_number_goes_to_bland_from_either_wallet_and_nothing_is_paid(self):
        with patch.object(web_actions, "pay_once") as pay:
            started = web_actions.start_call(SimpleNamespace(), Mock(), SOLANA, "+420 773 173 967", "Ask the weather.", "Czech")
            pay.assert_not_called()
        self.assertEqual(started["callId"], "bland:call_x")
        args = self.pilot.start.call_args.args
        self.assertEqual(args[:4], (SOLANA, MINE, "Ask the weather.", "Czech"))
        self.assertIn("Speak Czech", args[4])  # the same AI rules as every call
        with self.assertRaisesRegex(AllowanceError, "only \\+1"):
            web_actions.start_call(SimpleNamespace(), Mock(), BASE, "+420222316265", "Ask.", "Czech")
        web_actions.call_status(SimpleNamespace(), SOLANA, "bland:call_x")
        self.pilot.status.assert_called_once_with(SOLANA, "bland:call_x")

    def test_the_agent_drafts_a_pilot_call_from_a_solana_wallet_as_free(self):
        lane = Mock()
        lane.status.return_value = {"configured": True, "state": "granted", "limiter": "x", "dailyCapAtomic": 1,
                                    "perPurchaseCapAtomic": 1, "remainingTodayAtomic": 1}
        lane.stale_allowances.return_value = []
        agent = wg.WebAgent(allowance=lane, shop=Mock(), store=wg.ChatStore(Path(self.tmp.name) / "w.db"),
                            classify=lambda text: {"intent": "call"},
                            model=lambda m, json_mode=False, max_tokens=0: json.dumps(
                                {"phone": MINE, "place": "me", "task": "Ask the weather.", "language": "Czech"}))
        agent.solana = lane
        with patch.object(wg, "pilot_accepts", side_effect=lambda phone: phone == MINE):
            card = agent.message(SOLANA, None, f"call me {MINE} and ask the weather")["messages"][1]["cards"][0]
        self.assertEqual((card["type"], card["price"], card.get("pilot"), card["language"]), ("call_draft", "0", True, "Czech"))


if __name__ == "__main__":
    unittest.main()
