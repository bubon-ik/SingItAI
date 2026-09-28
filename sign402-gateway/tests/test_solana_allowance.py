import tempfile
import unittest
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

    def run(self, user_id, payer, key, operation, fee_payer_key=None, **payload):
        self.calls.append((operation, payer, bool(key), fee_payer_key, payload))
        if operation == "allowance-state":
            return {"owner": {"amount": str(self.owner_usdc), "delegatedToAgent": str(self.delegated)},
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
        self.assertEqual(prepare_call[4], {"owner": OWNER, "kind": "approve", "amount": "20000000"})
        self.assertEqual(prepare_call[3], "FEE-PAYER-KEY")  # our fee payer pays the network fee
        done = self.service.submit_wallet(ACCOUNT, prepared["operation"], "SIGNED")
        self.assertEqual((done["state"], done["detail"]), ("DONE", "Allowance now 20 USDC."))
        self.assertEqual(self.bridge.calls[-1][4]["messageHash"], "hash-approve20000000")  # checked against the prepared one
        status = self.service.status(ACCOUNT)
        self.assertEqual((status["state"], status["allowanceAtomic"], status["remainingTodayAtomic"]), ("granted", 20_000_000, 20_000_000))

    def test_a_purchase_pulls_only_what_it_needs_within_the_limits(self):
        self.grant()
        funded = self.service.fund(ACCOUNT, 5_000_000, "Venice credit")
        self.assertEqual((funded["pulled"], funded["pullTx"]), (5_000_000, "PullTx"))
        self.bridge.agent_usdc -= 5_000_000  # the merchant was paid from it
        self.bridge.agent_usdc += 1_000_000  # a leftover from an earlier purchase is used first
        self.assertEqual(self.service.fund(ACCOUNT, 3_000_000, "x")["pulled"], 2_000_000)
        self.assertEqual(self.service.status(ACCOUNT)["remainingTodayAtomic"], 12_000_000)

    def test_what_is_refused_before_anything_moves(self):
        with self.assertRaises(AllowanceUnavailable):
            self.service.fund(ACCOUNT, 1, "x")
        self.grant("6")
        pulls = lambda: [c for c in self.bridge.calls if c[0] == "allowance-pull"]
        for amount, reason in ((6_000_000, "per-purchase"), (5_000_000, None), (5_000_000, "approval is used up")):
            with self.subTest(amount=amount):
                if reason is None:
                    self.service.fund(ACCOUNT, amount, "ok")
                    self.bridge.agent_usdc = 0
                else:
                    with self.assertRaisesRegex(AllowanceError, reason):
                        self.service.fund(ACCOUNT, amount, "x")
        self.assertEqual(len(pulls()), 1)
        self.clock[0] += 31 * 86400
        with self.assertRaisesRegex(AllowanceError, "expired"):
            self.service.fund(ACCOUNT, 1, "x")

    def test_the_daily_limit_counts_every_purchase_of_the_day(self):
        self.grant("100")
        self.service.setup(ACCOUNT, "8", "5", "30")
        self.service.fund(ACCOUNT, 5_000_000, "a")
        with self.assertRaisesRegex(AllowanceError, "today's 8 USDC limit"):
            self.service.fund(ACCOUNT, 4_000_000, "b")
        self.clock[0] += 86400
        self.service.fund(ACCOUNT, 4_000_000, "b")

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


if __name__ == "__main__":
    unittest.main()
