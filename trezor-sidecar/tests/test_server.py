import http.client
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from trezor_sidecar.config import SidecarSettings
from trezor_sidecar.errors import SafeError
from trezor_sidecar.models import (
    Pairing,
    PaymentRequest,
    PaymentState,
    PaymentView,
    PurchaseIntent,
)
from trezor_sidecar.server import build_server
from trezor_sidecar.server import _SidecarHandler


NOW = 1_700_000_000
INTENT_ID = "0x" + "11" * 32
RECIPIENT_HASH = "0x" + "22" * 32
PAY_TO = "0x1111111111111111111111111111111111111111"


class FakeService:
    def __init__(self):
        self.settings = None
        self.pair_calls = []
        self.approve_calls = []
        self.create_calls = []
        self.run_calls = []
        self.run_clock_values = []
        self.get_calls = []
        self.failure = None
        self.run_entered = threading.Event()
        self.run_exited = threading.Event()
        self.release_run = threading.Event()
        self.release_run.set()
        self.run_failure = None
        self._lock = threading.Lock()
        self.payment = PaymentView(
            payment_id="payment-123",
            intent_id=INTENT_ID,
            invoice_id="invoice-123",
            state=PaymentState.INVOICE_CREATED,
            created_at=NOW,
            updated_at=NOW,
        )

    def pair(self, allow_repair=False):
        self.pair_calls.append(allow_repair)
        if self.failure is not None:
            raise self.failure
        return Pairing(
            pairing_id="pairing-123",
            address="0x2222222222222222222222222222222222222222",
            derivation_path="m/44'/60'/0'/0/0",
            created_at=NOW,
            updated_at=NOW,
        )

    def approve_intent(self, intent, now):
        self.approve_calls.append((intent, now))
        if self.failure is not None:
            raise self.failure
        return intent

    def create_payment(self, request, idempotency_key, now):
        with self._lock:
            self.create_calls.append((request, idempotency_key, now))
        if self.failure is not None:
            raise self.failure
        return self.payment

    def run_payment(self, payment_id, now):
        with self._lock:
            self.run_calls.append(payment_id)
            self.run_clock_values.append(now())
        self.run_entered.set()
        self.release_run.wait(timeout=2)
        self.run_exited.set()
        if self.run_failure is not None:
            raise self.run_failure
        return self.payment

    def get_payment(self, payment_id):
        self.get_calls.append(payment_id)
        if self.failure is not None:
            raise self.failure
        return self.payment


