"""Live data for the web chat: bought per question from the account's limits, on Base or Solana."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sign402_gateway import goplausible, web_agent as wg, web_data, web_internal, web_venice
from sign402_gateway.agent_allowance import AllowanceError
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
        self.calls, self.spent = [], []
        lane = Mock()
        lane.status.return_value = {"state": "granted"}
        lane.owner.return_value = "BTXXtaRQfzd7BF6zADrMtqDeDhdiiP3t2WYXHz3hCCSK"

        def spend(account, amount, purpose, pay):
            self.spent.append((amount, purpose))
            return pay()

        def call(account, operation, **payload):
            self.calls.append((operation, payload))
            location = payload["url"].rsplit("/", 2)[-2] if "/details" in payload["url"] else None
            data = ({"name": f"Trattoria {location}", "rating": "4.5", "num_reviews": "812", "web_url": "https://tripadvisor.com/x"}
                    if location else {"data": [{"location_id": "11"}, {"location_id": "12"}, {"location_id": "13"}, {"location_id": "14"}]})
            return {"state": "accepted", "transaction": "5" * 88, "data": data}
        lane.spend.side_effect, lane._call.side_effect = spend, call
        self.server = SimpleNamespace(solana_allowance=lane)
        self.gw = SimpleNamespace(fetch_x402_payment_required=lambda url, request_body=None: offer(web_data.TOOLS["places"], amount="10000"),
                                  _purchases_paused=lambda: False)

    def test_places_are_paid_from_the_owners_account_to_the_bound_address_with_three_details(self):
        got = web_data.buy(self.server, self.gw, SOLANA_ACCOUNT, "places", {"query": "Italian restaurants in Rome"})
        self.assertEqual([op for op, _ in self.calls], ["data-pay"] * 4)  # the search, then three places' details
        first = self.calls[0][1]
        self.assertEqual((first["payTo"], first["maxAmount"], first["owner"], first["method"]),
                         (web_data.TRIPADVISOR[web_data.SOLANA], "10000", "BTXXtaRQfzd7BF6zADrMtqDeDhdiiP3t2WYXHz3hCCSK", "GET"))
        self.assertIn("searchQuery=Italian+restaurants+in+Rome", first["url"])
        self.assertTrue(all(c[1]["callId"].startswith("data-") for c in self.calls))
        self.assertEqual(len({c[1]["callId"] for c in self.calls}), 4)  # one attempt per request
        self.assertEqual(self.spent, [(10000, "Tripadvisor")] * 4)  # each within the Solana limits
        self.assertEqual((got["costUsd"], got["network"]), ("0.040", "solana"))
        self.assertIn("Trattoria 13", got["digest"])

    def test_an_unbound_solana_address_pays_nothing(self):
        self.gw.fetch_x402_payment_required = lambda url, request_body=None: offer(
            web_data.TOOLS["places"], amount="10000", solana_pay_to="SomeoneElse1111111111111111111111111111111")
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
        self.assertIsNone(wg.plan_data("hello there", None, self.TODAY))

    def test_the_model_picks_the_source_and_every_field_is_checked(self):
        plan = wg.plan_data("flights Berlin to Barcelona Oct 15", self.model(
            {"tool": "flight_search", "from": "ber", "to": "BCN", "date": "2026-10-15", "return": "", "place": "x" * 200}), self.TODAY)
        self.assertEqual(plan, ("flight_search", {"from": "BER", "to": "BCN", "date": "2026-10-15"}, ""))
        self.assertEqual(wg.plan_data("flights to Rome", self.model({"tool": "flight_search", "to": "FCO", "date": "2020-01-01"}),
                                      self.TODAY)[2], "from")
        self.assertEqual(wg.plan_data("weather?", self.model({"tool": "weather"}), self.TODAY), ("weather", {}, "place"))
        self.assertIsNone(wg.plan_data("rm -rf", self.model({"tool": "shell"}), self.TODAY))


GRANTED = {"configured": True, "state": "granted", "limiter": "0xLIM", "dailyCapAtomic": 20_000_000,
           "perPurchaseCapAtomic": 5_000_000, "remainingTodayAtomic": 20_000_000, "chain": "solana"}


class AgentDataTests(unittest.TestCase):
    def setUp(self):
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
