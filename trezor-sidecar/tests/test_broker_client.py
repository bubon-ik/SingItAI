from unittest import TestCase

from trezor_sidecar.broker_client import RemoteSidecarClient
from trezor_sidecar.models import LOCAL_INTENT_TEST_ID, PurchaseIntent


NOW = 1_700_000_000


class FakeJsonClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def request(self, method, path, *, payload=None, expected=(200,)):
        self.calls.append((method, path, payload, expected))
        if path == "/v1/internal/jobs":
            return {
                "ok": True,
                "job": {
                    "jobId": "job_" + "a" * 24,
                    "state": "QUEUED",
                },
            }
        return {
            "ok": True,
            "job": {
                "jobId": "job_" + "a" * 24,
                "state": "SUCCEEDED",
                "result": self.result,
            },
        }


class RemoteSidecarClientTests(TestCase):
    def client(self, result):
        client = RemoteSidecarClient(
            base_url="http://127.0.0.1:8122",
            internal_token="x" * 32,
            user_id="12345",
            clock=lambda: NOW,
            sleeper=lambda _seconds: None,
        )
        client._client = FakeJsonClient(result)
        return client

    def intent(self):
        return PurchaseIntent(
            intent_id=LOCAL_INTENT_TEST_ID,
            product_slug="test",
            package_id="1",
            denomination="$0.10",
            quoted_total_usd_micros=100_000,
            max_payment_usdc_atomic=1_000_000,
            recipient_hash="0x" + "22" * 32,
            expires_at=NOW + 60,
        )

    def test_approval_job_preserves_exact_typed_intent(self):
        intent = self.intent()
        client = self.client(
            {"ok": True, "intentId": intent.intent_id, "state": "DEVICE_APPROVED"}
        )
        result = client.approve_intent(intent)
        self.assertEqual(result["state"], "DEVICE_APPROVED")
        payload = client._client.calls[0][2]
        self.assertEqual(payload["kind"], "purchase_intent")
        self.assertEqual(payload["payload"]["maxPaymentUsdcAtomic"], 1_000_000)
        self.assertEqual(payload["payload"]["paymentNetwork"], "Base Mainnet")

    def test_long_lived_invoice_still_produces_an_acceptable_job(self):
        # Break caught: the invoice expiry was used as the job expiry, and the
        # broker rejects any job lasting over 900s, so every payment failed
        # with "request failed safely" and no payment job was ever created.
        payment = {
            "ok": True,
            "payment": {
                "paymentId": "payment-1",
                "intentId": LOCAL_INTENT_TEST_ID,
                "invoiceId": "invoice-1",
                "state": "TX_BROADCAST",
                "createdAt": NOW,
                "updatedAt": NOW + 1,
                "txHash": "0x" + "33" * 32,
            },
        }
        client = self.client(payment)
        invoice_expiry = NOW + 3600

        client.pay_invoice(
            LOCAL_INTENT_TEST_ID,
            "invoice-1",
            "0x1111111111111111111111111111111111111111",
            "100000",
            invoice_expiry,
            "bitrefill-pay:invoice-1",
        )

        submitted = client._client.calls[0][2]
        self.assertLessEqual(submitted["expiresAt"] - NOW, 900)
        # The sidecar still checks the payment against the real invoice window.
        self.assertEqual(submitted["payload"]["expiresAt"], invoice_expiry)

    def test_short_invoice_window_is_never_extended(self):
        # Break caught: clamping turns into padding and a job outlives the
        # invoice it is paying.
        payment = {
            "ok": True,
            "payment": {
                "paymentId": "payment-1",
                "intentId": LOCAL_INTENT_TEST_ID,
                "invoiceId": "invoice-1",
                "state": "TX_BROADCAST",
                "createdAt": NOW,
                "updatedAt": NOW + 1,
                "txHash": "0x" + "33" * 32,
            },
        }
        client = self.client(payment)

        client.pay_invoice(
            LOCAL_INTENT_TEST_ID,
            "invoice-1",
            "0x1111111111111111111111111111111111111111",
            "100000",
            NOW + 120,
            "bitrefill-pay:invoice-1",
        )

        self.assertEqual(client._client.calls[0][2]["expiresAt"], NOW + 120)

    def test_payment_job_returns_only_validated_sidecar_receipt(self):
        payment = {
            "ok": True,
            "payment": {
                "paymentId": "payment-1",
                "intentId": LOCAL_INTENT_TEST_ID,
                "invoiceId": "invoice-1",
                "state": "TX_BROADCAST",
                "createdAt": NOW,
                "updatedAt": NOW + 1,
                "txHash": "0x" + "33" * 32,
            },
        }
        client = self.client(payment)
        result = client.pay_invoice(
            LOCAL_INTENT_TEST_ID,
            "invoice-1",
            "0x1111111111111111111111111111111111111111",
            "100000",
            NOW + 60,
            "bitrefill-pay:invoice-1",
        )
        self.assertEqual(result["payment"]["txHash"], "0x" + "33" * 32)
        submitted = client._client.calls[0][2]
        self.assertEqual(submitted["kind"], "usdc_payment")
        self.assertEqual(submitted["payload"]["amountAtomic"], "100000")
