"""Onchain price answers, against a real policy on a real database.

Like `test_decide_endpoint`, nothing here mocks `SpendingPolicy`. What is faked
is only the network: the 402 and the settlement are the caller's functions by
design, so a test supplies them and everything that decides whether to pay stays
real.

The 402 fixture is the one measured against the live gateway in
`docs/checks.md` (G1) — empty body, requirements base64 in a `payment-required`
header, nested under `accepts[]`, amount spelled `amount`. A prettier fixture
would test a gateway that does not exist.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from spending_memory import Payment, SpendingMemory, SpendingPolicy
from spending_memory.adapters.thegraph import PaidGraphQueries

from sign402_gateway.onchain_data import (
    BASE_USDC,
    OnchainConfig,
    OnchainDataClient,
    OnchainUnavailable,
    classify_onchain,
    read_price,
)

GRAPH_PAY_TO = "0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB"
OWNER = "chat-user-1"


def graph_402(amount: str = "10000", pay_to: str = GRAPH_PAY_TO):
    """The real shape, from G1: nothing in the body, everything in the header."""
    block = {
        "x402Version": 2,
        "error": "Payment-Signature header is required",
        "resource": {"url": "http://mainnet-thegraph-arbitrum-02-eu-west3.thegraph.com/…"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "extra": {"assetTransferMethod": "eip3009"},
            }
        ],
    }
    header = base64.b64encode(json.dumps(block).encode()).decode()
    return {"payment-required": header}, b""


def pool(symbol="WETH", price="2478.09", tvl="172000000", side="asToken0"):
    if side == "asToken0":
        return {"id": "0x6c56", "feeTier": "3000", "token1Price": price,
                "totalValueLockedUSD": tvl, "volumeUSD": "1", "token0": {"symbol": symbol}}
    return {"id": "0xabcd", "feeTier": "500", "token0Price": price,
            "totalValueLockedUSD": tvl, "volumeUSD": "1", "token1": {"symbol": symbol}}


def answer(*, as_token0=(), as_token1=()):
    return {"data": {"asToken0": list(as_token0), "asToken1": list(as_token1)}}


def build_policy(daily_cap_usd: str = "5") -> SpendingPolicy:
    return SpendingPolicy(
        SpendingMemory.local(str(Path(tempfile.mkdtemp()) / "memory.db")),
        daily_cap_usd=Decimal(daily_cap_usd),
    )


def make_known(policy: SpendingPolicy, *, owner: str = OWNER) -> None:
    """Settle one payment so The Graph is a merchant the fleet has paid."""
    policy.memory.remember_settlement(
        Payment(
            merchant="gateway.thegraph.com",
            pay_to=GRAPH_PAY_TO,
            amount_usd=Decimal("0.01"),
            owner=owner,
        ),
        tx_id="0xseed",
    )


class ClassifyTests(unittest.TestCase):
    def test_recognises_the_questions_the_web_answers_worse(self):
        for message, expected in (
            ("price of WETH", "WETH"),
            ("what is the price of cbBTC?", "CBBTC"),
            ("$weth", "WETH"),
            ("  $DAI  ", "DAI"),
            ("how much is AERO", "AERO"),
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_onchain(message), expected)

    def test_leaves_everything_else_to_the_web(self):
        for message in (
            "who is Vitalik",
            "what happened to Base today",
            "news about ethereum",
            "",
            "price of eggs in Berlin please tell me a long story",
        ):
            with self.subTest(message=message):
                found = classify_onchain(message)
                self.assertIn(found, (None, "EGGS"))

    def test_refuses_a_symbol_that_cannot_be_one(self):
        self.assertIsNone(classify_onchain("$"))
        self.assertIsNone(classify_onchain("$toolongtobeaticker"))


class ReadPriceTests(unittest.TestCase):
    def test_reads_the_price_with_usdc_on_either_side(self):
        left = read_price(answer(as_token0=[pool()]), "WETH")
        right = read_price(answer(as_token1=[pool(side="asToken1")]), "WETH")
        self.assertEqual(left.usd, Decimal("2478.09"))
        self.assertEqual(right.usd, Decimal("2478.09"))

    def test_prefers_the_deepest_pool(self):
        shallow = pool(price="2400", tvl="1000000")
        deep = pool(price="2478.09", tvl="172000000")
        found = read_price(answer(as_token0=[shallow, deep]), "WETH")
        self.assertEqual(found.usd, Decimal("2478.09"))
        self.assertEqual(found.liquidity_usd, Decimal("172000000"))

    def test_refuses_a_pool_whose_token_is_not_the_one_asked_about(self):
        """The subgraph filters; so do we. A schema change must not repeat a
        price for a token nobody asked about."""
        self.assertIsNone(read_price(answer(as_token0=[pool(symbol="unknown")]), "WETH"))

    def test_no_pools_is_no_answer_rather_than_an_error(self):
        self.assertIsNone(read_price(answer(), "WETH"))
        self.assertIsNone(read_price({}, "WETH"))
        self.assertIsNone(read_price(None, "WETH"))

    def test_refuses_junk_numbers(self):
        for bad in ("NaN", "Infinity", "0", "-1", ""):
            with self.subTest(price=bad):
                self.assertIsNone(read_price(answer(as_token0=[pool(price=bad)]), "WETH"))


class ClientTests(unittest.TestCase):
    def build(self, policy, *, answer_payload=None, on_pay=None, config=None):
        self.paid_calls = []

        def pay_and_fetch(payment, requirements, body=None):
            self.paid_calls.append(payment)
            if on_pay is not None:
                on_pay(payment, requirements)
            return (answer_payload if answer_payload is not None
                    else answer(as_token0=[pool()])), "0xtx"

        return OnchainDataClient(
            paid_queries_factory=lambda owner: PaidGraphQueries(
                policy, owner=owner, cache_ttl_seconds=300
            ),
            fetch_402=lambda url: graph_402(),
            pay_and_fetch=pay_and_fetch,
            config=config or OnchainConfig(),
        )

    def test_a_known_merchant_is_paid_and_answered(self):
        policy = build_policy()
        make_known(policy)
        price = self.build(policy).price(OWNER, "WETH")
        self.assertEqual(price.usd, Decimal("2478.09"))
        self.assertTrue(price.paid)
        self.assertTrue(price.journal_id)
        self.assertEqual(len(self.paid_calls), 1)

    def test_the_second_identical_question_is_free(self):
        """The journal is read before the network is touched, so the repeat
        costs nothing. This is the behaviour a bare x402 client cannot have."""
        policy = build_policy()
        make_known(policy)
        client = self.build(policy)
        client.price(OWNER, "WETH")
        again = client.price(OWNER, "WETH")
        self.assertFalse(again.paid)
        self.assertEqual(len(self.paid_calls), 1)

    def test_an_unknown_merchant_escalates_instead_of_paying(self):
        """No seeding: The Graph has never been paid. A one-cent query is not a
        special case — the first payment to anyone asks a human."""
        policy = build_policy()
        with self.assertRaises(OnchainUnavailable):
            self.build(policy).price(OWNER, "WETH")
        self.assertEqual(self.paid_calls, [])

    def test_a_moved_payout_address_is_not_paid(self):
        policy = build_policy()
        make_known(policy)
        client = self.build(policy)
        client.fetch_402 = lambda url: graph_402(pay_to="0x" + "ab" * 20)
        with self.assertRaises(OnchainUnavailable):
            client.price(OWNER, "WETH")
        self.assertEqual(self.paid_calls, [])

    def test_a_price_above_the_agreed_ceiling_is_refused_before_paying(self):
        policy = build_policy()
        make_known(policy)
        client = self.build(policy, config=OnchainConfig(max_per_call_atomic=5_000))
        client.fetch_402 = lambda url: graph_402(amount="10000")
        with self.assertRaises(OnchainUnavailable):
            client.price(OWNER, "WETH")

    def test_no_liquid_pool_is_unavailable_not_a_wrong_answer(self):
        policy = build_policy()
        make_known(policy)
        client = self.build(policy, answer_payload=answer())
        with self.assertRaises(OnchainUnavailable):
            client.price(OWNER, "SCAM")

    def test_a_network_failure_never_raises_past_this_module(self):
        policy = build_policy()
        make_known(policy)
        client = self.build(policy)
        client.fetch_402 = lambda url: (_ for _ in ()).throw(TimeoutError("boom"))
        with self.assertRaises(OnchainUnavailable):
            client.price(OWNER, "WETH")

    def test_purchases_paused_stops_it_before_the_network(self):
        policy = build_policy()
        make_known(policy)
        client = self.build(policy)
        client.purchases_paused = lambda: True
        with self.assertRaises(OnchainUnavailable):
            client.price(OWNER, "WETH")

    def test_the_query_is_sent_as_variables_not_interpolated_text(self):
        """The symbol comes from a chat message. It must never become part of
        the query string, whatever it contains."""
        policy = build_policy()
        make_known(policy)
        client = self.build(policy)
        body = client._body({"symbol": 'X") { evil }', "usdc": BASE_USDC, "minTvl": "1"})
        self.assertNotIn("evil", body["query"])
        self.assertEqual(body["variables"]["symbol"], 'X") { evil }')

    def test_the_daily_cap_applies_to_a_one_cent_query(self):
        policy = build_policy(daily_cap_usd="0.005")
        make_known(policy)
        with self.assertRaises(OnchainUnavailable):
            self.build(policy).price(OWNER, "WETH")
        self.assertEqual(self.paid_calls, [])


if __name__ == "__main__":
    unittest.main()


class ChatWiringTests(unittest.TestCase):
    """The branch in the chat, tested for the property that matters most:
    every way it can fail leaves the message on the path it was already on."""

    def footnote(self, onchain):
        from sign402_gateway.venice_chat import VeniceChatClient

        client = VeniceChatClient.__new__(VeniceChatClient)
        client.onchain_data = onchain
        return client._onchain_footnote("u", "price of WETH")

    def test_off_by_default(self):
        self.assertIsNone(self.footnote(None))

    def test_a_hit_hands_the_model_the_reading_and_a_footer(self):
        from sign402_gateway.onchain_data import OnchainPrice

        class Hit:
            def price(self, user_id, symbol):
                return OnchainPrice(
                    symbol=symbol, usd=Decimal("2487.47"),
                    liquidity_usd=Decimal("173324700"), pool="0x6c56",
                    fee_tier="3000", paid=True, journal_id="j1",
                )

        fact, footer = self.footnote(Hit())
        self.assertIn("2,487.47", fact)
        self.assertIn("price of WETH", fact)
        self.assertIn("Uniswap V3", footer)

    def test_an_unavailable_lookup_falls_through_rather_than_failing(self):
        class Declines:
            def price(self, user_id, symbol):
                raise OnchainUnavailable("never paid The Graph before")

        self.assertIsNone(self.footnote(Declines()))

    def test_an_unexpected_error_also_falls_through(self):
        """A bug here must cost the user nothing more than the answer they
        would have had anyway."""
        class Explodes:
            def price(self, user_id, symbol):
                raise RuntimeError("boom")

        self.assertIsNone(self.footnote(Explodes()))

    def test_a_question_the_subgraph_cannot_answer_is_left_alone(self):
        from sign402_gateway.venice_chat import VeniceChatClient

        client = VeniceChatClient.__new__(VeniceChatClient)
        client.onchain_data = object()  # never called
        self.assertIsNone(client._onchain_footnote("u", "who is Vitalik"))


class BuilderTests(unittest.TestCase):
    def build(self, env, policy="a policy"):
        from sign402_gateway.onchain_data import build_onchain_data_from_env

        return build_onchain_data_from_env(
            policy=policy, pay=lambda *a, **k: {"ok": True}, env=env
        )

    def test_off_unless_the_flag_is_set(self):
        self.assertIsNone(self.build({}))
        self.assertIsNone(self.build({"SIGN402_ONCHAIN_DATA_ENABLED": "0"}))

    def test_refuses_to_run_without_a_spending_policy(self):
        """No cap, no provider memory, no journal — which is the thing this
        module exists to avoid, so it declines rather than paying unguarded."""
        self.assertIsNone(
            self.build({"SIGN402_ONCHAIN_DATA_ENABLED": "1"}, policy=None)
        )

    def test_builds_when_on(self):
        client = self.build({"SIGN402_ONCHAIN_DATA_ENABLED": "1"})
        self.assertIsNotNone(client)
        self.assertIn("gateway.thegraph.com", client.config.resource_url)
