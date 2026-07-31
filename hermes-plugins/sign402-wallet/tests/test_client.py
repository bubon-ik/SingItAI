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

    def test_execute_approval_uses_independent_token_and_generic_payload(self):
        opener = RecordingOpener(response=FakeResponse(b'{"imessageText":"linked"}'))
        client = self.make_client(opener)

        result = client.execute_approval(
            "link",
            {
                "code": "ABCDEFGH",
                "approvalUserId": "420777111222",
                "channel": "whatsapp",
            },
        )

        self.assertEqual(result["imessageText"], "linked")
        request, _timeout = opener.requests[0]
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer photon-token-secret-value",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "code": "ABCDEFGH",
                "approvalUserId": "420777111222",
                "channel": "whatsapp",
            },
        )

    def test_execute_imessage_maps_operations_to_expected_endpoints(self):
        cases = {
            "connect-imessage": "/agent/imessage/pairing",
            "select-existing": "/agent/approval-channel/select-existing",
            "link": "/agent/imessage/link",
            "pending": "/agent/imessage/pending",
            "decision": "/agent/imessage/decision",
            "unlink": "/agent/imessage/unlink",
        }

        for operation, path in cases.items():
            with self.subTest(operation=operation):
                opener = RecordingOpener(response=FakeResponse(b'{"ok":true}'))
                self.make_client(opener).execute_imessage(operation, {})
                self.assertEqual(
                    opener.requests[0][0].full_url,
                    f"http://127.0.0.1:8099{path}",
                )

    def test_execute_imessage_surfaces_safe_gateway_text(self):
        error = HTTPError(
            "http://127.0.0.1:8099/agent/imessage/link",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "ok": False,
                        "imessageText": (
                            "This iMessage number is already linked. "
                            "Ask the operator to unlink it, then try again."
                        ),
                    }
                ).encode("utf-8")
            ),
        )
        opener = RecordingOpener(error=error)

        with self.assertRaises(GatewayClientError) as caught:
            self.make_client(opener).execute_imessage(
                "link",
                {"code": "ABCDEFGH", "photonUserId": "+15551234567"},
            )

        self.assertEqual(
            caught.exception.user_message,
            "This iMessage number is already linked. "
            "Ask the operator to unlink it, then try again.",
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

    def test_withdraw_tokens_posts_user_token_endpoint(self):
        opener = RecordingOpener(
            response=FakeResponse(b'{"ok":true,"tokens":[{"symbol":"SINGIT"}]}')
        )

        result = self.make_client(opener).withdraw_tokens(
            TelegramIdentity(user_id="1045618308"),
            user_access_token="user-token",
        )

        request, _timeout = opener.requests[0]
        self.assertEqual(result["tokens"][0]["symbol"], "SINGIT")
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8099/agent/withdraw/tokens",
        )
        self.assertEqual(request.get_header("X-sign402-user-token"), "user-token")
        self.assertEqual(json.loads(request.data), {"telegramUserId": "1045618308"})

    def test_execute_withdrawal_posts_user_token_endpoint(self):
        opener = RecordingOpener(
            response=FakeResponse(b'{"ok":true,"telegramText":"Withdrawal sent."}')
        )

        result = self.make_client(opener, purchase_timeout=180.0).execute_withdrawal(
            TelegramIdentity(user_id="1045618308"),
            token_address="0x" + "1" * 40,
            amount="10",
            to_address="0x" + "2" * 40,
            user_access_token="user-token",
        )

        request, timeout = opener.requests[0]
        self.assertEqual(result, "Withdrawal sent.")
        self.assertEqual(timeout, 180.0)
        self.assertEqual(request.full_url, "http://127.0.0.1:8099/agent/withdraw")
        self.assertEqual(request.get_header("X-sign402-user-token"), "user-token")
        self.assertEqual(
            json.loads(request.data),
            {
                "telegramUserId": "1045618308",
                "tokenAddress": "0x" + "1" * 40,
                "amount": "10",
                "toAddress": "0x" + "2" * 40,
            },
        )

    def test_execute_bitrefill_purchase_quotes_then_buys_with_user_token(self):
        responses = [
            FakeResponse(b'{"ok":true,"quoteId":"quote_1"}'),
            FakeResponse(b'{"ok":true,"telegramText":"Bitrefill delivered."}'),
        ]
        opener = RecordingOpener()

        def open_next(request, timeout):
            opener.requests.append((request, timeout))
            return responses.pop(0)

        client = self.make_client(open_next)

        result = client.execute_bitrefill_purchase(
            TelegramIdentity(user_id="1045618308", username="AlpskyKnedlik"),
            product_id="test-gift-card-link",
            package_id="1",
            country="US",
            recipient={},
            payment_token={
                "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "symbol": "USDC",
                "decimals": 6,
                "native": False,
            },
            user_access_token="user-token-1",
        )

        self.assertEqual(result, "Bitrefill delivered.")
        quote_request, quote_timeout = opener.requests[0]
        buy_request, buy_timeout = opener.requests[1]
        self.assertEqual(
            quote_request.full_url,
            "http://127.0.0.1:8099/agent/quote-bitrefill",
        )
        self.assertEqual(quote_timeout, 180.0)
        self.assertEqual(
            json.loads(quote_request.data),
            {
                "productId": "test-gift-card-link",
                "packageId": "1",
                "country": "US",
                "recipient": {},
                "paymentToken": {
                    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "symbol": "USDC",
                    "decimals": 6,
                    "native": False,
                },
                "telegramUserId": "1045618308",
                "telegramUsername": "AlpskyKnedlik",
            },
        )
        self.assertEqual(
            quote_request.get_header("X-sign402-user-token"),
            "user-token-1",
        )
        self.assertEqual(
            buy_request.full_url,
            "http://127.0.0.1:8099/agent/buy-wallet-bitrefill",
        )
        self.assertEqual(buy_timeout, 180.0)
        self.assertEqual(
            json.loads(buy_request.data),
            {
                "quoteId": "quote_1",
                "recipient": {},
                "telegramUserId": "1045618308",
                "telegramUsername": "AlpskyKnedlik",
            },
        )
        self.assertEqual(
            buy_request.get_header("X-sign402-user-token"),
            "user-token-1",
        )

    def test_execute_bitrefill_purchase_surfaces_safe_quote_errors(self):
        error = HTTPError(
            "http://127.0.0.1:8099/agent/quote-bitrefill",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"ok":false,"error":"unknown Bitrefill package"}'),
        )
        opener = RecordingOpener(error=error)

        with self.assertRaises(GatewayClientError) as raised:
            self.make_client(opener).execute_bitrefill_purchase(
                TelegramIdentity(user_id="1045618308"),
                product_id="test-gift-card-link",
                package_id="0.1",
                country="US",
                payment_token={
                    "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "symbol": "USDC",
                    "decimals": 6,
                },
                user_access_token="user-token-1",
            )

        self.assertEqual(
            raised.exception.user_message,
            "Bitrefill request failed: unknown Bitrefill package",
        )

    def test_execute_bitrefill_purchase_translates_live_max_errors(self):
        error = HTTPError(
            "http://127.0.0.1:8099/agent/quote-bitrefill",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                b'{"ok":false,"error":"Bitrefill quote exceeds live Bitrefill max $5.00"}'
            ),
        )
        opener = RecordingOpener(error=error)

        with self.assertRaises(GatewayClientError) as raised:
            self.make_client(opener).execute_bitrefill_purchase(
                TelegramIdentity(user_id="1045618308"),
                product_id="doordash-us",
                package_id="15",
                country="US",
                payment_token={
                    "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "symbol": "USDC",
                    "decimals": 6,
                },
                user_access_token="user-token-1",
            )

        self.assertEqual(
            raised.exception.user_message,
            "This product exceeds the Bitrefill product maximum ($5.00), "
            "which is separate from your wallet limits. Choose a smaller "
            "product or ask the operator to raise the Bitrefill limit.",
        )

    def test_execute_bitrefill_purchase_leads_with_the_limit_fix(self):
        # The buyer's first line has to be what to do, not "request failed".
        error = HTTPError(
            "http://127.0.0.1:8099/agent/buy-wallet-bitrefill",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                b'{"ok":false,"error":"Raise your spending limit to continue. '
                b'This purchase needs 116.4328 USDC, but your limit is 50 USDC '
                b'per transaction."}'
            ),
        )
        opener = RecordingOpener(error=error)

        with self.assertRaises(GatewayClientError) as raised:
            self.make_client(opener).execute_bitrefill_purchase(
                TelegramIdentity(user_id="1045618308"),
                product_id="alza-czech-republic",
                package_id="100",
                country="CZ",
                payment_token={
                    "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "symbol": "USDC",
                    "decimals": 6,
                },
                user_access_token="user-token-1",
            )

        message = raised.exception.user_message
        self.assertTrue(message.startswith("Raise your spending limit"))
        self.assertNotIn("Bitrefill request failed", message)
        self.assertIn("116.4328 USDC", message)
        self.assertIn("/set_limits", message)

    def test_execute_bitrefill_purchase_explains_a_busy_approval_channel(self):
        # `firefly_busy` is an internal code for "someone else is mid-approval".
        # The buyer needs to know it is transient and cost them nothing.
        error = HTTPError(
            "http://127.0.0.1:8099/agent/buy-wallet-bitrefill",
            409,
            "Conflict",
            {},
            io.BytesIO(
                b'{"approved":false,"error":"firefly_busy",'
                b'"message":"Firefly is already handling another approval request."}'
            ),
        )
        opener = RecordingOpener(error=error)

        with self.assertRaises(GatewayClientError) as raised:
            self.make_client(opener).execute_bitrefill_purchase(
                TelegramIdentity(user_id="1045618308"),
                product_id="carrefour-argentina",
                package_id="50",
                country="AR",
                payment_token={
                    "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "symbol": "USDC",
                    "decimals": 6,
                },
                user_access_token="user-token-1",
            )

        message = raised.exception.user_message
        self.assertNotIn("firefly", message.casefold())
        self.assertIn("Nothing was charged", message)
        self.assertIn("again", message)

    def test_execute_bitrefill_purchase_hides_upstream_stack_traces(self):
        error = HTTPError(
            "http://127.0.0.1:8099/agent/quote-bitrefill",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "APIError: Invalid request.\n"
                            "    at cdpApiClient "
                            "(file:///home/hermes/apps/sign402/node_modules/"
                            "@coinbase/cdp-sdk/cdpApiClient.js:105:23)"
                        ),
                    }
                ).encode("utf-8")
            ),
        )
        opener = RecordingOpener(error=error)

        with self.assertRaises(GatewayClientError) as raised:
            self.make_client(opener).execute_bitrefill_purchase(
                TelegramIdentity(user_id="1045618308"),
                product_id="test-gift-card-link",
                package_id="1",
                country="US",
                payment_token={
                    "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "symbol": "USDC",
                    "decimals": 6,
                },
                user_access_token="user-token-1",
            )

        self.assertEqual(
            raised.exception.user_message,
            "Bitrefill request failed. Please try another token or amount.",
        )
        self.assertNotIn("node_modules", raised.exception.user_message)
        self.assertNotIn("APIError", raised.exception.user_message)

    def test_search_bitrefill_products_posts_country_query(self):
        opener = RecordingOpener(
            response=FakeResponse(b'{"ok":true,"products":[]}')
        )

        result = self.make_client(opener).search_bitrefill_products(
            query="amazon",
            country="cz",
            include_test_products=False,
        )

        request, timeout = opener.requests[0]
        self.assertTrue(result["ok"])
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8099/agent/search-bitrefill",
        )
        self.assertEqual(timeout, 180.0)
        self.assertEqual(
            json.loads(request.data),
            {
                "query": "amazon",
                "country": "CZ",
                "searchAllCountries": True,
                "includeTestProducts": False,
            },
        )

    def test_list_bitrefill_products_posts_catalog_payload(self):
        opener = RecordingOpener(
            response=FakeResponse(
                b'{"ok":true,"products":[],"start":8,"limit":8,"hasNext":false}'
            )
        )

        result = self.make_client(opener).list_bitrefill_products(
            country="cz",
            category="food",
            start=8,
            limit=8,
            include_international=True,
            include_test_products=False,
        )

        request, timeout = opener.requests[0]
        self.assertTrue(result["ok"])
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8099/agent/list-bitrefill-products",
        )
        self.assertEqual(timeout, 180.0)
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer wallet-token-secret-value",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "country": "CZ",
                "category": "food",
                "start": 8,
                "limit": 8,
                "includeInternational": True,
                "includeTestProducts": False,
            },
        )

    def test_get_bitrefill_product_posts_country_product_id(self):
        opener = RecordingOpener(
            response=FakeResponse(b'{"ok":true,"productId":"amazon-cz"}')
        )

        result = self.make_client(opener).get_bitrefill_product(
            product_id="amazon-cz",
            country="cz",
        )

        request, timeout = opener.requests[0]
        self.assertEqual(result["productId"], "amazon-cz")
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8099/agent/get-bitrefill-product",
        )
        self.assertEqual(timeout, 180.0)
        self.assertEqual(
            json.loads(request.data),
            {"productId": "amazon-cz", "country": "CZ"},
        )

    def test_execute_spending_limits_shows_current_limits(self):
        message = (
            "Current spending limits.\n\n"
            "Your spending limits:\n"
            "- Max per transaction: 50 USDC\n"
            "- Daily cap: 1000 USDC\n\n"
            "Platform maximums:\n"
            "- Max per transaction: 1020 USDC\n"
            "- Daily cap: 5000 USDC\n\n"
            "Bitrefill product maximum: 1000 USD before the 2% service fee.\n"
            "Service fees count toward your spending limits.\n"
            "The lowest applicable limit wins.\n\n"
            "To change: /limits <per-transaction> <daily>"
        )
        opener = RecordingOpener(
            response=FakeResponse(json.dumps({"telegramText": message}).encode("utf-8"))
        )

        result = self.make_client(opener).execute_spending_limits(
            TelegramIdentity(user_id="1045618308"),
            user_access_token="user-token-1",
        )

        request, timeout = opener.requests[0]
        self.assertEqual(result, message)
        self.assertEqual(request.full_url, "http://127.0.0.1:8099/agent/spending-limits")
        self.assertEqual(timeout, 5.0)
        self.assertEqual(json.loads(request.data), {"telegramUserId": "1045618308"})
        self.assertEqual(request.get_header("X-sign402-user-token"), "user-token-1")

    def test_execute_spending_limits_posts_requested_limits(self):
        opener = RecordingOpener(
            response=FakeResponse(b'{"telegramText":"Spending limits updated."}')
        )

        result = self.make_client(opener).execute_spending_limits(
            TelegramIdentity(user_id="1045618308", username="AlpskyKnedlik"),
            max_per_tx_usdc="200",
            daily_cap_usdc="1000",
            user_access_token="user-token-1",
        )

        request, _timeout = opener.requests[0]
        self.assertEqual(result, "Spending limits updated.")
        self.assertEqual(
            json.loads(request.data),
            {
                "telegramUserId": "1045618308",
                "telegramUsername": "AlpskyKnedlik",
                "maxPerTxUsdc": "200",
                "dailyCapUsdc": "1000",
            },
        )
        self.assertEqual(request.get_header("X-sign402-user-token"), "user-token-1")

    def test_execute_llm_start_uses_user_token_and_purchase_timeout(self):
        opener = RecordingOpener(
            response=FakeResponse(
                b'{"ok":true,"state":"AWAITING_TERMS","telegramText":"Review terms."}'
            )
        )
        client = self.make_client(opener, purchase_timeout=240.0)

        result = client.execute_llm(
            "start",
            TelegramIdentity(user_id="123"),
            payload={"amountUsd": "10", "email": "user@example.com"},
            user_access_token="user-token",
        )

        request, timeout = opener.requests[0]
        self.assertEqual(result["state"], "AWAITING_TERMS")
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8099/agent/llm-key/start",
        )
        self.assertEqual(timeout, 240.0)
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer wallet-token-secret-value",
        )
        self.assertEqual(
            request.get_header("X-sign402-user-token"),
            "user-token",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "telegramUserId": "123",
                "amountUsd": "10",
                "email": "user@example.com",
            },
        )

    def test_execute_llm_maps_all_operations(self):
        cases = {
            "start": "/agent/llm-key/start",
            "accept-terms": "/agent/llm-key/accept-terms",
            "verify": "/agent/llm-key/verify",
            "credits": "/agent/llm-credits",
        }

        for operation, path in cases.items():
            with self.subTest(operation=operation):
                opener = RecordingOpener(
                    response=FakeResponse(b'{"ok":true,"telegramText":"ok"}')
                )
                self.make_client(opener).execute_llm(
                    operation,
                    TelegramIdentity(user_id="123"),
                    user_access_token="user-token",
                )
                self.assertEqual(
                    opener.requests[0][0].full_url,
                    f"http://127.0.0.1:8099{path}",
                )

    def test_execute_llm_requires_user_access_token(self):
        opener = RecordingOpener(
            response=FakeResponse(b'{"ok":true,"telegramText":"ok"}')
        )

        with self.assertRaises(GatewayClientError) as caught:
            self.make_client(opener).execute_llm(
                "credits",
                TelegramIdentity(user_id="123"),
                user_access_token="",
            )

        self.assertIn("authentication", caught.exception.user_message)
        self.assertEqual(opener.requests, [])

    def test_execute_llm_surfaces_only_gateway_telegram_text(self):
        error_stream = io.BytesIO(
            json.dumps(
                {
                    "ok": False,
                    "error": "raw provider token response",
                    "telegramText": "That verification code is invalid or expired.",
                }
            ).encode("utf-8")
        )
        opener = RecordingOpener(
            error=HTTPError(
                "http://127.0.0.1:8099/agent/llm-key/verify",
                400,
                "Bad Request",
                {},
                error_stream,
            )
        )

        with self.assertRaises(GatewayClientError) as caught:
            self.make_client(opener).execute_llm(
                "verify",
                TelegramIdentity(user_id="123"),
                payload={"code": "000000"},
                user_access_token="user-token",
            )

        self.assertEqual(
            caught.exception.user_message,
            "That verification code is invalid or expired.",
        )
        self.assertNotIn("raw provider", caught.exception.user_message)
        self.assertTrue(error_stream.closed)

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
