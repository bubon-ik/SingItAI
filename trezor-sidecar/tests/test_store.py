import inspect
import os
import sqlite3
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from trezor_sidecar.models import Pairing, PaymentState, PurchaseIntent
from trezor_sidecar.store import SidecarStore


class SidecarStoreTests(TestCase):
    """Integration tests for the isolated, non-secret sidecar state database."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.db"
        self.store = SidecarStore(self.path)
        self.intent = self.make_intent()

    def make_intent(self, **changes):
        values = {
            "intent_id": "0x" + "11" * 32,
            "product_slug": "test-gift",
            "package_id": "1",
            "denomination": "1 USD",
            "quoted_total_usd_micros": 1_000_000,
            "max_payment_usdc_atomic": 1_000_000,
            "recipient_hash": "0x" + "22" * 32,
            "expires_at": 1_800_000_000,
        }
        values.update(changes)
        return PurchaseIntent(**values)

    def approve_intent(self):
        self.store.insert_intent(self.intent, created_at=1_700_000_000)
        return self.store.approve_intent(
            self.intent.intent_id,
            approved_at=1_700_000_001,
            pairing_id="pairing-1",
        )

    def create_payment(self, **changes):
        values = {
            "payment_id": "pay-1",
            "intent_id": self.intent.intent_id,
            "invoice_id": "invoice-1",
            "idempotency_key": "key-1",
            "pay_to": "0x1111111111111111111111111111111111111111",
            "amount_atomic": "1000000",
            "expires_at": 1_800_000_000,
            "created_at": 1_700_000_002,
        }
        values.update(changes)
        return self.store.create_payment(**values)

    def test_pairing_replacement_requires_explicit_repair_and_new_identity(self):
        # Break caught: replacing the device without approval or reusing its old identity.
        original = Pairing(
            pairing_id="base",
            address="0x1111111111111111111111111111111111111111",
            derivation_path="m/44'/60'/0'/0/0",
            created_at=1_700_000_000,
            updated_at=1_700_000_000,
        )
        reused_identity = Pairing(
            pairing_id="base",
            address="0x2222222222222222222222222222222222222222",
            derivation_path="m/44'/60'/0'/0/0",
            created_at=1_700_000_001,
            updated_at=1_700_000_001,
        )
        replacement = Pairing(
            pairing_id="replacement",
            address=reused_identity.address,
            derivation_path=reused_identity.derivation_path,
            created_at=reused_identity.created_at,
            updated_at=reused_identity.updated_at,
        )

        self.assertEqual(self.store.save_pairing(original), original)
        with self.assertRaisesRegex(ValueError, "different Trezor"):
            self.store.save_pairing(replacement)
        with self.assertRaisesRegex(ValueError, "new pairing_id"):
            self.store.save_pairing(reused_identity, allow_repair=True)

        self.assertEqual(self.store.save_pairing(replacement, allow_repair=True), replacement)
        self.assertEqual(self.store.get_pairing(), replacement)

    def test_pairing_timestamps_cannot_move_backwards(self):
        # Break caught: a pairing repair makes its audit clock older than trusted state.
        original = Pairing(
            pairing_id="base",
            address="0x1111111111111111111111111111111111111111",
            derivation_path="m/44'/60'/0'/0/0",
            created_at=10,
            updated_at=20,
        )
        backwards = Pairing(
            pairing_id="backwards",
            address="0x2222222222222222222222222222222222222222",
            derivation_path="m/44'/60'/0'/0/0",
            created_at=10,
            updated_at=19,
        )
        invalid_order = Pairing(
            pairing_id="other",
            address="0x3333333333333333333333333333333333333333",
            derivation_path="m/44'/60'/0'/0/0",
            created_at=21,
            updated_at=20,
        )

        self.store.save_pairing(original)
        with self.assertRaisesRegex(ValueError, "updated_at"):
            self.store.save_pairing(invalid_order, allow_repair=True)
        with self.assertRaisesRegex(ValueError, "updated_at"):
            self.store.save_pairing(backwards, allow_repair=True)

    def test_rejects_symlinked_parent_without_touching_target(self):
        # Break caught: a state path symlink redirects chmod or SQLite writes outside its boundary.
        root = Path(self.temporary.name)
        target = root / "unrelated"
        target.mkdir()
        sentinel = target / "sentinel.txt"
        sentinel.write_text("keep")
        os.chmod(target, 0o755)
        linked_parent = root / "linked-state"
        linked_parent.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            SidecarStore(linked_parent / "state.db")

        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse((target / "state.db").exists())

    def test_rejects_symlinked_database_without_touching_target(self):
        # Break caught: a database symlink lets initialization alter another file.
        root = Path(self.temporary.name)
        target = root / "unrelated.db"
        with sqlite3.connect(target) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES ('keep')")
        os.chmod(target, 0o640)
        linked_database = root / "linked.db"
        linked_database.symlink_to(target)

        with self.assertRaisesRegex(ValueError, "symlink"):
            SidecarStore(linked_database)

        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        with sqlite3.connect(target) as connection:
            self.assertEqual(connection.execute("SELECT value FROM sentinel").fetchone()[0], "keep")

    def test_sqlite_timestamp_range_is_signed_64_bit(self):
        # Break caught: values accepted by models overflow SQLite INTEGER bindings.
        maximum = (1 << 63) - 1
        maximum_intent = self.make_intent(
            intent_id="0x" + "33" * 32,
            expires_at=maximum,
        )
        inserted = self.store.insert_intent(maximum_intent, created_at=maximum)
        approved = self.store.approve_intent(
            maximum_intent.intent_id,
            approved_at=maximum,
            pairing_id="pairing-maximum",
        )
        payment = self.store.create_payment(
            payment_id="pay-max",
            intent_id=maximum_intent.intent_id,
            invoice_id="invoice-max",
            idempotency_key="key-max",
            pay_to="0x1111111111111111111111111111111111111111",
            amount_atomic="1",
            expires_at=maximum,
            created_at=maximum,
        )
        pairing = Pairing(
            pairing_id="base",
            address="0x1111111111111111111111111111111111111111",
            derivation_path="m/44'/60'/0'/0/0",
            created_at=maximum,
            updated_at=maximum,
        )

        self.assertEqual(inserted.created_at, maximum)
        self.assertEqual(approved.approved_at, maximum)
        self.assertEqual(approved.approved_pairing_id, "pairing-maximum")
        self.assertEqual(payment.updated_at, maximum)
        self.assertEqual(self.store.save_pairing(pairing), pairing)
        with self.assertRaisesRegex(ValueError, "expires_at"):
            self.store.insert_intent(
                self.make_intent(intent_id="0x" + "44" * 32, expires_at=maximum + 1),
                created_at=1,
            )
        with self.assertRaisesRegex(ValueError, "created_at"):
            self.store.insert_intent(
                self.make_intent(intent_id="0x" + "55" * 32),
                created_at=maximum + 1,
            )
        with self.assertRaisesRegex(ValueError, "updated_at"):
            self.store.save_pairing(
                Pairing(
                    pairing_id="other",
                    address="0x2222222222222222222222222222222222222222",
                    derivation_path="m/44'/60'/0'/0/0",
                    created_at=maximum,
                    updated_at=maximum + 1,
                ),
                allow_repair=True,
            )

    def test_store_connections_enable_wal_and_foreign_keys_after_reopen(self):
        # Break caught: a new store connection loses durability or referential checks.
        for store in (self.store, SidecarStore(self.path)):
            connection = store._connect()
            try:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """INSERT INTO payments
                        (payment_id, intent_id, invoice_id, idempotency_key, pay_to, amount_atomic,
                         expires_at, state, created_at, updated_at, tx_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                        (
                            "invalid-reference",
                            "0x" + "ff" * 32,
                            "invalid-invoice",
                            "invalid-key",
                            "0x1111111111111111111111111111111111111111",
                            "1",
                            1,
                            PaymentState.INVOICE_CREATED.value,
                            1,
                            1,
                        ),
                    )
                connection.rollback()
            finally:
                connection.close()

    def test_intent_approval_persists_every_intent_field_after_reopen(self):
        # Break caught: an approved intent loses its commitment or immutable purchase terms.
        inserted = self.store.insert_intent(self.intent, created_at=1_700_000_000)
        approved = self.store.approve_intent(
            self.intent.intent_id,
            approved_at=1_700_000_001,
            pairing_id="pairing-1",
        )
        reopened = SidecarStore(self.path).get_intent(self.intent.intent_id)

        self.assertEqual(inserted.intent, self.intent)
        self.assertEqual(inserted.state, PaymentState.QUOTED)
        self.assertEqual(approved.intent, self.intent)
        self.assertEqual(approved.state, PaymentState.DEVICE_APPROVED)
        self.assertEqual(approved.approved_at, 1_700_000_001)
        self.assertEqual(approved.approved_pairing_id, "pairing-1")
        self.assertEqual(reopened, approved)
        with self.assertRaisesRegex(ValueError, "different pairing"):
            self.store.approve_intent(
                self.intent.intent_id,
                approved_at=1_700_000_002,
                pairing_id="pairing-2",
            )

    def test_intent_id_replay_rejects_changed_purchase_terms(self):
        # Break caught: a reused intent identifier is silently rebound to different terms.
        self.store.insert_intent(self.intent, created_at=1_700_000_000)

        with self.assertRaisesRegex(ValueError, "intent conflicts"):
            self.store.insert_intent(
                self.make_intent(product_slug="different-gift"),
                created_at=1_700_000_001,
            )

    def test_invoice_and_idempotency_replay_returns_original_only_for_same_binding(self):
        # Break caught: either unique identity aliases an unrelated payment.
        self.approve_intent()
        original = self.create_payment()

        repeated = self.create_payment(payment_id="pay-2")
        self.assertEqual(repeated, original)

        second_intent = self.make_intent(intent_id="0x" + "33" * 32)
        self.store.insert_intent(second_intent, created_at=1_700_000_002)
        self.store.approve_intent(
            second_intent.intent_id,
            approved_at=1_700_000_003,
            pairing_id="pairing-2",
        )
        second = self.create_payment(
            payment_id="pay-3",
            intent_id=second_intent.intent_id,
            invoice_id="invoice-2",
            idempotency_key="key-2",
        )
        with self.assertRaisesRegex(ValueError, "payment identity conflict"):
            self.create_payment(payment_id="pay-4", idempotency_key="key-2")
        with self.assertRaisesRegex(ValueError, "payment identity conflict"):
            self.create_payment(payment_id="pay-5", invoice_id="invoice-3")
        with self.assertRaisesRegex(ValueError, "idempotency replay conflicts"):
            self.create_payment(payment_id="pay-6", amount_atomic="999999")

    def test_payment_transition_uses_expected_state_and_legal_edges(self):
        # Break caught: stale or illegal state changes overwrite a payment job.
        self.approve_intent()
        self.create_payment()

        signed = self.store.transition_payment(
            payment_id="pay-1",
            expected=PaymentState.INVOICE_CREATED,
            target=PaymentState.TX_SIGNED,
            updated_at=1_700_000_003,
        )
        self.assertEqual(signed.state, PaymentState.TX_SIGNED)
        with self.assertRaisesRegex(ValueError, "illegal payment transition"):
            self.store.transition_payment(
                payment_id="pay-1",
                expected=PaymentState.TX_SIGNED,
                target=PaymentState.COMPLETE,
                updated_at=1_700_000_004,
            )
        with self.assertRaisesRegex(ValueError, "payment state changed"):
            self.store.transition_payment(
                payment_id="pay-1",
                expected=PaymentState.INVOICE_CREATED,
                target=PaymentState.TX_BROADCAST,
                updated_at=1_700_000_004,
            )

        broadcast = self.store.transition_payment(
            payment_id="pay-1",
            expected=PaymentState.TX_SIGNED,
            target=PaymentState.TX_BROADCAST,
            tx_hash="0x" + "ab" * 32,
            updated_at=1_700_000_004,
        )
        complete = self.store.transition_payment(
            payment_id="pay-1",
            expected=PaymentState.TX_BROADCAST,
            target=PaymentState.COMPLETE,
            updated_at=1_700_000_005,
        )
        self.assertEqual(broadcast.tx_hash, "0x" + "ab" * 32)
        self.assertEqual(complete.tx_hash, "0x" + "ab" * 32)
        self.assertEqual(SidecarStore(self.path).get_payment("pay-1"), complete)

    def test_purchase_log_has_exact_scalar_interface_and_is_idempotent(self):
        # Break caught: secret metadata can be attached to the purchase record.
        parameters = list(inspect.signature(SidecarStore.record_purchase).parameters)
        self.assertEqual(
            parameters,
            ["self", "invoice_id", "product_slug", "amount", "payment_method", "timestamp"],
        )
        self.approve_intent()
        self.create_payment()

        self.store.record_purchase("invoice-1", "test-gift", "1000000", "usdc_base", 1_700_000_010)
        self.store.record_purchase("invoice-1", "test-gift", "1000000", "usdc_base", 1_700_000_010)
        with self.assertRaisesRegex(ValueError, "purchase record conflicts"):
            self.store.record_purchase("invoice-1", "test-gift", "999999", "usdc_base", 1_700_000_010)
        with self.assertRaises(ValueError):
            self.store.record_purchase("invoice-1", "test-gift", {}, "usdc_base", 1_700_000_010)

    def test_schema_excludes_secret_columns_and_database_permissions_are_restricted(self):
        # Break caught: secret material gains a durable schema location or file protection weakens.
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        blocked = ("signature", "recipient", "raw", "calldata", "token", "redemption")
        with sqlite3.connect(self.path) as connection:
            for table in ("pairings", "intents", "payments", "purchase_log"):
                columns = [row[1].lower() for row in connection.execute(f"PRAGMA table_info({table})")]
                self.assertTrue(columns)
                for forbidden in blocked:
                    self.assertFalse(any(forbidden in column for column in columns))

    def test_missing_approval_pairing_column_fails_closed_with_reset_guidance(self):
        # Break caught: an old local schema silently treats unbound approvals as current.
        legacy_path = Path(self.temporary.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE intents (
                intent_id TEXT PRIMARY KEY,
                product_slug TEXT NOT NULL,
                package_id TEXT NOT NULL,
                denomination TEXT NOT NULL,
                quoted_total_usd_micros TEXT NOT NULL,
                max_payment_usdc_atomic TEXT NOT NULL,
                commitment TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                approved_at INTEGER
                )"""
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(legacy_path, 0o600)

        with self.assertRaisesRegex(ValueError, "reset local sidecar state"):
            SidecarStore(legacy_path)
