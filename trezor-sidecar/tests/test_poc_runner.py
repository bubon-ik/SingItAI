import io
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from trezor_sidecar.base import BASE_USDC_ADDRESS
from trezor_sidecar.errors import SafeError
from trezor_sidecar.models import LOCAL_INTENT_TEST_ID, PaymentState, PaymentView, PurchaseIntent
from trezor_sidecar.poc_runner import (
    PreparedAddressBitrefillClient,
    SidecarTreasuryClient,
    TrezorPocRunner,
    build_parser,
    main,
)
from trezor_sidecar.sidecar_client import SidecarClient
from trezor_sidecar.store import SidecarStore
from sign402_gateway.bitrefill_mcp import McpBitrefillClient


NOW = 1_700_000_000
INTENT_ID = "0x" + "11" * 32
RECIPIENT_HASH = "0x" + "22" * 32
PAY_TO = "0x1111111111111111111111111111111111111111"
OTHER_PAY_TO = "0x2222222222222222222222222222222222222222"
TX_HASH = "0x" + "ab" * 32


def valid_quote(**changes):
    value = {
        "productId": "test-gift",
        "name": "Test Gift",
        "productType": "gift_card",
        "packageId": "1",
        "packageValue": "1 USD",
        "country": "US",
        "currency": "USD",
        "priceUsd": "1.00",
        "recipientType": "email",
        "requiredRecipientFields": ["email"],
    }
    value.update(changes)
    return value


def valid_prepared(**changes):
    value = {
        "invoiceId": "invoice-1",
        "status": "unpaid",
        "productId": "test-gift",
        "packageValue": "1 USD",
        "paymentMethod": "usdc_base",
        "expiresAtEpoch": NOW + 300,
        "paymentAmount": "1.00",
        "paymentAsset": "USDC",
        "paymentNetwork": "base",
        "paymentAddress": PAY_TO,
        "invoiceAccessToken": "invoice-access-canary",
    }
    value.update(changes)
    return value


def valid_intent(**changes):
    values = {
        "intent_id": INTENT_ID,
        "product_slug": "test-gift",
        "package_id": "1",
        "denomination": "1 USD",
        "quoted_total_usd_micros": 1_000_000,
        "max_payment_usdc_atomic": 2_000_000,
        "recipient_hash": RECIPIENT_HASH,
        "expires_at": NOW + 600,
    }
    values.update(changes)
    return PurchaseIntent(**values)


class FakeStore:
    def __init__(self):
        self.payment = None
        self.transitions = []
        self.records = []

    def get_payment(self, payment_id):
        if self.payment is not None and self.payment.payment_id == payment_id:
            return self.payment
        return None

    def transition_payment(self, **arguments):
        self.transitions.append(arguments)
        if self.payment is None or self.payment.state is not arguments["expected"]:
            raise ValueError("payment state changed")
        self.payment = PaymentView(
            payment_id=self.payment.payment_id,
            intent_id=self.payment.intent_id,
            invoice_id=self.payment.invoice_id,
            state=arguments["target"],
            created_at=self.payment.created_at,
            updated_at=arguments["updated_at"],
            tx_hash=arguments.get("tx_hash") or self.payment.tx_hash,
        )
        return self.payment

    def record_purchase(self, invoice_id, product_slug, amount, payment_method, timestamp):
        self.records.append(
            (invoice_id, product_slug, str(amount), payment_method, timestamp)
        )


class FakeSidecar:
    def __init__(self, events=None, *, approved=True, store=None, tx_hash=TX_HASH):
        self.events = events if events is not None else []
        self.approved = approved
        self.store = store
        self.tx_hash = tx_hash
        self.approve_calls = []
        self.pay_calls = []
        self.pair_calls = 0

    def pair(self, allow_repair=False):
        self.pair_calls += 1
        return {
            "ok": True,
            "pairing": {"address": PAY_TO, "pairingId": "pair-1", "createdAt": NOW, "updatedAt": NOW},
        }

    def approve_intent(self, intent):
        self.events.append("approve-intent")
        self.approve_calls.append(intent)
        if not self.approved:
            raise SafeError("device_rejected", "Trezor operation was cancelled.")
        return {"ok": True, "intentId": intent.intent_id, "state": "DEVICE_APPROVED"}

    def pay_invoice(
        self,
        intent_id,
        invoice_id,
        pay_to,
        amount_atomic,
        expires_at,
        idempotency_key,
    ):
        self.events.append("sidecar-pay")
        self.pay_calls.append(
            {
                "intent_id": intent_id,
                "invoice_id": invoice_id,
                "pay_to": pay_to,
                "amount_atomic": amount_atomic,
                "expires_at": expires_at,
                "idempotency_key": idempotency_key,
            }
        )
        payment = {
            "paymentId": "payment-1",
            "intentId": intent_id,
            "invoiceId": invoice_id,
            "state": "TX_BROADCAST",
            "createdAt": NOW,
            "updatedAt": NOW + 1,
            "txHash": self.tx_hash,
        }
        if self.store is not None:
            self.store.payment = PaymentView(
                payment_id="payment-1",
                intent_id=intent_id,
                invoice_id=invoice_id,
                state=PaymentState.TX_BROADCAST,
                created_at=NOW,
                updated_at=NOW + 1,
                tx_hash=self.tx_hash,
            )
        return {"ok": True, "payment": payment}


