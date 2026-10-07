"""Live data for the web chat: bought per question from the account's limits, on Base or Solana."""
import json
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sign402_gateway import goplausible, web_agent as wg, web_data, web_internal, web_venice
from sign402_gateway.agent_allowance import AllowanceError, AllowanceUnavailable
from sign402_gateway.server import _validate_base_usdc_x402_requirement

BASE_ACCOUNT = "wallet:0x1111111111111111111111111111111111111111"
SOLANA_ACCOUNT = "solana:BTXXtaRQfzd7BF6zADrMtqDeDhdiiP3t2WYXHz3hCCSK"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ROW = {"limiter_address": "0xLIM", "daily_cap": 20_000_000, "per_purchase_cap": 5_000_000, "expiry": 4_000_000_000}
WEATHER = {"current": {"tempC": 14, "sky": "light rain"}, "forecast": [{"day": "Tue", "highC": 16}]}
FLIGHTS = {"flights": [{"ident_iata": "LH400", "status": "En Route / On Time", "departure_delay": 600, "progress_percent": 40,
                        "origin": {"code_iata": "FRA", "city": "Frankfurt", "name": "Frankfurt Intl"},
                        "destination": {"code_iata": "JFK", "city": "New York", "name": "JFK"},
                        "fa_flight_id": "DLH400-1790-schedule-0001", "route": "long string we do not need"}]}


