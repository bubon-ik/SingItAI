import time
from unittest import TestCase
from unittest.mock import patch

from trezor_sidecar.errors import SafeError


class FakeSidecar:
    def __init__(self):
        self.pair_calls = 0

    def pair(self):
        self.pair_calls += 1
        return {
            "ok": True,
            "pairing": {
                "address": "0xB80b5Ca13583fB7E0236db4bD8834B9035654558"
            },
        }


class FakeDetailsClient:
    def __init__(self, fields=None):
        self.fields = list(fields or [])
        self.calls = []

    def get_product_details(self, *, product_id, country):
        self.calls.append((product_id, country))
        return {"requiredRecipientFields": list(self.fields)}


class FakeRunner:
    def __init__(self):
        self.sidecar = FakeSidecar()
        self.quote_calls = []
        self.intent_calls = []
        self.buy_calls = []

    def quote(self, *, product_id, package_id, country, recipient):
        self.quote_calls.append((product_id, package_id, country, dict(recipient)))
        return {
            "productId": product_id,
            "name": "Test Gift",
            "productType": "gift_card",
            "packageId": package_id,
            "packageValue": "1 USD",
            "country": country,
            "currency": "USD",
            "priceUsd": "1.00",
            "recipientType": "email" if recipient else "none",
            "requiredRecipientFields": list(recipient),
        }

    def build_intent(self, quote, recipient, now, *, buyer_email=""):
        from trezor_sidecar.models import PurchaseIntent

        self.intent_calls.append((dict(quote), dict(recipient), now, buyer_email))
        return PurchaseIntent(
            intent_id="0x" + "11" * 32,
            product_slug=quote["productId"],
            package_id=quote["packageId"],
            denomination=quote["packageValue"],
            quoted_total_usd_micros=1_000_000,
            max_payment_usdc_atomic=1_000_000,
            recipient_hash="0x" + "22" * 32,
            expires_at=now + 600,
        )

    def buy(self, *, quote, recipient, buyer_email="", now=None):
        self.buy_calls.append((dict(quote), dict(recipient), buyer_email, now))
        return {
            "ok": True,
            "invoiceId": "invoice-1",
            "status": "complete",
            "treasuryPayment": {"txId": "0x" + "ab" * 32},
            "redemption": {"value": {"code": "REDEMPTION-CANARY"}},
        }


