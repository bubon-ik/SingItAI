import os
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sign402_gateway.chat_store import (
    PREFUND_CLAIM_TTL_SECONDS,
    ChatStore,
    PrefundClaimUnavailable,
)


DAY_ONE_NOON = 1786_400_000 - (1786_400_000 % 86_400) + 43_200


class ChatStoreTestCase(unittest.TestCase):
    def make_store(self, path=":memory:", **kwargs) -> ChatStore:
        store = ChatStore(path, **kwargs)
        self.addCleanup(store.close)
        return store


class ChatStoreWindowTests(ChatStoreTestCase):
    def test_window_rolls_over_and_zeroes_spend_but_keeps_credit(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 500_000)
        store.debit("u1", 3_000)
        store.now = lambda: DAY_ONE_NOON + 86_400
        session = store.get_session("u1")
        self.assertEqual(session.spent_atomic_this_window, 0)
        self.assertEqual(session.outstanding_atomic, 497_000)

    def test_prefund_counts_against_the_window_when_paid(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 500_000)
        self.assertEqual(store.get_session("u1").spent_atomic_this_window, 500_000)

    def test_debit_does_not_add_to_the_window(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 500_000)
        store.debit("u1", 3_000)
        session = store.get_session("u1")
        self.assertEqual(session.spent_atomic_this_window, 500_000)
        self.assertEqual(session.outstanding_atomic, 497_000)

    def test_window_start_is_the_utc_day_boundary(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        session = store.get_session("u1")
        self.assertEqual(session.window_start, DAY_ONE_NOON - 43_200)
        self.assertEqual(session.window_start % 86_400, 0)

    def test_rollover_is_computed_on_read_without_a_timer(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 500_000)
        store.now = lambda: DAY_ONE_NOON + (86_400 * 9)
        session = store.get_session("u1")
        self.assertEqual(session.spent_atomic_this_window, 0)
        self.assertEqual(
            session.window_start, DAY_ONE_NOON - 43_200 + (86_400 * 9)
        )

    def test_spend_inside_the_same_window_accumulates(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 500_000)
        store.now = lambda: DAY_ONE_NOON + 3_600
        store.record_prefund("u1", 500_000)
        self.assertEqual(store.get_session("u1").spent_atomic_this_window, 1_000_000)

    def test_debit_never_drives_outstanding_credit_negative(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 1_000)
        with self.assertRaises(ValueError):
            store.debit("u1", 5_000)
        self.assertEqual(store.get_session("u1").outstanding_atomic, 1_000)

    def test_users_are_isolated_from_each_other(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 500_000)
        session = store.get_session("u2")
        self.assertEqual(session.spent_atomic_this_window, 0)
        self.assertEqual(session.outstanding_atomic, 0)


class ChatStorePauseTests(ChatStoreTestCase):
    def test_pause_records_a_reason_and_resume_clears_it(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.pause("u1", "MERCHANT_CHANGED")
        session = store.get_session("u1")
        self.assertTrue(session.paused)
        self.assertEqual(session.pause_reason, "MERCHANT_CHANGED")
        store.resume("u1")
        session = store.get_session("u1")
        self.assertFalse(session.paused)
        self.assertEqual(session.pause_reason, "")

    def test_pause_preserves_outstanding_credit(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 500_000)
        store.pause("u1", "RECONCILIATION_REQUIRED")
        self.assertEqual(store.get_session("u1").outstanding_atomic, 500_000)


class ChatStoreBindingTests(ChatStoreTestCase):
    def test_binding_pay_to_is_stored_lowercased_with_the_policy_hash(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        store.bind_policy("u1", policy_hash="a" * 64, pay_to="0xBEEF")
        session = store.get_session("u1")
        self.assertEqual(session.bound_pay_to, "0xbeef")
        self.assertEqual(session.policy_hash, "a" * 64)


class ChatStorePrefundClaimTests(ChatStoreTestCase):
    def test_a_second_claim_on_a_held_row_fails_rather_than_waits(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        with store.claim_prefund("u1"):
            with self.assertRaises(PrefundClaimUnavailable):
                with store.claim_prefund("u1"):
                    pass

    def test_a_released_claim_can_be_retaken(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        with store.claim_prefund("u1"):
            pass
        with store.claim_prefund("u1"):
            pass

    def test_a_failed_claim_body_releases_the_claim(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        with self.assertRaises(RuntimeError):
            with store.claim_prefund("u1"):
                raise RuntimeError("settlement blew up")
        with store.claim_prefund("u1"):
            pass

    def test_two_concurrent_claims_yield_exactly_one_prefund(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(
                Path(tmp) / "chat.db", now=lambda: DAY_ONE_NOON
            )
            start = threading.Barrier(2)
            granted: list[str] = []

            def attempt() -> None:
                start.wait()
                try:
                    with store.claim_prefund("u1"):
                        granted.append("claimed")
                        store.record_prefund("u1", 500_000)
                        time.sleep(0.05)
                except PrefundClaimUnavailable:
                    granted.append("refused")

            with ThreadPoolExecutor(max_workers=2) as pool:
                for future in [pool.submit(attempt), pool.submit(attempt)]:
                    future.result()

            self.assertEqual(sorted(granted), ["claimed", "refused"])
            self.assertEqual(
                store.get_session("u1").spent_atomic_this_window, 500_000
            )

    def test_a_claim_abandoned_by_a_dead_process_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(
                Path(tmp) / "chat.db", now=lambda: DAY_ONE_NOON
            )
            claim = store.claim_prefund("u1")
            claim.__enter__()  # never released, as if the process died here

            stale = ChatStore(
                Path(tmp) / "chat.db",
                now=lambda: DAY_ONE_NOON + PREFUND_CLAIM_TTL_SECONDS + 1,
            )
            self.addCleanup(stale.close)
            with stale.claim_prefund("u1"):
                pass

            # Tidy up while the temp database still exists.
            claim.__exit__(None, None, None)

    def test_claims_on_different_users_do_not_block_each_other(self):
        store = self.make_store(":memory:", now=lambda: DAY_ONE_NOON)
        with store.claim_prefund("u1"):
            with store.claim_prefund("u2"):
                pass


class ChatStorePersistenceTests(ChatStoreTestCase):
    def test_outstanding_credit_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "chat.db"
            store = self.make_store(path, now=lambda: DAY_ONE_NOON)
            store.record_prefund("u1", 500_000)
            store.debit("u1", 3_000)

            reopened = self.make_store(path, now=lambda: DAY_ONE_NOON)
            self.assertEqual(reopened.get_session("u1").outstanding_atomic, 497_000)

    def test_directory_is_0700_and_database_is_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "chat.db"
            self.make_store(path, now=lambda: DAY_ONE_NOON)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()


class ChatPolicyTests(unittest.TestCase):
    """Task 6: the standing authorization the user approves once."""

    def build(self, **kwargs):
        from sign402_gateway.venice_chat import build_chat_policy

        defaults = dict(
            pay_to="0x2670b922Ef37C7Df47158725C0CC407b5382293F",
            network="eip155:8453",
            asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            daily_cap_atomic=5_000_000,
            expires_at=DAY_ONE_NOON + (30 * 86_400),
        )
        defaults.update(kwargs)
        return build_chat_policy(**defaults)

    def test_a_policy_without_an_expiry_is_rejected(self):
        from sign402_gateway.venice_chat import PolicyRejected

        for missing in (None, 0, ""):
            with self.subTest(expires_at=missing):
                with self.assertRaises(PolicyRejected):
                    self.build(expires_at=missing)

    def test_an_expiry_in_the_past_is_rejected(self):
        from sign402_gateway.venice_chat import PolicyRejected

        with self.assertRaises(PolicyRejected):
            self.build(expires_at=DAY_ONE_NOON - 1, now=DAY_ONE_NOON)

    def test_a_policy_without_a_pay_to_is_rejected(self):
        from sign402_gateway.venice_chat import PolicyRejected

        with self.assertRaises(PolicyRejected):
            self.build(pay_to="")

    def test_a_zero_or_negative_cap_is_rejected(self):
        from sign402_gateway.venice_chat import PolicyRejected

        for cap in (0, -1):
            with self.subTest(cap=cap):
                with self.assertRaises(PolicyRejected):
                    self.build(daily_cap_atomic=cap)

    def test_the_policy_binds_pay_to_lowercased(self):
        policy = self.build()
        self.assertEqual(
            policy.pay_to, "0x2670b922ef37c7df47158725c0cc407b5382293f"
        )

    def test_the_policy_hash_changes_with_the_cap(self):
        first = self.build(daily_cap_atomic=5_000_000)
        second = self.build(daily_cap_atomic=10_000_000)
        self.assertNotEqual(first.policy_hash, second.policy_hash)

    def test_the_policy_hash_changes_with_the_merchant(self):
        first = self.build()
        second = self.build(pay_to="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        self.assertNotEqual(first.policy_hash, second.policy_hash)


class ChatPolicyApprovalContextTests(unittest.TestCase):
    def policy(self):
        from sign402_gateway.venice_chat import build_chat_policy

        return build_chat_policy(
            pay_to="0x2670b922ef37c7df47158725c0cc407b5382293f",
            network="eip155:8453",
            asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            daily_cap_atomic=5_000_000,
            expires_at=1789_000_000,
        )

    def test_the_context_names_merchant_cap_and_expiry(self):
        from sign402_gateway.venice_chat import policy_approval_context

        lines = policy_approval_context(self.policy())
        joined = "\n".join(lines)

        self.assertIn("Venice AI", joined)
        self.assertIn("$5.00", joined)
        self.assertIn("per day", joined.lower())
        self.assertIn("2026", joined)

    def test_the_context_says_this_is_a_standing_approval(self):
        from sign402_gateway.venice_chat import policy_approval_context

        joined = "\n".join(policy_approval_context(self.policy())).lower()
        self.assertIn("standing", joined)
        self.assertIn("not a one-off", joined)

    def test_the_context_shows_the_bound_address(self):
        from sign402_gateway.venice_chat import policy_approval_context

        joined = "\n".join(policy_approval_context(self.policy()))
        self.assertIn("0x2670", joined)
        self.assertIn("293f", joined)


class ChatPolicyStoreTests(ChatStoreTestCase):
    def policy(self, *, cap=5_000_000, expires_at=DAY_ONE_NOON + 86_400 * 30):
        from sign402_gateway.venice_chat import build_chat_policy

        return build_chat_policy(
            pay_to="0x2670b922ef37c7df47158725c0cc407b5382293f",
            network="eip155:8453",
            asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            daily_cap_atomic=cap,
            expires_at=expires_at,
        )

    def test_approving_a_policy_stores_the_cap_and_the_expiry(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        policy = self.policy()

        store.approve_policy("u1", policy)

        session = store.get_session("u1")
        self.assertEqual(session.daily_cap_atomic, 5_000_000)
        self.assertEqual(session.policy_expires_at, policy.expires_at)
        self.assertEqual(session.bound_pay_to, policy.pay_to)
        self.assertEqual(session.policy_hash, policy.policy_hash)

    def test_raising_the_cap_requires_a_new_approval(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.approve_policy("u1", self.policy(cap=5_000_000))

        # Nothing may raise the cap without going through approve_policy again.
        self.assertFalse(hasattr(store, "set_daily_cap"))

        store.approve_policy("u1", self.policy(cap=10_000_000))
        self.assertEqual(store.get_session("u1").daily_cap_atomic, 10_000_000)

    def test_raising_the_cap_does_not_forgive_what_was_already_spent(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.approve_policy("u1", self.policy(cap=5_000_000))
        store.record_prefund("u1", 5_000_000)

        store.approve_policy("u1", self.policy(cap=10_000_000))

        self.assertEqual(
            store.get_session("u1").spent_atomic_this_window, 5_000_000
        )

    def test_a_new_approval_resumes_a_paused_session(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.approve_policy("u1", self.policy())
        store.pause("u1", "MERCHANT_CHANGED")

        store.approve_policy("u1", self.policy())

        self.assertFalse(store.get_session("u1").paused)

    def test_policy_expiry_is_reported(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.approve_policy("u1", self.policy(expires_at=DAY_ONE_NOON + 10))

        self.assertFalse(store.get_session("u1").policy_expired)
        store.now = lambda: DAY_ONE_NOON + 11
        self.assertTrue(store.get_session("u1").policy_expired)


class ChatRefundTests(ChatStoreTestCase):
    def test_credit_stays_claimable_after_a_pause(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 5_000_000)
        store.debit("u1", 3_000)
        store.pause("u1", "MERCHANT_CHANGED")

        self.assertEqual(store.claimable_credit_atomic("u1"), 4_997_000)

    def test_credit_stays_claimable_after_expiry(self):
        from sign402_gateway.venice_chat import build_chat_policy

        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.approve_policy(
            "u1",
            build_chat_policy(
                pay_to="0x2670b922ef37c7df47158725c0cc407b5382293f",
                network="eip155:8453",
                asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                daily_cap_atomic=5_000_000,
                expires_at=DAY_ONE_NOON + 10,
            ),
        )
        store.record_prefund("u1", 5_000_000)
        store.now = lambda: DAY_ONE_NOON + 11

        self.assertTrue(store.get_session("u1").policy_expired)
        self.assertEqual(store.claimable_credit_atomic("u1"), 5_000_000)

    def test_revoking_keeps_the_credit_and_stops_the_chat(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 5_000_000)

        store.revoke_policy("u1")

        session = store.get_session("u1")
        self.assertTrue(session.paused)
        self.assertEqual(session.bound_pay_to, "")
        self.assertEqual(store.claimable_credit_atomic("u1"), 5_000_000)

    def test_claimable_credit_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            store = self.make_store(path, now=lambda: DAY_ONE_NOON)
            store.record_prefund("u1", 5_000_000)
            store.debit("u1", 3_000)
            store.revoke_policy("u1")

            reopened = self.make_store(path, now=lambda: DAY_ONE_NOON)
            self.assertEqual(reopened.claimable_credit_atomic("u1"), 4_997_000)
            self.assertTrue(reopened.get_session("u1").paused)


class ReconcileOutstandingTests(ChatStoreTestCase):
    """The provider knows the balance. We only track the daily window."""

    def test_it_sets_credit_to_what_the_provider_reports(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 5_000_000)

        store.reconcile_outstanding("u1", 4_982_000)

        self.assertEqual(store.get_session("u1").outstanding_atomic, 4_982_000)

    def test_it_never_touches_the_daily_window(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 5_000_000)

        store.reconcile_outstanding("u1", 1_000)

        self.assertEqual(
            store.get_session("u1").spent_atomic_this_window, 5_000_000
        )

    def test_a_provider_balance_above_ours_is_accepted(self):
        # Someone topped the wallet up elsewhere; the provider is the truth.
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 1_000)

        store.reconcile_outstanding("u1", 9_000_000)

        self.assertEqual(store.get_session("u1").outstanding_atomic, 9_000_000)

    def test_a_negative_report_is_floored_at_zero(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 1_000)

        store.reconcile_outstanding("u1", -5)

        self.assertEqual(store.get_session("u1").outstanding_atomic, 0)


class ChatModelChoiceTests(ChatStoreTestCase):
    def test_a_user_without_a_choice_reports_none(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        self.assertEqual(store.get_session("u1").model, "")

    def test_a_chosen_model_is_remembered(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.set_model("u1", "venice-uncensored-1-2")
        self.assertEqual(store.get_session("u1").model, "venice-uncensored-1-2")

    def test_the_choice_is_per_user(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.set_model("u1", "grok-4-6")
        self.assertEqual(store.get_session("u2").model, "")

    def test_it_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            self.make_store(path, now=lambda: DAY_ONE_NOON).set_model("u1", "qwen3-5-9b")
            reopened = self.make_store(path, now=lambda: DAY_ONE_NOON)
            self.assertEqual(reopened.get_session("u1").model, "qwen3-5-9b")

    def test_choosing_a_model_touches_no_money(self):
        store = self.make_store(now=lambda: DAY_ONE_NOON)
        store.record_prefund("u1", 5_000_000)
        store.set_model("u1", "grok-4-6")
        session = store.get_session("u1")
        self.assertEqual(session.outstanding_atomic, 5_000_000)
        self.assertEqual(session.spent_atomic_this_window, 5_000_000)