def offer(tool, amount="2000", base_pay_to=None, solana_pay_to=None):
    """The seller's 402: the Base and Solana legs, plus another Base address to be ignored."""
    return {"x402Version": 2, "accepts": [
        {"scheme": "exact", "network": "eip155:8453", "amount": amount, "asset": USDC, "payTo": "0xB98eF29eb2be19Ae646A8FC0248255B90A332dbC",
         "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}},
        {"scheme": "exact", "network": "eip155:8453", "amount": amount, "asset": USDC,
         "payTo": base_pay_to or tool.pay_to[web_data.BASE], "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}},
        {"scheme": "exact", "network": web_data.SOLANA, "amount": amount, "asset": web_data.SOLANA_USDC,
         "payTo": solana_pay_to or tool.pay_to[web_data.SOLANA], "maxTimeoutSeconds": 60, "extra": {"feePayer": "BENr"}}]}


class BaseDataTests(unittest.TestCase):
    def setUp(self):
        self.allowance = Mock()
        self.allowance.lane_for.return_value = dict(ROW)
        self.server = SimpleNamespace(allowance=self.allowance)
        self.offers = []
        self.gw = SimpleNamespace(normalize_x402_payment_required=goplausible.normalize_x402_payment_required,
                                  _validate_base_usdc_x402_requirement=_validate_base_usdc_x402_requirement,
                                  fetch_x402_payment_required=Mock(side_effect=lambda url, request_body=None: self.offers.pop(0)),
                                  _purchases_paused=lambda: False)
        self.pay = Mock(return_value={"ok": True, "txId": "0xabc", "resourceResult": {"status": 200, "body": WEATHER}})
        patcher = patch.object(web_internal, "pay_from_allowance", self.pay)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_weather_is_paid_from_the_limiter_to_the_bound_address_and_digested(self):
        self.offers = [offer(web_data.TOOLS["weather"])]
        got = web_data.buy(self.server, self.gw, BASE_ACCOUNT, "weather", {"place": "Berlin"})
        _, _, account, tool, url, requirements = self.pay.call_args.args
        self.assertEqual((account, tool["id"], url), (BASE_ACCOUNT, "data.weather", "https://x402.ottoai.services/weather?location=Berlin"))
        self.assertEqual(requirements["receiver"].lower(), web_data.OTTO[web_data.BASE].lower())
        self.assertIsNone(self.pay.call_args.kwargs["request_body"])  # a GET
        self.assertFalse(self.pay.call_args.kwargs["record"])  # counted against the limits, not a Purchases row
        self.assertEqual((got["name"], got["costUsd"], got["network"]), ("Weather", "0.002", "base"))
        self.assertIn("light rain", got["digest"])

    def test_a_wallet_check_is_paid_to_agent402s_bound_address_and_keeps_the_address_case(self):
        address = "J7aN3PLJnTCF5qpEnvJHJsnCjcGuqC2rYtEM8Gv3xwg"
        self.offers = [offer(web_data.TOOLS["wallet_check"])]
        self.pay.return_value = {"ok": True, "txId": "0xabc", "resourceResult": {"status": 200, "body": {"address": address, "verdict": "no_match_on_lists_checked",
                                                                                              "listsChecked": [{"list": "OFAC SDN"}]}}}
        got = web_data.buy(self.server, self.gw, BASE_ACCOUNT, "wallet_check", {"address": address})
        _, _, _, tool, url, requirements = self.pay.call_args.args
        self.assertEqual((tool["id"], url), ("data.wallet_check", f"https://agent402.tools/api/sanctions/wallet?address={address}"))
        self.assertEqual(requirements["receiver"].lower(), web_data.AGENT402[web_data.BASE].lower())
        self.assertIsNone(self.pay.call_args.kwargs["request_body"])  # a GET
        self.assertEqual((got["name"], got["network"]), ("Wallet screening", "base"))
        self.assertIn("no_match_on_lists_checked", got["digest"])

    def test_another_address_a_higher_price_or_a_missing_place_pays_nothing(self):
        cases = [("weather", {"place": "Berlin"}, offer(web_data.TOOLS["weather"], base_pay_to="0x" + "9" * 40), "somewhere unexpected"),
                 ("weather", {"place": "Berlin"}, offer(web_data.TOOLS["weather"], amount="90000"), "more than its usual price"),
                 ("weather", {}, None, "needs place"),
                 ("nope", {}, None, "Unknown data source")]
        for tool, params, seller, refusal in cases:
            self.offers = [seller] if seller else []
            with self.assertRaisesRegex(AllowanceError, refusal):
                web_data.buy(self.server, self.gw, BASE_ACCOUNT, tool, params)
        self.pay.assert_not_called()

    def test_a_link_is_read_with_a_post_and_a_flight_is_cut_to_what_answers(self):
        self.offers = [offer(web_data.TOOLS["read_link"], amount="1000")]
        self.pay.return_value = {"ok": True, "txId": "0x1", "resourceResult": {"body": {"results": [
            {"title": "x402", "url": "https://x402.org", "text": "HTTP 402 payments."}]}}}
        got = web_data.buy(self.server, self.gw, BASE_ACCOUNT, "read_link", {"url": "https://x402.org"})
        self.assertEqual(self.pay.call_args.kwargs["request_body"]["urls"], ["https://x402.org"])
        self.assertEqual((got["link"], json.loads(got["digest"])["text"]), ("https://x402.org", "HTTP 402 payments."))
        self.offers = [offer(web_data.TOOLS["flight_status"], amount="10000")]
        self.pay.return_value = {"ok": True, "txId": "0x2", "resourceResult": {"body": FLIGHTS}}
        got = web_data.buy(self.server, self.gw, BASE_ACCOUNT, "flight_status", {"flight": "LH400"})
        flight = json.loads(got["digest"])["flights"][0]
        self.assertEqual((flight["status"], flight["departure_delay"], flight["origin"]["city"]), ("En Route / On Time", 600, "Frankfurt"))
        self.assertNotIn("route", flight)
        self.assertEqual(got["link"], "https://www.flightaware.com/live/flight/LH400")


class SolanaDataTests(unittest.TestCase):
    def setUp(self):
        on = patch.dict("os.environ", {web_data.OFF_ENV: ""})  # every source on: these test how sources pay
        on.start()
        self.addCleanup(on.stop)
        self.calls, self.spent = [], []
        lane = Mock()
        lane.status.return_value = {"state": "granted"}
        lane.owner.return_value = "BTXXtaRQfzd7BF6zADrMtqDeDhdiiP3t2WYXHz3hCCSK"

        def spend(account, amount, purpose, pay):
            self.spent.append((amount, purpose))
            return pay()

        def call(account, operation, **payload):
            self.calls.append((operation, payload))
            data = {"results": [{"title": "Best coffee near Alexanderplatz", "url": "https://berlin.example/coffee",
                                 "text": "The Barn, Bonanza and more."}]}
            return {"state": "accepted", "transaction": "5" * 88, "data": data}
        lane.spend.side_effect, lane._call.side_effect = spend, call
        self.server = SimpleNamespace(solana_allowance=lane)
        self.gw = SimpleNamespace(fetch_x402_payment_required=lambda url, request_body=None: offer(web_data.TOOLS["places"], amount="7000"),
                                  _purchases_paused=lambda: False)

    def test_places_are_one_exa_search_paid_from_the_owners_account_to_the_bound_address(self):
        got = web_data.buy(self.server, self.gw, SOLANA_ACCOUNT, "places",
                           {"query": "good coffee near Alexanderplatz Berlin", "kind": "restaurants"})
        self.assertEqual([op for op, _ in self.calls], ["data-pay"])  # one search; no follow-ups
        first = self.calls[0][1]
        self.assertEqual((first["payTo"], first["maxAmount"], first["owner"], first["method"]),
                         (web_data.EXA[web_data.SOLANA], "7000", "BTXXtaRQfzd7BF6zADrMtqDeDhdiiP3t2WYXHz3hCCSK", "POST"))
        self.assertEqual(first["url"], "https://api.exa.ai/search")
        self.assertEqual(first["body"]["query"], "good coffee near Alexanderplatz Berlin")
        self.assertEqual(self.spent, [(7000, "Web search")])  # within the Solana limits
        self.assertEqual((got["costUsd"], got["network"], got["name"]), ("0.007", "solana", "Web search"))
        self.assertIn("https://berlin.example/coffee", got["digest"])
        hotels = web_data.TOOLS["places"].request({"query": "Prague", "kind": "hotels"})[2]["query"]
        self.assertEqual(hotels, "hotels Prague")

    def test_a_stalled_payment_request_is_asked_once_more_and_never_waited_for_long(self):
        # 4 October: Exa's first payment request took 40 s, the next 0.4 s.
        asked = []
        def stall_then_answer(url, request_body=None):
            asked.append(url)
            if len(asked) == 1:
                time.sleep(0.5)
            return offer(web_data.TOOLS["places"], amount="7000")
        self.gw.fetch_x402_payment_required = stall_then_answer
        with patch.object(web_data, "OFFER_DEADLINES", (0.1, 0.3)):
            started = time.monotonic()
            web_data.buy(self.server, self.gw, SOLANA_ACCOUNT, "places", {"query": "Prague"})
            self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(len(asked), 2)
        self.assertEqual([op for op, _ in self.calls], ["data-pay"])  # the payment itself is never repeated

        self.calls.clear()
        self.gw.fetch_x402_payment_required = lambda url, request_body=None: time.sleep(0.5)
        with patch.object(web_data, "OFFER_DEADLINES", (0.1, 0.1)), self.assertRaisesRegex(AllowanceError, "did not answer"):
            web_data.buy(self.server, self.gw, SOLANA_ACCOUNT, "places", {"query": "Prague"})
        self.assertEqual(self.calls, [])

    def test_without_limits_nothing_is_read_or_paid(self):
        self.server.solana_allowance.store.limits.return_value = None
        with self.assertRaisesRegex(AllowanceUnavailable, "Set your limits"):
            web_data.buy(self.server, self.gw, SOLANA_ACCOUNT, "places", {"query": "Prague"})
        self.assertEqual((self.calls, self.spent), ([], []))
        self.server.solana_allowance.status.assert_not_called()  # the wallet is read once, in spend()

    def test_an_unbound_solana_address_pays_nothing(self):
        self.gw.fetch_x402_payment_required = lambda url, request_body=None: offer(
            web_data.TOOLS["places"], amount="7000", solana_pay_to="SomeoneElse1111111111111111111111111111111")
        with self.assertRaisesRegex(AllowanceError, "somewhere unexpected"):
            web_data.buy(self.server, self.gw, SOLANA_ACCOUNT, "places", {"query": "Rome"})
        self.assertEqual((self.calls, self.spent), ([], []))


class PlanTests(unittest.TestCase):
    TODAY = "2026-09-29"

    def model(self, reply):
        return lambda messages, json_mode=False, max_tokens=0: json.dumps(reply)

    def test_links_and_flight_numbers_need_no_model(self):
        self.assertEqual(wg.plan_data("summarize https://x402.org/writing.", None, self.TODAY),
                         ("read_link", {"url": "https://x402.org/writing"}, ""))
        self.assertEqual(wg.plan_data("where is flight LH400?", None, self.TODAY), ("flight_status", {"flight": "LH400"}, ""))
        self.assertEqual(wg.plan_data("кофе рядом с Колизеем", None, self.TODAY),
                         ("places", {"query": "кофе рядом с Колизеем"}, ""))
        self.assertIsNone(wg.plan_data("???", None, self.TODAY))  # nothing to search for

    def test_an_address_with_a_screening_word_is_a_wallet_check_without_a_model(self):
        evm = "0x7E6b00000000000000000000000000000000AbCd"
        self.assertEqual(wg.plan_data(f"is {evm} sanctioned?", None, self.TODAY), ("wallet_check", {"address": evm}, ""))
        sol = "J7aN3PLJnTCF5qpEnvJHJsnCjcGuqC2rYtEM8Gv3xwg"
        self.assertEqual(wg.plan_data(f"безопасно ли? проверь санкции {sol}", None, self.TODAY)[:2], ("wallet_check", {"address": sol}))
        self.assertEqual(wg.plan_data("check this wallet", self.model({"tool": "wallet_check", "address": "not an address"}), self.TODAY),
                         ("wallet_check", {}, "address"))

    def test_the_model_picks_the_source_and_every_field_is_checked(self):
        plan = wg.plan_data("flights Berlin to Barcelona Oct 15", self.model(
            {"tool": "flight_search", "from": "ber", "to": "BCN", "date": "2026-10-15", "return": "", "place": "x" * 200}), self.TODAY)
        self.assertEqual(plan, ("flight_search", {"from": "BER", "to": "BCN", "date": "2026-10-15"}, ""))
        self.assertEqual(wg.plan_data("flights to Rome", self.model({"tool": "flight_search", "to": "FCO", "date": "2020-01-01"}),
                                      self.TODAY)[2], "from")
        self.assertEqual(wg.plan_data("weather?", self.model({"tool": "weather"}), self.TODAY), ("weather", {}, "place"))
        # "none" or nonsense from the model: the question is searched on the web (Jev already read live data).
        self.assertEqual(wg.plan_data("What about good coffee near the Colosseum?", self.model({"tool": "none"}), self.TODAY),
                         ("places", {"query": "What about good coffee near the Colosseum"}, ""))
        self.assertEqual(wg.plan_data("dobrá káva v Praze", self.model({"tool": "shell"}), self.TODAY)[0], "places")

    def test_a_web_search_without_a_usable_query_searches_the_question(self):
        # Seen on 4 October: places, but nothing to search, and the agent asked "where and what?" about Prague.
        for reply in ({"tool": "places", "place": "Prague", "kind": "attractions"},
                      {"tool": "places", "query": "<what to see>", "kind": "attractions"}):
            self.assertEqual(wg.plan_data("Что посмотреть в Праге за один вечер?", self.model(reply), self.TODAY),
                             ("places", {"place": "Prague", "kind": "attractions", "query": "Что посмотреть в Праге за один вечер"}
                              if "place" in reply else {"kind": "attractions", "query": "Что посмотреть в Праге за один вечер"}, ""))
        self.assertEqual(wg.plan_data("???", self.model({"tool": "places"}), self.TODAY), ("places", {}, "query"))


GRANTED = {"configured": True, "state": "granted", "limiter": "0xLIM", "dailyCapAtomic": 20_000_000,
           "perPurchaseCapAtomic": 5_000_000, "remainingTodayAtomic": 20_000_000, "chain": "solana"}


class AgentDataTests(unittest.TestCase):
    def setUp(self):
        on = patch.dict("os.environ", {web_data.OFF_ENV: ""})
        on.start()
        self.addCleanup(on.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

        def shop(action, account, body):
            self.calls.append((action, dict(body)))
            if action == "data-buy":
                return 200, {"ok": True, "tool": body["tool"], "name": "FlightAware", "source": "StableTravel",
                             "costUsd": "0.010", "digest": '{"flights":[{"status":"Delayed"}]}',
                             "link": "https://www.flightaware.com/live/flight/LH400"}
            if action == "venice-chat":
                return 200, {"ok": True, "text": "LH400 is delayed.", "costAtomic": 800, "model": "m", "modelLabel": "M",
                             "promptTokens": 90, "completionTokens": 10}
            raise AssertionError(action)
        lane = Mock()
        lane.status.return_value = dict(GRANTED)
        lane.stale_allowances.return_value = []
        self.agent = wg.WebAgent(allowance=lane, shop=shop, store=wg.ChatStore(Path(self.tmp.name) / "web.db"),
                                 classify=lambda text: {"intent": self.intent})
        self.agent.solana = lane

    def test_a_flight_question_buys_the_data_and_venice_answers_from_it(self):
        self.intent = "live_data"
        reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "Where is flight LH400 now?")["messages"][1]
        self.assertEqual([a for a, _ in self.calls], ["data-buy", "venice-chat"])
        self.assertEqual(self.calls[0][1], {"tool": "flight_status", "params": {"flight": "LH400"}})
        self.assertEqual(self.calls[1][1]["context"]["digest"], '{"flights":[{"status":"Delayed"}]}')
        self.assertEqual(reply["text"], "LH400 is delayed.")
        self.assertEqual(reply["cards"][0]["data"], {"name": "FlightAware", "costUsd": "0.010",
                                                     "link": "https://www.flightaware.com/live/flight/LH400"})

    def test_a_refused_data_purchase_still_gets_an_answer_that_says_why(self):
        self.intent = "live_data"
        shop = self.agent.shop

        def refusing(action, account, body):
            if action == "data-buy":
                self.calls.append((action, dict(body)))
                return 400, {"ok": False, "text": "The data seller refused the payment. Nothing was paid. (payer_not_allowed)"}
            return shop(action, account, body)
        self.agent.shop = refusing
        reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "Where is flight LH400 now?")["messages"][1]
        self.assertEqual(reply["text"], "LH400 is delayed.")
        self.assertNotIn("context", self.calls[-1][1])
        self.assertEqual(reply["cards"][0]["searchNote"],
                         "The data seller refused the payment. Nothing was paid. (payer_not_allowed)")

    def test_paid_data_still_gets_an_answer_when_venice_needs_a_top_up_the_limits_cannot_fund(self):
        self.intent = "live_data"
        shop, asked = self.agent.shop, []

        def no_credit(action, account, body):
            if action == "venice-chat":
                self.calls.append((action, dict(body)))
                return 400, {"ok": False, "error": "chat_refused", "text": (
                    "Your private chat runs on Venice credit, bought from your limits $5 at a time, and that top-up did "
                    "not go through: Your limiter cannot fund 5 USDC now: it allows 2.006965 USDC.")}
            return shop(action, account, body)

        def model(messages, json_mode=False, max_tokens=700):
            if json_mode:  # plan_data: which source answers the question
                return '{"tool": "flight_status", "flight": "LH400"}'
            asked.append(messages)
            return "LH400 is delayed by 20 minutes."
        self.agent.shop, self.agent.model = no_credit, model
        account = wg.SOLANA_ACCOUNT + "BTXX"
        chat = self.agent.message(account, None, "My secret plan is to fly to Prague.")["chatId"]  # a private earlier turn
        asked.clear()
        reply = self.agent.message(account, chat, "Where is flight LH400 now?")["messages"][1]
        self.assertEqual([a for a, _ in self.calls][-2:], ["data-buy", "venice-chat"])
        self.assertTrue(reply["text"].startswith("LH400 is delayed by 20 minutes."))
        self.assertIn("needs a $5 credit top-up", reply["text"])
        self.assertEqual([c["type"] for c in reply["cards"]], ["add_funds", "data"])
        sent = asked[-1]
        self.assertEqual(len(sent), 2)  # the question and its data only, never the rest of the private chat
        self.assertIn('{"flights":[{"status":"Delayed"}]}', sent[1]["content"])
        self.assertTrue(sent[1]["content"].endswith("Where is flight LH400 now?"))
        self.assertNotIn("secret plan", json.dumps(sent))

    def test_places_bought_from_jevs_reading(self):
        self.intent = "live_data"
        self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "Restaurants in Czechia")
        self.assertEqual(self.calls[0], ("data-buy", {"tool": "places", "params": {"query": "Restaurants in Czechia"}}))

    def test_no_planner_searches_the_question_and_a_conversation_never_reaches_data(self):
        self.intent = "live_data"

        def down(messages, json_mode=False, max_tokens=700):
            if json_mode:
                raise wg.AgentUnavailable("model")
            return "Try Sant'Eustachio."
        self.agent.model = down
        self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "кофе рядом с Колизеем")
        self.assertEqual(self.calls[0], ("data-buy", {"tool": "places", "params": {"query": "кофе рядом с Колизеем"}}))
        self.calls.clear()
        self.intent = "chat"  # Jev reads "I work in a restaurant" as conversation (live check): no data is bought
        self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "I work in a restaurant in Prague")
        self.assertNotIn("data-buy", [a for a, _ in self.calls])

    def test_paid_places_and_no_model_at_all_show_their_names(self):
        self.intent = "live_data"
        shop = self.agent.shop

        def places(action, account, body):
            if action == "data-buy":
                self.calls.append((action, dict(body)))
                return 200, {"ok": True, "tool": "places", "name": "Tripadvisor", "costUsd": "0.040",
                             "digest": '[{"id":1,"name":"U Fleků","address":"Křemencova"},{"id":2,"name":"Eska"}]'}
            if action == "venice-chat":
                return 400, {"ok": False, "error": "chat_refused", "text": "Your limiter cannot fund 5 USDC now."}
            return shop(action, account, body)

        def down(*args, **kwargs):
            raise wg.AgentUnavailable("model")
        self.agent.shop, self.agent.model = places, down
        reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "Restaurants in Prague")["messages"][1]
        self.assertTrue(reply["text"].startswith("Here is what I found:\n- U Fleků\n- Eska"))
        self.assertIn("needs a $5 credit top-up", reply["text"])

    def test_a_signer_stack_never_reaches_the_page(self):
        # Production, 30 September: the paid Tripadvisor request failed in the Node signer and its stack was the reply.
        self.intent = "live_data"
        shop = self.agent.shop
        stack = ("TypeError: fetch failed\n    at node:internal/deps/undici/undici:15141:13\n    at async "
                 "buyPaidResourceWithSigner (file:///home/hermes/apps/sign402/cdp-x402-service/src/index.mjs:242:20)")

        def failing(action, account, body):
            if action == "data-buy":
                self.calls.append((action, dict(body)))
                return 400, {"ok": False, "error": "refused", "text": stack}
            return shop(action, account, body)
        self.agent.shop = failing
        reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "Where is flight LH400 now?")["messages"][1]
        for leak in ("node:internal", "at async", "file:///", "TypeError"):
            self.assertNotIn(leak, json.dumps(reply))
        self.assertIn("did not answer", reply["cards"][0]["searchNote"])

    def test_a_source_can_be_switched_off_and_is_then_never_paid(self):
        import os
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop(web_data.OFF_ENV, None)
            self.assertEqual(web_data.switched_off(), set())  # Exa answers places: nothing is off by default
            self.assertTrue(wg.places_on())
        with patch.dict("os.environ", {web_data.OFF_ENV: "places"}):
            with self.assertRaises(AllowanceError) as caught:
                web_data.buy(Mock(), Mock(), wg.SOLANA_ACCOUNT + "BTXX", "places", {"query": "coffee in Los Angeles"})
            self.assertIn("Nothing was paid", str(caught.exception))
            self.assertFalse(wg.places_on())

    def test_while_places_are_off_a_coffee_question_buys_nothing_and_is_answered(self):
        self.intent = "live_data"
        self.agent.model = lambda messages, json_mode=False, max_tokens=700: (
            '{"tool": "places", "query": "good coffee in Berlin", "kind": "restaurants"}' if json_mode else "The Barn.")
        with patch.dict("os.environ", {web_data.OFF_ENV: "places"}):
            reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "What about good coffee in Berlin?")["messages"][1]
        self.assertNotIn("data-buy", [a for a, _ in self.calls])
        self.assertNotIn("switched off", reply["text"])

    def test_a_failed_purchase_is_one_reason_not_two(self):
        self.intent = "live_data"
        shop = self.agent.shop

        def failing(action, account, body):
            if action == "data-buy":
                return 400, {"ok": False, "text": "Tripadvisor is switched off for now. Nothing was paid."}
            if action == "venice-chat":
                return 400, {"ok": False, "error": "chat_refused", "text": "Your limiter cannot fund 5 USDC now."}
            return shop(action, account, body)
        self.agent.shop = failing
        self.agent.model = lambda messages, json_mode=False, max_tokens=700: (
            '{"tool": "places", "query": "coffee in Los Angeles", "kind": "restaurants"}' if json_mode else "Try Blue Bottle.")
        reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "coffee in Los Angeles")["messages"][1]
        # The seller's reason first, then an answer anyway; never Venice's refusal on top.
        self.assertTrue(reply["text"].startswith("Tripadvisor is switched off for now. Nothing was paid.\n\nTry Blue Bottle."))
        self.assertNotIn("cannot fund", reply["text"])

    def test_exa_pages_and_no_model_are_listed_with_their_links(self):
        self.intent = "live_data"
        shop = self.agent.shop

        def pages(action, account, body):
            if action == "data-buy":
                self.calls.append((action, dict(body)))
                return 200, {"ok": True, "tool": "places", "name": "Places", "costUsd": "0.007",
                             "digest": '[{"title":"Best coffee near Alexanderplatz","url":"https://berlin.example/coffee",'
                                       '"text":"The Barn..."}]'}
            if action == "venice-chat":
                return 400, {"ok": False, "error": "chat_refused", "text": "Your limiter cannot fund 5 USDC now."}
            return shop(action, account, body)

        def down(*args, **kwargs):
            raise wg.AgentUnavailable("model")
        self.agent.shop, self.agent.model = pages, down
        reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "cafes near Alexanderplatz")["messages"][1]
        self.assertTrue(reply["text"].startswith(
            "Here is what I found:\n- [Best coffee near Alexanderplatz](https://berlin.example/coffee)"))

    def test_paid_data_and_no_model_at_all_still_explains(self):
        self.intent = "live_data"
        shop = self.agent.shop

        def no_credit(action, account, body):
            if action == "venice-chat":
                return 400, {"ok": False, "error": "chat_refused", "text": "Your limiter cannot fund 5 USDC now."}
            return shop(action, account, body)

        def model(messages, json_mode=False, max_tokens=700):
            if json_mode:
                return '{"tool": "flight_status", "flight": "LH400"}'
            raise wg.AgentUnavailable("model")
        self.agent.shop, self.agent.model = no_credit, model
        reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "Where is flight LH400 now?")["messages"][1]
        self.assertEqual(reply["text"], "Your limiter cannot fund 5 USDC now.")
        self.assertEqual([c["type"] for c in reply["cards"]], ["add_funds", "data"])

    def test_without_data_a_venice_refusal_is_the_reply(self):
        self.intent = "chat"
        shop = self.agent.shop

        def no_credit(action, account, body):
            if action == "venice-chat":
                return 400, {"ok": False, "error": "chat_refused", "text": "Your limiter cannot fund 5 USDC now."}
            return shop(action, account, body)
        asked = []

        def model(messages, json_mode=False, max_tokens=700):
            asked.append(messages)
            return "Why did the chicken cross the road?"
        self.agent.shop, self.agent.model = no_credit, model
        chat = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "My private secret is 42")["chatId"]
        asked.clear()
        reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", chat, "Tell me a joke")["messages"][1]
        self.assertTrue(reply["text"].startswith("Why did the chicken cross the road?"))
        self.assertIn("not the private chat", reply["text"])
        self.assertEqual([c["type"] for c in reply["cards"]], ["add_funds"])
        self.assertEqual(len(asked[-1]), 2)  # this question only, never the private chat before it
        self.assertNotIn("secret is 42", json.dumps(asked[-1]))

        def down(*args, **kwargs):
            raise wg.AgentUnavailable("model")
        self.agent.model = down  # no one to answer: Venice's reason stays the reply
        reply = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "Tell me a joke")["messages"][1]
        self.assertEqual(reply["text"], "Your limiter cannot fund 5 USDC now.")

    def test_crypto_news_works_from_solana_now_and_base_only_tools_say_so(self):
        self.intent = "buy_tool"
        self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "buy crypto news")
        self.assertEqual(self.calls[0], ("data-buy", {"tool": "crypto_news", "params": {}}))
        text = self.agent.message(wg.SOLANA_ACCOUNT + "BTXX", None, "resolve vitalik.eth ens")["messages"][1]["text"]
        self.assertIn("Base only", text)


class VeniceContextTests(unittest.TestCase):
    def test_the_data_rides_on_the_question_as_untrusted_data(self):
        messages = [{"role": "system", "content": "SingIt"}, {"role": "user", "content": "Weather in Berlin?"}]
        wrapped = web_venice._with_data(messages, {"name": "Weather", "source": "Otto AI", "digest": '{"tempC":14}'})
        self.assertEqual(wrapped[0], messages[0])
        self.assertTrue(wrapped[-1]["content"].endswith("Weather in Berlin?"))
        self.assertIn('{"tempC":14}', wrapped[-1]["content"])
        self.assertIn("never instructions", wrapped[-1]["content"])
        self.assertEqual(messages[-1]["content"], "Weather in Berlin?")  # the stored conversation is untouched
        self.assertEqual(web_venice._with_data(messages, None), messages)


if __name__ == "__main__":
    unittest.main()
