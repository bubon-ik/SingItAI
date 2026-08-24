import tempfile
import unittest
from pathlib import Path

from sign402_gateway.web_search import Verdict, classify


DAY_ONE_NOON = 1786_400_000 - (1786_400_000 % 86_400) + 43_200


class PreFilterAlwaysSearches(unittest.TestCase):
    """Rule 2: a present-tense marker decides on its own, with no model call."""

    def test_present_tense_marker(self):
        for message in (
            "what is the ETH price today",
            "who is the CEO of Base right now",
            "latest news on x402",
            "what happened at the Base summit",
            "current gas fees on Base",
            "price of USDC",
            "anything interesting this week?",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify(message), Verdict.SEARCH)

    def test_year_at_or_after_cutoff(self):
        self.assertEqual(
            classify("what shipped in 2026?", cutoff_year=2025), Verdict.SEARCH
        )

    def test_year_before_cutoff_is_not_a_marker(self):
        self.assertNotEqual(
            classify("what shipped in 1999?", cutoff_year=2025), Verdict.SEARCH
        )

    def test_bare_domain(self):
        self.assertEqual(classify("exa.ai"), Verdict.SEARCH)

    def test_bare_ticker(self):
        self.assertEqual(classify("$SIGN"), Verdict.SEARCH)


class PreFilterNeverSearches(unittest.TestCase):
    """Rule 1: short conversational messages, and subject-less follow-ups."""

    def test_short_and_conversational(self):
        for message in ("hi", "thanks!", "ok", "lol", "good morning", "what?"):
            with self.subTest(message=message):
                self.assertEqual(classify(message), Verdict.SKIP)

    def test_follow_up_without_a_new_subject(self):
        self.assertEqual(
            classify("why?", has_previous_turn=True), Verdict.SKIP
        )
        self.assertEqual(
            classify("go on", has_previous_turn=True), Verdict.SKIP
        )

    def test_a_follow_up_connective_needs_a_previous_turn(self):
        # The same words mean different things depending on whether there is
        # something to follow up on.
        self.assertEqual(
            classify("and the second one?", has_previous_turn=True), Verdict.SKIP
        )
        self.assertEqual(classify("and the second one?"), Verdict.ASK_MODEL)

    def test_rule_one_loses_to_a_present_tense_marker(self):
        # Short, but it names the present. Rule 2 must still win.
        self.assertEqual(classify("eth price now"), Verdict.SEARCH)


class PreFilterDefersToTheModel(unittest.TestCase):
    """Rule 3: everything else costs nothing to decide — the model decides."""

    def test_substantial_question_without_a_marker(self):
        self.assertEqual(
            classify("explain how EIP-712 typed data signing works"),
            Verdict.ASK_MODEL,
        )

    def test_empty_message_never_reaches_the_model(self):
        self.assertEqual(classify("   "), Verdict.SKIP)


if __name__ == "__main__":
    unittest.main()


BOUND_PAY_TO = "0x6d6e695b09861467c7d462f5aaf31cf3540b9192"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WALLET = "0x00000000000000000000000000000000000000aa"


def exa_challenge(*, pay_to=BOUND_PAY_TO, amount="7000"):
    """The shape Exa actually answers with, both legs included."""
    return {
        "x402Version": 2,
        "resource": {"url": "https://api.exa.ai/search"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": amount,
                "payTo": pay_to,
                "asset": USDC,
                "maxTimeoutSeconds": 60,
            },
            {
                "scheme": "exact",
                "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                "amount": amount,
                "payTo": "12Ec2cJmfR1C9uwejzxcuMhUgEC7wDrLgm1wBvvR5w9E",
                "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            },
        ],
    }


HITS = {
    "results": [
        {"url": "https://a.example", "title": "A", "text": "alpha", "score": 0.9},
        {"url": "https://b.example", "title": "B", "text": "beta", "score": 0.8},
    ]
}


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


