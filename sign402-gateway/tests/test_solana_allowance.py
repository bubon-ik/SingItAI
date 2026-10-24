import tempfile
import os
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from pathlib import Path

from cryptography.fernet import Fernet

from sign402_gateway import analytics
from sign402_gateway.agent_allowance import AllowanceError, AllowanceUnavailable
from sign402_gateway.solana_allowance import SolanaAllowanceService, SolanaAllowanceStore
from sign402_gateway.solana_keys import generate_keypair
from sign402_gateway.secure_state import SensitiveStateError

OWNER = generate_keypair()[0]
ACCOUNT = f"solana:{OWNER}"
NOW = 1_800_000_000


class FakeBridge:
    """The Node allowance operations, as the chain would answer them."""

    def __init__(self):
        self.calls, self.owner_usdc, self.delegated, self.agent_usdc, self.submit_state = [], 20_000_000, 0, 0, "confirmed"
        self.owner_sol = 7_000_000  # the owner's own SOL pays their approve's fee

    def charge(self, amount):
        """A purchase paid from the owner's account by the agent as delegate."""
        self.owner_usdc -= amount
        self.delegated -= amount

    def run(self, user_id, payer, key, operation, fee_payer_key=None, **payload):
        self.calls.append((operation, payer, bool(key), fee_payer_key, payload))
        if operation == "allowance-state":
            return {"owner": {"amount": str(self.owner_usdc), "delegatedToAgent": str(self.delegated),
                              "solLamports": str(self.owner_sol)},
                    "agent": {"usdcAtomic": str(self.agent_usdc)}}
        if operation == "allowance-prepare":
            return {"transaction": "BASE64TX", "messageHash": "hash-" + payload["kind"] + payload["amount"]}
        if operation == "allowance-inspect":
            return {"transaction": "SoLTx1", "blockhash": "blockhash"}
        if operation == "allowance-reconcile":
            return {"transaction": payload["transaction"], "state": self.submit_state}
        if operation == "allowance-submit":
            if self.submit_state == "confirmed":
                self.delegated = int(self.calls[-2][4]["amount"]) if self.calls[-2][0] == "allowance-prepare" else self.delegated
            return {"state": self.submit_state, "transaction": "SoLTx1"}
        if operation == "allowance-pull":
            amount = int(payload["amount"])
            self.delegated -= amount
            self.owner_usdc -= amount
            self.agent_usdc += amount
            return {"state": "confirmed", "transaction": "PullTx"}
        raise AssertionError(operation)


class SolanaStorePermissionsTests(unittest.TestCase):
    def test_new_and_existing_databases_are_private_and_keep_their_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "solana.db"
            previous = os.umask(0o022)
            try:
                store = SolanaAllowanceStore(path)
                store.add_agent("test", "address", "encrypted-fixture", NOW)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                path.chmod(0o644)
                path.parent.chmod(0o755)
                reopened = SolanaAllowanceStore(path)
                self.assertEqual(reopened.agent("test")["encrypted_key"], "encrypted-fixture")
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with reopened._db() as db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("INSERT INTO agents VALUES ('wal','wal-address','encrypted',1)")
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(path) + suffix)
                        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
                        sidecar.chmod(0o644)
                    reopened.agent("test")
                    for suffix in ("-wal", "-shm"):
                        self.assertEqual(Path(str(path) + suffix).stat().st_mode & 0o777, 0o600)
            finally:
                os.umask(previous)

    def test_symlink_database_and_directory_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_bytes(b"untouched")
            (root / "alias.db").symlink_to(target)
            with self.assertRaises(SensitiveStateError):
                SolanaAllowanceStore(root / "alias.db")
            real = root / "real"
            real.mkdir()
            (root / "alias-dir").symlink_to(real)
            with self.assertRaises(SensitiveStateError):
                SolanaAllowanceStore(root / "alias-dir" / "wallets.db")
            self.assertEqual(target.read_bytes(), b"untouched")


class SolanaAllowanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = [NOW]
        self.bridge = FakeBridge()
        self.fernet = Fernet(Fernet.generate_key())
        self.service = SolanaAllowanceService(
            store=SolanaAllowanceStore(Path(self.tmp.name) / "sol.db"), bridge=self.bridge, fernet=self.fernet,
            fee_payer_key=lambda: "FEE-PAYER-KEY", max_daily=100_000_000, max_per_purchase=25_000_000,
            max_days=90, max_grant=300_000_000, now=lambda: self.clock[0])

    def grant(self, amount="20"):
        self.service.setup(ACCOUNT, "20", "5", "30")
        prepared = self.service.prepare_wallet(ACCOUNT, "GRANT", amount=amount)
        return self.service.submit_wallet(ACCOUNT, prepared["operation"], "SIGNED")

    def test_limits_then_one_approval_from_the_wallet(self):
        self.assertEqual(self.service.status(ACCOUNT)["configured"], False)
        setup = self.service.setup(ACCOUNT, "20", "5", "30")
        status = self.service.status(ACCOUNT)
        self.assertEqual((status["state"], status["limiter"]), ("waiting_for_grant", setup["limiter"]))
        prepared = self.service.prepare_wallet(ACCOUNT, "GRANT", amount="20")
        self.assertEqual((prepared["chain"], prepared["transaction"]), ("solana", "BASE64TX"))
        self.assertIn("spend up to 20 USDC", prepared["walletShows"])
        prepare_call = self.bridge.calls[-1]
        self.assertEqual(prepare_call[4], {"owner": OWNER, "kind": "approve", "amount": "20000000", "ownerPaysFee": True})
        self.assertIsNone(prepare_call[3])  # the owner pays this fee from their SOL; ours is not used
        done = self.service.submit_wallet(ACCOUNT, prepared["operation"], "SIGNED")
        self.assertEqual((done["state"], done["detail"]), ("DONE", "Allowance now 20 USDC."))
        self.assertEqual(self.bridge.calls[-1][4]["messageHash"], "hash-approve20000000")  # checked against the prepared one
        status = self.service.status(ACCOUNT)
        self.assertEqual((status["state"], status["allowanceAtomic"], status["remainingTodayAtomic"]), ("granted", 20_000_000, 20_000_000))

    def test_what_the_chain_said_is_reused_for_a_moment_but_a_payment_reads_it_fresh(self):
        self.grant()
        reads = lambda: sum(1 for c in self.bridge.calls if c[0] == "allowance-state")
        before = reads()
        self.service.status(ACCOUNT)
        self.service.status(ACCOUNT)
        self.assertEqual(reads(), before + 1)  # one chat message asks several times; the public RPC refuses bursts
        self.service.spend(ACCOUNT, 1_000_000, "Weather", self.paid(1_000_000))
        self.assertEqual(reads(), before + 2)  # paying always looks again
        self.assertEqual(self.service.status(ACCOUNT)["allowanceAtomic"], 19_000_000)  # and forgets the old answer
        self.clock[0] += 11
        self.service.status(ACCOUNT)
        self.assertEqual(reads(), before + 4)

    def paid(self, amount, state="accepted"):
        def pay():
            self.bridge.charge(amount)
            return {"state": state, "transaction": "Tx"}
        return pay

    def test_a_purchase_is_paid_from_the_owners_account_and_counted(self):
        self.grant()
        result = self.service.spend(ACCOUNT, 5_000_000, "Venice credit", self.paid(5_000_000))
        self.assertEqual(result["state"], "accepted")
        status = self.service.status(ACCOUNT)
        self.assertEqual((status["remainingTodayAtomic"], status["allowanceAtomic"]), (15_000_000, 15_000_000))
        self.assertNotIn("allowance-pull", [c[0] for c in self.bridge.calls])  # no transfer to the agent, no fee for us

    def test_what_is_refused_before_anything_is_paid(self):
        pay = self.paid(1)
        with self.assertRaises(AllowanceUnavailable):
            self.service.spend(ACCOUNT, 1, "x", pay)
        self.grant("6")
        with self.assertRaisesRegex(AllowanceError, "per-purchase"):
            self.service.spend(ACCOUNT, 6_000_000, "x", pay)
        self.service.spend(ACCOUNT, 5_000_000, "ok", self.paid(5_000_000))
        with self.assertRaisesRegex(AllowanceError, "approval is used up"):
            self.service.spend(ACCOUNT, 5_000_000, "x", pay)
        self.bridge.delegated, self.bridge.owner_usdc = 5_000_000, 1_000_000
        with self.assertRaisesRegex(AllowanceError, "holds 1 USDC"):
            self.service.spend(ACCOUNT, 5_000_000, "x", pay)
        self.clock[0] += 31 * 86400
        with self.assertRaisesRegex(AllowanceError, "expired"):
            self.service.spend(ACCOUNT, 1, "x", pay)

    def test_the_daily_limit_counts_every_purchase_of_the_day_but_not_a_refused_one(self):
        self.grant("100")
        self.service.setup(ACCOUNT, "8", "5", "30")
        self.service.spend(ACCOUNT, 5_000_000, "a", self.paid(5_000_000))
        self.service.spend(ACCOUNT, 1_000_000, "refused", self.paid(0, state="failed"))
        self.assertEqual(self.service.status(ACCOUNT)["remainingTodayAtomic"], 3_000_000)
        with self.assertRaisesRegex(AllowanceError, "today's 8 USDC limit"):
            self.service.spend(ACCOUNT, 4_000_000, "b", self.paid(4_000_000))
        self.clock[0] += 86400
        self.service.spend(ACCOUNT, 4_000_000, "b", self.paid(4_000_000))

    def refused(self, code, *, charge=0):
        def pay():
            self.bridge.charge(charge)
            refusal = AllowanceError("The payment answer was lost.")
            refusal.code = code
            raise refusal
        return pay

    def test_a_payment_whose_answer_was_lost_still_counts_against_the_day(self):
        """The money left, the answer did not come back: the room it took must not be offered again."""
        for code in ("PAYMENT_UNCERTAIN", "BRIDGE_UNAVAILABLE", None):
            with self.subTest(code):
                self.setUp()
                self.grant("100")
                self.service.setup(ACCOUNT, "0.03", "0.03", "30")
                with self.assertRaises(AllowanceError):
                    self.service.spend(ACCOUNT, 20_000, "lost", self.refused(code, charge=20_000))
                with self.assertRaisesRegex(AllowanceError, "today's 0.03 USDC limit"):
                    self.service.spend(ACCOUNT, 30_000, "second", self.paid(30_000))
                self.assertEqual(self.bridge.owner_usdc, 20_000_000 - 20_000)

    def test_an_uncertain_answer_counts_against_the_day(self):
        self.grant("100")
        self.service.setup(ACCOUNT, "8", "5", "30")
        self.service.spend(ACCOUNT, 5_000_000, "unclear", self.paid(5_000_000, state="uncertain"))
        self.assertEqual(self.service.status(ACCOUNT)["remainingTodayAtomic"], 3_000_000)

    def test_a_refusal_before_anything_was_sent_gives_the_room_back(self):
        self.grant("100")
        self.service.setup(ACCOUNT, "8", "5", "30")
        with self.assertRaises(AllowanceError):
            self.service.spend(ACCOUNT, 5_000_000, "refused", self.refused("CHALLENGE_FAILED"))
        self.assertEqual(self.service.status(ACCOUNT)["remainingTodayAtomic"], 8_000_000)

    def test_a_bridge_refusal_keeps_its_code(self):
        from sign402_gateway.solana_chat import SolanaChatError

        def run(*args, **kwargs):
            raise SolanaChatError("PAYMENT_UNCERTAIN", "The answer was lost after paying.")
        self.service.agent_key = lambda account: ("Agent", "KEY")
        self.bridge.run = run
        with self.assertRaises(AllowanceError) as raised:
            self.service._call(ACCOUNT, "resource-pay")
        self.assertEqual(raised.exception.code, "PAYMENT_UNCERTAIN")

    def test_a_wallet_without_sol_never_uses_operator_fee_payer(self):
        self.bridge.owner_sol = 0
        self.service.setup(ACCOUNT, "20", "5", "30")
        with self.assertRaisesRegex(AllowanceError, "wallet needs SOL"):
            self.service.prepare_wallet(ACCOUNT, "GRANT", amount="20")
        self.assertFalse(any(c[0] == "allowance-prepare" for c in self.bridge.calls))

    def test_wallet_explicitly_funds_own_agent_sol_and_no_operator_key_is_used(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        op = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        self.assertIn("0.005 SOL", op["walletShows"])
        self.assertIn(self.service.agent_key(ACCOUNT)[0], op["walletShows"])
        call = self.bridge.calls[-1]
        self.assertEqual(call[4]["kind"], "fund-gas")
        self.assertEqual(call[4]["amount"], "5000000")
        self.assertIsNone(call[3])
        result = self.service.submit_wallet(ACCOUNT, op["operation"], "SIGNED")
        self.assertEqual(result["currency"], "SOL")
        self.assertIn("Added 0.005 SOL", result["detail"])
        self.assertIsNone(self.bridge.calls[-1][3])
        for amount in ["0", "-1", "NaN", "Infinity", "0.0000000001", "0.101"]:
            with self.assertRaises(AllowanceError):
                self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount=amount)

    def test_uncertain_gas_transfer_cannot_be_repeated_or_replaced(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        op = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        self.bridge.submit_state = "uncertain"
        self.assertEqual(self.service.submit_wallet(ACCOUNT, op["operation"], "SIGNED")["state"], "UNCERTAIN")
        calls = sum(c[0] == "allowance-submit" for c in self.bridge.calls)
        self.service.submit_wallet(ACCOUNT, op["operation"], "SIGNED")
        self.assertEqual(sum(c[0] == "allowance-submit" for c in self.bridge.calls), calls)
        with self.assertRaisesRegex(AllowanceError, "previous SOL transfer"):
            self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")

    def test_gas_routes_bind_the_operation_to_kind_and_account(self):
        from types import SimpleNamespace
        from sign402_gateway.web_api import WebApi, WebError
        api = SimpleNamespace(solana=self.service, prepare_by_account=Mock(), permit_by_account=Mock())
        self.service.setup(ACCOUNT, "20", "5", "30")
        status, prepared, _ = WebApi._solana_allowance(api, "POST", "/allowance/fund-gas/prepare", ACCOUNT, {"amount": "0.005"})
        self.assertEqual((status, prepared["kind"]), (200, "FUND_GAS"))
        body = {"operation": prepared["operation"], "transaction": "SIGNED"}
        with self.assertRaises(WebError):
            WebApi._solana_allowance(api, "POST", "/allowance/grant/submit", ACCOUNT, body)
        with self.assertRaises(WebError):
            WebApi._solana_allowance(api, "POST", "/allowance/fund-gas/submit", "solana:other", body)
        status, result, _ = WebApi._solana_allowance(api, "POST", "/allowance/fund-gas/submit", ACCOUNT, body)
        self.assertEqual((status, result["state"], result["currency"]), (200, "DONE", "SOL"))

    def test_the_bridge_knows_what_was_prepared_and_its_refusal_is_readable(self):
        from sign402_gateway.solana_chat import SolanaChatError
        self.grant()
        submit = [c for c in self.bridge.calls if c[0] == "allowance-submit"][-1][4]
        self.assertEqual((submit["kind"], submit["amount"]), ("approve", "20000000"))
        prepared = self.service.prepare_wallet(ACCOUNT, "REVOKE")
        self.bridge.run = Mock(side_effect=SolanaChatError("TRANSACTION_MISMATCH", "Your wallet signed something other than what was prepared. Nothing was sent."))
        with self.assertRaisesRegex(AllowanceError, "signed something other"):
            self.service.submit_wallet(ACCOUNT, prepared["operation"], "SIGNED")

    def test_a_late_or_foreign_submission_is_not_sent(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        prepared = self.service.prepare_wallet(ACCOUNT, "GRANT", amount="20")
        self.clock[0] += 120
        self.assertEqual(self.service.submit_wallet(ACCOUNT, prepared["operation"], "SIGNED")["state"], "EXPIRED")
        self.assertNotIn("allowance-submit", [c[0] for c in self.bridge.calls])
        with self.assertRaises(AllowanceError):
            self.service.submit_wallet("solana:" + generate_keypair()[0], prepared["operation"], "SIGNED")

    def test_the_agent_key_is_the_accounts_own_and_stored_encrypted(self):
        address, key = self.service.agent_key(ACCOUNT)
        self.assertEqual(self.service.agent_key(ACCOUNT), (address, key))
        raw = (Path(self.tmp.name) / "sol.db").read_bytes()
        self.assertNotIn(key.encode(), raw)
        self.assertNotEqual(self.service.agent_key("solana:" + generate_keypair()[0])[0], address)
        with self.assertRaises(AllowanceUnavailable):
            self.service.owner("wallet:0x1111111111111111111111111111111111111111")

    def test_limits_changed_while_reading_chain_are_rechecked_before_payment(self):
        self.grant("100")
        original = self.service.chain_state
        def reduced(*args, **kwargs):
            state = original(*args, **kwargs)
            self.service.setup(ACCOUNT, "1", "1", "30")
            return state
        self.service.chain_state = reduced
        pay = Mock(return_value={"state": "confirmed"})
        with self.assertRaisesRegex(AllowanceError, "per-purchase"):
            self.service.spend(ACCOUNT, 4_000_000, "queued", pay)
        pay.assert_not_called()
        self.assertEqual(self.service.store.spent_since(ACCOUNT, 0), 0)

    def test_queued_purchase_reads_limits_after_acquiring_lock(self):
        self.grant("100")
        waiting, release = threading.Event(), threading.Event()
        class Gate:
            def __enter__(self):
                waiting.set()
                if not release.wait(5):
                    raise AssertionError("test gate timed out")
            def __exit__(self, *args): pass
        self.service._spend_lock = Gate()
        pay = Mock(return_value={"state": "confirmed"})
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.service.spend, ACCOUNT, 4_000_000, "queued", pay)
            try:
                self.assertTrue(waiting.wait(2))
                self.service.setup(ACCOUNT, "1", "1", "30")
            finally:
                release.set()
            with self.assertRaisesRegex(AllowanceError, "per-purchase"):
                result.result(timeout=5)
        pay.assert_not_called()

    def test_independent_paying_instances_share_atomic_daily_budget(self):
        self.grant("100")
        self.service.setup(ACCOUNT, "5", "5", "30")
        other = SolanaAllowanceService(
            store=SolanaAllowanceStore(self.service.store.path), bridge=self.bridge, fernet=self.fernet,
            fee_payer_key=None, max_daily=100_000_000, max_per_purchase=25_000_000,
            max_days=90, max_grant=300_000_000, now=lambda: self.clock[0])
        barrier = threading.Barrier(2)
        def state(*args, **kwargs):
            barrier.wait(timeout=5)
            return {"owner": {"delegatedToAgent": "100000000", "amount": "100000000"}}
        self.service.chain_state = other.chain_state = state
        pay = Mock(return_value={"state": "confirmed", "transaction": "test"})
        def spend(lane):
            try:
                lane.spend(ACCOUNT, 4_000_000, "concurrent", pay)
                return True
            except AllowanceError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(spend, [self.service, other])), [False, True])
        pay.assert_called_once()
        self.assertEqual(self.service.store.spent_since(ACCOUNT, 0), 4_000_000)

    def test_metered_holds_use_current_limits_and_compete_with_other_spends(self):
        self.grant("100")
        self.service.setup(ACCOUNT, "5", "5", "30")
        self.service.store.reserve_metered("first", ACCOUNT, 3_000_000, 20_000_000, NOW)
        pay = Mock()
        with self.assertRaisesRegex(AllowanceError, "today's"):
            self.service.spend(ACCOUNT, 3_000_000, "other", pay)
        self.service.setup(ACCOUNT, "1", "1", "30")
        with self.assertRaisesRegex(AllowanceError, "per-purchase"):
            self.service.store.reserve_metered("stale", ACCOUNT, 2_000_000, 20_000_000, NOW)
        self.service.store.set_limits(ACCOUNT, OWNER, 5_000_000, 5_000_000, NOW, NOW)
        with self.assertRaisesRegex(AllowanceError, "expired"):
            self.service.store.reserve_metered("expired", ACCOUNT, 1, 20_000_000, NOW)
        pay.assert_not_called()

    def test_nonpositive_or_noninteger_spend_never_calls_payment(self):
        self.grant()
        for amount in (0, -1, True, 1.5, "100"):
            pay = Mock()
            with self.assertRaises(AllowanceError):
                self.service.spend(ACCOUNT, amount, "invalid", pay)
            pay.assert_not_called()

    def test_funding_validation_failure_allows_fresh_preparation(self):
        from sign402_gateway.solana_chat import SolanaChatError
        self.service.setup(ACCOUNT, "20", "5", "30")
        for code in ("TRANSACTION_MISMATCH", "SIGNATURE_REQUIRED", "BRIDGE_UNAVAILABLE"):
            with self.subTest(code):
                op = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
                with patch.object(self.bridge, "run", side_effect=SolanaChatError(code, "Validation failed.")):
                    with self.assertRaises(AllowanceError):
                        self.service.submit_wallet(ACCOUNT, op["operation"], "INVALID")
                self.assertEqual(self.service.operation(ACCOUNT, op["operation"])["state"], "FAILED")
                self.assertFalse(self.service.store.pending_gas(ACCOUNT))

    def test_lost_funding_reply_keeps_checkpoint_and_recovers_without_resubmission(self):
        from sign402_gateway.solana_chat import SolanaChatError
        self.service.setup(ACCOUNT, "20", "5", "30")
        op = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        original = self.bridge.run
        def lost(*args, **kwargs):
            if args[3] == "allowance-submit":
                stored = self.service.store.op(ACCOUNT, op["operation"])
                self.assertEqual((stored["state"], stored["tx"], stored["tx_blockhash"]), ("SUBMITTING", "SoLTx1", "blockhash"))
                raise SolanaChatError("BRIDGE_UNAVAILABLE", "Lost answer")
            return original(*args, **kwargs)
        with patch.object(self.bridge, "run", side_effect=lost):
            with self.assertRaises(AllowanceError):
                self.service.submit_wallet(ACCOUNT, op["operation"], "SIGNED")
        self.assertEqual(self.service.store.op(ACCOUNT, op["operation"])["state"], "UNCERTAIN")
        self.bridge.submit_state = "uncertain"
        self.assertEqual(self.service.operation(ACCOUNT, op["operation"])["state"], "UNCERTAIN")
        self.bridge.submit_state = "confirmed"
        self.assertEqual(self.service.operation(ACCOUNT, op["operation"])["state"], "DONE")
        self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")

    def test_expired_funding_recovers_but_unknown_legacy_submission_stays_blocked(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        op = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        self.bridge.submit_state = "uncertain"
        self.service.submit_wallet(ACCOUNT, op["operation"], "SIGNED")
        self.bridge.submit_state = "expired"
        self.assertEqual(self.service.operation(ACCOUNT, op["operation"])["state"], "EXPIRED")
        legacy = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        self.service.store.update_op(legacy["operation"], "SUBMITTING", NOW)
        self.clock[0] += 1000
        self.assertEqual(self.service.operation(ACCOUNT, legacy["operation"])["state"], "SUBMITTING")
        with self.assertRaisesRegex(AllowanceError, "previous SOL"):
            self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")

    def test_a_fresh_gas_prepare_supersedes_an_unclaimed_one_so_only_it_can_claim_the_account(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        first = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        second = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        self.assertEqual(self.service.store.op(ACCOUNT, first["operation"])["state"], "EXPIRED")
        self.assertFalse(self.service.store.claim_gas_submission(ACCOUNT, first["operation"], NOW))
        self.assertTrue(self.service.store.claim_gas_submission(ACCOUNT, second["operation"], NOW))
        self.clock[0] += 100
        self.assertFalse(self.service.store.checkpoint_gas(ACCOUNT, first["operation"], "tx", "block", NOW))

    def test_stale_validation_or_expiry_cannot_release_a_checkpointed_transfer(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        op = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")["operation"]
        self.service.store.claim_gas_submission(ACCOUNT, op, NOW)
        self.service.store.checkpoint_gas(ACCOUNT, op, "tx", "block", NOW)
        self.service.store.finish_pending_op(ACCOUNT, op, "FAILED", "stale validation timeout", NOW + 100, expected_state="VALIDATING")
        self.service.store.expire_prepared_op(ACCOUNT, op, NOW + 100)
        self.assertEqual(self.service.store.op(ACCOUNT, op)["state"], "SUBMITTING")
        self.assertTrue(self.service.store.pending_gas(ACCOUNT))

    # -- SI-45: a PREPARED op that is never signed must not sit forever --

    def test_a_fresh_grant_prepare_supersedes_an_older_unsigned_one(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        first = self.service.prepare_wallet(ACCOUNT, "GRANT", amount="10")
        with patch.object(analytics, "emit") as emit:
            second = self.service.prepare_wallet(ACCOUNT, "GRANT", amount="15")
        stale = self.service.store.op(ACCOUNT, first["operation"])
        self.assertEqual((stale["state"], stale["detail"]), ("EXPIRED", "Replaced by a newer request."))
        self.assertEqual(self.service.store.op(ACCOUNT, second["operation"])["state"], "PREPARED")
        superseded = [c for c in emit.call_args_list if c.args[0] == "approval_signed"]
        self.assertEqual(len(superseded), 1)
        self.assertEqual((superseded[0].args[1], superseded[0].kwargs["state"]), (ACCOUNT, "EXPIRED"))
        # Submitting the superseded one no longer touches the chain; it just reports EXPIRED.
        result = self.service.submit_wallet(ACCOUNT, first["operation"], "SIGNED")
        self.assertEqual(result["state"], "EXPIRED")
        self.assertNotIn("allowance-submit", [c[0] for c in self.bridge.calls])

    def test_superseding_is_scoped_to_the_same_kind_and_leaves_fund_gas_safety_states_alone(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        grant = self.service.prepare_wallet(ACCOUNT, "GRANT", amount="10")
        self.service.prepare_wallet(ACCOUNT, "REVOKE")
        self.assertEqual(self.service.store.op(ACCOUNT, grant["operation"])["state"], "PREPARED")
        gas = self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        self.service.store.claim_gas_submission(ACCOUNT, gas["operation"], self.clock[0])
        self.service.prepare_wallet(ACCOUNT, "GRANT", amount="12")
        self.assertEqual(self.service.store.op(ACCOUNT, gas["operation"])["state"], "VALIDATING")
        self.assertEqual(self.service.store.op(ACCOUNT, grant["operation"])["state"], "EXPIRED")

    def test_a_stale_unsigned_op_expires_lazily_in_status_and_is_still_reported_expired_on_submit(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        prepared = self.service.prepare_wallet(ACCOUNT, "GRANT", amount="10")
        self.clock[0] += 120
        with patch.object(analytics, "emit") as emit:
            status = self.service.status(ACCOUNT)
        self.assertEqual(status["state"], "waiting_for_grant")
        stale = self.service.store.op(ACCOUNT, prepared["operation"])
        self.assertEqual((stale["state"], stale["detail"]), ("EXPIRED", "Not signed in time."))
        expired = [c for c in emit.call_args_list if c.args[0] == "approval_signed" and c.kwargs.get("state") == "EXPIRED"]
        self.assertEqual(len(expired), 1)
        result = self.service.submit_wallet(ACCOUNT, prepared["operation"], "SIGNED")
        self.assertEqual(result["state"], "EXPIRED")
        self.assertNotIn("allowance-submit", [c[0] for c in self.bridge.calls])

    def test_a_stale_unsigned_op_expires_lazily_from_a_prepare_of_a_different_kind(self):
        self.service.setup(ACCOUNT, "20", "5", "30")
        prepared = self.service.prepare_wallet(ACCOUNT, "REVOKE")
        self.clock[0] += 120
        self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        self.assertEqual(self.service.store.op(ACCOUNT, prepared["operation"])["state"], "EXPIRED")

    def test_lack_of_sol_logs_without_the_address_and_reports_the_refusal(self):
        self.bridge.owner_sol = 0
        self.service.setup(ACCOUNT, "20", "5", "30")
        with patch.object(analytics, "emit") as emit:
            with self.assertLogs("sign402_gateway.solana_allowance", level="WARNING") as logs:
                with self.assertRaisesRegex(AllowanceError, "wallet needs SOL"):
                    self.service.prepare_wallet(ACCOUNT, "GRANT", amount="10")
        self.assertNotIn(OWNER, logs.output[0])
        self.assertIn(analytics.hash_account_id(ACCOUNT), logs.output[0])
        no_sol = [c for c in emit.call_args_list if c.args[0] == "approval_signed"]
        self.assertEqual(len(no_sol), 1)
        self.assertEqual(no_sol[0].kwargs["state"], "NO_SOL")

    def test_lack_of_sol_on_revoke_emits_revoke_not_approval_signed(self):
        self.bridge.owner_sol = 0
        self.service.setup(ACCOUNT, "20", "5", "30")
        with patch.object(analytics, "emit") as emit:
            with self.assertRaises(AllowanceError):
                self.service.prepare_wallet(ACCOUNT, "REVOKE")
        self.assertEqual([c.args[0] for c in emit.call_args_list], ["revoke"])
        self.assertEqual(emit.call_args_list[0].kwargs["state"], "NO_SOL")

    def test_lack_of_sol_on_fund_gas_emits_no_allowance_event(self):
        self.bridge.owner_sol = 0
        self.service.setup(ACCOUNT, "20", "5", "30")
        with patch.object(analytics, "emit") as emit:
            with self.assertRaises(AllowanceError):
                self.service.prepare_wallet(ACCOUNT, "FUND_GAS", amount="0.005")
        emit.assert_not_called()


class FeePayerKeyTests(unittest.TestCase):
    def test_recovery_column_migration_preserves_existing_keys_and_operations(self):
        from sign402_gateway.solana_allowance import SCHEMA
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with sqlite3.connect(path) as db:
                db.executescript(SCHEMA)
                db.execute("INSERT INTO agents VALUES (?, ?, ?, ?)", (ACCOUNT, "agent-fixture", "encrypted-fixture", NOW))
                db.execute("INSERT INTO ops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           ("legacy-op", ACCOUNT, "FUND_GAS", 5000000, "hash", "UNCERTAIN", "legacy-signature", "waiting", NOW, NOW, 1))
            store = SolanaAllowanceStore(path)
            self.assertEqual(store.agent(ACCOUNT)["encrypted_key"], "encrypted-fixture")
            row = store.op(ACCOUNT, "legacy-op")
            self.assertEqual((row["state"], row["tx"], row["tx_blockhash"]), ("UNCERTAIN", "legacy-signature", None))

    def test_a_new_fee_payer_is_encrypted_with_the_master_key(self):
        from sign402_gateway.solana_allowance import encrypt_fee_payer_key
        from sign402_gateway.solana_keys import keypair_address
        master = Fernet.generate_key().decode()
        address, blob = encrypt_fee_payer_key(master)
        self.assertEqual(keypair_address(Fernet(master.encode()).decrypt(blob.encode()).decode()), address)


if __name__ == "__main__":
    unittest.main()
