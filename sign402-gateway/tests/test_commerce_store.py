import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sign402_gateway.commerce_store import BitrefillCommerceStore


class CommerceStoreTests(unittest.TestCase):
    def test_initialization_closes_database_connection(self):
        connection = MagicMock()
        with patch(
            "sign402_gateway.commerce_store.sqlite3.connect",
            return_value=connection,
        ):
            BitrefillCommerceStore(Path("orders.sqlite3"))

        connection.close.assert_called_once_with()

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
