import inspect
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
        return self.store.approve_intent(self.intent.intent_id, approved_at=1_700_000_001)

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

    def test_pairing_replacement_requires_explicit_repair(self):
        # Break caught: replacing the connected device without operator approval.
        original = Pairing(
            pairing_id="base",
            address="0x1111111111111111111111111111111111111111",
            derivation_path="m/44'/60'/0'/0/0",
            created_at=1_700_000_000,
            updated_at=1_700_000_000,
        )
        replacement = Pairing(
            pairing_id="base",
            address="0x2222222222222222222222222222222222222222",
            derivation_path="m/44'/60'/0'/0/0",
            created_at=1_700_000_001,
            updated_at=1_700_000_001,
        )

        self.assertEqual(self.store.save_pairing(original), original)
        with self.assertRaisesRegex(ValueError, "different Trezor"):
            self.store.save_pairing(replacement)

        self.assertEqual(self.store.save_pairing(replacement, allow_repair=True), replacement)
        self.assertEqual(self.store.get_pairing(), replacement)

    def test_intent_approval_persists_every_intent_field_after_reopen(self):
        # Break caught: an approved intent loses its commitment or immutable purchase terms.
        inserted = self.store.insert_intent(self.intent, created_at=1_700_000_000)
        approved = self.store.approve_intent(self.intent.intent_id, approved_at=1_700_000_001)
        reopened = SidecarStore(self.path).get_intent(self.intent.intent_id)

        self.assertEqual(inserted.intent, self.intent)
        self.assertEqual(inserted.state, PaymentState.QUOTED)
        self.assertEqual(approved.intent, self.intent)
        self.assertEqual(approved.state, PaymentState.DEVICE_APPROVED)
        self.assertEqual(approved.approved_at, 1_700_000_001)
        self.assertEqual(reopened, approved)

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
        self.store.approve_intent(second_intent.intent_id, approved_at=1_700_000_003)
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