class FakeBitrefill:
    def __init__(self, events=None, *, prepared=None, prepare_error=None, **_ignored):
        self.events = events if events is not None else []
        self.prepared = valid_prepared() if prepared is None else prepared
        self.prepare_error = prepare_error
        self.quote_calls = []
        self.prepare_calls = []
        self.complete_calls = []
        self.details_calls = []
        self.treasury_client = None
        self.transfer_changes = {}

    def get_product_details(self, *, product_id, country):
        self.details_calls.append((product_id, country))
        return {
            "productId": product_id,
            "name": "Test Gift",
            "country": country,
            "requiredRecipientFields": ["email"],
        }

    def quote_product(self, *, product_id, package_id, country, recipient):
        self.quote_calls.append((product_id, package_id, country, dict(recipient)))
        return valid_quote(productId=product_id, packageId=package_id, country=country)

    def prepare_purchase(self, *, quote, recipient, buyer_email=""):
        self.events.append("prepare-purchase")
        self.prepare_calls.append((dict(quote), dict(recipient), buyer_email))
        if self.prepare_error is not None:
            raise self.prepare_error
        return dict(self.prepared)

    def complete_purchase(
        self,
        *,
        quote,
        prepared,
        checkpoint_callback=None,
        invoice_access_token="",
    ):
        self.events.append("complete-purchase")
        self.complete_calls.append((dict(quote), dict(prepared), invoice_access_token))
        transfer = {
            "token_address": BASE_USDC_ADDRESS,
            "to_address": prepared["paymentAddress"],
            "amount_atomic": str(int(Decimal(prepared["paymentAmount"]) * 1_000_000)),
            "chain": "base",
            "idempotency_key": f"bitrefill-pay:{prepared['invoiceId']}",
        }
        transfer.update(self.transfer_changes)
        paid = self.treasury_client.transfer_token_exact(**transfer)
        if checkpoint_callback is not None:
            checkpoint_callback({"invoiceId": prepared["invoiceId"], "treasuryPayment": paid})
        return {
            "ok": True,
            "provider": "bitrefill-mcp",
            "paymentMethod": "usdc_base",
            "orderId": "order-1",
            "invoiceId": prepared["invoiceId"],
            "status": "delivered",
            "redemption": {
                "type": "bitrefill",
                "label": "Bitrefill redemption",
                "value": {"code": "REDEMPTION-CANARY"},
            },
            "treasuryPayment": paid,
        }


class FakeHttpResponse:
    def __init__(self, status, payload=b"", headers=None):
        self.status = status
        self._payload = payload
        self._offset = 0
        self.headers = Message()
        for key, value in (headers or {}).items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                self.headers.add_header(key, item)
        if self.headers.get("Content-Type") is None:
            self.headers.add_header("Content-Type", "application/json")

    def read(self, amount=-1):
        if amount is None or amount < 0:
            amount = len(self._payload) - self._offset
        chunk = self._payload[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self):
        return None


class FailingReadHttpResponse(FakeHttpResponse):
    def read(self, amount=-1):
        raise TimeoutError("response-secret-canary")


class QueueRequester:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, headers, body):
        self.calls.append((method, path, dict(headers), body))
        return self.responses.pop(0)


def response(status, value, headers=None):
    return FakeHttpResponse(status, json.dumps(value, separators=(",", ":")).encode(), headers)


def payment_payload(state, *, tx_hash=None):
    payment = {
        "paymentId": "payment-1",
        "intentId": INTENT_ID,
        "invoiceId": "invoice-1",
        "state": state,
        "createdAt": NOW,
        "updatedAt": NOW + 1,
    }
    if tx_hash is not None:
        payment["txHash"] = tx_hash
    return {"ok": True, "payment": payment}


