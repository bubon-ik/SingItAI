import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

from cryptography.fernet import Fernet

from sign402_gateway.agent_allowance import AllowanceError, AllowanceUnavailable
from sign402_gateway.solana_allowance import SolanaAllowanceService, SolanaAllowanceStore
from sign402_gateway.solana_keys import generate_keypair

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
        self.service.spend(ACCOUNT, 1_000_000, "refused", self.paid(0, state="uncertain"))
        self.assertEqual(self.service.status(ACCOUNT)["remainingTodayAtomic"], 3_000_000)
        with self.assertRaisesRegex(AllowanceError, "today's 8 USDC limit"):
            self.service.spend(ACCOUNT, 4_000_000, "b", self.paid(4_000_000))
        self.clock[0] += 86400
        self.service.spend(ACCOUNT, 4_000_000, "b", self.paid(4_000_000))

    def test_a_wallet_without_sol_uses_our_fee_payer_only_if_there_is_one(self):
        self.bridge.owner_sol = 0
        self.service.setup(ACCOUNT, "20", "5", "30")
        self.service.prepare_wallet(ACCOUNT, "GRANT", amount="20")
        self.assertEqual(self.bridge.calls[-1][3], "FEE-PAYER-KEY")
        self.service.fee_payer_key = None
        with self.assertRaisesRegex(AllowanceError, "needs a little SOL"):
            self.service.prepare_wallet(ACCOUNT, "GRANT", amount="20")

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


class FeePayerKeyTests(unittest.TestCase):
    def test_a_new_fee_payer_is_encrypted_with_the_master_key(self):
        from sign402_gateway.solana_allowance import encrypt_fee_payer_key
        from sign402_gateway.solana_keys import keypair_address
        master = Fernet.generate_key().decode()
        address, blob = encrypt_fee_payer_key(master)
        self.assertEqual(keypair_address(Fernet(master.encode()).decrypt(blob.encode()).decode()), address)


if __name__ == "__main__":
    unittest.main()
