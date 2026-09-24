import base64
import json
import tempfile
import unittest
from pathlib import Path

from sign402_gateway.chat_store import ChatStore
from sign402_gateway.venice_chat import (
    DEFAULT_MODEL,
    ChatState,
    MerchantChanged,
    PrefundFailed,
    ProviderUnavailable,
    ReconciliationRequired,
    VeniceChatClient,
    VeniceConfig,
    WindowExhausted,
)


DAY_ONE_NOON = 1786_400_000 - (1786_400_000 % 86_400) + 43_200

BOUND_PAY_TO = "0x2670b922ef37c7df47158725c0cc407b5382293f"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
NETWORK = "eip155:8453"

WALLET = "0x00000000000000000000000000000000000000aa"


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def challenge(
    *, pay_to=BOUND_PAY_TO, network=NETWORK, asset=USDC, amount="5000000"
):
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "asset": asset,
                "amount": amount,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
        "extensions": {
            "sign-in-with-x": {
                "info": {
                    "domain": "api.venice.ai",
                    "uri": "https://api.venice.ai/api/v1/x402/top-up",
                    "version": "1",
                    "nonce": "test-nonce",
                    "issuedAt": "2026-08-13T01:00:00.000Z",
                    "expirationTime": "2026-08-13T01:05:00.000Z",
                    "statement": "Sign in to Venice AI",
                }
            }
        },
    }


ANSWER = {
    "choices": [{"message": {"role": "assistant", "content": "an answer"}}]
}


class VeniceChatTestCase(unittest.TestCase):
    def setUp(self):
        self.buy_calls = []
        self.requests = []
        self.signed = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # -- doubles ---------------------------------------------------------

    def make_store(self, **kwargs):
        store = ChatStore(
            Path(self.tmp.name) / "chat.db",
            now=lambda: DAY_ONE_NOON,
            **kwargs,
        )
        self.addCleanup(store.close)
        return store

    def make_client(
        self,
        *,
        store=None,
        challenge_pay_to=BOUND_PAY_TO,
        challenge_network=NETWORK,
        challenge_asset=USDC,
        chunk_atomic=5_000_000,
        max_outstanding_atomic=5_000_000,
        daily_cap_atomic=5_000_000,
        balance_atomic=0,
        chat_response=None,
        chat_status=200,
        balance_remaining="4.997",
        settle=None,
        purchases_paused=False,
    ):
        self.store = store or self.make_store()

        def transport(method, url, *, headers=None, json_body=None):
            self.requests.append((method, url, headers or {}, json_body))
            if "/x402/balance/" in url:
                # The shape Venice actually returns, copied from a live call.
                return FakeResponse(
                    200,
                    {
                        "success": True,
                        "data": {
                            "walletAddress": WALLET,
                            "balanceUsd": balance_atomic / 1_000_000,
                            "canConsume": balance_atomic >= 100_000,
                            "minimumTopUpUsd": 5,
                            "suggestedTopUpUsd": 10,
                        },
                    },
                )
            if url.endswith("/x402/top-up"):
                if headers and "X-402-Payment" in headers:
                    return FakeResponse(200, {"ok": True})
                return FakeResponse(
                    402,
                    challenge(
                        pay_to=challenge_pay_to,
                        network=challenge_network,
                        asset=challenge_asset,
                    ),
                )
            if url.endswith("/chat/completions"):
                if chat_status != 200:
                    return FakeResponse(chat_status, {"error": "boom"})
                return FakeResponse(
                    200,
                    chat_response if chat_response is not None else ANSWER,
                    {"X-Balance-Remaining": balance_remaining},
                )
            raise AssertionError(f"unexpected request to {url}")

        def default_settle(payment_requirements, *, user_id):
            # The buyer performs the paid POST itself and reports the outcome.
            self.buy_calls.append(payment_requirements)
            return {"ok": True, "status": 200}

        def signer(address, message):
            self.signed.append((address, message))
            return "0x" + "11" * 65

        return VeniceChatClient(
            store=self.store,
            transport=transport,
            signer=signer,
            settle=settle or default_settle,
            config=VeniceConfig(
                bound_pay_to=BOUND_PAY_TO,
                network=NETWORK,
                asset=USDC,
                chunk_atomic=chunk_atomic,
                max_outstanding_atomic=max_outstanding_atomic,
                daily_cap_atomic=daily_cap_atomic,
            ),
            purchases_paused=lambda: purchases_paused,
        )

    def session_bound_to(self, pay_to, *, user_id="u1", credit=0):
        self.store.bind_policy(user_id, policy_hash="a" * 64, pay_to=pay_to)
        if credit:
            self.store.record_prefund(user_id, credit)
        return user_id


class MerchantBindingTests(VeniceChatTestCase):
    def test_challenge_with_unexpected_pay_to_pauses_and_moves_no_funds(self):
        client = self.make_client(challenge_pay_to="0xdead")
        user = self.session_bound_to(BOUND_PAY_TO)
        with self.assertRaises(MerchantChanged):
            client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])
        session = self.store.get_session(user)
        self.assertTrue(session.paused)
        self.assertEqual(session.pause_reason, ChatState.MERCHANT_CHANGED)

    def test_network_mismatch_is_refused(self):
        client = self.make_client(challenge_network="eip155:1")
        user = self.session_bound_to(BOUND_PAY_TO)
        with self.assertRaises(MerchantChanged):
            client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])

    def test_asset_mismatch_is_refused(self):
        client = self.make_client(challenge_asset="0xbadc0ffee")
        user = self.session_bound_to(BOUND_PAY_TO)
        with self.assertRaises(MerchantChanged):
            client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])

    def test_pay_to_comparison_ignores_case(self):
        client = self.make_client(challenge_pay_to=BOUND_PAY_TO.upper())
        user = self.session_bound_to(BOUND_PAY_TO)
        result = client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(len(self.buy_calls), 1)
        self.assertTrue(result.prefunded)

    def test_a_paused_session_refuses_before_any_request(self):
        client = self.make_client()
        user = self.session_bound_to(BOUND_PAY_TO)
        self.store.pause(user, ChatState.MERCHANT_CHANGED)
        with self.assertRaises(MerchantChanged):
            client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.buy_calls, [])


class PrefundTests(VeniceChatTestCase):
    def test_local_credit_is_used_without_settling(self):
        client = self.make_client(balance_atomic=500_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=500_000)
        client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])

    def top_up_requests(self):
        return [r for r in self.requests if r[1].endswith("/x402/top-up")]

    def test_prefund_exceeding_remaining_window_is_refused_before_settlement(self):
        client = self.make_client(daily_cap_atomic=5_000_000)
        user = self.session_bound_to(BOUND_PAY_TO)
        # A prefund already consumed today's whole window.
        self.store.record_prefund(user, 5_000_000)
        self.store.debit(user, 5_000_000)
        with self.assertRaises(WindowExhausted):
            client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])
        self.assertEqual(self.top_up_requests(), [])

    def test_a_chunk_larger_than_what_is_left_today_is_refused_unasked(self):
        # Room left in the window, but not enough for a whole chunk. The
        # refusal must happen before the challenge is even requested.
        client = self.make_client(
            daily_cap_atomic=7_000_000,
            chunk_atomic=5_000_000,
            max_outstanding_atomic=5_000_000,
        )
        user = self.session_bound_to(BOUND_PAY_TO)
        self.store.record_prefund(user, 5_000_000)
        self.store.debit(user, 5_000_000)
        with self.assertRaises(WindowExhausted):
            client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])
        self.assertEqual(self.top_up_requests(), [])

    def test_prefund_exceeding_max_outstanding_is_refused(self):
        client = self.make_client(
            max_outstanding_atomic=1_000_000, chunk_atomic=5_000_000
        )
        user = self.session_bound_to(BOUND_PAY_TO)
        with self.assertRaises(PrefundFailed):
            client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])
        self.assertEqual(self.top_up_requests(), [])

    def test_a_successful_prefund_counts_against_the_window_immediately(self):
        client = self.make_client()
        user = self.session_bound_to(BOUND_PAY_TO)
        client.send(user, "hi", wallet_address=WALLET)
        session = self.store.get_session(user)
        self.assertEqual(session.spent_atomic_this_window, 5_000_000)

    def test_global_pause_stops_settlement_before_any_funding(self):
        client = self.make_client(purchases_paused=True)
        user = self.session_bound_to(BOUND_PAY_TO)
        with self.assertRaises(PrefundFailed):
            client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])
        self.assertEqual(self.requests, [])


