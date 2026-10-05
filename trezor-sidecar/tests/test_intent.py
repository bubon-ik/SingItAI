from dataclasses import FrozenInstanceError, fields
from math import inf, nan
from unittest import TestCase

from eth_account import Account
from eth_account.messages import encode_defunct

from trezor_sidecar.intent import (
    build_intent_message,
    recipient_hash,
    recover_intent_signer,
)
from trezor_sidecar.models import (
    IntentRecord,
    Pairing,
    PaymentRequest,
    PaymentState,
    PaymentView,
    PurchaseIntent,
)


class PurchaseIntentTests(TestCase):
    def make_intent(self, **changes):
        values = {
            "intent_id": "0x" + "11" * 32,
            "product_slug": "amazon-de",
            "package_id": "25",
            "denomination": "25 EUR",
            "quoted_total_usd_micros": 27_000_000,
            "max_payment_usdc_atomic": 27_100_000,
            "recipient_hash": "0x" + "22" * 32,
            "expires_at": 1_800_000_000,
        }
        values.update(changes)
        return PurchaseIntent(**values)

    def test_message_shows_the_purchase_in_readable_lines(self):
        # Break caught: intents drift back to a format a Safe 3 signs blind,
        # so the buyer approves without seeing what they are paying for.
        message = build_intent_message(self.make_intent())
        lines = message.splitlines()

        self.assertEqual(lines[0], "SingIt purchase")
        self.assertEqual(lines[1], "Product: amazon-de")
        self.assertEqual(lines[2], "Item: 25 EUR")
        self.assertEqual(lines[3], "Price: 27.000000 USD")
        self.assertEqual(lines[4], "Max pay: 27.100000 USDC")
        self.assertEqual(lines[5], "Asset: USDC on Base Mainnet")
        # The readable lines come first: the device is paged a few lines at a
        # time, so hashes must not push the amounts off the first screens.
        self.assertTrue(all("0x" not in line for line in lines[:6]))
        self.assertIn("Intent: 0x" + "11" * 32, lines)
        self.assertIn("Expires: 1800000000", lines)

    def test_message_is_plain_ascii_the_device_can_render(self):
        # Break caught: a non-ASCII field turns the screen into mojibake.
        message = build_intent_message(self.make_intent())

        self.assertTrue(message.isascii())
        self.assertNotIn("\t", message)

    def test_recipient_hash_is_order_independent_and_does_not_return_values(self):
        left = recipient_hash({"email": "buyer@example.com", "country": "DE"})
        right = recipient_hash({"country": "DE", "email": "buyer@example.com"})

        self.assertEqual(left, right)
        self.assertRegex(left, r"^0x[0-9a-f]{64}$")
        self.assertNotIn("buyer", left)

    def test_sign_and_recover_returns_the_signing_address(self):
        account = Account.create()
        intent = self.make_intent()
        signature = account.sign_message(
            encode_defunct(text=build_intent_message(intent))
        ).signature.hex()

        self.assertEqual(recover_intent_signer(intent, signature), account.address)

    def test_every_field_change_invalidates_the_signature(self):
        # Break caught: a field the buyer read on screen is not actually bound
        # by the signature, so it can be swapped after approval.
        account = Account.create()
        intent = self.make_intent()
        signature = account.sign_message(
            encode_defunct(text=build_intent_message(intent))
        ).signature.hex()

        changes = (
            {"product_slug": "amazon-us"},
            {"denomination": "50 EUR"},
            {"quoted_total_usd_micros": 27_000_001},
            {"max_payment_usdc_atomic": 27_100_001},
            {"package_id": "50"},
            {"recipient_hash": "0x" + "33" * 32},
            {"intent_id": "0x" + "44" * 32},
            {"expires_at": 1_800_000_001},
        )
        for change in changes:
            with self.subTest(change=change):
                tampered = self.make_intent(**change)
                self.assertNotEqual(
                    recover_intent_signer(tampered, signature),
                    account.address,
                )

    def test_rejects_invalid_bytes32_values(self):
        with self.assertRaisesRegex(ValueError, "intent_id"):
            self.make_intent(intent_id="0x" + "AA" * 32)
        with self.assertRaisesRegex(ValueError, "recipient_hash"):
            self.make_intent(recipient_hash="0x1234")

    def test_rejects_zero_and_boolean_integer_amounts(self):
        with self.assertRaisesRegex(ValueError, "quoted_total_usd_micros"):
            self.make_intent(quoted_total_usd_micros=0)
        with self.assertRaisesRegex(ValueError, "max_payment_usdc_atomic"):
            self.make_intent(max_payment_usdc_atomic=True)

    def test_uint256_amounts_accept_the_limit_and_reject_overflow(self):
        uint256_max = (1 << 256) - 1
        for field in ("quoted_total_usd_micros", "max_payment_usdc_atomic"):
            with self.subTest(field=field, value="maximum"):
                self.make_intent(**{field: uint256_max})
            with self.subTest(field=field, value="overflow"):
                with self.assertRaisesRegex(ValueError, field):
                    self.make_intent(**{field: uint256_max + 1})

    def test_rejects_non_positive_or_boolean_expiration(self):
        with self.assertRaisesRegex(ValueError, "expires_at"):
            self.make_intent(expires_at=0)
        with self.assertRaisesRegex(ValueError, "expires_at"):
            self.make_intent(expires_at=False)

    def test_recipient_hash_rejects_deep_container_nesting(self):
        with self.assertRaisesRegex(ValueError, "nested"):
            recipient_hash({"delivery": {"contacts": {"email": "buyer@example.com"}}})
        with self.assertRaisesRegex(ValueError, "nested"):
            recipient_hash({"delivery": [["buyer@example.com"]]})

    def test_recipient_hash_rejects_non_finite_numbers(self):
        for value in (nan, inf, -inf):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite"):
                recipient_hash({"amount": value})

    def test_recipient_hash_rejects_non_string_keys_with_a_safe_error(self):
        with self.assertRaisesRegex(ValueError, "keys"):
            recipient_hash({"email": "buyer@example.com", 1: "DE"})

    def test_models_are_frozen_and_recipient_values_are_not_model_fields(self):
        intent = self.make_intent()
        with self.assertRaises(FrozenInstanceError):
            intent.product_slug = "other"

        self.assertNotIn("recipient", {field.name for field in fields(PurchaseIntent)})
        self.assertEqual(intent.payment_asset, "USDC")
        self.assertEqual(intent.payment_network, "Base Mainnet")

    def test_addresses_are_normalized_and_amounts_are_strict_across_models(self):
        lower_address = "0x1111111111111111111111111111111111111111"
        pairing = Pairing(
            pairing_id="pair-1",
            address=lower_address,
            derivation_path="m/44'/60'/0'/0/0",
            created_at=1_700_000_000,
            updated_at=1_700_000_000,
        )
        request = PaymentRequest(
            intent_id="0x" + "11" * 32,
            invoice_id="invoice-1",
            pay_to=lower_address,
            amount_atomic=1,
            expires_at=1_800_000_000,
        )

        self.assertEqual(pairing.address, "0x1111111111111111111111111111111111111111")
        self.assertEqual(request.pay_to, "0x1111111111111111111111111111111111111111")
        with self.assertRaisesRegex(ValueError, "amount_atomic"):
            PaymentRequest(
                intent_id="0x" + "11" * 32,
                invoice_id="invoice-1",
                pay_to=lower_address,
                amount_atomic=False,
                expires_at=1_800_000_000,
            )

    def test_zero_address_is_rejected_at_every_model_address_boundary(self):
        zero = "0x" + "00" * 20
        with self.assertRaisesRegex(ValueError, "pay_to"):
            PaymentRequest(
                intent_id="0x" + "11" * 32,
                invoice_id="invoice-1",
                pay_to=zero,
                amount_atomic=1,
                expires_at=1_800_000_000,
            )
        with self.assertRaisesRegex(ValueError, "address"):
            Pairing(
                pairing_id="pair-1",
                address=zero,
                derivation_path="m/44'/60'/0'/0/0",
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )

    def test_all_payment_states_and_records_are_available(self):
        intent = self.make_intent()
        record = IntentRecord(
            intent=intent,
            state=PaymentState.QUOTED,
            created_at=1_700_000_000,
        )
        view = PaymentView(
            payment_id="pay-1",
            intent_id=intent.intent_id,
            invoice_id="invoice-1",
            state=PaymentState.INVOICE_CREATED,
            created_at=1_700_000_000,
            updated_at=1_700_000_001,
        )

        self.assertEqual(record.state, PaymentState.QUOTED)
        self.assertIsNone(record.approved_at)
        self.assertIsNone(record.approved_pairing_id)
        self.assertEqual(view.state, PaymentState.INVOICE_CREATED)
        self.assertIsNone(view.tx_hash)
        self.assertEqual(
            [state.value for state in PaymentState],
            [
                "QUOTED",
                "DEVICE_APPROVED",
                "INVOICE_CREATED",
                "TX_SIGNED",
                "TX_BROADCAST",
                "COMPLETE",
                "CANCELLED",
                "FAILED",
                "RECONCILIATION_REQUIRED",
            ],
        )

    def test_intent_approval_pairing_identity_is_bounded(self):
        # Break caught: an approval cannot be durably tied to one exact pairing identity.
        intent = self.make_intent()
        approved = IntentRecord(
            intent=intent,
            state=PaymentState.DEVICE_APPROVED,
            created_at=1_700_000_000,
            approved_at=1_700_000_001,
            approved_pairing_id="pairing-a",
        )

        self.assertEqual(approved.approved_pairing_id, "pairing-a")
        for invalid in ("", "x" * 257):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError,
                "approved_pairing_id",
            ):
                IntentRecord(
                    intent=intent,
                    state=PaymentState.DEVICE_APPROVED,
                    created_at=1_700_000_000,
                    approved_at=1_700_000_001,
                    approved_pairing_id=invalid,
                )