class SidecarClientTests(TestCase):
    def test_approve_uses_fixed_loopback_auth_timestamp_idempotency_and_exact_dto(self):
        requester = QueueRequester(
            [response(200, {"ok": True, "intentId": INTENT_ID, "state": "DEVICE_APPROVED"})]
        )
        client = SidecarClient(token="sidecar-token-canary", requester=requester, clock=lambda: NOW)

        result = client.approve_intent(valid_intent())

        self.assertEqual(result["state"], "DEVICE_APPROVED")
        method, path, headers, body = requester.calls[0]
        self.assertEqual((method, path), ("POST", "/v1/purchase-intents/approve"))
        self.assertEqual(headers["Authorization"], "Bearer sidecar-token-canary")
        self.assertEqual(headers["X-Sign402-Timestamp"], str(NOW))
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertEqual(headers["Idempotency-Key"], f"approve:{INTENT_ID}")
        self.assertEqual(
            set(json.loads(body)),
            {
                "intentId", "productSlug", "packageId", "denomination",
                "quotedTotalUsdMicros", "maxPaymentUsdcAtomic", "paymentAsset",
                "paymentNetwork", "recipientHash", "expiresAt",
            },
        )
        self.assertNotIn("sidecar-token-canary", repr(client))

    def test_client_rejects_redirect_compression_duplicates_and_oversize_bodies(self):
        cases = [
            FakeHttpResponse(302, b"{}", {"Location": "https://evil.invalid"}),
            FakeHttpResponse(200, b'{"ok":true}', {"Content-Encoding": "gzip"}),
            FakeHttpResponse(200, b'{"ok":true,"ok":true}'),
            FakeHttpResponse(200, b"{" + b" " * 65_536 + b"}"),
            FakeHttpResponse(200, b'[]'),
        ]
        for item in cases:
            with self.subTest(status=item.status, length=len(item._payload)):
                client = SidecarClient(
                    token="token",
                    requester=QueueRequester([item]),
                    clock=lambda: NOW,
                )
                with self.assertRaisesRegex(SafeError, "invalid response"):
                    client.approve_intent(valid_intent())

    def test_error_shape_and_messages_are_allowlisted_without_echoing_body_secrets(self):
        malformed = response(
            400,
            {"ok": False, "code": "device_rejected", "message": "secret-canary", "detail": "x"},
        )
        with self.assertRaises(SafeError) as malformed_error:
            SidecarClient(token="token", requester=QueueRequester([malformed]), clock=lambda: NOW).approve_intent(valid_intent())
        self.assertNotIn("secret-canary", str(malformed_error.exception))

        safe = response(400, {"ok": False, "code": "device_rejected", "message": "secret-canary"})
        with self.assertRaisesRegex(SafeError, "cancelled") as safe_error:
            SidecarClient(token="token", requester=QueueRequester([safe]), clock=lambda: NOW).approve_intent(valid_intent())
        self.assertNotIn("secret-canary", str(safe_error.exception))

    def test_all_direct_server_boundary_errors_have_fixed_public_messages(self):
        cases = {
            "forbidden": "Loopback access is required.",
            "internal_error": "Request failed safely.",
            "invalid_json": "Request body must be one JSON object.",
            "method_not_allowed": "Method not allowed.",
            "not_found": "Route not found.",
            "request_timeout": "Request timed out.",
            "request_too_large": "Request body is too large.",
            "stale_request": "Request timestamp is outside the allowed window.",
            "unauthorized": "Authentication failed.",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                requester = QueueRequester(
                    [response(400, {"ok": False, "code": code, "message": "body-secret-canary"})]
                )
                with self.assertRaises(SafeError) as raised:
                    SidecarClient(token="token", requester=requester, clock=lambda: NOW).approve_intent(valid_intent())
                self.assertEqual(raised.exception.message, expected)
                self.assertNotIn("body-secret-canary", str(raised.exception))

    def test_response_read_exception_becomes_fixed_safe_error_without_cause(self):
        client = SidecarClient(
            token="token",
            requester=QueueRequester([FailingReadHttpResponse(200)]),
            clock=lambda: NOW,
        )
        with self.assertRaises(SafeError) as raised:
            client.approve_intent(valid_intent())
        self.assertEqual(raised.exception.code, "sidecar_unavailable")
        self.assertNotIn("response-secret-canary", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_direct_digit_string_amount_is_canonicalized_to_json_integer(self):
        requester = QueueRequester(
            [response(202, payment_payload("TX_BROADCAST", tx_hash=TX_HASH))]
        )
        client = SidecarClient(token="token", requester=requester, clock=lambda: NOW)
        client.pay_invoice(
            INTENT_ID, "invoice-1", PAY_TO, "1000000", NOW + 300, "bitrefill-pay:invoice-1"
        )
        self.assertIs(type(json.loads(requester.calls[0][3])["amountAtomic"]), int)

    def test_payment_polls_boundedly_and_returns_only_exact_broadcast_receipt(self):
        requester = QueueRequester(
            [
                response(202, payment_payload("INVOICE_CREATED")),
                response(200, payment_payload("TX_SIGNED")),
                response(200, payment_payload("TX_BROADCAST", tx_hash=TX_HASH)),
            ]
        )
        sleeps = []
        client = SidecarClient(
            token="token",
            requester=requester,
            clock=lambda: NOW,
            sleeper=sleeps.append,
            poll_attempts=2,
            poll_interval_seconds=0.25,
        )

        result = client.pay_invoice(
            INTENT_ID, "invoice-1", PAY_TO, "1000000", NOW + 300, "bitrefill-pay:invoice-1"
        )

        self.assertEqual(result, payment_payload("TX_BROADCAST", tx_hash=TX_HASH))
        self.assertEqual([call[0] for call in requester.calls], ["POST", "GET", "GET"])
        self.assertEqual(sleeps, [0.25])

    def test_reconciliation_is_terminal_and_payment_post_is_never_resubmitted(self):
        requester = QueueRequester(
            [
                response(202, payment_payload("INVOICE_CREATED")),
                response(
                    200,
                    payment_payload("RECONCILIATION_REQUIRED", tx_hash=TX_HASH),
                ),
            ]
        )
        client = SidecarClient(token="token", requester=requester, clock=lambda: NOW)

        with self.assertRaisesRegex(SafeError, "reconciliation") as raised:
            client.pay_invoice(
                INTENT_ID, "invoice-1", PAY_TO, 1_000_000, NOW + 300, "bitrefill-pay:invoice-1"
            )

        self.assertEqual(raised.exception.code, "reconciliation_required")
        self.assertNotIn(TX_HASH, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual([call[0] for call in requester.calls], ["POST", "GET"])
        self.assertEqual([call[0] for call in requester.calls].count("POST"), 1)

    def test_reconciliation_rejects_malformed_transaction_hash(self):
        requester = QueueRequester(
            [
                response(202, payment_payload("INVOICE_CREATED")),
                response(
                    200,
                    payment_payload("RECONCILIATION_REQUIRED", tx_hash="0x1234"),
                ),
            ]
        )
        client = SidecarClient(token="token", requester=requester, clock=lambda: NOW)

        with self.assertRaisesRegex(SafeError, "invalid response"):
            client.pay_invoice(
                INTENT_ID, "invoice-1", PAY_TO, 1_000_000, NOW + 300, "bitrefill-pay:invoice-1"
            )

        self.assertEqual([call[0] for call in requester.calls], ["POST", "GET"])

    def test_prebroadcast_and_failed_states_forbid_transaction_hash(self):
        for state in ("INVOICE_CREATED", "TX_SIGNED", "CANCELLED", "FAILED"):
            requester = QueueRequester(
                [response(202, payment_payload(state, tx_hash=TX_HASH))]
            )
            client = SidecarClient(token="token", requester=requester, clock=lambda: NOW)

            with self.subTest(state=state), self.assertRaisesRegex(
                SafeError, "invalid response"
            ):
                client.pay_invoice(
                    INTENT_ID,
                    "invoice-1",
                    PAY_TO,
                    1_000_000,
                    NOW + 300,
                    "bitrefill-pay:invoice-1",
                )

            self.assertEqual([call[0] for call in requester.calls], ["POST"])

    def test_poll_timeout_is_bounded_and_never_resubmits(self):
        requester = QueueRequester(
            [
                response(202, payment_payload("INVOICE_CREATED")),
                response(200, payment_payload("TX_SIGNED")),
                response(200, payment_payload("TX_SIGNED")),
            ]
        )
        client = SidecarClient(
            token="token",
            requester=requester,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            poll_attempts=2,
            poll_interval_seconds=0,
        )
        with self.assertRaisesRegex(SafeError, "timed out"):
            client.pay_invoice(
                INTENT_ID, "invoice-1", PAY_TO, 1_000_000, NOW + 300, "bitrefill-pay:invoice-1"
            )
        self.assertEqual([call[0] for call in requester.calls].count("POST"), 1)


class PreparedAddressBridgeTests(TestCase):
    def make_client(self, *, call_tool=lambda *_args, **_kwargs: {}):
        return PreparedAddressBitrefillClient(
            api_key="api-key-canary",
            max_purchase_usd="2.00",
            payment_method="usdc_base",
            call_tool=call_tool,
            now_provider=lambda: NOW,
            invoice_poll_attempts=1,
            invoice_poll_interval_seconds=0,
        )

    def invoice(self, address=PAY_TO, **changes):
        value = {
            "invoice_id": "invoice-1",
            "status": "unpaid",
            "expiration_minutes": "5",
            "payment_method": "usdc_base",
            "payment_info": {
                "address": address,
                "amount": "1.00",
                "currency": "USDC",
                "network": "base",
                "contract_address": BASE_USDC_ADDRESS,
            },
        }
        value.update(changes)
        return value

    def test_prepared_snapshot_retains_only_validated_address_bridge(self):
        client = self.make_client()
        snapshot = client._validated_invoice_snapshot(
            self.invoice(), quote=valid_quote()
        )
        self.assertEqual(snapshot["paymentAddress"], PAY_TO)
        self.assertNotIn("payment_info", snapshot)
        self.assertNotIn("invoiceAccessToken", snapshot)
        base_snapshot = McpBitrefillClient._validated_invoice_snapshot(
            client,
            self.invoice(),
            quote=valid_quote(),
        )
        self.assertNotIn("paymentAddress", base_snapshot)

    def test_reload_without_payment_info_preserves_only_validated_fallback_address(self):
        client = self.make_client()
        fallback = valid_prepared()
        snapshot = client._validated_invoice_snapshot(
            {"invoice_id": "invoice-1", "status": "unpaid"},
            quote=valid_quote(),
            fallback=fallback,
        )
        self.assertEqual(snapshot["paymentAddress"], PAY_TO)
        for invalid in ("", "0x1234", "x" * 43):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "payment address"):
                    client._validated_invoice_snapshot(
                        {"invoice_id": "invoice-1", "status": "unpaid"},
                        quote=valid_quote(),
                        fallback={**fallback, "paymentAddress": invalid},
                    )

    def test_address_change_between_prepare_and_reload_causes_zero_sidecar_payments(self):
        calls = []

        def call_tool(name, _arguments):
            calls.append(name)
            if name == "buy-products":
                return self.invoice(PAY_TO)
            if name == "get-invoice-by-id":
                return self.invoice(OTHER_PAY_TO)
            raise AssertionError(name)

        sidecar = FakeSidecar()
        treasury = SidecarTreasuryClient(sidecar=sidecar, clock=lambda: NOW)
        treasury.register_approved_intent(valid_intent())
        client = self.make_client(call_tool=call_tool)
        client.treasury_client = treasury
        prepared = client.prepare_purchase(
            quote=valid_quote(), recipient={"email": "buyer@example.com"}
        )
        treasury.bind_prepared(INTENT_ID, prepared)

        with self.assertRaisesRegex(SafeError, "address"):
            client.complete_purchase(quote=valid_quote(), prepared=prepared)

        self.assertEqual(calls, ["buy-products", "get-invoice-by-id"])
        self.assertEqual(sidecar.pay_calls, [])


class TreasuryAdapterTests(TestCase):
    def setUp(self):
        self.sidecar = FakeSidecar()
        self.treasury = SidecarTreasuryClient(sidecar=self.sidecar, clock=lambda: NOW)
        self.treasury.register_approved_intent(valid_intent())
        self.treasury.bind_prepared(INTENT_ID, valid_prepared())

    def transfer(self, **changes):
        arguments = {
            "token_address": BASE_USDC_ADDRESS,
            "to_address": PAY_TO,
            "amount_atomic": "1000000",
            "chain": "base",
            "idempotency_key": "bitrefill-pay:invoice-1",
        }
        arguments.update(changes)
        return self.treasury.transfer_token_exact(**arguments)

    def test_exact_transfer_calls_sidecar_once_and_returns_allowlisted_receipt(self):
        result = self.transfer()
        self.assertEqual(
            result,
            {"txId": TX_HASH, "network": "base", "asset": "USDC", "amountAtomic": "1000000"},
        )
        self.assertEqual(len(self.sidecar.pay_calls), 1)

    def test_sidecar_http_boundary_receives_amount_atomic_as_json_integer(self):
        requester = QueueRequester(
            [response(202, payment_payload("TX_BROADCAST", tx_hash=TX_HASH))]
        )
        sidecar = SidecarClient(token="token", requester=requester, clock=lambda: NOW)
        treasury = SidecarTreasuryClient(sidecar=sidecar, clock=lambda: NOW)
        treasury.register_approved_intent(valid_intent())
        treasury.bind_prepared(INTENT_ID, valid_prepared())

        treasury.transfer_token_exact(
            token_address=BASE_USDC_ADDRESS,
            to_address=PAY_TO,
            amount_atomic="1000000",
            chain="base",
            idempotency_key="bitrefill-pay:invoice-1",
        )

        posted = json.loads(requester.calls[0][3])
        self.assertIs(type(posted["amountAtomic"]), int)
        self.assertEqual(posted["amountAtomic"], 1_000_000)

    def test_wrong_token_network_address_amount_or_idempotency_never_pays(self):
        cases = [
            {"token_address": OTHER_PAY_TO},
            {"chain": "ethereum"},
            {"to_address": OTHER_PAY_TO},
            {"amount_atomic": "999999"},
            {"idempotency_key": "bitrefill-order:invoice-1"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(SafeError):
                self.transfer(**changes)
        self.assertEqual(self.sidecar.pay_calls, [])

    def test_duplicate_exact_transfer_reuses_local_receipt_without_second_payment(self):
        first = self.transfer()
        second = self.transfer()
        self.assertEqual(first, second)
        self.assertEqual(len(self.sidecar.pay_calls), 1)

    def test_concurrent_exact_transfers_issue_exactly_one_sidecar_post(self):
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _index: self.transfer(), range(16)))

        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.sidecar.pay_calls), 1)

    def test_concurrent_exact_binding_is_idempotent_but_conflicts_fail(self):
        treasury = SidecarTreasuryClient(sidecar=self.sidecar, clock=lambda: NOW)
        treasury.register_approved_intent(valid_intent())
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda _index: treasury.bind_prepared(INTENT_ID, valid_prepared()), range(16)))

        with self.assertRaisesRegex(SafeError, "conflicts"):
            treasury.bind_prepared(INTENT_ID, valid_prepared(paymentAddress=OTHER_PAY_TO))

    def test_terminal_sidecar_error_is_replayed_without_second_payment(self):
        expected = SafeError(
            "reconciliation_required",
            "Transaction reconciliation is required; payment was not resubmitted.",
            409,
        )

        def fail(*_args, **_kwargs):
            self.sidecar.pay_calls.append("invoked")
            raise expected

        self.sidecar.pay_invoice = fail
        observed = []
        for _index in range(2):
            with self.assertRaises(SafeError) as raised:
                self.transfer()
            observed.append((raised.exception.code, raised.exception.message, raised.exception.status))

        self.assertEqual(observed, [(expected.code, expected.message, expected.status)] * 2)
        self.assertEqual(self.sidecar.pay_calls, ["invoked"])

    def test_validation_error_is_not_terminally_cached(self):
        with self.assertRaises(SafeError):
            self.transfer(token_address=OTHER_PAY_TO)
        result = self.transfer()
        self.assertEqual(result["txId"], TX_HASH)
        self.assertEqual(len(self.sidecar.pay_calls), 1)

    def test_bind_rejects_expired_or_above_intent_max_before_payment(self):
        for prepared in (
            valid_prepared(expiresAtEpoch=NOW),
            valid_prepared(paymentAmount="2.01"),
        ):
            treasury = SidecarTreasuryClient(sidecar=self.sidecar, clock=lambda: NOW)
            treasury.register_approved_intent(valid_intent())
            with self.assertRaises(SafeError):
                treasury.bind_prepared(INTENT_ID, prepared)
        self.assertEqual(self.sidecar.pay_calls, [])

    def test_bind_rejects_mismatched_product_package_or_payment_method(self):
        cases = (
            valid_prepared(productId="different-product"),
            valid_prepared(packageValue="different-package"),
            valid_prepared(paymentMethod="balance"),
        )
        for prepared in cases:
            treasury = SidecarTreasuryClient(sidecar=self.sidecar, clock=lambda: NOW)
            treasury.register_approved_intent(valid_intent())
            with self.subTest(prepared=prepared), self.assertRaises(SafeError):
                treasury.bind_prepared(INTENT_ID, prepared)
        self.assertEqual(self.sidecar.pay_calls, [])


