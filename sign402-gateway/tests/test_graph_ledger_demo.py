import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from cryptography.fernet import Fernet
from eth_account import Account
from eth_account.messages import encode_defunct

from sign402_gateway.graph_ledger_demo import GraphLedgerDemo
from sign402_gateway.secure_state import SensitiveStateCipher
from tests.test_onchain_data import graph_402, pool


class GraphLedgerDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.key = Account.create()
        self.payer_address = "0x" + "1" * 40
        self.tx = "0x" + "2" * 64
        self.cipher = SensitiveStateCipher(Fernet.generate_key().decode())
        self.quote = Mock(return_value=graph_402())
        self.payer = Mock(return_value={"ok": True, "status": 200, "payer": self.payer_address,
            "transactionHash": self.tx, "body": {"data": {"asToken0": [pool()],
                "_meta": {"block": {"number": 12345}, "hasIndexingErrors": False}}}})
        self.receipt = Mock(return_value={"verified": True, "transactionHash": self.tx})
        self.signer = Mock(side_effect=self.sign)
        self.demo = self.build()

    def build(self):
        return GraphLedgerDemo(owner="123456", approver=self.key.address,
            payer_address=self.payer_address, state=self.state, cipher=self.cipher,
            quote=self.quote, payer=self.payer, signer=self.signer, receipt=self.receipt,
            historical_proof={"verified": True, "transactionHash": "0x" + "3" * 64})

    def sign(self, pending, owner):
        self.payer.assert_not_called()
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["approval"]["message"]["purchase"], "The Graph - WETH price")
        self.assertIn("#query=", pending["approval"]["message"]["resource"])
        message = pending["approval"]
        signature = self.key.sign_message(encode_defunct(text=message["displayText"])).signature.hex()
        return {"signingMethod": "personal_sign", "signature": signature,
                "expiresAt": pending["expiresAt"], "journalId": message["message"]["journalId"]}

    def run_query(self, request="telegram-message-001"):
        return self.demo.start("123456", request, background=False)

    def test_real_verifier_gates_payer_and_result_has_tx_link(self):
        result = self.run_query()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["result"]["paid"])
        self.assertIn("https://basescan.org/tx/" + self.tx, result["text"])
        self.assertIn("12345", result["text"])
        self.signer.assert_called_once()
        self.payer.assert_called_once()
        self.assertEqual(self.payer.call_args.kwargs["request_body"]["variables"]["symbol"], "WETH")

    def test_cached_repeat_survives_reopening_without_device_or_payment(self):
        self.assertEqual(self.run_query()["status"], "succeeded")
        self.demo = self.build()
        self.signer.side_effect = AssertionError("Device must not be called for cached data")
        self.quote.side_effect = AssertionError("No second 402")
        self.payer.side_effect = AssertionError("No second payment")
        result = self.run_query("telegram-message-002")
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["result"]["paid"])
        self.assertIn("0 USDC", result["text"])
        self.assertEqual(self.payer.call_count, 1)
        self.assertEqual(self.signer.call_count, 1)

    def test_same_telegram_request_returns_saved_result(self):
        first = self.run_query()
        self.assertEqual(self.run_query(), first)
        self.payer.assert_called_once()

    def test_rejected_device_sends_no_payment(self):
        self.signer.side_effect = ValueError("Device rejected")
        self.assertEqual(self.run_query()["status"], "failed")
        self.payer.assert_not_called()
        self.assertFalse((self.state / "payment-attempt.json").exists())

    def test_wrong_signer_is_refused(self):
        bad = Account.create()
        def sign_bad(pending, owner):
            value = self.sign(pending, owner)
            value["signature"] = bad.sign_message(encode_defunct(text=pending["approval"]["displayText"])).signature.hex()
            return value
        self.signer.side_effect = sign_bad
        self.assertEqual(self.run_query()["status"], "failed")
        self.payer.assert_not_called()

    def test_changed_quote_after_signature_sends_no_payment(self):
        self.quote.side_effect = [graph_402(), graph_402(amount="20000")]
        self.assertEqual(self.run_query()["status"], "failed")
        self.signer.assert_called_once()
        self.payer.assert_not_called()

    def test_unknown_payment_outcome_blocks_another_request(self):
        self.payer.side_effect = TimeoutError("unknown settlement")
        self.assertEqual(self.run_query()["status"], "uncertain")
        self.demo = self.build()
        with self.assertRaises(ValueError):
            self.run_query("telegram-message-002")
        self.payer.assert_called_once()

    def test_other_owner_cannot_start_or_read_demo(self):
        with self.assertRaises(PermissionError):
            self.demo.start("654321", "telegram-message-001", background=False)
        with self.assertRaises(PermissionError):
            self.demo.latest("654321")
        self.quote.assert_not_called()
        self.payer.assert_not_called()

    def test_receipt_rpc_failure_does_not_erase_paid_result(self):
        self.receipt.side_effect = TimeoutError("RPC unavailable")
        result = self.run_query()
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["result"]["receiptVerified"])
        self.assertIn("verification pending", result["text"])
        self.payer.assert_called_once()

    def test_background_worker_exposes_pending_then_delivers_result(self):
        waiting, resume = threading.Event(), threading.Event()
        original_sign = self.sign
        def delayed_sign(pending, owner):
            waiting.set()
            if not resume.wait(5):
                raise ValueError("Test did not resume")
            return original_sign(pending, owner)
        self.signer.side_effect = delayed_sign
        self.demo.start("123456", "telegram-message-async")
        self.assertTrue(waiting.wait(5))
        try:
            snapshot = self.demo.status("123456", "telegram-message-async")
            self.assertEqual(snapshot["status"], "pending")
            self.payer.assert_not_called()
            duplicate = self.demo.start("123456", "telegram-message-another")
            self.assertEqual(duplicate["requestId"], "telegram-message-async")
        finally:
            resume.set()
        deadline = time.monotonic() + 5
        while self.demo.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.demo.status("123456", "telegram-message-async")["status"], "succeeded")
        self.payer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