class Recorder:
    """A settle callable that records what it was asked to pay for."""

    def __init__(self, payload=None, error=None):
        self.calls = []
        self.payload = payload if payload is not None else {
            "ok": True,
            "status": 200,
            "body": HITS,
            "transactionHash": "0xdead",
        }
        self.error = error

    def __call__(self, requirement, *, user_id, request_body):
        self.calls.append(
            {
                "requirement": requirement,
                "user_id": user_id,
                "request_body": request_body,
            }
        )
        if self.error is not None:
            raise self.error
        return self.payload


class SearchClientTest(unittest.TestCase):
    def build(
        self,
        *,
        challenge=None,
        status=402,
        gateway=None,
        user=None,
        purchases_paused=False,
        free_calls=5,
        max_per_day=20,
        transport=None,
    ):
        from sign402_gateway.web_search import (
            InMemorySearchLedger,
            SearchConfig,
            WebSearchClient,
        )

        payload = exa_challenge() if challenge is None else challenge

        def default_transport(method, url, *, headers=None, json_body=None):
            self.probes.append({"method": method, "url": url, "body": json_body})
            return FakeResponse(status, payload)

        self.probes = []
        self.alerts = []
        self.ledger = InMemorySearchLedger()
        self.gateway_settle = gateway if gateway is not None else Recorder()
        self.user_settle = user if user is not None else Recorder()
        return WebSearchClient(
            ledger=self.ledger,
            transport=transport or default_transport,
            settle_from_gateway=self.gateway_settle,
            settle_from_user=self.user_settle,
            config=SearchConfig(
                bound_pay_to=BOUND_PAY_TO,
                free_calls=free_calls,
                max_per_day=max_per_day,
                results=3,
            ),
            purchases_paused=lambda: purchases_paused,
            on_merchant_change=self.alerts.append,
        )

    # -- the free trial ---------------------------------------------------

    def test_first_search_is_free_to_the_user_and_paid_by_the_gateway(self):
        client = self.build()

        outcome = client.search("u1", "eth price now", wallet_address=WALLET)

        self.assertEqual(outcome.cost_atomic, 0)
        self.assertTrue(outcome.free)
        self.assertEqual(len(outcome.results), 2)
        self.assertEqual(len(self.gateway_settle.calls), 1)
        self.assertEqual(self.user_settle.calls, [])

    def test_the_sixth_search_is_charged_to_the_user(self):
        client = self.build(free_calls=5)
        for _ in range(5):
            client.search("u1", "q", wallet_address=WALLET)

        outcome = client.search("u1", "q", wallet_address=WALLET)

        self.assertEqual(outcome.cost_atomic, 7000)
        self.assertFalse(outcome.free)
        self.assertEqual(len(self.user_settle.calls), 1)
        self.assertEqual(len(self.gateway_settle.calls), 5)

    def test_free_calls_are_per_user(self):
        client = self.build(free_calls=1)
        client.search("u1", "q", wallet_address=WALLET)

        outcome = client.search("u2", "q", wallet_address=WALLET)

        self.assertTrue(outcome.free)

    # -- the merchant binding --------------------------------------------

    def test_an_unexpected_pay_to_pays_nothing_and_pauses(self):
        from sign402_gateway.web_search import MerchantChanged

        client = self.build(
            challenge=exa_challenge(pay_to="0x" + "b" * 40)
        )

        with self.assertRaises(MerchantChanged):
            client.search("u1", "q", wallet_address=WALLET)

        self.assertEqual(self.gateway_settle.calls, [])
        self.assertEqual(self.user_settle.calls, [])
        self.assertTrue(self.ledger.is_paused("u1"))
        self.assertEqual(len(self.alerts), 1)

    def test_the_solana_leg_is_never_selected(self):
        client = self.build()
        client.search("u1", "q", wallet_address=WALLET)
        requirement = self.gateway_settle.calls[0]["requirement"]
        self.assertEqual(requirement["network"], "eip155:8453")
        self.assertEqual(requirement["payTo"], BOUND_PAY_TO)

    def test_a_price_above_the_per_call_ceiling_is_refused(self):
        from sign402_gateway.web_search import SearchTooExpensive

        client = self.build(challenge=exa_challenge(amount="900000"))

        with self.assertRaises(SearchTooExpensive):
            client.search("u1", "q", wallet_address=WALLET)

        self.assertEqual(self.gateway_settle.calls, [])

    # -- budget -----------------------------------------------------------

    def test_the_daily_count_stops_searching(self):
        from sign402_gateway.web_search import SearchBudgetExhausted

        client = self.build(max_per_day=2, free_calls=0)
        client.search("u1", "q", wallet_address=WALLET)
        client.search("u1", "q", wallet_address=WALLET)

        with self.assertRaises(SearchBudgetExhausted):
            client.search("u1", "q", wallet_address=WALLET)

        self.assertEqual(len(self.user_settle.calls), 2)

    def test_free_searches_count_against_the_daily_limit(self):
        from sign402_gateway.web_search import SearchBudgetExhausted

        client = self.build(max_per_day=1, free_calls=5)
        client.search("u1", "q", wallet_address=WALLET)

        with self.assertRaises(SearchBudgetExhausted):
            client.search("u1", "q", wallet_address=WALLET)

    def test_paused_purchases_stop_paid_search(self):
        from sign402_gateway.web_search import SearchUnavailable

        client = self.build(purchases_paused=True)

        with self.assertRaises(SearchUnavailable):
            client.search("u1", "q", wallet_address=WALLET)

        self.assertEqual(self.gateway_settle.calls, [])

    # -- provider failure --------------------------------------------------

    def test_a_provider_error_charges_nothing(self):
        from sign402_gateway.web_search import SearchUnavailable

        client = self.build(status=503, challenge={"error": "nope"})

        with self.assertLogs("sign402_gateway.web_search", level="WARNING"):
            with self.assertRaises(SearchUnavailable):
                client.search("u1", "q", wallet_address=WALLET)

        self.assertEqual(self.gateway_settle.calls, [])
        self.assertEqual(self.ledger.count_today("u1"), 0)

    def test_a_timeout_charges_nothing(self):
        from sign402_gateway.web_search import SearchUnavailable

        def boom(method, url, *, headers=None, json_body=None):
            raise TimeoutError("too slow")

        client = self.build(transport=boom)

        with self.assertLogs("sign402_gateway.web_search", level="WARNING"):
            with self.assertRaises(SearchUnavailable):
                client.search("u1", "q", wallet_address=WALLET)

        self.assertEqual(self.ledger.count_today("u1"), 0)

    def test_a_paid_search_with_no_results_still_costs(self):
        client = self.build(
            free_calls=0,
            user=Recorder(
                payload={"ok": True, "status": 200, "body": {"results": []}}
            ),
        )

        outcome = client.search("u1", "q", wallet_address=WALLET)

        self.assertEqual(outcome.results, ())
        self.assertEqual(outcome.cost_atomic, 7000)
        self.assertEqual(self.ledger.count_today("u1"), 1)

    def test_a_settlement_failure_charges_nothing(self):
        from sign402_gateway.web_search import SearchUnavailable

        client = self.build(
            free_calls=0, user=Recorder(error=ValueError("signer refused"))
        )

        with self.assertLogs("sign402_gateway.web_search", level="WARNING"):
            with self.assertRaises(SearchUnavailable):
                client.search("u1", "q", wallet_address=WALLET)

        self.assertEqual(self.ledger.count_today("u1"), 0)

    # -- privacy ------------------------------------------------------------

    def test_the_query_and_page_text_are_never_logged(self):
        client = self.build(
            free_calls=0, user=Recorder(error=ValueError("signer refused"))
        )
        from sign402_gateway.web_search import SearchUnavailable

        with self.assertLogs("sign402_gateway.web_search", level="DEBUG") as logs:
            with self.assertRaises(SearchUnavailable):
                client.search(
                    "u1", "my embarrassing query", wallet_address=WALLET
                )

        joined = "\n".join(logs.output)
        self.assertNotIn("embarrassing", joined)

    def test_the_request_body_carries_the_query_and_the_result_count(self):
        client = self.build()
        client.search("u1", "eth price now", wallet_address=WALLET)
        body = self.gateway_settle.calls[0]["request_body"]
        self.assertEqual(body["query"], "eth price now")
        self.assertEqual(body["numResults"], 3)