class MeteringTests(VeniceChatTestCase):
    def test_actual_cost_is_debited_from_the_balance_remaining_header(self):
        client = self.make_client(balance_remaining="4.997")
        user = self.session_bound_to(BOUND_PAY_TO)
        result = client.send(user, "hi", wallet_address=WALLET)
        # $5.00 prefunded, $4.997 left on Venice's side => $0.003 consumed.
        self.assertEqual(result.cost_atomic, 3_000)
        self.assertEqual(
            self.store.get_session(user).outstanding_atomic, 4_997_000
        )

    def test_answer_text_is_returned(self):
        client = self.make_client()
        user = self.session_bound_to(BOUND_PAY_TO)
        result = client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(result.text, "an answer")

    def test_a_missing_balance_header_falls_back_without_crashing(self):
        client = self.make_client(balance_remaining=None, balance_atomic=500_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=500_000)
        result = client.send(user, "hi", wallet_address=WALLET)
        self.assertGreaterEqual(result.cost_atomic, 0)


class SignInWithXTests(VeniceChatTestCase):
    def test_chat_requests_carry_the_signed_header(self):
        client = self.make_client(balance_atomic=500_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=500_000)
        client.send(user, "hi", wallet_address=WALLET)
        chat = [r for r in self.requests if r[1].endswith("/chat/completions")]
        header = chat[-1][2]["X-Sign-In-With-X"]
        decoded = json.loads(base64.b64decode(header))
        self.assertEqual(decoded["address"], WALLET)
        self.assertIn("signature", decoded)
        self.assertIn("api.venice.ai", decoded["message"])

    def test_the_signed_message_is_eip4361_shaped(self):
        client = self.make_client(balance_atomic=500_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=500_000)
        client.send(user, "hi", wallet_address=WALLET)
        _, message = self.signed[-1]
        self.assertIn("URI:", message)
        self.assertIn("Nonce:", message)
        self.assertIn("Chain ID:", message)


class FailureStateTests(VeniceChatTestCase):
    def test_provider_5xx_leaves_credit_intact_and_does_not_pause(self):
        client = self.make_client(chat_status=503, balance_atomic=500_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=500_000)
        with self.assertRaises(ProviderUnavailable):
            client.send(user, "hi", wallet_address=WALLET)
        session = self.store.get_session(user)
        self.assertEqual(session.outstanding_atomic, 500_000)
        self.assertFalse(session.paused)

    def test_settlement_failure_records_no_prefund_and_does_not_pause(self):
        def failing_settle(payment_requirements, *, user_id):
            raise RuntimeError("facilitator rejected")

        client = self.make_client(settle=failing_settle)
        user = self.session_bound_to(BOUND_PAY_TO)
        with self.assertRaises(PrefundFailed):
            client.send(user, "hi", wallet_address=WALLET)
        session = self.store.get_session(user)
        self.assertEqual(session.spent_atomic_this_window, 0)
        self.assertEqual(session.outstanding_atomic, 0)
        self.assertFalse(session.paused)

    def test_settled_but_answer_never_arrived_pauses_without_retry(self):
        client = self.make_client(chat_status=503)
        user = self.session_bound_to(BOUND_PAY_TO)
        with self.assertRaises(ReconciliationRequired):
            client.send(user, "hi", wallet_address=WALLET)
        session = self.store.get_session(user)
        self.assertTrue(session.paused)
        self.assertEqual(session.pause_reason, ChatState.RECONCILIATION_REQUIRED)
        # The prefund was paid, so the credit is preserved, not written off.
        self.assertEqual(session.outstanding_atomic, 5_000_000)
        # Exactly one chat attempt: never retried automatically.
        chat = [r for r in self.requests if r[1].endswith("/chat/completions")]
        self.assertEqual(len(chat), 1)

    def test_only_reconciliation_pauses_among_the_failure_states(self):
        for status, error in ((503, ProviderUnavailable),):
            with self.subTest(status=status):
                client = self.make_client(
                    chat_status=status, balance_atomic=500_000
                )
                user = self.session_bound_to(BOUND_PAY_TO, credit=500_000)
                with self.assertRaises(error):
                    client.send(user, "hi", wallet_address=WALLET)
                self.assertFalse(self.store.get_session(user).paused)