class LocalAgentSettingsTests(TestCase):
    def test_disabled_mode_needs_no_credentials_and_redacts_enabled_credentials(self):
        from trezor_sidecar.local_agent import LocalAgentSettings

        disabled = LocalAgentSettings.from_env({})
        self.assertFalse(disabled.enabled)

        enabled = LocalAgentSettings.from_env(
            {
                "SIGN402_TREZOR_LOCAL_AGENT_ENABLED": "1",
                "SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED": "1",
                "SIGN402_TREZOR_POC_ENABLED": "1",
                "SIGN402_TREZOR_LOCAL_AGENT_USER_ID": "12345",
                "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-secret-canary",
                "SIGN402_TREZOR_POC_MAX_USD": "1.00",
                "BITREFILL_API_KEY": "bitrefill-secret-canary",
                "SIGN402_TREZOR_LOCAL_BUYER_EMAIL": "buyer@example.com",
            }
        )
        self.assertTrue(enabled.enabled)
        self.assertNotIn("sidecar-secret-canary", repr(enabled))
        self.assertNotIn("bitrefill-secret-canary", repr(enabled))
        self.assertNotIn("buyer@example.com", repr(enabled))

    def test_enabled_mode_requires_second_poc_gate_and_one_numeric_user(self):
        from trezor_sidecar.local_agent import LocalAgentSettings

        base = {
            "SIGN402_TREZOR_LOCAL_AGENT_ENABLED": "1",
            "SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED": "1",
            "SIGN402_TREZOR_LOCAL_AGENT_USER_ID": "12345",
            "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-token",
            "SIGN402_TREZOR_POC_MAX_USD": "1.00",
            "BITREFILL_API_KEY": "bitrefill-key",
        }
        with self.assertRaisesRegex(ValueError, "POC_ENABLED"):
            LocalAgentSettings.from_env(base)
        with self.assertRaisesRegex(ValueError, "USER_ID"):
            LocalAgentSettings.from_env(
                {**base, "SIGN402_TREZOR_POC_ENABLED": "1", "SIGN402_TREZOR_LOCAL_AGENT_USER_ID": "*"}
            )

    def test_pair_only_mode_does_not_require_or_accept_a_bitrefill_runtime(self):
        from trezor_sidecar.local_agent import LocalAgentSettings, build_local_agent_controller

        settings = LocalAgentSettings.from_env(
            {
                "SIGN402_TREZOR_LOCAL_AGENT_ENABLED": "1",
                "SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED": "0",
                "SIGN402_TREZOR_POC_ENABLED": "1",
                "SIGN402_TREZOR_LOCAL_AGENT_USER_ID": "12345",
                "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-token",
                "SIGN402_TREZOR_POC_MAX_USD": "1.00",
            }
        )
        self.assertTrue(settings.enabled)
        self.assertFalse(settings.purchases_enabled)
        self.assertEqual(settings.bitrefill_api_key, "")

        sidecar = FakeSidecar()
        with (
            patch("trezor_sidecar.local_agent.SidecarClient", return_value=sidecar),
            patch(
                "trezor_sidecar.local_agent.PreparedAddressBitrefillClient",
                side_effect=AssertionError("Bitrefill runtime must remain disabled"),
            ),
        ):
            controller = build_local_agent_controller(
                {
                    "SIGN402_TREZOR_LOCAL_AGENT_ENABLED": "1",
                    "SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED": "0",
                    "SIGN402_TREZOR_POC_ENABLED": "1",
                    "SIGN402_TREZOR_LOCAL_AGENT_USER_ID": "12345",
                    "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-token",
                    "SIGN402_TREZOR_POC_MAX_USD": "1.00",
                }
            )
        self.assertIn("0xB80b5Ca", controller.pair("12345"))
        with self.assertRaisesRegex(SafeError, "purchases are disabled"):
            controller.prepare("12345", "test-gift", "1", "US")