class TrezorPocRunnerTests(TestCase):
    def make_runner(self, *, events=None, approved=True, prepared=None, prepare_error=None):
        events = [] if events is None else events
        store = FakeStore()
        sidecar = FakeSidecar(events, approved=approved, store=store)
        bitrefill = FakeBitrefill(events, prepared=prepared, prepare_error=prepare_error)
        runner = TrezorPocRunner(
            bitrefill=bitrefill,
            sidecar=sidecar,
            max_usd="2.00",
            summary_sink=lambda _summary: events.append("display-summary"),
            _test_store=store,
            clock=lambda: NOW,
        )
        return runner, bitrefill, sidecar, store, events

    def test_invoice_is_created_only_after_device_approval(self):
        runner, bitrefill, sidecar, store, events = self.make_runner()
        quote = runner.quote(
            product_id="test-gift",
            package_id="1",
            country="US",
            recipient={"email": "buyer@example.com"},
        )

        result = runner.buy(
            quote=quote,
            recipient={"email": "buyer@example.com"},
            buyer_email="buyer@example.com",
            now=NOW,
        )

        self.assertEqual(events[:3], ["display-summary", "approve-intent", "prepare-purchase"])
        self.assertEqual(events.count("prepare-purchase"), 1)
        self.assertEqual(events.count("complete-purchase"), 1)
        self.assertEqual(len(sidecar.pay_calls), 1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(store.records, [("invoice-1", "test-gift", "1000000", "usdc_base", NOW + 1)])

    def test_rejected_intent_never_calls_prepare_purchase(self):
        runner, bitrefill, sidecar, store, events = self.make_runner(approved=False)
        with self.assertRaisesRegex(SafeError, "cancelled"):
            runner.buy(
                quote=valid_quote(),
                recipient={"email": "buyer@example.com"},
                buyer_email="buyer@example.com",
                now=NOW,
            )
        self.assertNotIn("prepare-purchase", events)
        self.assertEqual(bitrefill.complete_calls, [])
        self.assertEqual(sidecar.pay_calls, [])

    def test_prepare_exception_stops_binding_completion_and_payment(self):
        runner, bitrefill, sidecar, store, events = self.make_runner(
            prepare_error=RuntimeError("provider-secret-canary")
        )
        with self.assertRaisesRegex(SafeError, "safely") as raised:
            runner.buy(
                quote=valid_quote(),
                recipient={"email": "buyer@example.com"},
                buyer_email="buyer@example.com",
                now=NOW,
            )
        self.assertNotIn("provider-secret-canary", str(raised.exception))
        self.assertEqual(bitrefill.complete_calls, [])
        self.assertEqual(sidecar.pay_calls, [])
        self.assertEqual(store.records, [])

    def test_invoice_cap_and_expiry_are_checked_before_completion_or_payment(self):
        for prepared in (
            valid_prepared(paymentAmount="2.01"),
            valid_prepared(expiresAtEpoch=NOW),
        ):
            runner, bitrefill, sidecar, store, _ = self.make_runner(prepared=prepared)
            with self.subTest(prepared=prepared), self.assertRaises(SafeError):
                runner.buy(
                    quote=valid_quote(),
                    recipient={"email": "buyer@example.com"},
                    buyer_email="buyer@example.com",
                    now=NOW,
                )
            self.assertEqual(bitrefill.complete_calls, [])
            self.assertEqual(sidecar.pay_calls, [])
            self.assertEqual(store.records, [])

    def test_summary_contains_exact_receipt_facts_and_neutral_warning_before_approval(self):
        summaries = []
        store = FakeStore()
        sidecar = FakeSidecar(approved=False, store=store)
        runner = TrezorPocRunner(
            bitrefill=FakeBitrefill(),
            sidecar=sidecar,
            max_usd="2.00",
            summary_sink=summaries.append,
            _test_store=store,
            clock=lambda: NOW,
        )
        with self.assertRaises(SafeError):
            runner.buy(
                quote=valid_quote(),
                recipient={"email": "buyer@example.com"},
                buyer_email="buyer@example.com",
                now=NOW,
            )
        rendered = summaries[0]
        for fact in (
            "Test Gift", "test-gift", "1 USD", "$1.00", "2.000000 USDC",
            "Base Mainnet", "buyer@example.com", str(NOW + 600), "Non-refundable once issued.",
        ):
            self.assertIn(fact, rendered)
        self.assertEqual(len(sidecar.approve_calls), 1)

    def test_callbacks_cannot_mutate_the_approved_purchase_before_prepare(self):
        quote = valid_quote()
        recipient = {"email": "buyer@example.com"}
        store = FakeStore()
        bitrefill = FakeBitrefill()

        def mutate_originals(_summary):
            quote["productId"] = "attacker-product"
            quote["name"] = "Attacker Product"
            quote["packageValue"] = "999 USD"
            quote["priceUsd"] = "999.00"
            recipient["email"] = "attacker@example.com"

        class MutatingSidecar(FakeSidecar):
            def approve_intent(self, intent):
                quote["packageId"] = "attacker-package"
                recipient["email"] = "second-attacker@example.com"
                return super().approve_intent(intent)

        sidecar = MutatingSidecar(store=store)
        runner = TrezorPocRunner(
            bitrefill=bitrefill,
            sidecar=sidecar,
            max_usd="2.00",
            summary_sink=mutate_originals,
            _test_store=store,
            clock=lambda: NOW,
        )

        runner.buy(
            quote=quote,
            recipient=recipient,
            buyer_email="buyer@example.com",
            now=NOW,
        )

        prepared_quote, prepared_recipient, prepared_buyer = bitrefill.prepare_calls[0]
        self.assertEqual(prepared_quote, valid_quote())
        self.assertEqual(prepared_recipient, {"email": "buyer@example.com"})
        self.assertEqual(prepared_buyer, "buyer@example.com")
        self.assertEqual(bitrefill.complete_calls[0][0], valid_quote())

    def test_buyer_email_is_committed_and_reserved_recipient_key_is_rejected(self):
        runner, bitrefill, sidecar, _store, _events = self.make_runner()
        first = runner.build_intent(
            valid_quote(), {"email": "buyer@example.com"}, NOW, buyer_email="buyer@example.com"
        )
        second = runner.build_intent(
            valid_quote(), {"email": "buyer@example.com"}, NOW, buyer_email="other@example.com"
        )
        self.assertNotEqual(first.recipient_hash, second.recipient_hash)
        with self.assertRaisesRegex(SafeError, "Recipient fields"):
            runner.buy(
                quote=valid_quote(requiredRecipientFields=["__sign402_buyer_email__"]),
                recipient={"__sign402_buyer_email__": "collision"},
                buyer_email="buyer@example.com",
                now=NOW,
            )
        self.assertEqual(sidecar.approve_calls, [])
        self.assertEqual(bitrefill.prepare_calls, [])

    def test_public_store_override_rejects_nonfixed_sidecar_store(self):
        with TemporaryDirectory() as temporary:
            parent = Path(temporary) / "state"
            parent.mkdir(mode=0o700)
            store = SidecarStore(parent / "other.db")
            with self.assertRaisesRegex(ValueError, "fixed proof state path"):
                TrezorPocRunner(
                    bitrefill=FakeBitrefill(),
                    sidecar=FakeSidecar(),
                    max_usd="2.00",
                    store=store,
                )

    def test_quote_is_read_only_and_intent_contains_only_recipient_hash(self):
        runner, bitrefill, sidecar, store, events = self.make_runner()
        recipient = {"email": "buyer@example.com"}
        quote = runner.quote(product_id="test-gift", package_id="1", country="US", recipient=recipient)
        intent = runner.build_intent(quote, recipient, NOW)
        self.assertEqual(len(bitrefill.quote_calls), 1)
        self.assertEqual(bitrefill.prepare_calls, [])
        self.assertIsInstance(intent, PurchaseIntent)
        self.assertNotIn("buyer@example.com", repr(intent))
        self.assertEqual(intent.expires_at, NOW + 600)

    def test_duplicate_invoice_never_double_pays_transitions_or_logs(self):
        runner, bitrefill, sidecar, store, events = self.make_runner()
        arguments = {
            "quote": valid_quote(),
            "recipient": {"email": "buyer@example.com"},
            "buyer_email": "buyer@example.com",
            "now": NOW,
        }
        runner.buy(**arguments)
        runner.buy(**arguments)
        self.assertEqual(len(sidecar.pay_calls), 1)
        self.assertEqual(len(store.transitions), 1)
        self.assertEqual(len(store.records), 1)
        self.assertEqual(len(bitrefill.prepare_calls), 2)

    def test_local_transaction_hash_must_match_durable_broadcast_before_completion(self):
        runner, bitrefill, sidecar, store, events = self.make_runner()
        original_pay = sidecar.pay_invoice

        def mismatched(*args, **kwargs):
            result = original_pay(*args, **kwargs)
            store.payment = PaymentView(
                payment_id=store.payment.payment_id,
                intent_id=store.payment.intent_id,
                invoice_id=store.payment.invoice_id,
                state=PaymentState.TX_BROADCAST,
                created_at=store.payment.created_at,
                updated_at=store.payment.updated_at,
                tx_hash="0x" + "cd" * 32,
            )
            return result

        sidecar.pay_invoice = mismatched
        with self.assertRaisesRegex(SafeError, "transaction"):
            runner.buy(
                quote=valid_quote(),
                recipient={"email": "buyer@example.com"},
                buyer_email="buyer@example.com",
                now=NOW,
            )
        self.assertEqual(store.transitions, [])
        self.assertEqual(store.records, [])

    def test_logs_and_persistence_exclude_all_bearer_values_and_recipient(self):
        runner, bitrefill, sidecar, store, events = self.make_runner()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            result = runner.buy(
                quote=valid_quote(),
                recipient={"email": "buyer@example.com"},
                buyer_email="buyer@example.com",
                now=NOW,
            )
        finally:
            root.removeHandler(handler)
        persisted = repr(store.records) + repr(store.transitions)
        logs = stream.getvalue()
        for secret in (
            "buyer@example.com", "invoice-access-canary", "REDEMPTION-CANARY",
            "sidecar-token-canary", "signature-canary", "raw-transaction-canary",
            "payment-link-canary",
        ):
            self.assertNotIn(secret, logs)
            self.assertNotIn(secret, persisted)
        self.assertEqual(result["redemption"]["value"]["code"], "REDEMPTION-CANARY")


class CliTests(TestCase):
    def enabled_env(self):
        return {
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-token",
            "SIGN402_TREZOR_POC_MAX_USD": "2.00",
            "BITREFILL_API_KEY": "bitrefill-key",
        }

    def test_parser_exposes_exact_three_commands_and_only_receipt_selection_flags(self):
        parser = build_parser()
        commands = next(action for action in parser._actions if action.dest == "command")
        self.assertEqual(set(commands.choices), {"pair", "intent-test", "buy"})
        buy = commands.choices["buy"]
        flags = {
            option
            for action in buy._actions
            for option in action.option_strings
            if option != "--help" and option != "-h"
        }
        self.assertEqual(flags, {"--product-id", "--package-id", "--country"})
        forbidden = ("recipient", "private", "calldata", "token", "chain", "destination", "amount")
        self.assertFalse(any(word in flag for flag in flags for word in forbidden))

    def test_intent_test_constructs_no_bitrefill_client(self):
        sidecar = FakeSidecar()
        output = io.StringIO()
        with (
            patch("trezor_sidecar.poc_runner.SidecarClient", return_value=sidecar),
            patch(
                "trezor_sidecar.poc_runner.PreparedAddressBitrefillClient",
                side_effect=AssertionError("Bitrefill client must not be constructed"),
            ),
            patch("trezor_sidecar.poc_runner.time.time", return_value=NOW),
            redirect_stdout(output),
        ):
            environment = self.enabled_env()
            environment.pop("BITREFILL_API_KEY")
            status = main(["intent-test"], env=environment)
        self.assertEqual(status, 0)
        self.assertEqual(len(sidecar.approve_calls), 1)
        self.assertEqual(sidecar.approve_calls[0].intent_id, LOCAL_INTENT_TEST_ID)
        self.assertNotIn("MCP", output.getvalue())
        self.assertNotIn("x402", output.getvalue().lower())

    def test_pair_calls_only_local_sidecar_pairing_route(self):
        sidecar = FakeSidecar()
        with (
            patch("trezor_sidecar.poc_runner.SidecarClient", return_value=sidecar),
            patch(
                "trezor_sidecar.poc_runner.PreparedAddressBitrefillClient",
                side_effect=AssertionError("Bitrefill client must not be constructed"),
            ),
            redirect_stdout(io.StringIO()),
        ):
            environment = self.enabled_env()
            environment.pop("BITREFILL_API_KEY")
            status = main(["pair"], env=environment)
        self.assertEqual(status, 0)
        self.assertEqual(sidecar.pair_calls, 1)

    def test_buy_discovers_and_secret_prompts_recipient_before_hardware_gated_purchase(self):
        events = []
        store = FakeStore()
        sidecar = FakeSidecar(events, store=store)
        bitrefill = FakeBitrefill(events)
        prompts = []

        def secret_prompt(label):
            prompts.append(label)
            return "buyer@example.com"

        output = io.StringIO()
        with (
            patch("trezor_sidecar.poc_runner.SidecarClient", return_value=sidecar),
            patch("trezor_sidecar.poc_runner.PreparedAddressBitrefillClient", return_value=bitrefill),
            patch("trezor_sidecar.poc_runner.SidecarStore", return_value=store),
            patch("trezor_sidecar.poc_runner.getpass.getpass", side_effect=secret_prompt),
            patch("trezor_sidecar.poc_runner.time.time", return_value=NOW),
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            status = main(
                ["buy", "--product-id", "test-gift", "--package-id", "1", "--country", "US"],
                env=self.enabled_env(),
            )

        self.assertEqual(status, 0)
        self.assertEqual(prompts, ["email: "])
        self.assertEqual(events[:3], ["approve-intent", "prepare-purchase", "complete-purchase"])
        self.assertEqual(len(sidecar.pay_calls), 1)
        rendered = output.getvalue()
        self.assertIn("Test Gift", rendered)
        self.assertIn("Invoice: invoice-1", rendered)
        self.assertIn("REDEMPTION-CANARY", rendered)
        self.assertGreater(
            rendered.rindex("Non-refundable once issued."),
            rendered.index("REDEMPTION-CANARY"),
        )
        self.assertIn("Keep redemption details safe.", rendered)
        self.assertNotIn("MCP", rendered)
        self.assertNotIn("x402", rendered.lower())
