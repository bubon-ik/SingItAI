import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from sign402_gateway.commerce_store import BitrefillCommerceStore
from sign402_gateway.secure_state import (
    SensitiveStateCipher,
    SensitiveStateDecryptionError,
    SensitiveStateError,
)


def test_cipher():
    return SensitiveStateCipher(Fernet.generate_key().decode("ascii"))


def raw_metadata(path: Path, quote_id: str) -> dict:
    with sqlite3.connect(path) as db:
        value = db.execute(
            "SELECT metadata_json FROM bitrefill_orders WHERE quote_id = ?",
            (quote_id,),
        ).fetchone()[0]
    return json.loads(value)


class CommerceStoreTests(unittest.TestCase):
    def test_initialization_closes_database_connection(self):
        connection = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "sign402_gateway.commerce_store.sqlite3.connect",
                return_value=connection,
            ):
                BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")

        connection.close.assert_called_once_with()

    def test_new_recipient_is_encrypted_in_raw_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=test_cipher())
            store.save_quote(
                {
                    "quoteId": "q1",
                    "productId": "p1",
                    "packageId": "pkg1",
                    "packageValue": "10",
                    "expiresAtEpoch": 999,
                }
            )
            store.advance_state(
                "q1",
                "USER_APPROVED",
                {"recipient": {"email": "buyer@example.com"}},
            )

            raw = raw_metadata(path, "q1")
            self.assertNotIn("recipient", raw)
            self.assertNotIn("buyer@example.com", json.dumps(raw))
            self.assertIn("encryptedRecipient", raw)
            self.assertEqual(
                store.get_quote("q1")["metadata"]["recipient"],
                {"email": "buyer@example.com"},
            )
            self.assertNotIn(
                "encryptedRecipient",
                store.get_quote("q1")["metadata"],
            )
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_recipient_write_without_cipher_fails_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path)
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            before = store.get_quote("q1")

            with self.assertRaises(SensitiveStateError):
                store.advance_state(
                    "q1",
                    "USER_APPROVED",
                    {"recipient": {"email": "buyer@example.com"}},
                )

            self.assertEqual(store.get_quote("q1"), before)
            self.assertEqual(raw_metadata(path, "q1"), {})
            self.assertNotIn(
                "buyer@example.com",
                json.dumps(raw_metadata(path, "q1")),
            )

    def test_legacy_recipient_reads_but_row_update_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=test_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            legacy = {"recipient": {"email": "legacy@example.com"}}
            with sqlite3.connect(path) as db:
                db.execute(
                    "UPDATE bitrefill_orders SET metadata_json = ? "
                    "WHERE quote_id = ?",
                    (json.dumps(legacy), "q1"),
                )

            self.assertEqual(store.get_quote("q1")["metadata"], legacy)
            with self.assertRaisesRegex(
                SensitiveStateError,
                "legacy plaintext recipient must be migrated",
            ):
                store.advance_state("q1", "USER_APPROVED", {"paymentHash": "a" * 64})
            self.assertEqual(raw_metadata(path, "q1"), legacy)

    def test_malformed_encrypted_recipient_never_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=test_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            seeded = {
                "encryptedRecipient": "not-ciphertext",
                "recipient": {"email": "legacy@example.com"},
            }
            with sqlite3.connect(path) as db:
                db.execute(
                    "UPDATE bitrefill_orders SET metadata_json = ? "
                    "WHERE quote_id = ?",
                    (json.dumps(seeded), "q1"),
                )

            with self.assertRaises(SensitiveStateDecryptionError) as captured:
                store.get_quote("q1")
            self.assertNotIn(
                "legacy@example.com",
                str(captured.exception),
            )

    def test_sqlite_modes_are_private_under_umask_022(self):
        previous_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "private" / "orders.sqlite3"
                BitrefillCommerceStore(path, cipher=test_cipher())
                self.assertEqual(
                    stat.S_IMODE(path.parent.stat().st_mode),
                    0o700,
                )
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    0o600,
                )
        finally:
            os.umask(previous_umask)

    def test_existing_sqlite_modes_are_repaired_before_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            path = parent / "orders.sqlite3"
            BitrefillCommerceStore(path, cipher=test_cipher())
            os.chmod(parent, 0o755)
            os.chmod(path, 0o644)

            BitrefillCommerceStore(path, cipher=test_cipher())

            self.assertEqual(
                stat.S_IMODE(parent.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(path.stat().st_mode),
                0o600,
            )

    def test_dangling_sqlite_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir()
            path = parent / "orders.sqlite3"
            outside = Path(tmp) / "outside.sqlite3"
            path.symlink_to(outside)

            with self.assertRaises(SensitiveStateError):
                BitrefillCommerceStore(path, cipher=test_cipher())

            self.assertFalse(outside.exists())

    def test_try_mark_fulfilling_refuses_legacy_recipient_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=test_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            legacy = {"recipient": {"email": "legacy@example.com"}}
            with sqlite3.connect(path) as db:
                db.execute(
                    "UPDATE bitrefill_orders SET metadata_json = ? "
                    "WHERE quote_id = ?",
                    (json.dumps(legacy), "q1"),
                )

            with self.assertRaises(SensitiveStateError):
                store.try_mark_fulfilling("q1")

            self.assertEqual(raw_metadata(path, "q1"), legacy)
            with sqlite3.connect(path) as db:
                state = db.execute(
                    "SELECT state FROM bitrefill_orders WHERE quote_id = ?",
                    ("q1",),
                ).fetchone()[0]
            self.assertEqual(state, "QUOTED")

    def test_save_and_read_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote = {
                "quoteId": "quote_1",
                "productId": "test-gift-card-code",
                "productName": "Test Gift Card Code",
                "country": "US",
                "packageValue": "25",
                "priceUsd": "25.00",
                "maxSingitAtomic": "2625000000000000000000",
                "expiresAtEpoch": 1_719_000_120,
            }

            store.save_quote(quote)
            loaded = store.get_quote("quote_1")

            self.assertEqual(loaded["quote"]["quoteId"], "quote_1")
            self.assertEqual(loaded["state"], "QUOTED")

    def test_state_transition_is_monotonic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote({"quoteId": "quote_1", "productId": "p", "expiresAtEpoch": 1})
            store.advance_state("quote_1", "FIREFLY_APPROVED", {"paymentHash": "a" * 64})

            with self.assertRaisesRegex(ValueError, "cannot move order state backward"):
                store.advance_state("quote_1", "QUOTED", {})

    def test_checkpoint_metadata_does_not_change_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote({"quoteId": "quote_1", "productId": "p", "expiresAtEpoch": 1})

            store.checkpoint("quote_1", {"bitrefillCheckpoint": {"invoiceId": "invoice_1"}})

            record = store.get_quote("quote_1")
            self.assertEqual(record["state"], "QUOTED")
            self.assertEqual(record["metadata"]["bitrefillCheckpoint"]["invoiceId"], "invoice_1")

    def test_reserve_fulfillment_lock_prevents_second_purchase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote({"quoteId": "quote_1", "productId": "p", "expiresAtEpoch": 1})

            self.assertTrue(store.try_mark_fulfilling("quote_1"))
            self.assertFalse(store.try_mark_fulfilling("quote_1"))

    def test_concurrent_fulfillment_lock_lets_only_one_winner(self):
        # Two store instances == two connections (separate process simulation).
        # A shared threading.Lock would not protect across processes, so the
        # check-and-set must be atomic at the database level.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            seed = BitrefillCommerceStore(path)
            for i in range(40):
                seed.save_quote(
                    {"quoteId": f"q{i}", "productId": "p", "expiresAtEpoch": 1}
                )

            for i in range(40):
                quote_id = f"q{i}"
                store_a = BitrefillCommerceStore(path)
                store_b = BitrefillCommerceStore(path)
                barrier = threading.Barrier(2)
                results: list[object] = []
                lock = threading.Lock()

                def worker(store):
                    barrier.wait()
                    try:
                        outcome = store.try_mark_fulfilling(quote_id)
                    except Exception as exc:  # noqa: BLE001 - capture for assertion
                        outcome = exc
                    with lock:
                        results.append(outcome)

                threads = [
                    threading.Thread(target=worker, args=(store_a,)),
                    threading.Thread(target=worker, args=(store_b,)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertNotIn(
                    True,
                    [isinstance(r, Exception) for r in results],
                    msg=f"iteration {i} raised: {results}",
                )
                self.assertEqual(
                    sorted(bool(r) for r in results),
                    [False, True],
                    msg=f"iteration {i} did not yield exactly one winner: {results}",
                )


if __name__ == "__main__":
    unittest.main()
