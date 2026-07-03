import io
import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError


PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from client import GatewayClient, GatewayClientError  # noqa: E402
from identity import TelegramIdentity  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.requested_size = None

    def read(self, size: int = -1) -> bytes:
        self.requested_size = size
        return self.body if size < 0 else self.body[:size]


class RecordingOpener:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class GatewayClientTests(unittest.TestCase):
    def make_client(self, opener, **kwargs):
        return GatewayClient(
            base_url="http://127.0.0.1:8099",
            api_token="wallet-token-secret-value",
            photon_api_token="photon-token-secret-value",
            opener=opener,
            **kwargs,
        )

    def test_execute_posts_trusted_identity_and_bearer_token(self):
        response = FakeResponse(
            json.dumps({"telegramText": "Wallet 0xabc"}).encode("utf-8")
        )
        opener = RecordingOpener(response=response)
        client = self.make_client(opener)

        result = client.execute(
            "create-wallet",
            TelegramIdentity(
                user_id="1045618308",
                username="AlpskyKnedlik",
                chat_id="ignored-chat",
            ),
        )

        self.assertEqual(result, "Wallet 0xabc")
        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8099/agent/create-wallet",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(timeout, 5.0)
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer wallet-token-secret-value",
        )
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(request.data),
            {
                "telegramUserId": "1045618308",
                "telegramUsername": "AlpskyKnedlik",
            },
        )
        self.assertEqual(response.requested_size, 65537)

    def test_execute_maps_every_operation_to_expected_endpoint(self):
        cases = {
            "wallet": "/agent/wallet",
            "create-wallet": "/agent/create-wallet",
            "balance": "/agent/wallet-balance",
        }

        for operation, path in cases.items():
            with self.subTest(operation=operation):
                opener = RecordingOpener(
                    response=FakeResponse(b'{"telegramText":"ok"}')
                )
                self.make_client(opener).execute(
                    operation,
                    TelegramIdentity(user_id="1045618308"),
                )
                self.assertEqual(
                    opener.requests[0][0].full_url,
                    f"http://127.0.0.1:8099{path}",
                )

    def test_execute_imessage_uses_photon_token_and_payload(self):
        opener = RecordingOpener(response=FakeResponse(b'{"imessageText":"linked"}'))
        client = self.make_client(opener)

        result = client.execute_imessage(
            "link",
            {"code": "ABCDEFGH", "photonUserId": "+15551234567"},
        )

        self.assertEqual(result["imessageText"], "linked")
        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8099/agent/imessage/link",
        )
        self.assertEqual(timeout, 5.0)
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer photon-token-secret-value",
        )
        self.assertEqual(
            json.loads(request.data),
            {"code": "ABCDEFGH", "photonUserId": "+15551234567"},
        )

    def test_execute_imessage_maps_operations_to_expected_endpoints(self):
        cases = {
            "connect-imessage": "/agent/imessage/pairing",
            "test-imessage-approval": "/agent/test-imessage-approval",
            "link": "/agent/imessage/link",
            "pending": "/agent/imessage/pending",
            "decision": "/agent/imessage/decision",
        }

        for operation, path in cases.items():
            with self.subTest(operation=operation):
                opener = RecordingOpener(response=FakeResponse(b'{"ok":true}'))
                self.make_client(opener).execute_imessage(operation, {})
                self.assertEqual(
                    opener.requests[0][0].full_url,
                    f"http://127.0.0.1:8099{path}",
                )

    def test_from_env_reads_independent_photon_api_token(self):
        client = GatewayClient.from_env(
            {
                "SIGN402_GATEWAY_URL": "http://127.0.0.1:8099",
                "SIGN402_WALLET_API_TOKEN": "wallet-token",
                "SIGN402_PHOTON_API_TOKEN": "photon-token",
            }
        )

        self.assertEqual(client.api_token, "wallet-token")
        self.assertEqual(client.photon_api_token, "photon-token")

    def test_execute_imessage_requires_photon_api_token(self):
        opener = RecordingOpener(response=FakeResponse(b'{"ok":true}'))
        client = GatewayClient(
            base_url="http://127.0.0.1:8099",
            api_token="wallet-token",
            photon_api_token="",
            opener=opener,
        )

        with self.assertRaises(GatewayClientError) as caught:
            client.execute_imessage("pending", {"photonUserId": "+15551234567"})

        self.assertIn("not configured", caught.exception.user_message)
        self.assertEqual(opener.requests, [])

    def test_execute_paid_tool_posts_buy_tool_with_long_timeout(self):
        opener = RecordingOpener(
            response=FakeResponse(
                json.dumps({"telegramText": "Crypto News unlocked."}).encode("utf-8")
            )
        )
        client = self.make_client(opener, purchase_timeout=180.0)

        result = client.execute_paid_tool(
            "news",
            TelegramIdentity(user_id="1045618308", username="AlpskyKnedlik"),
        )

        self.assertEqual(result, "Crypto News unlocked.")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8099/agent/buy-tool")
        self.assertEqual(timeout, 180.0)
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer wallet-token-secret-value",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "tool": "news",
                "telegramUserId": "1045618308",
                "telegramUsername": "AlpskyKnedlik",
            },
        )

    def test_execute_omits_missing_username(self):
        opener = RecordingOpener(response=FakeResponse(b'{"telegramText":"ok"}'))

        self.make_client(opener).execute(
            "wallet",
            TelegramIdentity(user_id="1045618308"),
        )

        self.assertEqual(
            json.loads(opener.requests[0][0].data),
            {"telegramUserId": "1045618308"},
        )

    def test_execute_last_purchase_posts_last_purchase_endpoint(self):
        opener = RecordingOpener(response=FakeResponse(b'{"telegramText":"latest"}'))

        result = self.make_client(opener).execute(
            "last-purchase",
            TelegramIdentity(user_id="1045618308"),
        )

        request, _timeout = opener.requests[0]
        self.assertEqual(result, "latest")
        self.assertEqual(request.full_url, "http://127.0.0.1:8099/agent/last-purchase")
        self.assertEqual(json.loads(request.data), {"telegramUserId": "1045618308"})

    def test_from_env_requires_gateway_url_and_token(self):
        with self.assertRaises(GatewayClientError) as missing_all:
            GatewayClient.from_env({})
        with self.assertRaises(GatewayClientError) as missing_token:
            GatewayClient.from_env(
                {"SIGN402_GATEWAY_URL": "http://127.0.0.1:8099"}
            )

        self.assertIn("not configured", missing_all.exception.user_message)
        self.assertIn("not configured", missing_token.exception.user_message)

    def test_from_env_strips_trailing_slash(self):
        client = GatewayClient.from_env(
            {
                "SIGN402_GATEWAY_URL": "http://127.0.0.1:8099/",
                "SIGN402_WALLET_API_TOKEN": "token",
            }
        )

        self.assertEqual(client.base_url, "http://127.0.0.1:8099")

    def test_from_env_rejects_non_loopback_gateway_url(self):
        with self.assertRaises(GatewayClientError) as caught:
            GatewayClient.from_env(
                {
                    "SIGN402_GATEWAY_URL": "https://gateway.example.com",
                    "SIGN402_WALLET_API_TOKEN": "token",
                }
            )

        self.assertIn("localhost", caught.exception.user_message)

    def test_authorization_error_returns_safe_message(self):
        upstream_body = b'{"error":"bad secret wallet-token-secret-value"}'
        error_stream = io.BytesIO(upstream_body)
        opener = RecordingOpener(
            error=HTTPError(
                "http://127.0.0.1:8099/agent/wallet",
                401,
                "Unauthorized",
                {},
                error_stream,
            )
        )

        with self.assertLogs("client", level="WARNING"):
            with self.assertRaises(GatewayClientError) as caught:
                self.make_client(opener).execute(
                    "wallet",
                    TelegramIdentity(user_id="1045618308"),
                )

        message = caught.exception.user_message
        self.assertIn("authentication failed", message)
        self.assertNotIn("wallet-token", message)
        self.assertNotIn("bad secret", message)
        self.assertTrue(error_stream.closed)

    def test_connection_failures_return_temporarily_unavailable(self):
        for error in (TimeoutError("slow"), URLError("offline")):
            with self.subTest(error=type(error).__name__):
                opener = RecordingOpener(error=error)
                with self.assertLogs("client", level="WARNING"):
                    with self.assertRaises(GatewayClientError) as caught:
                        self.make_client(opener).execute(
                            "wallet",
                            TelegramIdentity(user_id="1045618308"),
                        )
                self.assertIn(
                    "temporarily unavailable",
                    caught.exception.user_message,
                )
                self.assertNotIn(str(error), caught.exception.user_message)

    def test_rejects_invalid_or_unsafe_success_responses(self):
        cases = (
            b"not-json",
            b"[]",
            b'{"ok":true}',
            b'{"telegramText":""}',
            b'{"telegramText":42}',
        )

        for body in cases:
            with self.subTest(body=body):
                opener = RecordingOpener(response=FakeResponse(body))
                with self.assertRaises(GatewayClientError) as caught:
                    self.make_client(opener).execute(
                        "wallet",
                        TelegramIdentity(user_id="1045618308"),
                    )
                self.assertIn("invalid response", caught.exception.user_message)

    def test_rejects_oversized_response(self):
        opener = RecordingOpener(response=FakeResponse(b"x" * 65537))

        with self.assertLogs("client", level="WARNING"):
            with self.assertRaises(GatewayClientError) as caught:
                self.make_client(opener).execute(
                    "wallet",
                    TelegramIdentity(user_id="1045618308"),
                )

        self.assertIn("invalid response", caught.exception.user_message)

    def test_rejects_unknown_operation_without_sending_request(self):
        opener = RecordingOpener(response=FakeResponse(b'{"telegramText":"ok"}'))

        with self.assertRaises(GatewayClientError) as caught:
            self.make_client(opener).execute(
                "delete-wallet",
                TelegramIdentity(user_id="1045618308"),
            )

        self.assertIn("not supported", caught.exception.user_message)
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