class SearchLedgerPersistenceTest(unittest.TestCase):
    """The counters have to survive a restart, and roll over with the day."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chat.sqlite3"
        self.clock = DAY_ONE_NOON

    def store(self):
        from sign402_gateway.chat_store import ChatStore

        store = ChatStore(self.path, now=lambda: self.clock)
        self.addCleanup(store.close)
        return store

    def ledger(self, store=None):
        from sign402_gateway.web_search import ChatStoreSearchLedger

        return ChatStoreSearchLedger(store or self.store())

    def test_a_search_is_counted_for_the_day_and_for_all_time(self):
        ledger = self.ledger()

        ledger.record("u1", 7000)
        ledger.record("u1", 0)

        self.assertEqual(ledger.count_today("u1"), 2)
        self.assertEqual(ledger.total_count("u1"), 2)

    def test_the_daily_count_resets_at_the_utc_day_but_the_total_does_not(self):
        store = self.store()
        ledger = self.ledger(store)
        ledger.record("u1", 7000)

        self.clock += 86_400

        self.assertEqual(ledger.count_today("u1"), 0)
        self.assertEqual(ledger.total_count("u1"), 1)

    def test_counts_survive_a_restart(self):
        self.ledger().record("u1", 7000)

        self.assertEqual(self.ledger().total_count("u1"), 1)

    def test_pausing_search_does_not_pause_the_chat(self):
        store = self.store()
        ledger = self.ledger(store)

        ledger.pause("u1", "merchant_changed")

        self.assertTrue(ledger.is_paused("u1"))
        self.assertFalse(store.get_session("u1").paused)

    def test_an_older_database_gains_the_search_columns(self):
        import sqlite3

        db = sqlite3.connect(self.path)
        db.execute(
            """
            CREATE TABLE chat_sessions (
                user_id TEXT PRIMARY KEY,
                window_start INTEGER NOT NULL,
                spent_atomic INTEGER NOT NULL DEFAULT 0,
                outstanding_atomic INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                pause_reason TEXT NOT NULL DEFAULT '',
                policy_hash TEXT NOT NULL DEFAULT '',
                bound_pay_to TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO chat_sessions (user_id, window_start, updated_at)"
            " VALUES ('u1', ?, ?)",
            (DAY_ONE_NOON, DAY_ONE_NOON),
        )
        db.commit()
        db.close()

        ledger = self.ledger()

        self.assertEqual(ledger.count_today("u1"), 0)
        ledger.record("u1", 7000)
        self.assertEqual(ledger.count_today("u1"), 1)


