import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sign402_gateway.bitrefill_mcp import (
    McpBitrefillClient,
    McpToolCaller,
    decode_mcp_tool_result,
)


class FakeText:
    def __init__(self, text):
        self.text = text


class FakeToolResult:
    def __init__(self, *, structured=None, text="", is_error=False):
        self.structuredContent = structured
        self.content = [FakeText(text)] if text else []
        self.isError = is_error


class BitrefillMcpDecodeTests(unittest.TestCase):
    def test_decoder_prefers_structured_content(self):
        result = decode_mcp_tool_result(
            FakeToolResult(structured={"products": [{"id": "steam-usa"}]})
        )

        self.assertEqual(result["products"][0]["id"], "steam-usa")

    def test_decoder_accepts_json_text(self):
        result = decode_mcp_tool_result(
            FakeToolResult(text='{"invoice_id":"invoice_1"}')
        )

        self.assertEqual(result, {"invoice_id": "invoice_1"})

    def test_decoder_accepts_toon_text(self):
        result = decode_mcp_tool_result(
            FakeToolResult(text="products[1]{id,name}:\n  steam-usa,Steam")
        )

        self.assertEqual(result["products"][0]["name"], "Steam")

    def test_decoder_hides_tool_error_text(self):
        with self.assertRaisesRegex(ValueError, "Bitrefill MCP tool failed") as raised:
            decode_mcp_tool_result(FakeToolResult(text="key_123", is_error=True))

        self.assertNotIn("key_123", str(raised.exception))

    def test_decoder_rejects_oversized_response(self):
        with self.assertRaisesRegex(ValueError, "response is too large"):
            decode_mcp_tool_result(
                FakeToolResult(text='{"value":"oversized"}'),
                max_bytes=8,
            )

    def test_decoder_rejects_scalar_response(self):
        with self.assertRaisesRegex(ValueError, "non-object response"):
            decode_mcp_tool_result(FakeToolResult(text='"scalar"'))


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class BitrefillMcpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_caller_initializes_lists_and_calls_named_tool(self):
        session = SimpleNamespace(
            initialize=AsyncMock(),
            list_tools=AsyncMock(
                return_value=SimpleNamespace(
                    tools=[SimpleNamespace(name="search-products")]
                )
            ),
            call_tool=AsyncMock(
                return_value=FakeToolResult(structured={"products": []})
            ),
        )
        session_context = AsyncContext(session)
        transport_context = AsyncContext((Mock(name="read"), Mock(name="write"), Mock()))
        http_context = AsyncContext(Mock(name="http_client"))
        caller = McpToolCaller("https://api.bitrefill.com/mcp/key_123")

        with (
            patch(
                "sign402_gateway.bitrefill_mcp.httpx.AsyncClient",
                return_value=http_context,
            ),
            patch(
                "sign402_gateway.bitrefill_mcp.streamable_http_client",
                return_value=transport_context,
            ) as streamable,
            patch(
                "sign402_gateway.bitrefill_mcp.ClientSession",
                return_value=session_context,
            ),
        ):
            result = await caller._call("search-products", {"query": "Steam"})

        self.assertEqual(result, {"products": []})
        session.initialize.assert_awaited_once_with()
        session.list_tools.assert_awaited_once_with()
        session.call_tool.assert_awaited_once_with(
            "search-products",
            arguments={"query": "Steam"},
        )
        streamable.assert_called_once()

    async def test_tool_caller_rejects_missing_required_tool(self):
        session = SimpleNamespace(
            initialize=AsyncMock(),
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
            call_tool=AsyncMock(),
        )
        caller = McpToolCaller("https://api.bitrefill.com/mcp/key_123")

        with (
            patch(
                "sign402_gateway.bitrefill_mcp.httpx.AsyncClient",
                return_value=AsyncContext(Mock()),
            ),
            patch(
                "sign402_gateway.bitrefill_mcp.streamable_http_client",
                return_value=AsyncContext((Mock(), Mock(), Mock())),
            ),
            patch(
                "sign402_gateway.bitrefill_mcp.ClientSession",
                return_value=AsyncContext(session),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "required Bitrefill MCP tool"):
                await caller._call("buy-products", {})

        session.call_tool.assert_not_awaited()

    def test_tool_caller_repr_redacts_server_url(self):
        caller = McpToolCaller("https://api.bitrefill.com/mcp/key_123")

        self.assertNotIn("key_123", repr(caller))
        self.assertNotIn("api.bitrefill.com", repr(caller))


class FakeMcpCaller:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if not self.responses:
            raise AssertionError(f"unexpected MCP call: {name}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return deepcopy(response)


class BitrefillMcpCatalogTests(unittest.TestCase):
    def test_search_uses_mcp_and_normalizes_products(self):
        caller = FakeMcpCaller(
            [
                {
                    "products": [
                        {
                            "product_id": "steam-usa",
                            "name": "Steam USA",
                            "country": "US",
                            "currency": "USD",
                            "recipient_type": "none",
                            "category": "games",
                            "in_stock": True,
                        }
                    ]
                }
            ]
        )
        client = McpBitrefillClient(api_key="key_123", call_tool=caller)

        products = client.search_products(
            query="Steam",
            country="US",
            category="games",
            product_type="gift_card",
            include_test_products=False,
        )

        self.assertEqual(products[0]["productId"], "steam-usa")
        self.assertEqual(products[0]["country"], "US")
        self.assertEqual(products[0]["productType"], "gift_card")
        self.assertEqual(
            caller.calls,
            [
                (
                    "search-products",
                    {
                        "query": "Steam",
                        "country": "US",
                        "category": "games",
                        "type": "gift_card",
                        "include_test_products": False,
                        "per_page": 100,
                    },
                )
            ],
        )

    def test_list_searches_each_country_then_filters_and_slices(self):
        caller = FakeMcpCaller(
            [
                {
                    "products": [
                        {
                            "product_id": "food-cz",
                            "name": "Food CZ",
                            "country": "CZ",
                            "category": "food",
                            "currency": "CZK",
                        },
                        {
                            "product_id": "games-cz",
                            "name": "Games CZ",
                            "country": "CZ",
                            "category": "games",
                            "currency": "CZK",
                        },
                    ]
                },
                {
                    "products": [
                        {
                            "product_id": "food-global",
                            "name": "Food Global",
                            "country": "XI",
                            "category": "food",
                            "currency": "USD",
                        }
                    ]
                },
            ]
        )
        client = McpBitrefillClient(api_key="key_123", call_tool=caller)

        products = client.list_products(
            country="CZ,XI",
            category="food,restaurants",
            start=1,
            limit=1,
            include_test_products=False,
        )

        self.assertEqual([item["productId"] for item in products], ["food-global"])
        self.assertEqual(
            [arguments["country"] for _, arguments in caller.calls],
            ["CZ", "XI"],
        )

    def test_details_use_mcp_package_value_and_usd_price(self):
        caller = FakeMcpCaller(
            [
                {
                    "product_id": "steam-usa",
                    "name": "Steam USA",
                    "country": "US",
                    "currency": "USD",
                    "recipient_type": "none",
                    "packages": [
                        {
                            "package_id": "steam-usa<&>50",
                            "package_value": "50",
                            "price": "50.25",
                        }
                    ],
                }
            ]
        )
        client = McpBitrefillClient(api_key="key_123", call_tool=caller)

        details = client.get_product_details(product_id="steam-usa", country="US")

        self.assertEqual(
            details["packages"][0],
            {
                "packageId": "steam-usa<&>50",
                "value": "50",
                "priceUsd": "50.25",
            },
        )
        self.assertEqual(
            caller.calls,
            [
                (
                    "get-product-details",
                    {"product_id": "steam-usa", "currency": "USD"},
                )
            ],
        )

    def test_details_expose_range_minimum_recipient_and_prepayment(self):
        caller = FakeMcpCaller(
            [
                {
                    "product_id": "prepaid-visa-usa",
                    "name": "Prepaid Visa USA",
                    "country_code": "US",
                    "currency": "USD",
                    "recipient_type": "email",
                    "range": {"min": "10", "max": "100", "step": "5"},
                    "prepayment": {"step": 1},
                }
            ]
        )
        client = McpBitrefillClient(api_key="key_123", call_tool=caller)

        details = client.get_product_details(
            product_id="prepaid-visa-usa",
            country="US",
        )

        self.assertEqual(details["packages"][0]["value"], "10")
        self.assertEqual(details["requiredRecipientFields"], ["email"])
        self.assertTrue(details["requiresPrepayment"])

    def test_details_reject_country_mismatch(self):
        caller = FakeMcpCaller(
            [
                {
                    "product_id": "steam-usa",
                    "name": "Steam USA",
                    "country": "US",
                    "currency": "USD",
                    "packages": [],
                }
            ]
        )
        client = McpBitrefillClient(api_key="key_123", call_tool=caller)

        with self.assertRaisesRegex(ValueError, "requested country"):
            client.get_product_details(product_id="steam-usa", country="CZ")

    def test_quote_validates_recipient_cap_and_prepayment(self):
        phone_product = {
            "product_id": "tmobile-usa",
            "name": "T-Mobile USA",
            "country": "US",
            "currency": "USD",
            "recipient_type": "phone_number",
            "packages": [
                {
                    "package_id": "tmobile-usa<&>5",
                    "package_value": "5",
                    "price": "5.00",
                }
            ],
        }
        client = McpBitrefillClient(
            api_key="key_123",
            max_purchase_usd="5.00",
            call_tool=FakeMcpCaller([phone_product, phone_product]),
        )

        with self.assertRaisesRegex(ValueError, "recipient.phone is required"):
            client.quote_product(
                product_id="tmobile-usa",
                package_id="5",
                country="US",
                recipient={},
            )

        quote = client.quote_product(
            product_id="tmobile-usa",
            package_id="tmobile-usa<&>5",
            country="US",
            recipient={"phone": "+12025550123"},
        )
        self.assertEqual(quote["packageValue"], "5")
        self.assertEqual(quote["priceUsd"], "5.00")

        prepayment_client = McpBitrefillClient(
            api_key="key_123",
            call_tool=FakeMcpCaller(
                [
                    {
                        **phone_product,
                        "prepayment": {"step": 1},
                    }
                ]
            ),
        )
        with self.assertRaisesRegex(ValueError, "prepayment form"):
            prepayment_client.quote_product(
                product_id="tmobile-usa",
                package_id="5",
                country="US",
                recipient={"phone": "+12025550123"},
            )


if __name__ == "__main__":
    unittest.main()
