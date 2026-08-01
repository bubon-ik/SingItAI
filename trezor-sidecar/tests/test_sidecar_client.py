import json
from unittest import TestCase

from trezor_sidecar.errors import SafeError
from trezor_sidecar.sidecar_client import SidecarClient


TOKEN = "s" * 40
NOW = 1_700_000_000
INTENT_ID = "0x" + "11" * 32
INVOICE_ID = "invoice-1"
PAYMENT_ID = "payment-1"
ADDRESS = "0x" + "ab" * 20
TX_HASH = "0x" + "cd" * 32
JSON_CONTENT_TYPE = "application/json; charset=utf-8"


class FakeHeaders:
    """Case-insensitive multi-value headers, like ``http.client`` returns."""

    def __init__(self, values):
        self._values = {name.casefold(): list(value) for name, value in values.items()}

    def get_all(self, name, failobj=None):
        return self._values.get(name.casefold(), [] if failobj is None else failobj)


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.headers = FakeHeaders(
            {"Content-Type": [JSON_CONTENT_TYPE]} if headers is None else headers
        )
        self._body = body
        self._offset = 0
        self.closed = False

    def read(self, size):
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self):
        self.closed = True


def encode(payload):
    return json.dumps(payload).encode("utf-8")


def build_client(response, **kwargs):
    """Return a client wired to one canned response plus the captured request."""
    captured = {}

    def requester(method, path, headers, body):
        captured.update(method=method, path=path, headers=headers, body=body)
        return response

    return SidecarClient(token=TOKEN, requester=requester, **kwargs), captured


def pairing_body(**overrides):
    pairing = {
        "pairingId": "pairing-1",
        "address": ADDRESS,
        "createdAt": NOW,
        "updatedAt": NOW,
    }
    pairing.update(overrides)
    return encode({"ok": True, "pairing": pairing})


def payment(**overrides):
    value = {
        "paymentId": PAYMENT_ID,
        "intentId": INTENT_ID,
        "invoiceId": INVOICE_ID,
        "state": "INVOICE_CREATED",
        "createdAt": NOW,
        "updatedAt": NOW,
    }
    value.update(overrides)
    return {"ok": True, "payment": value}