class StubSearch:
    """Stands in for the paid client. Never calls Exa, never moves funds."""

    def __init__(self, outcome=None, error=None):
        from sign402_gateway.web_search import SearchHit, SearchOutcome

        self.calls = []
        self.error = error
        self.outcome = outcome or SearchOutcome(
            results=(
                SearchHit("https://a.example", "A", "alpha fact"),
                SearchHit("https://b.example", "B", "beta fact"),
            ),
            cost_atomic=7000,
            free=False,
            searches_left_today=19,
        )

    def search(self, user_id, query, *, wallet_address):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return self.outcome


class AnswerWithWebTest(unittest.TestCase):
    def run_turn(self, message, *, answers, search=None, has_previous_turn=False):
        from sign402_gateway.web_search import answer_with_web

        self.asked = []
        replies = list(answers)

        def ask(prompt):
            self.asked.append(prompt)
            return replies.pop(0)

        self.search = search or StubSearch()
        return answer_with_web(
            ask=ask,
            search=self.search,
            user_id="u1",
            message=message,
            wallet_address=WALLET,
            has_previous_turn=has_previous_turn,
        )

    def test_a_present_tense_question_searches_before_answering(self):
        answer = self.run_turn("eth price now", answers=["It is $3,200."])

        self.assertEqual(self.search.calls, ["eth price now"])
        self.assertEqual(len(self.asked), 1)
        self.assertIn("alpha fact", self.asked[0])
        self.assertIn("https://a.example", self.asked[0])
        self.assertEqual(answer.text, "It is $3,200.")
        self.assertTrue(answer.searched)

    def test_chit_chat_never_searches(self):
        answer = self.run_turn("thanks!", answers=["Any time."])

        self.assertEqual(self.search.calls, [])
        self.assertEqual(len(self.asked), 1)
        self.assertFalse(answer.searched)
        self.assertEqual(answer.footer, "")

    def test_the_model_asking_for_the_web_costs_one_extra_completion(self):
        answer = self.run_turn(
            "explain the x402 spec revision",
            answers=["NEED_WEB: x402 spec revision", "Revision two, shipped."],
        )

        self.assertEqual(self.search.calls, ["x402 spec revision"])
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(answer.text, "Revision two, shipped.")
        self.assertTrue(answer.searched)

    def test_need_web_never_fires_twice(self):
        answer = self.run_turn(
            "explain the x402 spec revision",
            answers=["NEED_WEB: once", "NEED_WEB: twice"],
        )

        self.assertEqual(self.search.calls, ["once"])
        self.assertEqual(len(self.asked), 2)
        self.assertNotIn("NEED_WEB", answer.text)

    def test_a_failed_search_still_answers(self):
        from sign402_gateway.web_search import SearchUnavailable

        answer = self.run_turn(
            "eth price now",
            answers=["From what I know, roughly $3,000."],
            search=StubSearch(error=SearchUnavailable("down")),
        )

        self.assertEqual(answer.text, "From what I know, roughly $3,000.")
        self.assertFalse(answer.searched)
        self.assertIn("web", answer.footer.lower())
        self.assertNotIn("$0.007", answer.footer)

    def test_the_daily_cap_says_so_instead_of_degrading_silently(self):
        from sign402_gateway.web_search import SearchBudgetExhausted

        answer = self.run_turn(
            "eth price now",
            answers=["Roughly $3,000."],
            search=StubSearch(error=SearchBudgetExhausted("spent")),
        )

        self.assertFalse(answer.searched)
        self.assertIn("tomorrow", answer.footer.lower())

    def test_the_footer_shows_the_cost_and_what_is_left(self):
        answer = self.run_turn("eth price now", answers=["ok"])

        self.assertIn("$0.007", answer.footer)
        self.assertIn("19", answer.footer)

    def test_a_free_search_shows_zero_rather_than_hiding_the_meter(self):
        from sign402_gateway.web_search import SearchHit, SearchOutcome

        answer = self.run_turn(
            "eth price now",
            answers=["ok"],
            search=StubSearch(
                outcome=SearchOutcome(
                    results=(SearchHit("https://a.example", "A", "alpha"),),
                    cost_atomic=0,
                    free=True,
                    searches_left_today=19,
                )
            ),
        )

        self.assertIn("$0.000", answer.footer)

    def test_a_paid_search_with_no_results_still_shows_its_cost(self):
        from sign402_gateway.web_search import SearchOutcome

        answer = self.run_turn(
            "eth price now",
            answers=["I could not find anything current."],
            search=StubSearch(
                outcome=SearchOutcome(
                    results=(), cost_atomic=7000, free=False, searches_left_today=19
                )
            ),
        )

        self.assertIn("$0.007", answer.footer)
        self.assertTrue(answer.searched)


