from unittest import TestCase

from trezor_sidecar.companion import CompanionWorker
from trezor_sidecar.errors import SafeError
from trezor_sidecar.models import LOCAL_INTENT_TEST_ID


NOW = 1_700_000_000
ADDRESS = "0x1111111111111111111111111111111111111111"
RECIPIENT_HASH = "0x" + "22" * 32


class FakeBroker:
    def __init__(self, job):
        self.job = job
        self.completed = []
        self.failed = []

    def claim(self):
        job, self.job = self.job, None
        return job

    def complete(self, job_id, result):
        self.completed.append((job_id, result))

    def fail(self, job_id, error_code):
        self.failed.append((job_id, error_code))


class FakeSidecar:
    def __init__(self):
        self.intents = []
        self.payments = []

    def approve_intent(self, intent):
        self.intents.append(intent)
        return {"ok": True, "intentId": intent.intent_id, "state": "DEVICE_APPROVED"}

    def pay_invoice(self, *args):
        self.payments.append(args)
        return {
            "ok": True,
            "payment": {
                "paymentId": "payment-1",
                "intentId": args[0],
                "invoiceId": args[1],
                "state": "TX_BROADCAST",
                "createdAt": NOW,
                "updatedAt": NOW + 1,
                "txHash": "0x" + "33" * 32,
            },
        }


def intent_job():
    return {
        "jobId": "job_" + "a" * 24,
        "kind": "purchase_intent",
        "expiresAt": NOW + 60,
        "payload": {
            "intentId": LOCAL_INTENT_TEST_ID,
            "productSlug": "local-intent-test",
            "packageId": "test-only",
            "denomination": "No purchase",
            "quotedTotalUsdMicros": 1,
            "maxPaymentUsdcAtomic": 1_000_000,
            "paymentAsset": "USDC",
            "paymentNetwork": "Base Mainnet",
            "recipientHash": RECIPIENT_HASH,
            "expiresAt": NOW + 60,
        },
    }


class CompanionWorkerTests(TestCase):
    def test_executes_only_typed_purchase_intent_and_completes(self):
        broker = FakeBroker(intent_job())
        sidecar = FakeSidecar()
        worker = CompanionWorker(broker=broker, sidecar=sidecar, clock=lambda: NOW)
        self.assertTrue(worker.run_once())
        self.assertEqual(sidecar.intents[0].intent_id, LOCAL_INTENT_TEST_ID)
        self.assertEqual(broker.completed[0][1]["state"], "DEVICE_APPROVED")
        self.assertEqual(broker.failed, [])

    def test_executes_exact_payment_arguments(self):
        job = {
            "jobId": "job_" + "b" * 24,
            "kind": "usdc_payment",
            "expiresAt": NOW + 60,
            "payload": {
                "intentId": LOCAL_INTENT_TEST_ID,
                "invoiceId": "invoice-1",
                "payTo": ADDRESS,
                "amountAtomic": "100000",
                "expiresAt": NOW + 60,
                "idempotencyKey": "bitrefill-pay:invoice-1",
            },
        }
        broker = FakeBroker(job)
        sidecar = FakeSidecar()
        CompanionWorker(broker=broker, sidecar=sidecar, clock=lambda: NOW).run_once()
        self.assertEqual(
            sidecar.payments,
            [(LOCAL_INTENT_TEST_ID, "invoice-1", ADDRESS, "100000", NOW + 60, "bitrefill-pay:invoice-1")],
        )
        self.assertEqual(broker.completed[0][1]["payment"]["txHash"], "0x" + "33" * 32)

    def test_unknown_job_never_becomes_a_generic_mcp_call(self):
        job = {
            "jobId": "job_" + "c" * 24,
            "kind": "generic_mcp",
            "expiresAt": NOW + 60,
            "payload": {"tool": "trezor_push_transaction"},
        }
        broker = FakeBroker(job)
        sidecar = FakeSidecar()
        CompanionWorker(broker=broker, sidecar=sidecar, clock=lambda: NOW).run_once()
        self.assertEqual(broker.completed, [])
        self.assertEqual(broker.failed, [(job["jobId"], "broker_failed")])
        self.assertEqual(sidecar.intents, [])
        self.assertEqual(sidecar.payments, [])

    def test_safe_local_failure_is_reported_by_code_only(self):
        class RejectingSidecar(FakeSidecar):
            def approve_intent(self, intent):
                raise SafeError("device_rejected", "secret upstream detail", 400)

        broker = FakeBroker(intent_job())
        CompanionWorker(broker=broker, sidecar=RejectingSidecar(), clock=lambda: NOW).run_once()
        self.assertEqual(broker.failed, [("job_" + "a" * 24, "device_rejected")])