class SidecarHttpTests(TestCase):
    def setUp(self):
        self.settings = SidecarSettings(
            enabled=True,
            mcp_token="mcp-secret-not-for-http",
            api_token="local-secret",
            max_usd=Decimal("10"),
            base_rpc_url="https://rpc.example.invalid",
            state_path=Path("unused.db"),
            host="127.0.0.1",
            port=0,
        )
        self.service = FakeService()
        self.server = build_server(
            self.settings,
            self.service,
            clock=lambda: NOW,
            _allow_test_port=True,
            _test_only_connection_timeout=0.15,
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.service.release_run.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    @property
    def address(self):
        return self.server.server_address

    def valid_headers(self, key="request-key"):
        return {
            "Authorization": "Bearer local-secret",
            "Content-Type": "application/json",
            "X-Sign402-Timestamp": str(NOW),
            "Idempotency-Key": key,
        }

    def request(self, method, path, *, headers=None, body=None):
        connection = http.client.HTTPConnection(*self.address, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), json.loads(payload)

    def raw_request(self, request):
        with socket.create_connection(self.address, timeout=2) as client:
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                response += chunk
        head, body = response.split(b"\r\n\r\n", 1)
        status = int(head.split(b" ", 2)[1])
        return status, json.loads(body)

    @staticmethod
    def intent_body(**changes):
        body = {
            "intentId": INTENT_ID,
            "productSlug": "example-product",
            "packageId": "package-1",
            "denomination": "5 USD",
            "quotedTotalUsdMicros": 5_000_000,
            "maxPaymentUsdcAtomic": 5_100_000,
            "paymentAsset": "USDC",
            "paymentNetwork": "Base Mainnet",
            "recipientHash": RECIPIENT_HASH,
            "expiresAt": NOW + 600,
        }
        body.update(changes)
        return json.dumps(body).encode()

    @staticmethod
    def payment_body(**changes):
        body = {
            "intentId": INTENT_ID,
            "invoiceId": "invoice-123",
            "payTo": PAY_TO,
            "amountAtomic": 5_000_000,
            "expiresAt": NOW + 600,
        }
        body.update(changes)
        return json.dumps(body).encode()

    def test_build_server_refuses_every_non_fixed_loopback_host(self):
        # Break caught: configuration can expose the authenticated API off-machine.
        for host in ("0.0.0.0", "localhost", "::1", "127.0.0.2"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                build_server(
                    replace(self.settings, host=host),
                    self.service,
                    _allow_test_port=True,
                )

    def test_handler_rejects_a_nonloopback_peer_before_routing(self):
        # Break caught: a proxy or future bind change bypasses the peer boundary.
        client, accepted = socket.socketpair()
        try:
            client.sendall(b"GET /health HTTP/1.1\r\nHost: local\r\n\r\n")
            _SidecarHandler(accepted, ("192.0.2.10", 12345), self.server)
            accepted.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                response += chunk
            head, body = response.split(b"\r\n\r\n", 1)
            self.assertEqual(int(head.split(b" ", 2)[1]), 403)
            self.assertEqual(
                json.loads(body),
                {
                    "ok": False,
                    "code": "forbidden",
                    "message": "Loopback access is required.",
                },
            )
            self.assertEqual(self.service.pair_calls, [])
        finally:
            client.close()
            accepted.close()

    def test_mutation_requires_exact_bearer_timestamp_and_idempotency_key(self):
        # Break caught: one missing or loosely compared credential reaches device work.
        cases = [
            {},
            {"Authorization": "Bearer local-secret"},
            {**self.valid_headers(), "Authorization": "bearer local-secret"},
            {**self.valid_headers(), "Authorization": "Bearer local-secret "},
            {**self.valid_headers(), "X-Sign402-Timestamp": str(NOW + 61)},
            {**self.valid_headers(), "Idempotency-Key": "short"},
            {**self.valid_headers(), "Idempotency-Key": "invalid key"},
        ]
        for headers in cases:
            with self.subTest(headers=set(headers)):
                status, _, payload = self.request("POST", "/v1/pair", headers=headers, body=b"{}")
                self.assertIn(status, {400, 401})
                self.assertEqual(set(payload), {"ok", "code", "message"})
                self.assertFalse(payload["ok"])
        self.assertEqual(self.service.pair_calls, [])

    def test_timestamp_window_is_inclusive_and_clock_is_injected(self):
        # Break caught: either valid boundary timestamps fail or stale requests pass.
        for timestamp in (NOW - 60, NOW + 60):
            headers = self.valid_headers(f"boundary-{timestamp}")
            headers["X-Sign402-Timestamp"] = str(timestamp)
            status, _, _ = self.request("POST", "/v1/pair", headers=headers, body=b"{}")
            self.assertEqual(status, 200)
        headers = self.valid_headers("outside-window")
        headers["X-Sign402-Timestamp"] = str(NOW - 61)
        status, _, _ = self.request("POST", "/v1/pair", headers=headers, body=b"{}")
        self.assertEqual(status, 400)

    def test_pathological_timestamp_digits_are_caller_error_not_clock_error(self):
        # Break caught: integer conversion failure is misreported as server clock failure.
        headers = self.valid_headers("huge-timestamp")
        headers["X-Sign402-Timestamp"] = "9" * 5_000
        status, _, payload = self.request(
            "POST", "/v1/pair", headers=headers, body=b"{}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "code": "invalid_request",
                "message": "Request authentication metadata is invalid.",
            },
        )

    def test_unknown_route_never_becomes_generic_mcp_proxy(self):
        # Break caught: caller-controlled tool names become a generic signing proxy.
        status, _, payload = self.request(
            "POST",
            "/v1/tools/call",
            headers=self.valid_headers("unknown-tool"),
            body=b'{"name":"trezor_sign_message","arguments":{}}',
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"ok": False, "code": "not_found", "message": "Route not found."})

    def test_known_routes_reject_wrong_methods_and_unknown_gets_are_404(self):
        # Break caught: framework defaults expose verbose 501/HTML responses.
        status, _, payload = self.request("GET", "/v1/pair")
        self.assertEqual(status, 405)
        self.assertEqual(set(payload), {"ok", "code", "message"})
        status, _, payload = self.request("GET", "/not-a-route")
        self.assertEqual(status, 404)
        self.assertEqual(set(payload), {"ok", "code", "message"})

    def test_arbitrary_http_method_returns_fixed_json_405(self):
        # Break caught: BaseHTTPRequestHandler emits a verbose HTML 501 response.
        request = b"BREW /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        status, payload = self.raw_request(request)
        self.assertEqual(status, 405)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "code": "method_not_allowed",
                "message": "Method not allowed.",
            },
        )

    def test_arbitrary_http_method_on_unknown_path_returns_fixed_json_404(self):
        # Break caught: an unknown verb makes an unknown route appear registered.
        request = b"BREW /not-a-route HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        status, payload = self.raw_request(request)
        self.assertEqual(status, 404)
        self.assertEqual(
            payload,
            {"ok": False, "code": "not_found", "message": "Route not found."},
        )

    def test_request_framing_and_json_are_strict_and_bounded(self):
        # Break caught: ambiguous framing, duplicate keys, or non-object JSON reaches a route.
        invalid_bodies = [
            (b'{"allowRepair":false} trailing', "application/json"),
            (b'{"allowRepair":false,"allowRepair":true}', "application/json"),
            (b"[]", "application/json"),
            (b"null", "application/json"),
            (b'{"allowRepair":false}', "text/plain"),
        ]
        for index, (body, content_type) in enumerate(invalid_bodies):
            headers = self.valid_headers(f"invalid-{index}")
            headers["Content-Type"] = content_type
            status, _, payload = self.request("POST", "/v1/pair", headers=headers, body=body)
            self.assertEqual(status, 400)
            self.assertEqual(set(payload), {"ok", "code", "message"})
        huge = b"{" + b" " * 65_535 + b"}"
        status, _, _ = self.request(
            "POST", "/v1/pair", headers=self.valid_headers("oversized"), body=huge
        )
        self.assertEqual(status, 413)
        self.assertEqual(self.service.pair_calls, [])

    def test_nonstandard_json_constants_are_rejected(self):
        # Break caught: Python-specific NaN/Infinity values cross the JSON boundary.
        for index, value in enumerate((b"NaN", b"Infinity", b"-Infinity")):
            body = b'{"allowRepair":' + value + b"}"
            status, _, payload = self.request(
                "POST",
                "/v1/pair",
                headers=self.valid_headers(f"constant-{index}"),
                body=body,
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "invalid_json")

    def test_missing_negative_multiple_lengths_and_chunked_transfer_are_rejected(self):
        # Break caught: request smuggling or unbounded EOF reads bypass body limits.
        base = (
            b"POST /v1/pair HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Authorization: Bearer local-secret\r\nContent-Type: application/json\r\n"
            b"X-Sign402-Timestamp: 1700000000\r\nIdempotency-Key: framing-key\r\n"
        )
        requests = [
            base + b"\r\n{}",
            base + b"Content-Length: -1\r\n\r\n{}",
            base + b"Content-Length: " + b"9" * 5_000 + b"\r\n\r\n{}",
            base + b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            base + b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
        ]
        for request in requests:
            status, payload = self.raw_request(request)
            self.assertEqual(status, 400)
            self.assertEqual(set(payload), {"ok", "code", "message"})
        self.assertEqual(self.service.pair_calls, [])

    def test_stalled_request_headers_release_connection_within_deadline(self):
        # Break caught: a local slowloris retains a handler thread indefinitely.
        started = time.monotonic()
        with socket.create_connection(self.address, timeout=1) as client:
            client.settimeout(0.75)
            client.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Stalled:")
            try:
                while client.recv(8192):
                    pass
            except TimeoutError:
                self.fail("stalled header connection exceeded its deadline")
        self.assertLess(time.monotonic() - started, 0.75)

    def test_stalled_exact_length_body_returns_fixed_safe_timeout(self):
        # Break caught: exact-length streaming blocks forever when the caller stops writing.
        request = (
            b"POST /v1/pair HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Authorization: Bearer local-secret\r\nContent-Type: application/json\r\n"
            b"X-Sign402-Timestamp: 1700000000\r\nIdempotency-Key: timeout-key\r\n"
            b"Content-Length: 10\r\n\r\n{}"
        )
        started = time.monotonic()
        with socket.create_connection(self.address, timeout=1) as client:
            client.settimeout(0.75)
            client.sendall(request)
            response = b""
            try:
                while True:
                    chunk = client.recv(8192)
                    if not chunk:
                        break
                    response += chunk
            except TimeoutError:
                self.fail("stalled body connection exceeded its deadline")
        self.assertLess(time.monotonic() - started, 0.75)
        head, body = response.split(b"\r\n\r\n", 1)
        self.assertEqual(int(head.split(b" ", 2)[1]), 408)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "code": "request_timeout", "message": "Request timed out."},
        )

    def test_pair_dto_is_closed_and_bool_typed(self):
        # Break caught: arbitrary transaction fields or integer truthiness reaches pairing.
        invalid = [
            {"allowRepair": 1},
            {"allowRepair": False, "tool": "trezor_get_address"},
            {"allow_repair": True},
        ]
        for index, body in enumerate(invalid):
            status, _, _ = self.request(
                "POST",
                "/v1/pair",
                headers=self.valid_headers(f"pair-invalid-{index}"),
                body=json.dumps(body).encode(),
            )
            self.assertEqual(status, 400)
        status, _, payload = self.request(
            "POST",
            "/v1/pair",
            headers=self.valid_headers("pair-valid"),
            body=b'{"allowRepair":true}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.service.pair_calls, [True])
        self.assertEqual(
            payload,
            {
                "ok": True,
                "pairing": {
                    "pairingId": "pairing-123",
                    "address": "0x2222222222222222222222222222222222222222",
                    "createdAt": NOW,
                    "updatedAt": NOW,
                },
            },
        )

    def test_purchase_intent_dto_is_exact_and_integer_fields_reject_booleans(self):
        # Break caught: arbitrary chain/token/calldata fields or boolean amounts are signed.
        invalid = [
            self.intent_body(chain="base"),
            self.intent_body(paymentAsset="DAI"),
            self.intent_body(quotedTotalUsdMicros=True),
        ]
        for index, body in enumerate(invalid):
            status, _, _ = self.request(
                "POST",
                "/v1/purchase-intents/approve",
                headers=self.valid_headers(f"intent-invalid-{index}"),
                body=body,
            )
            self.assertEqual(status, 400)
        status, _, payload = self.request(
            "POST",
            "/v1/purchase-intents/approve",
            headers=self.valid_headers("intent-valid"),
            body=self.intent_body(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.service.approve_calls[0][1], NOW)
        self.assertIsInstance(self.service.approve_calls[0][0], PurchaseIntent)
        self.assertEqual(payload, {"ok": True, "intentId": INTENT_ID, "state": "DEVICE_APPROVED"})

    def test_payment_dto_is_exact_and_never_accepts_raw_transaction(self):
        # Break caught: generic transaction, chain, calldata, or boolean amount reaches payment.
        invalid = [
            self.payment_body(transaction={"to": PAY_TO}),
            self.payment_body(calldata="0xa9059cbb"),
            self.payment_body(chain="base"),
            self.payment_body(amountAtomic=True),
        ]
        for index, body in enumerate(invalid):
            status, _, _ = self.request(
                "POST",
                "/v1/payments",
                headers=self.valid_headers(f"payment-invalid-{index}"),
                body=body,
            )
            self.assertEqual(status, 400)
        self.assertEqual(self.service.create_calls, [])

    def test_payment_success_is_allowlisted_and_worker_runs_once_on_replay(self):
        # Break caught: replay starts a second signer or response leaks service internals.
        self.service.release_run.clear()
        for _ in range(2):
            status, _, payload = self.request(
                "POST",
                "/v1/payments",
                headers=self.valid_headers("payment-replay"),
                body=self.payment_body(),
            )
            self.assertEqual(status, 202)
            self.assertEqual(
                payload,
                {
                    "ok": True,
                    "payment": {
                        "paymentId": "payment-123",
                        "intentId": INTENT_ID,
                        "invoiceId": "invoice-123",
                        "state": "INVOICE_CREATED",
                        "createdAt": NOW,
                        "updatedAt": NOW,
                    },
                },
            )
        self.assertTrue(self.service.run_entered.wait(timeout=1))
        self.assertEqual(self.service.run_calls, ["payment-123"])

    def test_simultaneous_payment_replays_launch_exactly_one_worker(self):
        # Break caught: a check-then-start race launches duplicate device prompts.
        self.service.release_run.clear()

        def submit(_):
            return self.request(
                "POST",
                "/v1/payments",
                headers=self.valid_headers("concurrent-payment"),
                body=self.payment_body(),
            )[0]

        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(submit, range(8)))
        self.assertEqual(statuses, [202] * 8)
        self.assertTrue(self.service.run_entered.wait(timeout=1))
        self.assertEqual(self.service.run_calls, ["payment-123"])

    def test_successfully_started_worker_is_not_relaunched_after_exception(self):
        # Break caught: a worker exception silently enables a second device prompt.
        self.service.run_failure = RuntimeError("secret provider response")
        status, _, _ = self.request(
            "POST",
            "/v1/payments",
            headers=self.valid_headers("failed-worker"),
            body=self.payment_body(),
        )
        self.assertEqual(status, 202)
        self.assertTrue(self.service.run_exited.wait(timeout=1))
        _, _, health = self.request("GET", "/health")
        self.assertEqual(health, {"ok": True, "status": "device_unavailable"})
        status, _, _ = self.request(
            "POST",
            "/v1/payments",
            headers=self.valid_headers("failed-worker"),
            body=self.payment_body(),
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.service.run_calls, ["payment-123"])

    def test_default_worker_clock_is_normalized_for_real_service_contract(self):
        # Break caught: time.time returns float, which the real service rejects before signing.
        service = FakeService()
        server = build_server(self.settings, service, _allow_test_port=True)
        request = PaymentRequest(
            intent_id=INTENT_ID,
            invoice_id="invoice-123",
            pay_to=PAY_TO,
            amount_atomic=5_000_000,
            expires_at=NOW + 600,
        )
        try:
            server.create_and_schedule(request, "default-clock", NOW)
            self.assertTrue(service.run_exited.wait(timeout=1))
            self.assertEqual(len(service.run_clock_values), 1)
            self.assertIs(type(service.run_clock_values[0]), int)
            self.assertEqual(server.health_status, "ready")
        finally:
            server.server_close()

    def test_distinct_payment_workers_wait_instead_of_losing_to_device_busy(self):
        # Break caught: the second launched job loses the device race and is stranded forever.
        class DistinctPaymentService(FakeService):
            def __init__(self):
                super().__init__()
                self.first_entered = threading.Event()
                self.second_entered = threading.Event()
                self.release_first = threading.Event()
                self.both_finished = threading.Event()

            def create_payment(self, request, idempotency_key, now):
                return PaymentView(
                    payment_id="payment-" + request.invoice_id[-1],
                    intent_id=request.intent_id,
                    invoice_id=request.invoice_id,
                    state=PaymentState.INVOICE_CREATED,
                    created_at=NOW,
                    updated_at=NOW,
                )

            def run_payment(self, payment_id, now):
                with self._lock:
                    self.run_calls.append(payment_id)
                    count = len(self.run_calls)
                if count == 1:
                    self.first_entered.set()
                    self.release_first.wait(timeout=2)
                else:
                    self.second_entered.set()
                    self.both_finished.set()
                return self.payment

        service = DistinctPaymentService()
        server = build_server(
            self.settings,
            service,
            clock=lambda: NOW,
            _allow_test_port=True,
        )
        first = PaymentRequest(INTENT_ID, "invoice-1", PAY_TO, 1, NOW + 600)
        second = PaymentRequest(INTENT_ID, "invoice-2", PAY_TO, 1, NOW + 600)
        try:
            server.create_and_schedule(first, "distinct-1", NOW)
            server.create_and_schedule(second, "distinct-2", NOW)
            self.assertTrue(service.first_entered.wait(timeout=1))
            self.assertFalse(service.second_entered.wait(timeout=0.1))
            service.release_first.set()
            self.assertTrue(service.both_finished.wait(timeout=1))
            self.assertEqual(service.run_calls, ["payment-1", "payment-2"])
        finally:
            service.release_first.set()
            server.server_close()

    def test_payment_worker_waits_for_synchronous_pairing_device_work(self):
        # Break caught: pairing holds the device while a launched payment gets stranded busy.
        class BlockingPairService(FakeService):
            def __init__(self):
                super().__init__()
                self.pair_entered = threading.Event()
                self.release_pair = threading.Event()

            def pair(self, allow_repair=False):
                self.pair_entered.set()
                self.release_pair.wait(timeout=2)
                return super().pair(allow_repair)

        service = BlockingPairService()
        self.server.service = service

        def submit_pair():
            return self.request(
                "POST",
                "/v1/pair",
                headers=self.valid_headers("blocking-pair"),
                body=b"{}",
            )[0]

        with ThreadPoolExecutor(max_workers=1) as executor:
            pairing = executor.submit(submit_pair)
            self.assertTrue(service.pair_entered.wait(timeout=1))
            status, _, _ = self.request(
                "POST",
                "/v1/payments",
                headers=self.valid_headers("after-pair"),
                body=self.payment_body(),
            )
            self.assertEqual(status, 202)
            self.assertFalse(service.run_entered.wait(timeout=0.1))
            service.release_pair.set()
            self.assertEqual(pairing.result(timeout=1), 200)
        self.assertTrue(service.run_entered.wait(timeout=1))

    def test_worker_construction_failure_leaves_invoice_created_retryable(self):
        # Break caught: a pre-launch failure permanently strands a runnable payment.
        request = PaymentRequest(
            intent_id=INTENT_ID,
            invoice_id="invoice-123",
            pay_to=PAY_TO,
            amount_atomic=5_000_000,
            expires_at=NOW + 600,
        )

        class BrokenThread:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("thread construction failed")

        with patch("trezor_sidecar.server.threading.Thread", BrokenThread):
            with self.assertRaisesRegex(SafeError, "worker"):
                self.server.create_and_schedule(request, "retry-worker", NOW)

        payment, _ = self.server.create_and_schedule(request, "retry-worker", NOW)
        self.assertEqual(payment.state, PaymentState.INVOICE_CREATED)
        self.assertTrue(self.service.run_entered.wait(timeout=1))
        self.assertEqual(self.service.run_calls, ["payment-123"])

    def test_worker_start_failure_leaves_invoice_created_retryable(self):
        # Break caught: Thread.start failure leaves a stale launch reservation.
        request = PaymentRequest(
            intent_id=INTENT_ID,
            invoice_id="invoice-123",
            pay_to=PAY_TO,
            amount_atomic=5_000_000,
            expires_at=NOW + 600,
        )

        class StartFailureThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        with patch("trezor_sidecar.server.threading.Thread", StartFailureThread):
            with self.assertRaises(SafeError) as raised:
                self.server.create_and_schedule(request, "retry-start", NOW)
        self.assertEqual(raised.exception.code, "worker_unavailable")

        payment, _ = self.server.create_and_schedule(request, "retry-start", NOW)
        self.assertEqual(payment.state, PaymentState.INVOICE_CREATED)
        self.assertTrue(self.service.run_entered.wait(timeout=1))
        self.assertEqual(self.service.run_calls, ["payment-123"])

    def test_get_payment_requires_bearer_and_payment_id_is_bounded_safe_segment(self):
        # Break caught: unauthenticated status disclosure or path-shaped IDs reach storage.
        status, _, _ = self.request("GET", "/v1/payments/payment-123")
        self.assertEqual(status, 401)
        for payment_id in ("a" * 129, "..%2Fsecret", "bad%00id"):
            status, _, _ = self.request(
                "GET",
                f"/v1/payments/{payment_id}",
                headers={"Authorization": "Bearer local-secret"},
            )
            self.assertEqual(status, 404)
        status, _, payload = self.request(
            "GET",
            "/v1/payments/payment-123",
            headers={"Authorization": "Bearer local-secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["payment"]["paymentId"], "payment-123")

    def test_health_and_errors_have_only_fixed_safe_content(self):
        # Break caught: tokens, addresses, session IDs, or exception details escape in JSON.
        status, _, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "status": "suite_unavailable"})
        serialized = json.dumps(payload)
        for secret in ("local-secret", "mcp-secret", "0x2222", "session"):
            self.assertNotIn(secret, serialized)

        self.service.failure = SafeError(
            "trezor_unavailable", "Trezor Suite is unavailable.", 503
        )
        status, _, payload = self.request(
            "POST", "/v1/pair", headers=self.valid_headers("safe-error"), body=b"{}"
        )
        self.assertEqual(status, 503)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "code": "trezor_unavailable",
                "message": "Trezor Suite is unavailable.",
            },
        )
        _, _, health = self.request("GET", "/health")
        self.assertEqual(health, {"ok": True, "status": "suite_unavailable"})

        self.service.failure = RuntimeError("provider response contains local-secret and address")
        status, _, payload = self.request(
            "POST", "/v1/pair", headers=self.valid_headers("hidden-error"), body=b"{}"
        )
        self.assertEqual(status, 500)
        self.assertEqual(
            payload,
            {"ok": False, "code": "internal_error", "message": "Request failed safely."},
        )
        _, _, health = self.request("GET", "/health")
        self.assertEqual(health, {"ok": True, "status": "device_unavailable"})

    def test_health_changes_only_from_coarse_observed_device_outcomes(self):
        # Break caught: enabled configuration is treated as runtime readiness.
        allowed = {"ready", "disabled", "suite_unavailable", "device_unavailable"}

        status, _, _ = self.request(
            "POST",
            "/v1/pair",
            headers=self.valid_headers("health-ready"),
            body=b"{}",
        )
        self.assertEqual(status, 200)
        _, _, health = self.request("GET", "/health")
        self.assertEqual(health, {"ok": True, "status": "ready"})

        self.service.failure = SafeError(
            "device_lock_unavailable",
            "canary-device-address-session-token",
            599,
        )
        status, headers, payload = self.request(
            "POST",
            "/v1/pair",
            headers=self.valid_headers("health-device"),
            body=b"{}",
        )
        self.assertEqual(status, 503)
        self.assertEqual(set(payload), {"ok", "code", "message"})
        _, _, health = self.request("GET", "/health")
        self.assertEqual(health, {"ok": True, "status": "device_unavailable"})

        self.service.failure = SafeError(
            "trezor_unavailable",
            "canary-suite-provider-error-token",
            418,
        )
        status, headers, payload = self.request(
            "POST",
            "/v1/pair",
            headers=self.valid_headers("health-suite"),
            body=b"{}",
        )
        self.assertEqual(status, 503)
        _, _, health = self.request("GET", "/health")
        self.assertEqual(health, {"ok": True, "status": "suite_unavailable"})

        for observed in (payload["message"], json.dumps(health), json.dumps(headers)):
            self.assertNotIn("canary", observed)
        self.assertIn(health["status"], allowed)

    def test_unknown_and_forged_safe_errors_never_control_http_output(self):
        # Break caught: an upstream-looking SafeError reflects provider content or status.
        canary = "canary-provider-error-local-secret-address-session"
        self.service.failure = SafeError("provider_error", canary, 599)
        status, headers, payload = self.request(
            "POST",
            "/v1/pair",
            headers=self.valid_headers("unknown-safe-error"),
            body=b"{}",
        )
        self.assertEqual(status, 500)
        self.assertEqual(
            payload,
            {"ok": False, "code": "internal_error", "message": "Request failed safely."},
        )
        self.assertEqual(set(payload), {"ok", "code", "message"})
        self.assertNotIn(canary, json.dumps(headers) + json.dumps(payload))

        self.service.failure = SafeError("payment_not_found", canary, 599)
        status, headers, payload = self.request(
            "GET",
            "/v1/payments/payment-123",
            headers={"Authorization": "Bearer local-secret"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(
            payload,
            {"ok": False, "code": "payment_not_found", "message": "Payment was not found."},
        )
        self.assertEqual(set(payload), {"ok", "code", "message"})
        self.assertNotIn(canary, json.dumps(headers) + json.dumps(payload))


class DisabledHealthTests(TestCase):
    def test_health_reports_only_disabled_when_feature_is_off(self):
        # Break caught: disabled health leaks configuration while reporting readiness.
        settings = SidecarSettings(
            False, "", "", Decimal("0"), "", Path("unused.db"), port=0
        )
        service = FakeService()
        server = build_server(
            settings,
            service,
            clock=lambda: NOW,
            _allow_test_port=True,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request("GET", "/health")
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(payload, {"ok": True, "status": "disabled"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class PortValidationTests(TestCase):
    def setUp(self):
        self.service = FakeService()
        self.settings = SidecarSettings(
            True,
            "mcp-token",
            "api-token",
            Decimal("1"),
            "https://rpc.example.invalid",
            Path("unused.db"),
            port=8111,
        )

    def test_production_port_8111_is_accepted_without_test_escape_hatch(self):
        # Break caught: the fixed production port is accidentally rejected.
        with patch("trezor_sidecar.server._SidecarHttpServer") as server_type:
            result = build_server(self.settings, self.service)
        self.assertIs(result, server_type.return_value)

    def test_alternate_nonzero_port_is_rejected_before_bind(self):
        # Break caught: configuration exposes the sidecar on an unapproved port.
        with patch("trezor_sidecar.server._SidecarHttpServer") as server_type:
            with self.assertRaises(ValueError):
                build_server(replace(self.settings, port=8112), self.service)
        server_type.assert_not_called()

    def test_port_zero_requires_explicit_private_test_escape_hatch(self):
        # Break caught: production callers silently request an arbitrary ephemeral port.
        with patch("trezor_sidecar.server._SidecarHttpServer") as server_type:
            with self.assertRaises(ValueError):
                build_server(replace(self.settings, port=0), self.service)
            result = build_server(
                replace(self.settings, port=0),
                self.service,
                _allow_test_port=True,
            )
        self.assertIs(result, server_type.return_value)

    def test_production_port_cannot_override_fixed_connection_timeout(self):
        # Break caught: a caller disables or weakens the production socket deadline.
        with patch("trezor_sidecar.server._SidecarHttpServer") as server_type:
            with self.assertRaises(ValueError):
                build_server(
                    self.settings,
                    self.service,
                    _allow_test_port=True,
                    _test_only_connection_timeout=0.1,
                )
        server_type.assert_not_called()
