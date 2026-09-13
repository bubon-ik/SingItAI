import importlib.util
import json
from pathlib import Path
import tempfile
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "graph_live_check", Path(__file__).resolve().parents[1] / "scripts/graph-live-check.py"
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class VideoDemoSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.payer = "0x" + "1" * 40
        self.tx = "0x" + "2" * 64

    def completed(self):
        (self.base / "attempt.json").write_text("{}")
        (self.base / "result.json").write_text(json.dumps(
            {"ok": True, "status": 200, "transactionHash": self.tx}))
        (self.base / "verification.json").write_text(json.dumps({
            "restartCachePassed": True,
            "proof": {"verified": True, "transactionHash": self.tx}}))

    def test_uncertain_original_cannot_be_bypassed(self):
        (self.base / "attempt.json").write_text("{}")
        with patch.object(CHECK, "verify_receipt") as verify:
            with self.assertRaises(FileNotFoundError):
                CHECK.video_demo_state(self.base, self.payer)
            verify.assert_not_called()
        self.assertFalse((self.base / "video-demo").exists())

    def test_mismatched_receipt_is_refused(self):
        self.completed()
        (self.base / "verification.json").write_text(json.dumps({
            "restartCachePassed": True,
            "proof": {"verified": True, "transactionHash": "another transaction"}}))
        with patch.object(CHECK, "verify_receipt") as verify:
            with self.assertRaises(ValueError):
                CHECK.video_demo_state(self.base, self.payer)
            verify.assert_not_called()

    def test_receipt_must_still_verify(self):
        self.completed()
        with patch.object(CHECK, "verify_receipt", side_effect=ValueError("unverified")):
            with self.assertRaises(ValueError):
                CHECK.video_demo_state(self.base, self.payer)

    def test_repeated_selection_preserves_demo_attempt_and_original(self):
        self.completed()
        original = (self.base / "result.json").read_bytes()
        demo = self.base / "video-demo"
        demo.mkdir()
        marker = demo / "attempt.json"
        marker.write_text('{"startedAt":"existing attempt"}')
        with patch.object(CHECK, "verify_receipt") as verify:
            self.assertEqual(CHECK.video_demo_state(self.base, self.payer), demo)
            self.assertEqual(CHECK.video_demo_state(self.base, self.payer), demo)
            verify.assert_called_with(self.tx, self.payer)
        self.assertEqual(marker.read_text(), '{"startedAt":"existing attempt"}')
        self.assertEqual((self.base / "result.json").read_bytes(), original)

    def test_run_refuses_existing_demo_attempt_before_quote_or_payment(self):
        self.completed()
        demo = self.base / "video-demo"
        demo.mkdir()
        (demo / "attempt.json").write_text("{}")
        with patch.object(CHECK, "STATE", self.base), \
                patch.object(CHECK, "payer_address", return_value=self.payer), \
                patch.object(CHECK, "verify_receipt"), \
                patch.object(CHECK, "_urllib_402") as quote, \
                patch.object(CHECK, "CdpBaseX402PaymentClient") as payer, \
                patch("sys.argv", ["graph-live-check.py", "run", "--video-demo"]):
            with self.assertRaisesRegex(ValueError, "payment attempt already exists"):
                CHECK.main()
            quote.assert_not_called()
            payer.assert_not_called()

    def test_balance_failure_preserves_verified_payment_and_purchase_record(self):
        proof = {"transactionHash": self.tx, "payer": self.payer, "blockNumber": 123, "verified": True}
        with patch.object(CHECK, "STATE", self.base), \
                patch.object(CHECK, "balance", side_effect=ValueError("RPC unavailable")), \
                patch("builtins.print"):
            result = CHECK.save_completed_check({"proof": proof, "restartCachePassed": True}, self.tx)
        self.assertEqual(result["balanceCheck"], "unavailable")
        saved = json.loads((self.base / "verification.json").read_text())
        self.assertEqual(saved["proof"], proof)
        self.assertTrue(saved["restartCachePassed"])
        self.assertEqual(json.loads((self.base / "purchase-record.json").read_text())["invoice_id"], self.tx)

    def test_status_recovers_journal_evidence_without_quote_or_payer(self):
        self.completed()
        (self.base / "verification.json").unlink()
        (self.base / "plan.json").write_text('{"balanceUsdc":"1.00"}')
        proof = {"transactionHash": self.tx, "payer": self.payer, "blockNumber": 123, "verified": True}
        paid = {"id": "paid", "extra": {"paid": True, "owner": CHECK.OWNER, "tx_id": self.tx}}
        cached = {"id": "cached", "extra": {"paid": False, "owner": CHECK.OWNER, "served_from": "paid"}}
        policy = Mock()
        policy.memory.journal.return_value = [paid, cached]
        price = SimpleNamespace(usd=Decimal("2500"), liquidity_usd=Decimal("100000"), pool="pool", block_number=120)
        with patch.object(CHECK, "STATE", self.base), \
                patch.object(CHECK, "payer_address", return_value=self.payer), \
                patch.object(CHECK, "verify_receipt", return_value=proof), \
                patch.object(CHECK, "policy", return_value=policy), \
                patch.object(CHECK, "read_price", return_value=price), \
                patch.object(CHECK, "PaidGraphQueries") as queries, \
                patch.object(CHECK, "balance", return_value=990000), \
                patch.object(CHECK, "_urllib_402") as quote, \
                patch.object(CHECK, "CdpBaseX402PaymentClient") as payer, \
                patch("sys.argv", ["graph-live-check.py", "status"]), patch("builtins.print"):
            queries.return_value.spent_on_data.return_value = {"queries_paid": 1, "queries_from_memory": 1, "spent_usd": "0.01"}
            CHECK.main()
            quote.assert_not_called()
            payer.assert_not_called()
        saved = json.loads((self.base / "verification.json").read_text())
        self.assertTrue(saved["recoveredFromSavedResponseAndJournal"])
        self.assertTrue(saved["cachedRepeatRecorded"])
        self.assertEqual(saved["balanceAfterUsdc"], "0.99")

    def test_recovery_refuses_unlinked_cache_entry(self):
        proof = {"transactionHash": self.tx, "payer": self.payer, "blockNumber": 123, "verified": True}
        result = {"ok": True, "status": 200, "transactionHash": self.tx}
        policy = Mock()
        policy.memory.journal.return_value = [
            {"id": "paid", "extra": {"paid": True, "owner": CHECK.OWNER, "tx_id": self.tx}},
            {"id": "cached", "extra": {"paid": False, "owner": CHECK.OWNER, "served_from": "different"}}]
        with patch.object(CHECK, "policy", return_value=policy), \
                patch.object(CHECK, "read_price", return_value=Mock()), \
                patch.object(CHECK, "PaidGraphQueries"), \
                patch.object(CHECK, "save_completed_check") as save:
            with self.assertRaisesRegex(ValueError, "paid-once cache reuse"):
                CHECK.recover_completed_check(result, proof)
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
