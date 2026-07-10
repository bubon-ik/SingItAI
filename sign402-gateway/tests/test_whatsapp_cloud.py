import io
import json
import unittest
from urllib.error import HTTPError, URLError

from sign402_gateway.whatsapp_cloud import MetaWhatsAppTemplateNotifier


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.closed = False

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class RecordingOpener:
    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None):
        self.response = response or FakeResponse(b'{"messages":[{"id":"wamid.123"}]}')
        self.error = error
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class MetaWhatsAppTemplateNotifierTests(unittest.TestCase):
    def make_notifier(self, opener):
        return MetaWhatsAppTemplateNotifier(
            access_token="test-system-user-token",
            phone_number_id="1246376715225104",
            template_name="sign402_payment_approval",
            template_language="en_US",
            graph_api_version="v25.0",
            opener=opener,
            timeout=7.5,
        )

    def test_send_approval_posts_template_with_bound_quick_reply_payloads(self):
        opener = RecordingOpener()
        notifier = self.make_notifier(opener)

        result = notifier.send_approval(
            wa_id="420777111222",
            approval_id="approval-123",
            context_lines=["Merchant: Bitrefill", "Amount: 10 USDC"],
            expires_at=1_800_000_600,
        )

        self.assertEqual(result, {"ok": True, "messageId": "wamid.123"})
        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://graph.facebook.com/v25.0/1246376715225104/messages",
        )
        self.assertEqual(timeout, 7.5)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-system-user-token")
        payload = json.loads(request.data)
        self.assertEqual(payload["messaging_product"], "whatsapp")
        self.assertEqual(payload["to"], "420777111222")
        self.assertEqual(payload["type"], "template")
        self.assertEqual(payload["template"]["name"], "sign402_payment_approval")
        components = payload["template"]["components"]
        self.assertEqual(components[0]["type"], "body")
        self.assertEqual(
            components[0]["parameters"][0]["text"],
            "Merchant: Bitrefill\nAmount: 10 USDC",
        )
        self.assertEqual(
            components[-2]["parameters"][0]["payload"],
            "sign402:approve:approval-123",
        )
        self.assertEqual(
            components[-1]["parameters"][0]["payload"],
            "sign402:reject:approval-123",
        )
        self.assertTrue(opener.response.closed)

    def test_send_approval_rejects_invalid_values_before_http(self):
        cases = (
            {"wa_id": "+420777111222"},
            {"approval_id": "short"},
            {"context_lines": []},
            {"expires_at": 0},
        )
        for override in cases:
            with self.subTest(override=override):
                opener = RecordingOpener()
                notifier = self.make_notifier(opener)
                kwargs = {
                    "wa_id": "420777111222",
                    "approval_id": "approval-123",
                    "context_lines": ["Amount: 10 USDC"],
                    "expires_at": 1_800_000_600,
                }
                kwargs.update(override)

                result = notifier.send_approval(**kwargs)

                self.assertEqual(result, {"ok": False, "error": "invalid_request"})
                self.assertEqual(opener.requests, [])

    def test_send_approval_returns_fixed_transport_error_without_secret(self):
        opener = RecordingOpener(error=URLError("test-system-user-token leaked"))
        notifier = self.make_notifier(opener)

        result = notifier.send_approval(
            wa_id="420777111222",
            approval_id="approval-123",
            context_lines=["Amount: 10 USDC"],
            expires_at=1_800_000_600,
        )

        self.assertEqual(result, {"ok": False, "error": "transport_error"})
        self.assertNotIn("test-system-user-token", repr(result))

    def test_send_approval_returns_fixed_http_and_response_errors(self):
        http_error = HTTPError(
            "https://graph.facebook.com/redacted",
            400,
            "bad request",
            {},
            io.BytesIO(b'{"error":{"message":"test-system-user-token"}}'),
        )
        cases = (
            (RecordingOpener(error=http_error), "http_error"),
            (RecordingOpener(response=FakeResponse(b"not-json")), "invalid_response"),
            (RecordingOpener(response=FakeResponse(b'{"messages":[]}')), "invalid_response"),
        )
        for opener, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                notifier = self.make_notifier(opener)
                result = notifier.send_approval(
                    wa_id="420777111222",
                    approval_id="approval-123",
                    context_lines=["Amount: 10 USDC"],
                    expires_at=1_800_000_600,
                )
                self.assertEqual(result, {"ok": False, "error": expected_error})
                self.assertNotIn("test-system-user-token", repr(result))
        self.assertTrue(http_error.fp.closed)


if __name__ == "__main__":
    unittest.main()