class EnvBuilderTest(unittest.TestCase):
    def build(self, env):
        from sign402_gateway.web_search import build_web_search_from_env

        return build_web_search_from_env(
            store=None,
            settle_from_gateway=lambda *a, **k: {"ok": True},
            settle_from_user=lambda *a, **k: {"ok": True},
            env=env,
        )

    def test_the_feature_is_off_without_the_flag(self):
        self.assertIsNone(self.build({}))
        self.assertIsNone(self.build({"SIGN402_AI_SEARCH_ENABLED": "0"}))

    def test_enabling_it_without_a_merchant_is_refused(self):
        with self.assertRaises(ValueError):
            self.build({"SIGN402_AI_SEARCH_ENABLED": "1"})

    def test_the_limits_come_from_the_environment(self):
        client = self.build(
            {
                "SIGN402_AI_SEARCH_ENABLED": "1",
                "SIGN402_AI_SEARCH_MERCHANT_PAYTO": BOUND_PAY_TO,
                "SIGN402_AI_SEARCH_MAX_PER_DAY": "7",
                "SIGN402_AI_SEARCH_FREE_CALLS": "2",
                "SIGN402_AI_SEARCH_RESULTS": "4",
            }
        )

        self.assertEqual(client.config.max_per_day, 7)
        self.assertEqual(client.config.free_calls, 2)
        self.assertEqual(client.config.results, 4)
        self.assertEqual(client.config.bound_pay_to, BOUND_PAY_TO)