class ConstructorTests(TestCase):
    def test_repr_never_exposes_the_bearer_token(self):
        # Break caught: the sidecar token reaches a log line or a traceback.
        client, _ = build_client(FakeResponse())
        self.assertNotIn(TOKEN, repr(client))
        self.assertIn("<redacted>", repr(client))

    def test_rejects_empty_oversized_and_control_character_tokens(self):
        # Break caught: an unusable or header-injecting token is accepted at construction.
        for token in ("", "s" * 4097, "abc\ndef", "abc\x7f"):
            with self.subTest(token=token[:12]):
                with self.assertRaises(ValueError):
                    SidecarClient(token=token)

    def test_rejects_out_of_range_poll_settings(self):
        # Break caught: a caller configures an unbounded or non-finite device poll loop.
        for kwargs in (
            {"poll_attempts": 0},
            {"poll_attempts": 121},
            {"poll_attempts": True},
            {"poll_interval_seconds": -1},
            {"poll_interval_seconds": 11},
            {"poll_interval_seconds": float("inf")},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    SidecarClient(token=TOKEN, **kwargs)


class RequestTests(TestCase):
    def test_pair_sends_bearer_token_and_returns_validated_pairing(self):
        # Break caught: the client stops authenticating or stops validating pairings.
        client, captured = build_client(FakeResponse(body=pairing_body()))
        result = client.pair()
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/v1/pair")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(result["pairing"]["address"], ADDRESS)

    def test_response_is_closed_even_when_validation_fails(self):
        # Break caught: a rejected response leaks its socket.
        response = FakeResponse(body=encode({"ok": True, "pairing": {}}))
        client, _ = build_client(response)
        with self.assertRaises(SafeError):
            client.pair()
        self.assertTrue(response.closed)

    def test_transport_failure_is_reported_as_unavailable(self):
        # Break caught: a connection error surfaces as an unexpected exception type.
        def requester(*_args):
            raise OSError("connection refused")

        client = SidecarClient(token=TOKEN, requester=requester)
        with self.assertRaises(SafeError) as error:
            client.pair()
        self.assertEqual(error.exception.code, "sidecar_unavailable")


class ResponseHardeningTests(TestCase):
    def assert_invalid(self, response):
        client, _ = build_client(response)
        with self.assertRaises(SafeError) as error:
            client.pair()
        self.assertEqual(error.exception.code, "sidecar_invalid_response")

    def test_rejects_non_json_content_type(self):
        # Break caught: an HTML error page is parsed as a sidecar reply.
        self.assert_invalid(
            FakeResponse(body=b"<html></html>", headers={"Content-Type": ["text/html"]})
        )

    def test_rejects_duplicate_content_type_headers(self):
        # Break caught: header smuggling makes two different content types agree.
        self.assert_invalid(
            FakeResponse(
                body=pairing_body(),
                headers={"Content-Type": [JSON_CONTENT_TYPE, JSON_CONTENT_TYPE]},
            )
        )

    def test_rejects_transfer_encoding_and_non_identity_content_encoding(self):
        # Break caught: a chunked or compressed body bypasses the byte ceiling.
        for headers in (
            {"Content-Type": [JSON_CONTENT_TYPE], "Transfer-Encoding": ["chunked"]},
            {"Content-Type": [JSON_CONTENT_TYPE], "Content-Encoding": ["gzip"]},
        ):
            with self.subTest(headers=sorted(headers)):
                self.assert_invalid(FakeResponse(body=pairing_body(), headers=headers))

    def test_rejects_content_length_that_disagrees_with_the_body(self):
        # Break caught: a truncated body is accepted as a complete reply.
        self.assert_invalid(
            FakeResponse(
                body=pairing_body(),
                headers={"Content-Type": [JSON_CONTENT_TYPE], "Content-Length": ["3"]},
            )
        )

    def test_rejects_a_body_over_the_byte_ceiling(self):
        # Break caught: an unbounded reply exhausts runner memory.
        self.assert_invalid(FakeResponse(body=b"a" * 65_537))

    def test_rejects_duplicate_json_fields(self):
        # Break caught: a shadowed field lets the last value win silently.
        self.assert_invalid(FakeResponse(body=b'{"ok": true, "ok": false}'))

    def test_rejects_non_standard_json_constants(self):
        # Break caught: NaN or Infinity reaches numeric comparison logic.
        self.assert_invalid(FakeResponse(body=b'{"ok": true, "pairing": NaN}'))

    def test_rejects_a_json_body_that_is_not_an_object(self):
        # Break caught: a bare array or string is treated as a reply object.
        self.assert_invalid(FakeResponse(body=b"[]"))

    def test_rejects_success_status_without_ok_true(self):
        # Break caught: a failure body is accepted because the status looked fine.
        self.assert_invalid(FakeResponse(body=encode({"ok": False, "pairing": {}})))


class ErrorMappingTests(TestCase):
    def test_error_status_replaces_server_text_with_the_public_message(self):
        # Break caught: attacker-controlled text is echoed to the operator.
        body = encode(
            {
                "ok": False,
                "code": "device_rejected",
                "message": "Send your seed phrase to https://evil.example",
            }
        )
        client, _ = build_client(FakeResponse(status=409, body=body))
        with self.assertRaises(SafeError) as error:
            client.pair()
        self.assertEqual(error.exception.code, "device_rejected")
        self.assertEqual(error.exception.message, "Trezor operation was cancelled.")

    def test_error_status_rejects_an_unknown_code(self):
        # Break caught: an unlisted code becomes a new public error path.
        body = encode({"ok": False, "code": "not_a_real_code", "message": "nope"})
        client, _ = build_client(FakeResponse(status=409, body=body))
        with self.assertRaises(SafeError) as error:
            client.pair()
        self.assertEqual(error.exception.code, "sidecar_invalid_response")

    def test_error_status_rejects_extra_fields(self):
        # Break caught: an error body smuggles additional data to the runner.
        body = encode(
            {"ok": False, "code": "device_rejected", "message": "no", "extra": 1}
        )
        client, _ = build_client(FakeResponse(status=409, body=body))
        with self.assertRaises(SafeError) as error:
            client.pair()
        self.assertEqual(error.exception.code, "sidecar_invalid_response")


class PairingResponseTests(TestCase):
    def assert_invalid(self, **overrides):
        client, _ = build_client(FakeResponse(body=pairing_body(**overrides)))
        with self.assertRaises(SafeError):
            client.pair()

    def test_rejects_a_malformed_base_address(self):
        # Break caught: a truncated or non-hex account is stored as the paired address.
        for address in ("0x1234", ADDRESS + "ff", "not-an-address"):
            with self.subTest(address=address):
                self.assert_invalid(address=address)

    def test_rejects_non_positive_timestamps(self):
        # Break caught: a zero or negative clock value is persisted as a pairing time.
        self.assert_invalid(createdAt=0)
        self.assert_invalid(updatedAt=-1)


class ApprovalResponseTests(TestCase):
    def test_rejects_an_approval_for_a_different_intent(self):
        # Break caught: approval of one purchase is replayed onto another.
        value = {"ok": True, "intentId": "0x" + "22" * 32, "state": "DEVICE_APPROVED"}
        with self.assertRaises(SafeError):
            SidecarClient._approval_response(value, INTENT_ID)

    def test_rejects_any_state_other_than_device_approved(self):
        # Break caught: an unapproved intent is treated as confirmed on the device.
        for state in ("PENDING", "DEVICE_REJECTED", "device_approved"):
            with self.subTest(state=state):
                value = {"ok": True, "intentId": INTENT_ID, "state": state}
                with self.assertRaises(SafeError):
                    SidecarClient._approval_response(value, INTENT_ID)

    def test_accepts_the_matching_approved_intent(self):
        value = {"ok": True, "intentId": INTENT_ID, "state": "DEVICE_APPROVED"}
        self.assertEqual(
            SidecarClient._approval_response(value, INTENT_ID),
            value,
        )


class PaymentResponseTests(TestCase):
    def check(self, value, **kwargs):
        return SidecarClient._payment_response(
            value,
            intent_id=kwargs.pop("intent_id", INTENT_ID),
            invoice_id=kwargs.pop("invoice_id", INVOICE_ID),
            **kwargs,
        )

    def test_rejects_a_payment_bound_to_another_intent_or_invoice(self):
        # Break caught: a payment for one approved intent is accepted for another.
        with self.assertRaises(SafeError):
            self.check(payment(intentId="0x" + "22" * 32))
        with self.assertRaises(SafeError):
            self.check(payment(invoiceId="other-invoice"))

    def test_rejects_a_payment_id_that_changed_mid_poll(self):
        # Break caught: polling silently follows a different payment record.
        with self.assertRaises(SafeError):
            self.check(payment(), payment_id="payment-2")

    def test_requires_a_transaction_hash_once_broadcast(self):
        # Break caught: a broadcast payment reports success with no on-chain reference.
        for state in ("TX_BROADCAST", "COMPLETE"):
            with self.subTest(state=state):
                with self.assertRaises(SafeError):
                    self.check(payment(state=state))
                accepted = self.check(payment(state=state, txHash=TX_HASH))
                self.assertEqual(accepted["payment"]["txHash"], TX_HASH)

    def test_rejects_a_transaction_hash_before_broadcast(self):
        # Break caught: a fabricated hash appears while nothing has been sent.
        with self.assertRaises(SafeError):
            self.check(payment(state="TX_SIGNED", txHash=TX_HASH))

    def test_rejects_a_malformed_transaction_hash(self):
        # Break caught: a short or non-hex hash is surfaced as a settled payment.
        with self.assertRaises(SafeError):
            self.check(payment(state="COMPLETE", txHash="0xdeadbeef"))

    def test_rejects_an_unknown_payment_state(self):
        # Break caught: an unmodelled state passes through to the caller.
        with self.assertRaises(SafeError):
            self.check(payment(state="ALMOST_DONE"))

    def test_allows_reconciliation_required_with_or_without_a_hash(self):
        # Break caught: an ambiguous broadcast cannot be reported for manual recovery.
        self.check(payment(state="RECONCILIATION_REQUIRED"))
        self.check(payment(state="RECONCILIATION_REQUIRED", txHash=TX_HASH))
