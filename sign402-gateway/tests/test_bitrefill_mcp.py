import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sign402_gateway.bitrefill_mcp import (
    McpBitrefillClient,
    McpToolCaller,
    decode_mcp_tool_result,
)
from sign402_gateway.bankr_swap import BASE_USDC_MAINNET


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
    def test_search_accepts_production_mcp_product_shape(self):
        caller = FakeMcpCaller(
            [
                {
                    "products": [
                        {
                            "_id": "steam-usa",
                            "slug": "steam-usa",
                            "name": "Steam USD",
                            "countries": [],
                            "currency": "USD",
                            "categories": ["games", "game-stores"],
                            "type": "giftcards",
                            "in_stock": True,
                        }
                    ],
                    "found": 1,
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

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["productId"], "steam-usa")
        self.assertEqual(products[0]["country"], "US")
        self.assertEqual(products[0]["category"], "games")
        self.assertEqual(products[0]["productType"], "gift_card")

    def test_search_does_not_assign_requested_country_to_other_country_product(self):
        caller = FakeMcpCaller(
            [
                {
                    "products": [
                        {
                            "_id": "bitrefill-esim-europe",
                            "name": "Europe eSIM",
                            "countries": ["AT", "DE", "CZ"],
                            "currency": "USD",
                            "categories": ["esim"],
                            "type": "esims",
                        }
                    ]
                }
            ]
        )
        client = McpBitrefillClient(api_key="key_123", call_tool=caller)

        products = client.search_products(
            query="eSIM",
            country="US",
            category="esim",
            product_type="esim",
            include_test_products=False,
        )

        self.assertEqual(products, [])

    def test_details_use_production_mcp_payment_price(self):
        caller = FakeMcpCaller(
            [
                {
                    "id": "steam-usa",
                    "name": "Steam USD",
                    "country_code": "US",
                    "currency": "USD",
                    "categories": ["games", "game-stores"],
                    "recipient_type": "none",
                    "packages": [
                        {
                            "package_value": "50",
                            "package_currency": "USD",
                            "payment_price": "54.06",
                            "payment_currency": "USD",
                        }
                    ],
                }
            ]
        )
        client = McpBitrefillClient(
            api_key="key_123",
            max_purchase_usd="60.00",
            call_tool=caller,
        )

        details = client.get_product_details(product_id="steam-usa", country="US")

        self.assertEqual(details["category"], "games")
        self.assertEqual(
            details["packages"][0],
            {
                "packageId": "steam-usa<&>50",
                "value": "50",
                "priceUsd": "54.06",
            },
        )

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


APPROVED_QUOTE = {
    "quoteId": "quote_live_1",
    "productId": "steam-usa",
    "name": "Steam USA",
    "productType": "gift_card",
    "packageId": "steam-usa<&>50",
    "packageValue": "50",
    "priceUsd": "50.00",
    "recipientType": "none",
}


class FakeTreasuryClient:
    def __init__(self):
        self.transfers = []

    def transfer_usdc(self, *, to_address, amount, chain="base"):
        self.transfers.append(
            {"to_address": to_address, "amount": amount, "chain": chain}
        )
        return {"ok": True, "txId": "0xUSDC"}


class BitrefillMcpPurchaseTests(unittest.TestCase):
    def test_balance_purchase_uses_buy_and_invoice_mcp_tools(self):
        caller = FakeMcpCaller(
            [
                {"invoice_id": "inv_1", "status": "complete"},
                {
                    "invoice_id": "inv_1",
                    "status": "complete",
                    "orders": [
                        {
                            "order_id": "ord_1",
                            "status": "delivered",
                            "redemption_available": True,
                            "redemption_info": {"code": "SECRET-CODE"},
                        }
                    ],
                },
            ]
        )
        checkpoints = []
        client = McpBitrefillClient(
            api_key="key_123",
            max_purchase_usd="50.00",
            call_tool=caller,
        )

        result = client.buy_product(
            quote=APPROVED_QUOTE,
            recipient={},
            checkpoint_callback=checkpoints.append,
        )

        self.assertEqual(
            [name for name, _ in caller.calls],
            ["buy-products", "get-invoice-by-id"],
        )
        self.assertEqual(
            caller.calls[0][1],
            {
                "cart_items": [
                    {"product_id": "steam-usa", "package_id": "50"}
                ],
                "payment_method": "balance",
                "return_payment_link": False,
            },
        )
        self.assertEqual(result["provider"], "bitrefill-mcp")
        self.assertEqual(result["invoiceId"], "inv_1")
        self.assertEqual(result["orderId"], "ord_1")
        self.assertEqual(result["redemption"]["value"]["code"], "SECRET-CODE")
        self.assertEqual(checkpoints[0]["invoiceId"], "inv_1")
        self.assertNotIn("SECRET-CODE", str(checkpoints))

    def test_purchase_maps_committed_recipient_to_refill_input(self):
        caller = FakeMcpCaller(
            [
                {"invoice_id": "inv_phone", "status": "complete"},
                {
                    "invoice_id": "inv_phone",
                    "status": "complete",
                    "orders": [
                        {"order_id": "ord_phone", "status": "delivered"}
                    ],
                },
            ]
        )
        client = McpBitrefillClient(
            api_key="key_123",
            max_purchase_usd="50.00",
            call_tool=caller,
        )

        client.buy_product(
            quote={
                **APPROVED_QUOTE,
                "productId": "tmobile-usa",
                "productType": "phone_refill",
                "recipientType": "phone_number",
            },
            recipient={"phone": "+12025550123"},
        )

        self.assertEqual(
            caller.calls[0][1]["cart_items"][0]["refill_input"],
            "+12025550123",
        )

    def test_refresh_uses_get_invoice_tool(self):
        caller = FakeMcpCaller(
            [
                {
                    "invoice_id": "inv_1",
                    "status": "complete",
                    "orders": [
                        {
                            "order_id": "ord_1",
                            "status": "delivered",
                            "redemption_info": {"code": "SECRET-CODE"},
                        }
                    ],
                }
            ]
        )
        client = McpBitrefillClient(api_key="key_123", call_tool=caller)

        result = client.refresh_purchase(
            {"invoiceId": "inv_1", "orderId": "ord_1"},
            APPROVED_QUOTE,
        )

        self.assertEqual(
            caller.calls,
            [("get-invoice-by-id", {"invoice_id": "inv_1"})],
        )
        self.assertEqual(result["status"], "delivered")


class BitrefillMcpUsdcPurchaseTests(unittest.TestCase):
    def _client(self, caller, treasury, **overrides):
        return McpBitrefillClient(
            api_key="key_123",
            max_purchase_usd=overrides.pop("max_purchase_usd", "55.00"),
            max_invoice_overage_bps=overrides.pop("max_invoice_overage_bps", 500),
            payment_method="usdc_base",
            treasury_client=treasury,
            invoice_poll_attempts=overrides.pop("invoice_poll_attempts", 2),
            invoice_poll_interval_seconds=0,
            call_tool=caller,
            **overrides,
        )

    def _payment_info(self, **overrides):
        payment = {
            "address": "0xBitrefill",
            "amount": "50.01",
            "currency": "USDC",
            "network": "base",
            "contract_address": BASE_USDC_MAINNET,
        }
        payment.update(overrides)
        return payment

    def test_valid_base_usdc_purchase_transfers_once_then_polls(self):
        caller = FakeMcpCaller(
            [
                {
                    "invoice_id": "inv_2",
                    "status": "unpaid",
                    "payment_info": self._payment_info(),
                },
                {
                    "invoice_id": "inv_2",
                    "status": "complete",
                    "orders": [
                        {
                            "order_id": "ord_2",
                            "status": "delivered",
                            "redemption_info": {"code": "SECRET-CODE"},
                        }
                    ],
                },
            ]
        )
        treasury = FakeTreasuryClient()
        client = self._client(caller, treasury)

        result = client.buy_product(quote=APPROVED_QUOTE, recipient={})

        self.assertEqual(
            treasury.transfers,
            [
                {
                    "to_address": "0xBitrefill",
                    "amount": "50.01",
                    "chain": "base",
                }
            ],
        )
        self.assertFalse(caller.calls[0][1]["return_payment_link"])
        self.assertEqual(result["treasuryPayment"]["txId"], "0xUSDC")

    def test_documented_minimal_payment_info_is_accepted(self):
        caller = FakeMcpCaller(
            [
                {
                    "invoice_id": "inv_minimal",
                    "status": "unpaid",
                    "payment_info": {
                        "address": "0xBitrefill",
                        "altcoinPrice": "50.01",
                        "currency": "USDC",
                    },
                },
                {
                    "invoice_id": "inv_minimal",
                    "status": "complete",
                    "orders": [
                        {"order_id": "ord_minimal", "status": "delivered"}
                    ],
                },
            ]
        )
        treasury = FakeTreasuryClient()

        result = self._client(caller, treasury).buy_product(
            quote=APPROVED_QUOTE,
            recipient={},
        )

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(treasury.transfers[0]["amount"], "50.01")

    def test_invalid_payment_requirements_never_transfer(self):
        invalid_cases = {
            "above live cap": self._payment_info(amount="56"),
            "above quote overage": self._payment_info(amount="53"),
            "wrong currency": self._payment_info(currency="USDT"),
            "wrong network": self._payment_info(network="ethereum"),
            "wrong contract": self._payment_info(contract_address="0xWrong"),
            "missing address": self._payment_info(address=""),
        }
        for label, payment_info in invalid_cases.items():
            with self.subTest(label=label):
                caller = FakeMcpCaller(
                    [
                        {
                            "invoice_id": "inv_bad",
                            "status": "unpaid",
                            "payment_info": payment_info,
                        }
                    ]
                )
                treasury = FakeTreasuryClient()
                client = self._client(caller, treasury)

                with self.assertRaises(ValueError):
                    client.buy_product(quote=APPROVED_QUOTE, recipient={})

                self.assertEqual(treasury.transfers, [])

    def test_terminal_invoice_error_stops_polling(self):
        caller = FakeMcpCaller(
            [
                {
                    "invoice_id": "inv_denied",
                    "status": "unpaid",
                    "payment_info": self._payment_info(),
                },
                {"invoice_id": "inv_denied", "status": "denied", "orders": []},
            ]
        )
        treasury = FakeTreasuryClient()
        client = self._client(caller, treasury)

        with self.assertRaisesRegex(ValueError, "denied"):
            client.buy_product(quote=APPROVED_QUOTE, recipient={})

        self.assertEqual(len(caller.calls), 2)


if __name__ == "__main__":
    unittest.main()