class ChatIntegrationTest(unittest.TestCase):
    """The flag off must leave the chat byte-identical to today."""

    ANSWER = {"choices": [{"message": {"content": "Roughly $3,000."}}]}

    def make_client(self, web_search=None):
        from sign402_gateway.chat_store import ChatStore
        from sign402_gateway.venice_chat import VeniceChatClient, VeniceConfig

        store = ChatStore(":memory:")
        self.addCleanup(store.close)
        self.store = store
        self.prompts = []

        def transport(method, url, *, headers=None, json_body=None):
            if "/x402/balance/" in url:
                return FakeResponse(
                    200, {"success": True, "data": {"canConsume": True}}
                )
            if url.endswith("/chat/completions"):
                self.prompts.append(json_body["messages"][0]["content"])
                return FakeResponse(200, self.ANSWER)
            raise AssertionError(f"unexpected request to {url}")

        client = VeniceChatClient(
            store=store,
            transport=transport,
            signer=lambda address, message: "0x" + "11" * 65,
            settle=lambda requirement, *, user_id: {"ok": True},
            config=VeniceConfig(
                bound_pay_to=BOUND_PAY_TO,
                network="eip155:8453",
                asset=USDC,
                chunk_atomic=5_000_000,
                max_outstanding_atomic=10_000_000,
                daily_cap_atomic=5_000_000,
            ),
            web_search=web_search,
        )
        store.bind_policy("u1", policy_hash="a" * 64, pay_to=BOUND_PAY_TO)
        store.record_prefund("u1", 5_000_000)
        return client

    def test_without_the_feature_the_prompt_reaches_venice_untouched(self):
        client = self.make_client()

        result = client.send("u1", "eth price now", wallet_address=WALLET)

        self.assertEqual(self.prompts, ["eth price now"])
        self.assertEqual(result.web_footer, "")
        self.assertEqual(result.text, "Roughly $3,000.")

    def test_with_the_feature_the_results_reach_the_model(self):
        client = self.make_client(web_search=StubSearch())

        result = client.send("u1", "eth price now", wallet_address=WALLET)

        self.assertEqual(len(self.prompts), 1)
        self.assertIn("alpha fact", self.prompts[0])
        self.assertIn("$0.007", result.web_footer)

    def test_a_broken_search_does_not_break_the_message(self):
        from sign402_gateway.web_search import SearchUnavailable

        client = self.make_client(
            web_search=StubSearch(error=SearchUnavailable("down"))
        )

        result = client.send("u1", "eth price now", wallet_address=WALLET)

        self.assertEqual(result.text, "Roughly $3,000.")
        self.assertIn("unavailable", result.web_footer)
