import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from sign402_gateway.commerce_store import BitrefillCommerceStore
from sign402_gateway.secure_state import (
    SensitiveStateCipher,
    SensitiveStateDecryptionError,
    SensitiveStateError,
)


def make_cipher():
    return SensitiveStateCipher(Fernet.generate_key().decode("ascii"))


def raw_metadata(path: Path, quote_id: str) -> dict:
    with closing(sqlite3.connect(path)) as db:
        value = db.execute(
            "SELECT metadata_json FROM bitrefill_orders WHERE quote_id = ?",
            (quote_id,),
        ).fetchone()[0]
    return json.loads(value)


def raw_order_row(path: Path, quote_id: str) -> tuple[str, str, int]:
    with closing(sqlite3.connect(path)) as db:
        return db.execute(
            "SELECT state, metadata_json, updated_at "
            "FROM bitrefill_orders WHERE quote_id = ?",
            (quote_id,),
        ).fetchone()


class CommerceStoreTests(unittest.TestCase):
    def test_refunded_is_terminal_and_token_return_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=make_cipher())
            store.save_quote(
                {
                    "quoteId": "q1",
                    "productId": "trusted-product",
                    "expiresAtEpoch": 999,
                }
            )
            store.advance_state("q1", "RECONCILIATION_REQUIRED")
            store.advance_state(
                "q1",
                "REFUNDED",
                {
                    "tokenReturn": {
                        "transactionHash": "0xRETURN",
                        "network": "base",
                        "token": "0xTOKEN",
                        "amountAtomic": "101000000",
                        "from": "0xCDP",
                        "to": "0xUSER",
                        "privateKey": "SECRET-MARKER",
                        "stdout": "UNSAFE-STDOUT",
                    }
                },
            )

            record = store.get_quote("q1")
            self.assertEqual(record["state"], "REFUNDED")
            self.assertEqual(
                record["metadata"]["tokenReturn"],
                {
                    "transactionHash": "0xRETURN",
                    "network": "base",
                    "token": "0xTOKEN",
                    "amountAtomic": "101000000",
                    "from": "0xCDP",
                    "to": "0xUSER",
                },
            )
            self.assertFalse(store.try_mark_fulfilling("q1"))
            with self.assertRaisesRegex(ValueError, "backward"):
                store.advance_state("q1", "RECONCILIATION_REQUIRED")

    def test_provider_and_checkpoint_snapshots_are_strict_allowlists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=make_cipher())
            store.save_quote(
                {
                    "quoteId": "q1",
                    "productId": "trusted-product",
                    "packageId": "trusted-package",
                    "packageValue": "25",
                    "expiresAtEpoch": 999,
                }
            )
            forbidden = {
                "redemption": {"value": {"code": "MARKER-REDEMPTION"}},
                "code": "MARKER-CODE",
                "pin": "MARKER-PIN",
                "activationUrl": "MARKER-ACTIVATION",
                "esim": {"activation": {"code": "MARKER-ESIM"}},
                "paymentLink": "MARKER-PAYMENT-LINK",
                "apiKey": "MARKER-API-KEY",
                "stdout": "MARKER-STDOUT",
                "stderr": "MARKER-STDERR",
                "command": "MARKER-COMMAND",
            }
            store.advance_state(
                "q1",
                "BITREFILL_PURCHASED",
                {
                    "bitrefill": {
                        "provider": "bitrefill-mcp",
                        "invoiceId": "invoice-1",
                        "orderId": "order-1",
                        "status": "DELIVERED",
                        "paymentMethod": "usdc_base",
                        "createdAt": "100",
                        "updatedAt": "101",
                        "expiresAt": "102",
                        "productId": "untrusted-product",
                        "packageId": "untrusted-package",
                        "packageValue": "999",
                        "treasuryPayment": {
                            "transactionHash": "0xTX",
                            "chain": "base",
                            "currency": "USDC",
                            "amount": "25",
                            "amountAtomic": "25000000",
                            "credentials": "MARKER-TREASURY-CREDENTIAL",
                        },
                        **forbidden,
                    },
                    "bitrefillCheckpoint": {
                        "invoiceId": "invoice-1",
                        "status": "PROCESSING",
                        "orderIds": ["order-1", "order-2"],
                        "paymentInfo": {
                            "amount": "25",
                            "asset": "USDC",
                            "network": "base",
                            "address": "MARKER-PAYMENT-ADDRESS",
                        },
                        "treasuryPayment": {
                            "hash": "0xTX",
                            "network": "base",
                            "token": "USDC",
                            "amount": "25",
                            "amountAtomic": "25000000",
                        },
                        **forbidden,
                    },
                },
            )

            metadata = raw_metadata(path, "q1")
            encoded = json.dumps(metadata, sort_keys=True)
            for key in (
                "redemption",
                "code",
                "pin",
                "activationUrl",
                "esim",
                "paymentLink",
                "apiKey",
                "stdout",
                "stderr",
                "command",
                "address",
                "credentials",
            ):
                self.assertNotIn(f'"{key}"', encoded)
            for marker in (
                "MARKER-REDEMPTION",
                "MARKER-CODE",
                "MARKER-PIN",
                "MARKER-ACTIVATION",
                "MARKER-ESIM",
                "MARKER-PAYMENT-LINK",
                "MARKER-API-KEY",
                "MARKER-STDOUT",
                "MARKER-STDERR",
                "MARKER-COMMAND",
                "MARKER-PAYMENT-ADDRESS",
                "MARKER-TREASURY-CREDENTIAL",
            ):
                self.assertNotIn(marker, encoded)
                for sidecar in path.parent.glob(f"{path.name}*"):
                    self.assertNotIn(
                        marker,
                        sidecar.read_bytes().decode("utf-8", errors="ignore"),
                    )
            provider = metadata["bitrefill"]
            self.assertEqual(
                provider,
                {
                    "provider": "bitrefill-mcp",
                    "invoiceId": "invoice-1",
                    "orderId": "order-1",
                    "status": "delivered",
                    "paymentMethod": "usdc_base",
                    "createdAt": "100",
                    "updatedAt": "101",
                    "expiresAt": "102",
                    "productId": "trusted-product",
                    "packageId": "trusted-package",
                    "packageValue": "25",
                    "treasuryPayment": {
                        "txId": "0xTX",
                        "network": "base",
                        "asset": "USDC",
                        "amount": "25",
                        "amountAtomic": "25000000",
                    },
                },
            )
            checkpoint = metadata["bitrefillCheckpoint"]
            self.assertEqual(
                checkpoint,
                {
                    "invoiceId": "invoice-1",
                    "status": "processing",
                    "orderIds": ["order-1", "order-2"],
                    "productId": "trusted-product",
                    "packageId": "trusted-package",
                    "packageValue": "25",
                    "paymentInfo": {
                        "amount": "25",
                        "asset": "USDC",
                        "network": "base",
                    },
                    "treasuryPayment": {
                        "txId": "0xTX",
                        "network": "base",
                        "asset": "USDC",
                        "amount": "25",
                        "amountAtomic": "25000000",
                    },
                },
            )

    def test_bankr_snapshot_keeps_only_bounded_reconciliation_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=make_cipher())
            store.save_quote(
                {
                    "quoteId": "q1",
                    "productId": "trusted-product",
                    "packageId": "trusted-package",
                    "packageValue": "25",
                    "expiresAtEpoch": 999,
                }
            )
            raw_bankr = {
                "ok": True,
                "status": 200,
                "txHash": "0xTOP",
                "startBlock": 47_751_000,
                "paymentMade": {
                    "network": "eip155:8453",
                    "txId": "0xPAYMENT",
                    "payTo": "0x1111111111111111111111111111111111111111",
                    "amountUsd": "0.0057",
                    "amountAtomic": "5700000000000000",
                    "asset": "SINGIT",
                    "credential": "BANKR-PAYMENT-CREDENTIAL-MARKER",
                },
                "command": ["bankr", "BANKR-COMMAND-MARKER"],
                "stdout": "BANKR-STDOUT-REDEMPTION-MARKER",
                "stderr": "BANKR-STDERR-PAYMENT-LINK-MARKER",
                "body": {
                    "fulfillmentToken": "BANKR-TOKEN-MARKER",
                    "redemption": "BANKR-REDEMPTION-MARKER",
                    "paymentLink": "BANKR-LINK-MARKER",
                },
            }

            store.advance_state(
                "q1",
                "SINGIT_SETTLED",
                {"bankr": raw_bankr},
            )

            persisted = raw_metadata(path, "q1")["bankr"]
            self.assertEqual(
                persisted,
                {
                    "ok": True,
                    "status": "200",
                    "transactionHash": "0xTOP",
                    "startBlock": "47751000",
                    "paymentMade": {
                        "network": "eip155:8453",
                        "transactionHash": "0xPAYMENT",
                        "payTo": "0x1111111111111111111111111111111111111111",
                        "amountUsd": "0.0057",
                        "amountAtomic": "5700000000000000",
                        "asset": "SINGIT",
                    },
                },
            )
            for marker in (
                "BANKR-PAYMENT-CREDENTIAL-MARKER",
                "BANKR-COMMAND-MARKER",
                "BANKR-STDOUT-REDEMPTION-MARKER",
                "BANKR-STDERR-PAYMENT-LINK-MARKER",
                "BANKR-TOKEN-MARKER",
                "BANKR-REDEMPTION-MARKER",
                "BANKR-LINK-MARKER",
            ):
                for sidecar in path.parent.glob(f"{path.name}*"):
                    self.assertNotIn(
                        marker,
                        sidecar.read_bytes().decode("utf-8", errors="ignore"),
                    )

    def test_every_row_mutation_rejects_noncanonical_reserved_snapshots(self):
        unsafe_snapshots = {
            "bitrefill": {
                "bitrefill": {
                    "invoiceId": "invoice-1",
                    "unsafe": {"secret": "UNSAFE-BITREFILL-MARKER"},
                }
            },
            "bitrefillCheckpoint": {
                "bitrefillCheckpoint": {
                    "invoiceId": "invoice-1",
                    "unsafe": {"secret": "UNSAFE-CHECKPOINT-MARKER"},
                }
            },
            "bankr": {
                "bankr": {
                    "status": "200",
                    "stdout": "UNSAFE-BANKR-MARKER",
                }
            },
            "nonmapping-bankr": {
                "bankr": "UNSAFE-NONMAPPING-BANKR-MARKER",
            },
        }
        mutations = {
            "advance_state": lambda store: store.advance_state(
                "q1",
                "USER_APPROVED",
                {"paymentHash": "a" * 64},
            ),
            "checkpoint": lambda store: store.checkpoint(
                "q1",
                {"paymentHash": "a" * 64},
            ),
            "try_mark_fulfilling": lambda store: store.try_mark_fulfilling("q1"),
        }

        for snapshot_name, snapshot in unsafe_snapshots.items():
            for mutation_name, mutate in mutations.items():
                with self.subTest(
                    snapshot=snapshot_name,
                    mutation=mutation_name,
                ), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "orders.sqlite3"
                    store = BitrefillCommerceStore(path, cipher=make_cipher())
                    store.save_quote(
                        {
                            "quoteId": "q1",
                            "productId": "p1",
                            "packageId": "pkg1",
                            "packageValue": "25",
                            "expiresAtEpoch": 999,
                        }
                    )
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute(
                            "UPDATE bitrefill_orders "
                            "SET metadata_json = ?, updated_at = 123 "
                            "WHERE quote_id = ?",
                            (json.dumps(snapshot), "q1"),
                        )
                    before = raw_order_row(path, "q1")

                    with self.assertRaises(SensitiveStateError) as captured:
                        mutate(store)

                    self.assertIn(
                        "unsafe legacy",
                        str(captured.exception),
                    )
                    self.assertNotIn("MARKER", str(captured.exception))
                    self.assertEqual(raw_order_row(path, "q1"), before)

    def test_allowlisted_scalar_field_rejects_nested_secret_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=make_cipher())
            store.save_quote(
                {
                    "quoteId": "q1",
                    "productId": "trusted-product",
                    "packageId": "trusted-package",
                    "packageValue": "25",
                    "expiresAtEpoch": 999,
                }
            )
            before = raw_metadata(path, "q1")
            cases = (
                (
                    {"bitrefill": {"status": {"secret": "NESTED-MARKER"}}},
                    "bitrefill.status must be a scalar value",
                ),
                (
                    {"bitrefill": "NESTED-MARKER"},
                    "bitrefill must be an object",
                ),
                (
                    {"bitrefillCheckpoint": "NESTED-MARKER"},
                    "bitrefillCheckpoint must be an object",
                ),
            )
            for update, error in cases:
                with self.subTest(update=update):
                    with self.assertRaisesRegex(ValueError, error):
                        store.checkpoint("q1", update)
                    self.assertEqual(raw_metadata(path, "q1"), before)

    def test_checkpoint_rejects_non_scalar_order_id_after_sixteenth_without_mutation(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=make_cipher())
            store.save_quote(
                {
                    "quoteId": "q1",
                    "productId": "trusted-product",
                    "packageId": "trusted-package",
                    "packageValue": "25",
                    "expiresAtEpoch": 999,
                }
            )
            before = raw_metadata(path, "q1")
            nested_marker = "ORDER-ID-NESTED-SECRET"

            with self.assertRaisesRegex(
                ValueError,
                r"bitrefillCheckpoint\.orderIds\[16\] must be a scalar value",
            ):
                store.checkpoint(
                    "q1",
                    {
                        "bitrefillCheckpoint": {
                            "invoiceId": "invoice-1",
                            "orderIds": [
                                *(f"order-{index}" for index in range(16)),
                                {"secret": nested_marker},
                            ],
                        }
                    },
                )

            self.assertEqual(raw_metadata(path, "q1"), before)
            for sidecar in path.parent.glob(f"{path.name}*"):
                self.assertNotIn(
                    nested_marker,
                    sidecar.read_bytes().decode("utf-8", errors="ignore"),
                )

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
            store = BitrefillCommerceStore(path, cipher=make_cipher())
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
            store = BitrefillCommerceStore(path, cipher=make_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            legacy = {"recipient": {"email": "legacy@example.com"}}
            with closing(sqlite3.connect(path)) as db, db:
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
            store = BitrefillCommerceStore(path, cipher=make_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            seeded = {
                "encryptedRecipient": "not-ciphertext",
                "recipient": {"email": "legacy@example.com"},
            }
            with closing(sqlite3.connect(path)) as db, db:
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

    def test_null_encrypted_recipient_never_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=make_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            seeded = {
                "encryptedRecipient": None,
                "recipient": {"email": "legacy@example.com"},
            }
            with closing(sqlite3.connect(path)) as db, db:
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
                BitrefillCommerceStore(path, cipher=make_cipher())
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
            BitrefillCommerceStore(path, cipher=make_cipher())
            os.chmod(parent, 0o755)
            os.chmod(path, 0o644)

            BitrefillCommerceStore(path, cipher=make_cipher())

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
                BitrefillCommerceStore(path, cipher=make_cipher())

            self.assertFalse(outside.exists())

    def test_try_mark_fulfilling_refuses_legacy_recipient_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=make_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            legacy = {"recipient": {"email": "legacy@example.com"}}
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "UPDATE bitrefill_orders SET metadata_json = ? "
                    "WHERE quote_id = ?",
                    (json.dumps(legacy), "q1"),
                )

            with self.assertRaises(SensitiveStateError):
                store.try_mark_fulfilling("q1")

            self.assertEqual(raw_metadata(path, "q1"), legacy)
            with closing(sqlite3.connect(path)) as db:
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