class PrivacyTests(VeniceChatTestCase):
    def test_prompt_text_never_reaches_the_store(self):
        client = self.make_client()
        user = self.session_bound_to(BOUND_PAY_TO)
        client.send(user, "my secret prompt", wallet_address=WALLET)
        with open(Path(self.tmp.name) / "chat.db", "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"my secret prompt", raw)
        self.assertNotIn(b"an answer", raw)

    def test_errors_do_not_carry_prompt_text(self):
        client = self.make_client(challenge_pay_to="0xdead")
        user = self.session_bound_to(BOUND_PAY_TO)
        try:
            client.send(user, "my secret prompt", wallet_address=WALLET)
        except MerchantChanged as exc:
            self.assertNotIn("my secret prompt", str(exc))


if __name__ == "__main__":
    unittest.main()


class PolicyExpiryTests(VeniceChatTestCase):
    def test_an_expired_policy_refuses_before_the_402_request_is_made(self):
        from sign402_gateway.venice_chat import PolicyExpired, build_chat_policy

        client = self.make_client()
        policy = build_chat_policy(
            pay_to=BOUND_PAY_TO,
            network=NETWORK,
            asset=USDC,
            daily_cap_atomic=5_000_000,
            expires_at=DAY_ONE_NOON + 10,
        )
        self.store.approve_policy("u1", policy)
        self.store.now = lambda: DAY_ONE_NOON + 11

        with self.assertRaises(PolicyExpired):
            client.send("u1", "hi", wallet_address=WALLET)

        self.assertEqual(self.requests, [])
        self.assertEqual(self.buy_calls, [])

    def test_the_expiry_refusal_points_at_the_credit_still_owed(self):
        from sign402_gateway.venice_chat import PolicyExpired, build_chat_policy

        client = self.make_client()
        self.store.approve_policy(
            "u1",
            build_chat_policy(
                pay_to=BOUND_PAY_TO,
                network=NETWORK,
                asset=USDC,
                daily_cap_atomic=5_000_000,
                expires_at=DAY_ONE_NOON + 10,
            ),
        )
        self.store.record_prefund("u1", 5_000_000)
        self.store.now = lambda: DAY_ONE_NOON + 11

        with self.assertRaises(PolicyExpired) as caught:
            client.send("u1", "hi", wallet_address=WALLET)

        self.assertIn("still yours", str(caught.exception))
        self.assertEqual(self.store.claimable_credit_atomic("u1"), 5_000_000)


class RaiseLimitTests(VeniceChatTestCase):
    def policy(self, cap):
        from sign402_gateway.venice_chat import build_chat_policy

        return build_chat_policy(
            pay_to=BOUND_PAY_TO,
            network=NETWORK,
            asset=USDC,
            daily_cap_atomic=cap,
            expires_at=DAY_ONE_NOON + 86_400 * 30,
        )

    def test_the_approved_cap_beats_the_configured_default(self):
        # Config says $5/day; the user approved $10/day, so a second $5 top-up
        # inside the same window must be allowed.
        client = self.make_client(daily_cap_atomic=5_000_000)
        self.store.approve_policy("u1", self.policy(10_000_000))
        self.store.record_prefund("u1", 5_000_000)
        self.store.debit("u1", 5_000_000)

        client.send("u1", "hi", wallet_address=WALLET)

        self.assertEqual(len(self.buy_calls), 1)
        self.assertEqual(
            self.store.get_session("u1").spent_atomic_this_window, 10_000_000
        )

    def test_without_a_raise_the_second_top_up_is_refused(self):
        client = self.make_client(daily_cap_atomic=5_000_000)
        self.store.approve_policy("u1", self.policy(5_000_000))
        self.store.record_prefund("u1", 5_000_000)
        self.store.debit("u1", 5_000_000)

        with self.assertRaises(WindowExhausted):
            client.send("u1", "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])


class PolicyApprovalDeliveryTests(unittest.TestCase):
    """Task 6 Step 2: the policy is approved through the existing channel."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ChatStore(
            Path(self.tmp.name) / "chat.db", now=lambda: DAY_ONE_NOON
        )
        self.addCleanup(self.store.close)
        self.requests = []

    def make_service(self, *, approved=True, ok=True, text="Approved."):
        from sign402_gateway.venice_chat import ChatPolicyApprovalService

        class FakeApprovals:
            def __init__(self, requests):
                self.requests = requests

            def request_hash_approval(
                self, *, telegram_user_id, action_type, commitment_hash, context_lines
            ):
                self.requests.append(
                    {
                        "user": telegram_user_id,
                        "actionType": action_type,
                        "hash": commitment_hash,
                        "context": list(context_lines),
                    }
                )
                return {
                    "ok": ok,
                    "approved": approved,
                    "approvedHash": commitment_hash if approved else "",
                    "telegramText": text,
                }

        return ChatPolicyApprovalService(
            store=self.store,
            approval_service=FakeApprovals(self.requests),
            pay_to=BOUND_PAY_TO,
            network=NETWORK,
            asset=USDC,
            now=lambda: DAY_ONE_NOON,
        )

    def test_approval_goes_through_the_shared_hash_approval_path(self):
        service = self.make_service()

        service.approve("u1", daily_cap_atomic=5_000_000, days=30)

        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["actionType"], "sign402_chat_policy")
        self.assertEqual(self.requests[0]["user"], "u1")

    def test_the_commitment_hash_is_the_policy_hash(self):
        from sign402_gateway.venice_chat import build_chat_policy

        service = self.make_service()
        result = service.approve("u1", daily_cap_atomic=5_000_000, days=30)

        expected = build_chat_policy(
            pay_to=BOUND_PAY_TO,
            network=NETWORK,
            asset=USDC,
            daily_cap_atomic=5_000_000,
            expires_at=DAY_ONE_NOON + 30 * 86_400,
        )
        self.assertEqual(self.requests[0]["hash"], expected.policy_hash)
        self.assertTrue(result["ok"])

    def test_the_approver_sees_merchant_cap_and_expiry(self):
        service = self.make_service()
        service.approve("u1", daily_cap_atomic=5_000_000, days=30)

        context = "\n".join(self.requests[0]["context"])
        self.assertIn("Venice AI", context)
        self.assertIn("$5.00", context)
        self.assertIn("standing", context.lower())

    def test_an_approved_policy_is_bound_and_stored(self):
        service = self.make_service(approved=True)

        service.approve("u1", daily_cap_atomic=5_000_000, days=30)

        session = self.store.get_session("u1")
        self.assertEqual(session.bound_pay_to, BOUND_PAY_TO)
        self.assertEqual(session.daily_cap_atomic, 5_000_000)
        self.assertEqual(session.policy_expires_at, DAY_ONE_NOON + 30 * 86_400)

    def test_a_declined_policy_binds_nothing(self):
        service = self.make_service(approved=False)

        result = service.approve("u1", daily_cap_atomic=5_000_000, days=30)

        self.assertFalse(result["ok"])
        session = self.store.get_session("u1")
        self.assertEqual(session.bound_pay_to, "")
        self.assertEqual(session.daily_cap_atomic, 0)

    def test_a_policy_without_an_expiry_never_reaches_the_approver(self):
        from sign402_gateway.venice_chat import PolicyRejected

        service = self.make_service()
        with self.assertRaises(PolicyRejected):
            service.approve("u1", daily_cap_atomic=5_000_000, days=0)
        self.assertEqual(self.requests, [])

    def test_a_zero_cap_never_reaches_the_approver(self):
        from sign402_gateway.venice_chat import PolicyRejected

        service = self.make_service()
        with self.assertRaises(PolicyRejected):
            service.approve("u1", daily_cap_atomic=0, days=30)
        self.assertEqual(self.requests, [])

    def test_raising_the_limit_asks_again(self):
        service = self.make_service()
        service.approve("u1", daily_cap_atomic=5_000_000, days=30)
        service.approve("u1", daily_cap_atomic=10_000_000, days=30)

        self.assertEqual(len(self.requests), 2)
        self.assertNotEqual(self.requests[0]["hash"], self.requests[1]["hash"])
        self.assertEqual(
            self.store.get_session("u1").daily_cap_atomic, 10_000_000
        )

    def test_a_declined_raise_leaves_the_old_cap_in_place(self):
        granted = self.make_service(approved=True)
        granted.approve("u1", daily_cap_atomic=5_000_000, days=30)

        refused = self.make_service(approved=False)
        refused.approve("u1", daily_cap_atomic=10_000_000, days=30)

        self.assertEqual(
            self.store.get_session("u1").daily_cap_atomic, 5_000_000
        )


class PolicyDecisionTextTests(unittest.TestCase):
    def test_an_approved_policy_reads_as_a_standing_allowance(self):
        from sign402_gateway.imessage_approvals import _decision_text

        text = _decision_text("sign402_chat_policy", "approved")
        self.assertIn("daily", text.lower())
        self.assertNotIn("purchase is being processed", text)

    def test_a_declined_policy_says_no_funds_moved(self):
        from sign402_gateway.imessage_approvals import _decision_text

        self.assertIn(
            "no funds were moved",
            _decision_text("sign402_chat_policy", "denied").lower(),
        )


BOUND_ABBREV = "0x2670…293f"


def payto_change(
    *, slug="venice-ai", removed=BOUND_ABBREV, added="0xdead…beef",
    observed_at="2026-08-12T00:32:06.721Z",
):
    """A change record shaped exactly like the live x402-list feed.

    Note the addresses: the feed only ever publishes abbreviated forms, in the
    summary and in both snapshots. There is no full address to be had.
    """
    return {
        "slug": slug,
        "name": "Venice AI",
        "type": "payto_changed",
        "observed_at": observed_at,
        "summary": {"payToAdded": [added], "payToRemoved": [removed]},
        "old_snapshot": [{"payTo": removed, "asset": USDC}],
        "new_snapshot": [{"payTo": added, "asset": USDC}],
    }


class PayToWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ChatStore(
            Path(self.tmp.name) / "chat.db", now=lambda: DAY_ONE_NOON
        )
        self.addCleanup(self.store.close)
        self.notices = []
        self.fetches = []

    def make_watcher(self, changes, *, slug="venice-ai"):
        from sign402_gateway.venice_chat import PayToWatcher

        def fetch(**kwargs):
            self.fetches.append(kwargs)
            return list(changes)

        return PayToWatcher(
            store=self.store,
            fetch_changes=fetch,
            merchant_slug=slug,
            bound_pay_to=BOUND_PAY_TO,
            notify=self.notices.append,
        )

    def bind(self, *users):
        for user in users:
            self.store.bind_policy(
                user, policy_hash="a" * 64, pay_to=BOUND_PAY_TO
            )

    def test_a_payto_change_pauses_every_policy_bound_to_the_old_address(self):
        self.bind("u1", "u2", "u3")
        watcher = self.make_watcher([payto_change()])

        watcher.poll()

        for user in ("u1", "u2", "u3"):
            session = self.store.get_session(user)
            self.assertTrue(session.paused, user)
            self.assertEqual(session.pause_reason, ChatState.MERCHANT_CHANGED)

    def test_it_notifies_once_not_per_policy(self):
        self.bind("u1", "u2", "u3")
        watcher = self.make_watcher([payto_change()])

        watcher.poll()

        self.assertEqual(len(self.notices), 1)
        self.assertEqual(sorted(self.notices[0]["users"]), ["u1", "u2", "u3"])

    def test_it_notifies_once_not_per_poll(self):
        self.bind("u1")
        watcher = self.make_watcher([payto_change()])

        watcher.poll()
        watcher.poll()
        watcher.poll()

        self.assertEqual(len(self.notices), 1)

    def test_the_same_event_is_not_re_handled_after_a_restart(self):
        from sign402_gateway.venice_chat import PayToWatcher

        self.bind("u1")
        self.make_watcher([payto_change()]).poll()
        self.assertEqual(len(self.notices), 1)

        reopened = PayToWatcher(
            store=self.store,
            fetch_changes=lambda **kwargs: [payto_change()],
            merchant_slug="venice-ai",
            bound_pay_to=BOUND_PAY_TO,
            notify=self.notices.append,
        )
        reopened.poll()

        self.assertEqual(len(self.notices), 1)

    def test_a_later_event_is_handled_even_after_an_earlier_one(self):
        self.bind("u1")
        watcher = self.make_watcher([payto_change()])
        watcher.poll()

        watcher.fetch_changes = lambda **kwargs: [
            payto_change(observed_at="2026-08-13T00:00:00.000Z")
        ]
        watcher.poll()

        self.assertEqual(len(self.notices), 2)

    def test_a_change_for_another_merchant_is_ignored(self):
        self.bind("u1")
        watcher = self.make_watcher(
            [payto_change(slug="some-other-service", removed="0xaaaa…bbbb")]
        )

        watcher.poll()

        self.assertFalse(self.store.get_session("u1").paused)
        self.assertEqual(self.notices, [])

    def test_it_asks_the_feed_only_for_payto_changes(self):
        watcher = self.make_watcher([])
        watcher.poll()

        self.assertEqual(self.fetches[0].get("change_type"), "payto_changed")

    def test_the_abbreviated_address_is_matched_against_the_bound_one(self):
        # The feed never publishes full addresses, so the match is head+tail.
        self.bind("u1")
        watcher = self.make_watcher(
            [payto_change(slug="unknown-slug", removed="0x2670…293F")]
        )

        watcher.poll()

        self.assertTrue(self.store.get_session("u1").paused)

    def test_an_unrelated_abbreviation_does_not_match(self):
        self.bind("u1")
        watcher = self.make_watcher(
            [payto_change(slug="unknown-slug", removed="0x2670…ffff")]
        )

        watcher.poll()

        self.assertFalse(self.store.get_session("u1").paused)

    def test_the_new_address_is_never_bound_from_the_feed(self):
        # The feed only has "0xdead…beef". Migrating to a truncated address is
        # not possible, and migrating silently is not permitted anyway.
        self.bind("u1")
        watcher = self.make_watcher([payto_change()])

        watcher.poll()

        session = self.store.get_session("u1")
        self.assertEqual(session.bound_pay_to, BOUND_PAY_TO)
        self.assertNotIn("…", session.bound_pay_to)

    def test_the_notice_tells_the_user_a_fresh_approval_is_needed(self):
        self.bind("u1")
        watcher = self.make_watcher([payto_change()])

        watcher.poll()

        text = self.notices[0]["telegramText"].lower()
        self.assertIn("venice", text)
        self.assertIn("approve", text)
        self.assertNotIn("x402", text)

    def test_users_bound_elsewhere_are_untouched(self):
        self.bind("u1")
        self.store.bind_policy("other", policy_hash="b" * 64, pay_to="0xfeed")
        watcher = self.make_watcher([payto_change()])

        watcher.poll()

        self.assertTrue(self.store.get_session("u1").paused)
        self.assertFalse(self.store.get_session("other").paused)

    def test_a_feed_failure_does_not_pause_anyone(self):
        from sign402_gateway.venice_chat import PayToWatcher

        self.bind("u1")
        def failing(**kwargs):
            raise RuntimeError("feed down")

        watcher = PayToWatcher(
            store=self.store,
            fetch_changes=failing,
            merchant_slug="venice-ai",
            bound_pay_to=BOUND_PAY_TO,
            notify=self.notices.append,
        )

        self.assertFalse(watcher.poll())
        self.assertFalse(self.store.get_session("u1").paused)
        self.assertEqual(self.notices, [])


class LiveChallengeIsTheSameEventTests(VeniceChatTestCase):
    def test_an_unexpected_live_payto_pauses_like_a_feed_event(self):
        # The feed lags, so a challenge that disagrees is treated as the change
        # itself rather than waited on.
        client = self.make_client(challenge_pay_to="0xdeadbeefdeadbeefdead")
        user = self.session_bound_to(BOUND_PAY_TO)

        with self.assertRaises(MerchantChanged):
            client.send(user, "hi", wallet_address=WALLET)

        session = self.store.get_session(user)
        self.assertTrue(session.paused)
        self.assertEqual(session.pause_reason, ChatState.MERCHANT_CHANGED)
        self.assertEqual(self.buy_calls, [])


class ChatServiceTests(unittest.TestCase):
    """The facade the gateway routes call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ChatStore(
            Path(self.tmp.name) / "chat.db",
            now=lambda: DAY_ONE_NOON,
        )
        self.addCleanup(self.store.close)
        self.signed = []
        self.decrypt_calls = []

    def make_service(self, **client_kwargs):
        from sign402_gateway.venice_chat import ChatService

        outer = self

        class FakeWallets:
            def wallet_status(self, user_id):
                return {"ok": True, "wallet": {"address": WALLET}}

            def decrypt_private_key_for_future_signing(self, user_id):
                outer.decrypt_calls.append(user_id)
                return "0x" + "22" * 32

        class FakeConfig:
            # The real client always carries one; start() reads the default
            # model from it.
            model = DEFAULT_MODEL

        class FakeClient:
            config = FakeConfig()

            def __init__(self):
                self.sent = []

            def send(self, user_id, prompt, *, wallet_address):
                self.sent.append(("paid", user_id, wallet_address))
                return ChatResultStub(cost=3_000)


        from dataclasses import dataclass

        @dataclass
        class ChatResultStub:
            cost: int
            text: str = "an answer"
            prefunded: bool = False
            remaining_window_atomic: int = 5_000_000
            outstanding_atomic: int = 0

            @property
            def cost_atomic(self):
                return self.cost

        self.client = FakeClient()
        return ChatService(
            store=self.store,
            client=self.client,
            wallet_service=FakeWallets(),
            daily_cap_atomic=5_000_000,
        )

    def test_start_reports_the_policy_and_cap(self):
        service = self.make_service()

        result = service.start("u1")

        self.assertTrue(result["ok"])
        self.assertFalse(result["hasPolicy"])
        self.assertEqual(result["dailyCapAtomic"], 5_000_000)

    def test_start_reports_an_existing_policy(self):
        from sign402_gateway.venice_chat import build_chat_policy

        service = self.make_service()
        self.store.approve_policy(
            "u1",
            build_chat_policy(
                pay_to=BOUND_PAY_TO,
                network=NETWORK,
                asset=USDC,
                daily_cap_atomic=10_000_000,
                expires_at=DAY_ONE_NOON + 86_400,
            ),
        )

        result = service.start("u1")

        self.assertTrue(result["hasPolicy"])
        self.assertEqual(result["dailyCapAtomic"], 10_000_000)

    def test_send_passes_the_users_own_wallet_address(self):
        service = self.make_service()

        service.send("u1", "hi")

        self.assertEqual(self.client.sent[-1], ("paid", "u1", WALLET))

    def test_a_user_without_a_wallet_is_refused_cleanly(self):
        from sign402_gateway.venice_chat import ChatService, PrefundFailed

        class NoWallet:
            def wallet_status(self, user_id):
                return {"ok": False, "wallet": None}

        self.make_service()  # gives us a client to hand over
        service = ChatService(
            store=self.store,
            client=self.client,
            wallet_service=NoWallet(),
            daily_cap_atomic=5_000_000,
        )
        with self.assertRaises(PrefundFailed):
            service.send("u1", "hi")

    def test_end_clears_nothing_it_should_not(self):
        service = self.make_service()
        self.store.record_prefund("u1", 5_000_000)

        result = service.end("u1")

        self.assertTrue(result["ok"])
        # Ending a chat is a UI action, not a refund: credit is untouched.
        self.assertEqual(self.store.claimable_credit_atomic("u1"), 5_000_000)


class ChatServiceEnvBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def env(self, **overrides):
        base = {
            "SIGN402_AI_CHAT_ENABLED": "1",
            "SIGN402_AI_CHAT_MERCHANT_PAYTO": BOUND_PAY_TO,
            "SIGN402_CHAT_STORE_PATH": str(Path(self.tmp.name) / "chat.db"),
        }
        base.update(overrides)
        return base

    def test_it_returns_none_when_the_flag_is_off(self):
        from sign402_gateway.venice_chat import build_chat_service_from_env

        service = build_chat_service_from_env(
            wallet_service=object(),
            settle=lambda requirement: {},
            env=self.env(SIGN402_AI_CHAT_ENABLED=""),
        )
        self.assertIsNone(service)

    def test_it_refuses_to_build_without_a_bound_pay_to(self):
        from sign402_gateway.venice_chat import build_chat_service_from_env

        with self.assertRaises(ValueError):
            build_chat_service_from_env(
                wallet_service=object(),
                settle=lambda requirement: {},
                env=self.env(SIGN402_AI_CHAT_MERCHANT_PAYTO=""),
            )

    def test_it_uses_the_documented_defaults(self):
        from sign402_gateway.venice_chat import build_chat_service_from_env

        service = build_chat_service_from_env(
            wallet_service=object(),
            settle=lambda requirement: {},
            env=self.env(),
        )
        self.addCleanup(service.store.close)

        config = service.client.config
        self.assertEqual(config.bound_pay_to, BOUND_PAY_TO)
        self.assertEqual(config.chunk_atomic, 5_000_000)
        self.assertEqual(config.max_outstanding_atomic, 10_000_000)
        self.assertEqual(config.daily_cap_atomic, 5_000_000)

    def test_env_overrides_are_honoured(self):
        from sign402_gateway.venice_chat import build_chat_service_from_env

        service = build_chat_service_from_env(
            wallet_service=object(),
            settle=lambda requirement: {},
            env=self.env(
                SIGN402_AI_CHAT_DEFAULT_DAILY_CAP_ATOMIC="100000",
            ),
        )
        self.addCleanup(service.store.close)

        self.assertEqual(service.client.config.daily_cap_atomic, 100_000)


class WalletSignerTests(unittest.TestCase):
    def test_it_signs_with_the_key_of_the_address_owner(self):
        from sign402_gateway.venice_chat import _wallet_signer

        calls = []

        class Wallets:
            def decrypt_private_key_for_future_signing(self, user_id):
                calls.append(user_id)
                return "0x" + "22" * 32

        signer = _wallet_signer(Wallets(), {WALLET.lower(): "u1"})
        signature = signer(WALLET, "hello")

        self.assertEqual(calls, ["u1"])
        self.assertTrue(signature)

    def test_an_unknown_address_is_refused_without_decrypting(self):
        from sign402_gateway.venice_chat import PrefundFailed, _wallet_signer

        calls = []

        class Wallets:
            def decrypt_private_key_for_future_signing(self, user_id):
                calls.append(user_id)
                return "0x" + "22" * 32

        signer = _wallet_signer(Wallets(), {})
        with self.assertRaises(PrefundFailed):
            signer(WALLET, "hello")
        self.assertEqual(calls, [])

    def test_resolving_a_wallet_records_its_owner(self):
        from sign402_gateway.venice_chat import ChatService

        class Wallets:
            def wallet_status(self, user_id):
                return {"ok": True, "wallet": {"address": WALLET}}

        service = ChatService(
            store=None, client=None, wallet_service=Wallets(),
            daily_cap_atomic=5_000_000,
        )
        service._wallet_address("u1")

        self.assertEqual(service.wallet_owners[WALLET.lower()], "u1")


class WatcherRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ChatStore(
            Path(self.tmp.name) / "chat.db", now=lambda: DAY_ONE_NOON
        )
        self.addCleanup(self.store.close)

    def test_the_runner_polls_and_returns_a_live_watcher(self):
        from sign402_gateway.venice_chat import start_payto_watcher

        seen = []
        self.store.bind_policy("u1", policy_hash="a" * 64, pay_to=BOUND_PAY_TO)

        watcher = start_payto_watcher(
            store=self.store,
            bound_pay_to=BOUND_PAY_TO,
            interval_seconds=3600,
            notify=seen.append,
        )
        watcher.fetch_changes = lambda **kwargs: [payto_change()]
        watcher.poll()

        self.assertTrue(self.store.get_session("u1").paused)
        self.assertEqual(len(seen), 1)
        self.assertTrue(watcher.thread.daemon)

    def test_the_default_notifier_stores_the_notice(self):
        from sign402_gateway.venice_chat import _record_payto_notice

        _record_payto_notice(self.store)(
            {"observedAt": "2026-08-12T00:00:00Z", "users": ["u1"],
             "telegramText": "Venice AI changed its payout address."}
        )

        stored = json.loads(self.store.get_watcher_state("payto_last_notice"))
        self.assertEqual(stored["users"], ["u1"])
        self.assertIn("Venice", stored["telegramText"])


class SignInWithXWireFormatTests(VeniceChatTestCase):
    """Both of these were found by a live call, not by a stub.

    Venice rejects a bare signature outright, and rejects a repeated nonce with
    X402_SIGN_IN_NONCE_REUSED. A prefund makes three signed requests in a row,
    so a clock-derived nonce collides in production every single time.
    """

    def test_the_signature_is_0x_prefixed(self):
        from sign402_gateway.venice_chat import _wallet_signer

        class FakeWallets:
            def decrypt_private_key_for_future_signing(self, user_id):
                return "0x" + "22" * 32

        signer = _wallet_signer(FakeWallets(), {WALLET.lower(): "u1"})
        signature = signer(WALLET, "hello")

        self.assertTrue(signature.startswith("0x"), signature[:12])
        self.assertEqual(len(signature), 132)

    def test_an_unknown_wallet_is_never_signed_for(self):
        from sign402_gateway.venice_chat import _wallet_signer

        class ExplodingWallets:
            def decrypt_private_key_for_future_signing(self, user_id):
                raise AssertionError("must not decrypt for an unknown wallet")

        signer = _wallet_signer(ExplodingWallets(), {})
        with self.assertRaises(PrefundFailed):
            signer(WALLET, "hello")

    def test_every_request_carries_a_fresh_nonce(self):
        client = self.make_client()
        user = self.session_bound_to(BOUND_PAY_TO)

        client.send(user, "hi", wallet_address=WALLET)

        nonces = []
        for _method, _url, headers, _body in self.requests:
            header = headers.get("X-Sign-In-With-X")
            if header:
                message = json.loads(base64.b64decode(header))["message"]
                nonces.append(
                    [
                        line.split("Nonce:", 1)[1].strip()
                        for line in message.splitlines()
                        if line.startswith("Nonce:")
                    ][0]
                )

        self.assertGreaterEqual(len(nonces), 3)
        self.assertEqual(len(nonces), len(set(nonces)), nonces)

    def test_nonces_do_not_depend_on_the_clock(self):
        # Same frozen second, different nonces.
        client = self.make_client()
        client.now = lambda: DAY_ONE_NOON
        first = client._siwx_header(WALLET)
        second = client._siwx_header(WALLET)

        self.assertNotEqual(first, second)


class MinimumBalanceTests(VeniceChatTestCase):
    """Venice refuses a chat request below minimumBalanceUsd ($0.10 live).

    Credit under that floor is unusable, so it must trigger a top-up rather
    than a request that is certain to come back 402.
    """

    def test_credit_below_venices_floor_tops_up_instead_of_asking(self):
        from sign402_gateway.venice_chat import VENICE_MINIMUM_BALANCE_ATOMIC

        # Room for a whole chunk on top of the seeded credit, so the refusal
        # under test is the balance floor and not the daily window.
        client = self.make_client(
            daily_cap_atomic=10_000_000, max_outstanding_atomic=10_000_000
        )
        user = self.session_bound_to(BOUND_PAY_TO, credit=50_000)  # $0.05

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(len(self.buy_calls), 1)
        self.assertLess(50_000, VENICE_MINIMUM_BALANCE_ATOMIC)

    def test_credit_above_the_floor_is_spent_without_topping_up(self):
        client = self.make_client(balance_atomic=200_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=200_000)  # $0.20

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(self.buy_calls, [])


class StrandedCreditTests(VeniceChatTestCase):
    def test_leftover_dust_never_blocks_the_next_top_up(self):
        # One chunk of headroom exactly would make dust unrecoverable.
        client = self.make_client(
            daily_cap_atomic=10_000_000, max_outstanding_atomic=10_000_000
        )
        user = self.session_bound_to(BOUND_PAY_TO, credit=1)

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(len(self.buy_calls), 1)


class DocumentedBalanceStepTests(VeniceChatTestCase):
    """Venice publishes the authoritative answer; we must ask for it.

    The integration guide's step 2 is a balance check before use. Deciding from
    a local counter instead lets our view drift from the provider's, and a
    guessed threshold is a guess where an authoritative number exists.
    """

    def balance_calls(self):
        return [r for r in self.requests if "/x402/balance/" in r[1]]

    def test_the_balance_endpoint_is_consulted_before_sending(self):
        client = self.make_client(balance_atomic=500_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=500_000)

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(len(self.balance_calls()), 1)

    def test_can_consume_false_triggers_a_top_up(self):
        # Venice says no, even though our own ledger thinks there is credit.
        client = self.make_client(balance_atomic=0, daily_cap_atomic=20_000_000,
                                  max_outstanding_atomic=20_000_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=5_000_000)

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(len(self.buy_calls), 1)

    def test_can_consume_true_sends_without_paying(self):
        client = self.make_client(balance_atomic=200_000)
        user = self.session_bound_to(BOUND_PAY_TO)

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(self.buy_calls, [])

    def test_the_balance_check_precedes_the_top_up_challenge(self):
        client = self.make_client(balance_atomic=0)
        user = self.session_bound_to(BOUND_PAY_TO)

        client.send(user, "hi", wallet_address=WALLET)

        order = [r[1] for r in self.requests]
        self.assertLess(
            next(i for i, u in enumerate(order) if "/x402/balance/" in u),
            next(i for i, u in enumerate(order) if u.endswith("/x402/top-up")),
        )


class SettlementOutcomeTests(VeniceChatTestCase):
    """The buyer reports whether the paid POST landed. Never assume it did."""

    def test_an_unconfirmed_top_up_pauses_and_records_no_credit(self):
        def unconfirmed(payment_requirements, *, user_id):
            self.buy_calls.append(payment_requirements)
            return {"ok": False, "status": 502}

        client = self.make_client(settle=unconfirmed)
        user = self.session_bound_to(BOUND_PAY_TO)

        with self.assertRaises(ReconciliationRequired):
            client.send(user, "hi", wallet_address=WALLET)

        session = self.store.get_session(user)
        self.assertTrue(session.paused)
        self.assertEqual(session.outstanding_atomic, 0)
        self.assertEqual(session.spent_atomic_this_window, 0)

    def test_the_settlement_is_told_which_user_is_paying(self):
        seen = []

        def recording(payment_requirements, *, user_id):
            seen.append(user_id)
            return {"ok": True}

        client = self.make_client(settle=recording)
        user = self.session_bound_to(BOUND_PAY_TO)
        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(seen, [user])


class WalletOwnerCacheTests(unittest.TestCase):
    def test_the_owner_cache_is_bounded(self):
        from sign402_gateway.venice_chat import (
            ChatService,
            WALLET_OWNER_CACHE_MAX,
        )

        class Wallets:
            def wallet_status(self, user_id):
                return {
                    "ok": True,
                    "wallet": {"address": f"0x{int(user_id):040x}"},
                }

        service = ChatService(
            store=None, client=None, wallet_service=Wallets(),
            daily_cap_atomic=5_000_000,
        )
        for user in range(WALLET_OWNER_CACHE_MAX + 100):
            service._wallet_address(str(user + 1))

        self.assertLessEqual(len(service.wallet_owners), WALLET_OWNER_CACHE_MAX)

    def test_an_evicted_owner_is_refilled_on_the_next_message(self):
        from sign402_gateway.venice_chat import ChatService

        class Wallets:
            def wallet_status(self, user_id):
                return {"ok": True, "wallet": {"address": WALLET}}

        service = ChatService(
            store=None, client=None, wallet_service=Wallets(),
            daily_cap_atomic=5_000_000,
        )
        service._wallet_address("u1")
        service.wallet_owners.clear()

        service._wallet_address("u1")
        self.assertEqual(service.wallet_owners[WALLET.lower()], "u1")


class ApprovedPolicyIsRequiredTests(VeniceChatTestCase):
    """No approved policy, no spending. The binding is the user's, not the env's."""

    def test_a_user_who_approved_nothing_cannot_spend(self):
        from sign402_gateway.venice_chat import PolicyMissing

        client = self.make_client()
        with self.assertRaises(PolicyMissing):
            client.send("nobody", "hi", wallet_address=WALLET)

        self.assertEqual(self.buy_calls, [])
        self.assertEqual(self.requests, [])

    def test_the_binding_checked_is_the_one_the_user_approved(self):
        # The operator's env says one address; this user approved another.
        # The user's approval wins, and the mismatch pauses.
        from sign402_gateway.venice_chat import MerchantChanged

        client = self.make_client(challenge_pay_to=BOUND_PAY_TO)
        user = self.session_bound_to("0xaaaabbbbccccddddeeeeffff0000111122223333")

        with self.assertRaises(MerchantChanged):
            client.send(user, "hi", wallet_address=WALLET)
        self.assertEqual(self.buy_calls, [])

    def test_a_matching_approved_binding_pays(self):
        client = self.make_client()
        user = self.session_bound_to(BOUND_PAY_TO)

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(len(self.buy_calls), 1)


class RemainingBudgetTests(unittest.TestCase):
    """What the user sees when they ask how much is left."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ChatStore(
            Path(self.tmp.name) / "chat.db",
            now=lambda: DAY_ONE_NOON,
        )
        self.addCleanup(self.store.close)

    def service(self):
        from sign402_gateway.venice_chat import ChatService

        class Wallets:
            def wallet_status(self, user_id):
                return {"ok": True, "wallet": {"address": WALLET}}

        return ChatService(
            store=self.store, client=None, wallet_service=Wallets(),
            daily_cap_atomic=5_000_000,
        )

    def test_a_fresh_user_has_the_whole_cap_left(self):
        status = self.service().start("u1")

        self.assertEqual(status["remainingWindowAtomic"], 5_000_000)
        self.assertEqual(status["spentTodayAtomic"], 0)
        self.assertEqual(status["remainingWindowUsdc"], "5.00")

    def test_a_prefund_shows_up_as_spent_today(self):
        self.store.record_prefund("u1", 5_000_000)

        status = self.service().start("u1")

        self.assertEqual(status["spentTodayAtomic"], 5_000_000)
        self.assertEqual(status["remainingWindowAtomic"], 0)
        self.assertEqual(status["outstandingAtomic"], 5_000_000)

    def test_credit_left_is_reported_separately_from_the_daily_budget(self):
        # Two different numbers: what is still loaded at Venice, and how much
        # more may be paid today. Conflating them would mislead.
        self.store.record_prefund("u1", 5_000_000)
        self.store.debit("u1", 3_000)

        status = self.service().start("u1")

        self.assertEqual(status["outstandingAtomic"], 4_997_000)
        self.assertEqual(status["outstandingUsdc"], "4.99")
        self.assertEqual(status["remainingWindowAtomic"], 0)

    def test_the_window_resets_the_next_day(self):
        self.store.record_prefund("u1", 5_000_000)
        self.store.now = lambda: DAY_ONE_NOON + 86_400

        status = self.service().start("u1")

        self.assertEqual(status["remainingWindowAtomic"], 5_000_000)
        self.assertEqual(status["outstandingAtomic"], 5_000_000)

    def test_an_approved_cap_beats_the_default_in_the_report(self):
        from sign402_gateway.venice_chat import build_chat_policy

        self.store.approve_policy("u1", build_chat_policy(
            pay_to=BOUND_PAY_TO, network=NETWORK, asset=USDC,
            daily_cap_atomic=10_000_000, expires_at=DAY_ONE_NOON + 86_400))

        status = self.service().start("u1")

        self.assertEqual(status["dailyCapAtomic"], 10_000_000)
        self.assertEqual(status["remainingWindowAtomic"], 10_000_000)



class SettlementFailureIsLoggedTests(VeniceChatTestCase):
    def test_the_reason_reaches_the_log_but_not_the_user(self):
        import logging

        def failing(payment_requirements, *, user_id):
            raise ValueError("refusing to pay without approved terms: max-atomic")

        client = self.make_client(settle=failing)
        user = self.session_bound_to(BOUND_PAY_TO)

        with self.assertLogs("sign402_gateway.venice_chat", level="WARNING") as caught:
            with self.assertRaises(PrefundFailed) as raised:
                client.send(user, "hi", wallet_address=WALLET)

        logged = "\n".join(caught.output)
        self.assertIn("max-atomic", logged)
        self.assertNotIn("max-atomic", str(raised.exception))

    def test_the_log_never_carries_the_prompt(self):
        import logging

        def failing(payment_requirements, *, user_id):
            raise ValueError("boom")

        client = self.make_client(settle=failing)
        user = self.session_bound_to(BOUND_PAY_TO)

        with self.assertLogs("sign402_gateway.venice_chat", level="WARNING") as caught:
            with self.assertRaises(PrefundFailed):
                client.send(user, "my secret prompt", wallet_address=WALLET)

        self.assertNotIn("my secret prompt", "\n".join(caught.output))


class ActualCostFromTheProviderTests(VeniceChatTestCase):
    """Venice does not send X-Balance-Remaining. Ask it for the balance.

    Observed live: every message was billed the fallback estimate, $0.10,
    while the real cost is a fraction of a cent. Guessing 30x high empties the
    local ledger long before the credit is actually gone.
    """

    def make_metered(self, *, start_usd, after_usd, header=None):
        """A provider that meters, and reports the balance only when asked."""
        self.balance = {"usd": start_usd}
        self.after = after_usd

        def transport(method, url, *, headers=None, json_body=None):
            self.requests.append((method, url, headers or {}, json_body))
            if "/x402/balance/" in url:
                return FakeResponse(200, {
                    "success": True,
                    "data": {
                        "canConsume": self.balance["usd"] >= 0.10,
                        "balanceUsd": self.balance["usd"],
                    },
                })
            if url.endswith("/chat/completions"):
                self.balance["usd"] = self.after
                return FakeResponse(200, ANSWER, {} if header is None else header)
            raise AssertionError(url)

        client = self.make_client()
        client.transport = transport
        return client

    def test_the_real_cost_comes_from_the_providers_balance(self):
        client = self.make_metered(start_usd=5.0, after_usd=4.997)
        user = self.session_bound_to(BOUND_PAY_TO, credit=5_000_000)

        result = client.send(user, "hi", wallet_address=WALLET)

        # $5.000 -> $4.997 is three tenths of a cent, not the $0.10 estimate.
        self.assertEqual(result.cost_atomic, 3_000)
        self.assertEqual(
            self.store.get_session(user).outstanding_atomic, 4_997_000
        )

    def test_credit_tracks_the_provider_rather_than_our_arithmetic(self):
        client = self.make_metered(start_usd=5.0, after_usd=4.982)
        user = self.session_bound_to(BOUND_PAY_TO, credit=5_000_000)

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(
            self.store.get_session(user).outstanding_atomic, 4_982_000
        )

    def test_the_header_is_still_used_when_a_provider_sends_one(self):
        client = self.make_metered(
            start_usd=5.0, after_usd=4.0,  # balance endpoint would say $4.00
            header={"X-Balance-Remaining": "4.990"},
        )
        user = self.session_bound_to(BOUND_PAY_TO, credit=5_000_000)

        result = client.send(user, "hi", wallet_address=WALLET)

        # The header is cheaper than another round trip, so it wins.
        self.assertEqual(result.cost_atomic, 10_000)

    def test_a_long_answer_costs_more_than_a_short_one(self):
        short = self.make_metered(start_usd=5.0, after_usd=4.998)
        user = self.session_bound_to(BOUND_PAY_TO, credit=5_000_000)
        cheap = short.send(user, "hi", wallet_address=WALLET).cost_atomic

        self.setUp()
        long = self.make_metered(start_usd=5.0, after_usd=4.980)
        user = self.session_bound_to(BOUND_PAY_TO, credit=5_000_000)
        dear = long.send(user, "hi", wallet_address=WALLET).cost_atomic

        self.assertLess(cheap, dear)


class ModelChoiceTests(VeniceChatTestCase):
    def sent_model(self):
        chat = [r for r in self.requests if r[1].endswith("/chat/completions")]
        return chat[-1][3]["model"]

    def test_the_users_choice_is_what_gets_asked(self):
        client = self.make_client(balance_atomic=500_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=500_000)
        self.store.set_model(user, "grok-4-6")

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(self.sent_model(), "grok-4-6")

    def test_without_a_choice_the_configured_default_is_used(self):
        client = self.make_client(balance_atomic=500_000)
        user = self.session_bound_to(BOUND_PAY_TO, credit=500_000)

        client.send(user, "hi", wallet_address=WALLET)

        self.assertEqual(self.sent_model(), client.config.model)

    def test_an_unknown_model_is_refused_before_anything_is_sent(self):
        from sign402_gateway.venice_chat import UnknownModel, CHAT_MODELS

        self.assertNotIn("definitely-not-a-model", {m.model_id for m in CHAT_MODELS})
        store = self.make_store()
        with self.assertRaises(UnknownModel):
            store  # keep the store alive for cleanup
            from sign402_gateway.venice_chat import resolve_model
            resolve_model("definitely-not-a-model")

    def test_every_offered_model_has_a_label_and_a_price(self):
        from sign402_gateway.venice_chat import CHAT_MODELS

        self.assertGreaterEqual(len(CHAT_MODELS), 3)
        for model in CHAT_MODELS:
            self.assertTrue(model.model_id)
            self.assertTrue(model.label)
            self.assertGreater(model.output_usd_per_mtok, 0)

    def test_the_offered_models_are_ordered_cheapest_first(self):
        from sign402_gateway.venice_chat import CHAT_MODELS

        prices = [m.output_usd_per_mtok for m in CHAT_MODELS]
        self.assertEqual(prices, sorted(prices))


class ModelListShapeTests(unittest.TestCase):
    """The offered list is Venice's own designations, not our taste."""

    def models(self):
        from sign402_gateway.venice_chat import CHAT_MODELS

        return CHAT_MODELS

    def test_it_covers_the_jobs_venice_tags(self):
        blurbs = " ".join(m.blurb.lower() for m in self.models())
        for job in ("refusals", "images", "tool use", "code", "reasoning"):
            self.assertIn(job, blurbs)

    def test_the_dearest_is_flagged_as_expensive_in_its_own_blurb(self):
        dearest = max(self.models(), key=lambda m: m.output_usd_per_mtok)
        # A model that burns a daily budget 125x faster than the cheapest has
        # to say so where the user is choosing.
        self.assertIn("budget", dearest.blurb.lower())

    def test_still_cheapest_first(self):
        prices = [m.output_usd_per_mtok for m in self.models()]
        self.assertEqual(prices, sorted(prices))

    def test_the_ids_are_explicit_not_aliases(self):
        from sign402_gateway.venice_chat import DEFAULT_MODEL, resolve_model

        # An alias lets the provider move what we pay for without telling us.
        resolve_model(DEFAULT_MODEL)
        for model in self.models():
            self.assertNotEqual(model.model_id, "venice-uncensored")


class ModelCatalogueTests(unittest.TestCase):
    """Built from Venice's own model list, not from a list we maintain."""

    SAMPLE = {
        "data": [
            {
                "id": "cheap-1",
                "model_spec": {
                    "name": "Cheap One",
                    "description": "A small fast model. " * 20,
                    "availableContextTokens": 32000,
                    "pricing": {"input": {"usd": 0.05}, "output": {"usd": 0.10}},
                    "capabilities": {},
                    "traits": [],
                },
            },
            {
                "id": "sees-1",
                "model_spec": {
                    "name": "Sees One",
                    "description": "Reads pictures.",
                    "availableContextTokens": 128000,
                    "pricing": {"input": {"usd": 1.0}, "output": {"usd": 2.0}},
                    "capabilities": {"supportsVision": True, "optimizedForCode": True},
                    "traits": ["default_vision"],
                },
            },
            {
                "id": "dear-1",
                "model_spec": {
                    "name": "Dear One",
                    "description": "Thinks hard.",
                    "availableContextTokens": 1000000,
                    "pricing": {"input": {"usd": 4.0}, "output": {"usd": 20.0}},
                    "capabilities": {"supportsReasoning": True, "supportsE2EE": True},
                    "traits": ["most_intelligent"],
                },
            },
            {
                "id": "no-price",
                "model_spec": {"name": "Free?", "pricing": {}, "capabilities": {}},
            },
        ]
    }

    def catalogue(self, payload=None, fail=False):
        from sign402_gateway.venice_chat import VeniceModelCatalogue

        self.fetches = 0

        def fetch():
            self.fetches += 1
            if fail:
                raise RuntimeError("model list unavailable")
            return payload if payload is not None else self.SAMPLE

        return VeniceModelCatalogue(fetch=fetch, now=lambda: 1_000)

    def test_it_lists_every_priced_model(self):
        models = self.catalogue().models()
        self.assertEqual({m.model_id for m in models}, {"cheap-1", "sees-1", "dear-1"})

    def test_a_model_without_a_price_is_dropped(self):
        # We cannot show a cost we do not know, and the daily cap depends on it.
        self.assertNotIn("no-price", {m.model_id for m in self.catalogue().models()})

    def test_models_are_ordered_cheapest_first(self):
        prices = [m.output_usd_per_mtok for m in self.catalogue().models()]
        self.assertEqual(prices, sorted(prices))

    def test_the_blurb_comes_from_venice_not_from_us(self):
        model = next(m for m in self.catalogue().models() if m.model_id == "sees-1")
        self.assertIn("Reads pictures", model.blurb)

    def test_a_long_description_is_trimmed_for_a_phone(self):
        model = next(m for m in self.catalogue().models() if m.model_id == "cheap-1")
        self.assertLessEqual(len(model.blurb), 160)

    def test_categories_are_derived_from_capabilities(self):
        catalogue = self.catalogue()
        names = {c.key for c in catalogue.categories()}
        self.assertIn("all", names)
        self.assertIn("vision", names)
        self.assertIn("code", names)

    def test_a_category_filters(self):
        catalogue = self.catalogue()
        ids = {m.model_id for m in catalogue.models(category="vision")}
        self.assertEqual(ids, {"sees-1"})

    def test_an_empty_category_is_not_offered(self):
        catalogue = self.catalogue()
        keys = {c.key for c in catalogue.categories()}
        # Nothing in the sample supports video.
        self.assertNotIn("video", keys)

    def test_the_list_is_cached_between_calls(self):
        catalogue = self.catalogue()
        catalogue.models()
        catalogue.models()
        self.assertEqual(self.fetches, 1)

    def test_the_cache_expires(self):
        from sign402_gateway.venice_chat import MODEL_CACHE_TTL_SECONDS

        catalogue = self.catalogue()
        catalogue.models()
        catalogue.now = lambda: 1_000 + MODEL_CACHE_TTL_SECONDS + 1
        catalogue.models()
        self.assertEqual(self.fetches, 2)

    def test_a_failed_fetch_falls_back_to_the_built_in_list(self):
        from sign402_gateway.venice_chat import CHAT_MODELS

        models = self.catalogue(fail=True).models()
        self.assertEqual(
            {m.model_id for m in models}, {m.model_id for m in CHAT_MODELS}
        )

    def test_a_stale_list_is_preferred_to_no_list(self):
        catalogue = self.catalogue()
        catalogue.models()
        catalogue.fetch = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        catalogue.now = lambda: 99_999_999

        ids = {m.model_id for m in catalogue.models()}

        self.assertIn("cheap-1", ids)

    def test_resolving_accepts_anything_in_the_live_list(self):
        catalogue = self.catalogue()
        self.assertEqual(catalogue.resolve("dear-1").label, "Dear One")

    def test_resolving_still_refuses_an_unknown_id(self):
        from sign402_gateway.venice_chat import UnknownModel

        with self.assertRaises(UnknownModel):
            self.catalogue().resolve("not-a-model")


class NoHiddenNetworkTests(unittest.TestCase):
    def test_a_service_without_a_catalogue_never_opens_a_socket(self):
        import socket
        from sign402_gateway.venice_chat import ChatService

        class Wallets:
            def wallet_status(self, user_id):
                return {"ok": True, "wallet": {"address": WALLET}}

        with tempfile.TemporaryDirectory() as tmp:
            store = ChatStore(Path(tmp) / "chat.db")
            self.addCleanup(store.close)
            service = ChatService(
                store=store, client=None, wallet_service=Wallets(),
                daily_cap_atomic=5_000_000,
            )

            real = socket.socket

            def forbidden(*args, **kwargs):
                raise AssertionError("a test opened a network connection")

            socket.socket = forbidden
            try:
                result = service.models("u1")
                service.models("u1", category="all")
            finally:
                socket.socket = real

        self.assertTrue(result["ok"])


class ModelSearchTests(unittest.TestCase):
    def catalogue(self):
        from sign402_gateway.venice_chat import VeniceModelCatalogue

        payload = {"data": [
            {"id": "grok-4-6", "model_spec": {
                "name": "Grok 4.6", "description": "Reasoning.",
                "pricing": {"input": {"usd": 2.27}, "output": {"usd": 6.8}},
                "capabilities": {"supportsReasoning": True}}},
            {"id": "kimi-k3", "model_spec": {
                "name": "Kimi K3", "description": "Deep thinking.",
                "pricing": {"input": {"usd": 3.75}, "output": {"usd": 18.75}},
                "capabilities": {}}},
            {"id": "qwen3-5-9b", "model_spec": {
                "name": "Qwen 3.5 9B", "description": "Small.",
                "pricing": {"input": {"usd": 0.1}, "output": {"usd": 0.15}},
                "capabilities": {"supportsVision": True}}},
            {"id": "qwen-3-8-27b", "model_spec": {
                "name": "Qwen 3.8 27B", "description": "Bigger.",
                "pricing": {"input": {"usd": 0.45}, "output": {"usd": 3.2}},
                "capabilities": {"supportsVision": True}}},
        ]}
        return VeniceModelCatalogue(fetch=lambda: payload, now=lambda: 1000)

    def ids(self, **kwargs):
        return [m.model_id for m in self.catalogue().models(**kwargs)]

    def test_a_name_finds_its_model(self):
        self.assertEqual(self.ids(query="grok"), ["grok-4-6"])

    def test_search_ignores_case_and_spaces(self):
        self.assertEqual(self.ids(query="  KIMI  "), ["kimi-k3"])

    def test_a_partial_name_can_match_several(self):
        self.assertEqual(
            self.ids(query="qwen"), ["qwen3-5-9b", "qwen-3-8-27b"]
        )

    def test_matches_stay_cheapest_first(self):
        prices = [
            m.output_usd_per_mtok
            for m in self.catalogue().models(query="qwen")
        ]
        self.assertEqual(prices, sorted(prices))

    def test_the_id_is_searchable_too(self):
        self.assertEqual(self.ids(query="k3"), ["kimi-k3"])

    def test_a_hyphen_or_space_does_not_matter(self):
        # "grok 4.6" and "grok-4-6" are the same request from a human.
        self.assertEqual(self.ids(query="grok 4 6"), ["grok-4-6"])

    def test_nothing_matches_returns_nothing(self):
        self.assertEqual(self.ids(query="llama"), [])

    def test_search_can_be_narrowed_by_category(self):
        self.assertEqual(
            self.ids(query="qwen", category="vision"),
            ["qwen3-5-9b", "qwen-3-8-27b"],
        )
        self.assertEqual(self.ids(query="grok", category="vision"), [])
