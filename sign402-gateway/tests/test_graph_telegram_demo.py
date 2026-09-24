import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cryptography.fernet import Fernet

from sign402_gateway.ledger_payments import LedgerOperationStore
from sign402_gateway.secure_state import SensitiveStateCipher


SPEC = importlib.util.spec_from_file_location(
    "graph_telegram_runner", Path(__file__).resolve().parents[1] / "scripts/graph-telegram-demo.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class RecordingSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cipher = SensitiveStateCipher(Fernet.generate_key().decode())
        self.store = LedgerOperationStore(self.base / "operations.sqlite3", self.cipher)
        self.request = "original-paid-request"
        self.tx = "0x" + "2" * 64
        self.payer = "0x" + "1" * 40
        self.verify = Mock(return_value={"verified": True, "transactionHash": self.tx})
        self.store.create("123456", self.request, {})
        (self.base / "payment-attempt.json").write_text(json.dumps({"requestId": self.request}))

    def completed(self):
        self.store.transition("123456", self.request, "preparing", "succeeded",
                              result={"paid": True, "transactionHash": self.tx})

    def select(self, owner="123456"):
        return RUNNER.recording_state(self.base, owner, self.cipher, self.payer, self.verify)

    def test_unresolved_original_refuses_recording(self):
        for state in ("preparing", "pending", "executing", "uncertain"):
            with self.subTest(state=state):
                current = self.store.get("123456", self.request)["status"]
                self.store.transition("123456", self.request, current, state)
                with self.assertRaises(ValueError):
                    self.select()
        self.verify.assert_not_called()
        self.assertFalse((self.base / "recording").exists())

    def test_successful_selection_preserves_both_sessions_on_restarts(self):
        self.completed()
        original = (self.base / "operations.sqlite3").read_bytes()
        recording = self.base / "recording"
        recording.mkdir()
        marker = recording / "payment-attempt.json"
        marker.write_text('{"requestId":"recording-already-paid"}')
        before = marker.read_bytes()
        self.assertEqual(self.select()[0], recording)
        self.assertEqual(self.select()[0], recording)
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual((self.base / "operations.sqlite3").read_bytes(), original)
        self.verify.assert_called_with(self.tx, self.payer)

    def test_other_owner_cannot_reuse_original_payment(self):
        self.completed()
        with self.assertRaises(ValueError):
            self.select("654321")
        self.verify.assert_not_called()

    def test_cached_result_cannot_stand_in_for_original_payment(self):
        self.store.transition("123456", self.request, "preparing", "succeeded",
                              result={"paid": False, "transactionHash": self.tx})
        with self.assertRaises(ValueError):
            self.select()
        self.verify.assert_not_called()

    def test_receipt_must_verify_and_match(self):
        self.completed()
        for proof in ({"verified": False, "transactionHash": self.tx},
                      {"verified": True, "transactionHash": "different"}):
            self.verify.return_value = proof
            with self.assertRaises(ValueError):
                self.select()
        self.verify.side_effect = TimeoutError("RPC unavailable")
        with self.assertRaises(TimeoutError):
            self.select()
        self.assertFalse((self.base / "recording").exists())


if __name__ == "__main__":
    unittest.main()