class LocalAgentControllerTests(TestCase):
    def make_controller(self, *, fields=None, buyer_email="buyer@example.com", now=1_700_000_000):
        from trezor_sidecar.local_agent import LocalAgentController, LocalAgentSettings

        settings = LocalAgentSettings.from_env(
            {
                "SIGN402_TREZOR_LOCAL_AGENT_ENABLED": "1",
                "SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED": "1",
                "SIGN402_TREZOR_POC_ENABLED": "1",
                "SIGN402_TREZOR_LOCAL_AGENT_USER_ID": "12345",
                "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-token",
                "SIGN402_TREZOR_POC_MAX_USD": "1.00",
                "BITREFILL_API_KEY": "bitrefill-key",
                "SIGN402_TREZOR_LOCAL_BUYER_EMAIL": buyer_email,
            }
        )
        runner = FakeRunner()
        details = FakeDetailsClient(fields)
        controller = LocalAgentController(
            settings=settings,
            runner=runner,
            details_client=details,
            clock=lambda: now,
            code_factory=lambda: "A1B2C3D4",
        )
        return controller, runner, details

    def test_prepare_quotes_but_never_buys_and_returns_exact_confirmation(self):
        controller, runner, details = self.make_controller(fields=["email"])

        summary = controller.prepare("12345", "test-gift", "1", "US")

        self.assertEqual(details.calls, [("test-gift", "US")])
        self.assertEqual(runner.quote_calls, [("test-gift", "1", "US", {"email": "buyer@example.com"})])
        self.assertEqual(runner.buy_calls, [])
        self.assertIn("Product: Test Gift (test-gift)", summary)
        self.assertIn("Quoted total: $1.00", summary)
        self.assertIn("Payment method: USDC on Base Mainnet", summary)
        self.assertIn("Recipient email: buyer@example.com", summary)
        self.assertIn("/trezor_confirm A1B2C3D4", summary)

    def test_confirmation_code_is_required_and_purchase_is_single_use(self):
        controller, runner, _details = self.make_controller()
        controller.prepare("12345", "test-gift", "1", "US")

        with self.assertRaisesRegex(SafeError, "confirmation"):
            controller.confirm("12345", "00000000")
        self.assertEqual(runner.buy_calls, [])

        receipt = controller.confirm("12345", "A1B2C3D4")
        self.assertIn("invoice-1", receipt)
        self.assertIn("REDEMPTION-CANARY", receipt)
        self.assertEqual(len(runner.buy_calls), 1)

    def test_confirm_blocks_new_prepare_before_runner_can_reach_invoice_creation(self):
        controller, runner, details = self.make_controller()
        controller.prepare("12345", "test-gift", "1", "US")
        nested_errors = []
        original_buy = runner.buy

        def guarded_buy(**kwargs):
            try:
                controller.prepare("12345", "other-gift", "2", "US")
            except SafeError as error:
                nested_errors.append(error)
            return original_buy(**kwargs)

        runner.buy = guarded_buy
        controller.confirm("12345", "A1B2C3D4")

        self.assertEqual(len(nested_errors), 1)
        self.assertIn("already", nested_errors[0].message)
        self.assertEqual(details.calls, [("test-gift", "US")])
        self.assertEqual(runner.buy_calls[0][3], 1_700_000_000)
        with self.assertRaisesRegex(SafeError, "pending"):
            controller.confirm("12345", "A1B2C3D4")
        self.assertEqual(len(runner.buy_calls), 1)

    def test_unauthorized_user_and_unsupported_recipient_fail_before_quote(self):
        controller, runner, details = self.make_controller(fields=["phoneNumber"])
        with self.assertRaisesRegex(SafeError, "authorized"):
            controller.prepare("99999", "test-gift", "1", "US")
        self.assertEqual(details.calls, [])
        with self.assertRaisesRegex(SafeError, "recipient"):
            controller.prepare("12345", "test-gift", "1", "US")
        self.assertEqual(runner.quote_calls, [])

    def test_pair_is_device_gated_and_cancel_removes_only_owners_pending_quote(self):
        controller, runner, _details = self.make_controller()
        paired = controller.pair("12345")
        self.assertIn("0xB80b5Ca", paired)
        self.assertEqual(runner.sidecar.pair_calls, 1)

        controller.prepare("12345", "test-gift", "1", "US")
        with self.assertRaisesRegex(SafeError, "authorized"):
            controller.cancel("99999")
        self.assertIn("cancelled", controller.cancel("12345").lower())
        with self.assertRaisesRegex(SafeError, "pending"):
            controller.confirm("12345", "A1B2C3D4")

    def test_expired_summary_cannot_reach_buy(self):
        current = [1_700_000_000]
        from trezor_sidecar.local_agent import LocalAgentController, LocalAgentSettings

        settings = LocalAgentSettings.from_env(
            {
                "SIGN402_TREZOR_LOCAL_AGENT_ENABLED": "1",
                "SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED": "1",
                "SIGN402_TREZOR_POC_ENABLED": "1",
                "SIGN402_TREZOR_LOCAL_AGENT_USER_ID": "12345",
                "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-token",
                "SIGN402_TREZOR_POC_MAX_USD": "1.00",
                "BITREFILL_API_KEY": "bitrefill-key",
            }
        )
        runner = FakeRunner()
        controller = LocalAgentController(
            settings=settings,
            runner=runner,
            details_client=FakeDetailsClient(),
            clock=lambda: current[0],
            code_factory=lambda: "A1B2C3D4",
        )
        controller.prepare("12345", "test-gift", "1", "US")
        current[0] += 601
        with self.assertRaisesRegex(SafeError, "expired"):
            controller.confirm("12345", "A1B2C3D4")
        self.assertEqual(runner.buy_calls, [])
