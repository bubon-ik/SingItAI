import json
import logging
import os
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
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
        logger = logging.getLogger("sign402_gateway.bitrefill_mcp")
        with self.assertLogs(logger, level="ERROR"):
            with self.assertRaisesRegex(
                ValueError,
                "Bitrefill MCP tool failed",
            ) as raised:
                decode_mcp_tool_result(FakeToolResult(text="key_123", is_error=True))

        self.assertNotIn("key_123", str(raised.exception))

    def test_decoder_logs_only_safe_provider_error_fields(self):
        logger = logging.getLogger("sign402_gateway.bitrefill_mcp")
        api_key = "key_1234567890abcdef"
        address = "0x1111111111111111111111111111111111111111"
        with patch.dict(os.environ, {"BITREFILL_API_KEY": api_key}, clear=False):
            with self.assertLogs(logger, level="ERROR") as captured:
                with self.assertRaises(ValueError):
                    decode_mcp_tool_result(
                        FakeToolResult(
                            text=json.dumps(
                                {
                                    "code": "PACKAGE_VALUE_INVALID",
                                    "message": (
                                        "package not purchasable "
                                        f"{api_key} pay https://pay.example/inv "
                                        f"to {address} pin=1234"
                                    ),
                                    "status": 422,
                                    "request_id": "request_123",
                                    "payment_link": "https://pay.example/secret",
                                }
                            ),
                            is_error=True,
                        )
                    )

        joined = "\n".join(captured.output)
        self.assertIn("PACKAGE_VALUE_INVALID", joined)
        self.assertIn("package not purchasable", joined)
        self.assertIn("request_123", joined)
        self.assertNotIn(api_key, joined)
        self.assertIn("<redacted:BITREFILL_API_KEY>", joined)
        self.assertNotIn("https://", joined)
        self.assertNotIn(address, joined)
        self.assertNotIn("1234", joined)

    def test_decoder_logs_only_fingerprint_for_unparseable_tool_error(self):
        logger = logging.getLogger("sign402_gateway.bitrefill_mcp")
        detail = (
            "bad invoice at https://pay.example/secret "
            "0x1111111111111111111111111111111111111111"
        )

        with self.assertLogs(logger, level="ERROR") as captured:
            with self.assertRaises(ValueError):
                decode_mcp_tool_result(
                    FakeToolResult(text=detail, is_error=True)
                )

        joined = "\n".join(captured.output)
        self.assertIn("sha256", joined)
        self.assertNotIn(detail, joined)
        self.assertNotIn("https://", joined)
        self.assertNotIn("0x111111", joined)

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
    def test_catalog_read_succeeds_when_persistence_path_is_unwritable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            blocked_parent = Path(tmpdir) / "blocked"
            blocked_parent.write_text("not a directory", encoding="utf-8")
            caller = FakeMcpCaller(
                [
                    {
                        "products": [
                            {
                                "product_id": "live-nl",
                                "name": "Live Netherlands",
                                "country": "NL",
                                "category": "shopping",
                                "currency": "EUR",
                            }
                        ]
                    }
                ]
            )
            client = McpBitrefillClient(
                api_key="key_123",
                call_tool=caller,
                catalog_cache_path=blocked_parent / "catalog.json",
            )

            products = client.list_products(
                country="NL",
                category="",
                start=0,
                limit=8,
                include_test_products=False,
            )

        self.assertEqual([row["productId"] for row in products], ["live-nl"])

    def test_catalog_uses_read_caller_and_details_use_live_commerce_caller(self):
        catalog_caller = FakeMcpCaller(
            [
                {
                    "products": [
                        {
                            "product_id": "catalog-nl",
                            "name": "Catalog Netherlands",
                            "country": "NL",
                            "category": "shopping",
                            "currency": "EUR",
                        }
                    ]
                }
            ]
        )
        commerce_caller = FakeMcpCaller(
            [
                {
                    "product_id": "catalog-nl",
                    "name": "Catalog Netherlands",
                    "country": "NL",
                    "category": "shopping",
                    "currency": "EUR",
                    "packages": [
                        {
                            "package_value": "25",
                            "payment_price": "26.00",
                        }
                    ],
                }
            ]
        )
        client = McpBitrefillClient(
            api_key="key_123",
            call_tool=commerce_caller,
            catalog_call_tool=catalog_caller,
        )

        products = client.list_products(
            country="NL",
            category="",
            start=0,
            limit=8,
            include_test_products=False,
        )
        details = client.get_product_details(product_id="catalog-nl", country="NL")

        self.assertEqual([row["productId"] for row in products], ["catalog-nl"])
        self.assertEqual(details["packages"][0]["priceUsd"], "26.00")
        self.assertEqual([name for name, _ in catalog_caller.calls], ["search-products"])
        self.assertEqual(
            [name for name, _ in commerce_caller.calls],
            ["get-product-details"],
        )

    def test_catalog_cache_settings_and_short_timeout_come_from_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "SIGN402_BITREFILL_CATALOG_CACHE_TTL_SECONDS": "321",
                "SIGN402_BITREFILL_CATALOG_TIMEOUT_SECONDS": "7",
                "SIGN402_BITREFILL_CATALOG_CACHE_PATH": str(
                    Path(tmpdir) / "catalog.json"
                ),
            },
        ), patch(
            "sign402_gateway.bitrefill_mcp.McpToolCaller",
            side_effect=lambda *args, **kwargs: Mock(),
        ) as caller_factory:
            client = McpBitrefillClient(api_key="key_123")

        self.assertEqual(client.catalog_cache_ttl_seconds, 321.0)
        self.assertEqual(client.catalog_cache_path, Path(tmpdir) / "catalog.json")
        self.assertEqual(caller_factory.call_count, 2)
        self.assertEqual(caller_factory.call_args_list[0].kwargs, {})
        self.assertEqual(
            caller_factory.call_args_list[1].kwargs,
            {"timeout_seconds": 7.0},
        )

    def test_concurrent_cold_catalog_requests_are_single_flight(self):
        started = threading.Event()
        release = threading.Event()
        calls = []
        call_lock = threading.Lock()
        payload = {
            "products": [
                {
                    "product_id": "shared-nl",
                    "name": "Shared Netherlands",
                    "country": "NL",
                    "category": "shopping",
                    "currency": "EUR",
                }
            ]
        }

        def blocking_caller(name, arguments):
            with call_lock:
                calls.append((name, deepcopy(arguments)))
            started.set()
            release.wait(1.0)
            return deepcopy(payload)

        with tempfile.TemporaryDirectory() as tmpdir:
            client = McpBitrefillClient(
                api_key="key_123",
                call_tool=blocking_caller,
                catalog_cache_path=Path(tmpdir) / "catalog.json",
            )
            results = []
            errors = []

            def load():
                try:
                    results.append(
                        client.list_products(
                            country="NL",
                            category="",
                            start=0,
                            limit=8,
                            include_test_products=False,
                        )
                    )
                except Exception as exc:
                    errors.append(exc)

            first = threading.Thread(target=load)
            second = threading.Thread(target=load)
            first.start()
            self.assertTrue(started.wait(0.5))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(1.0)
            second.join(1.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(
            [[row["productId"] for row in result] for result in results],
            [["shared-nl"], ["shared-nl"]],
        )
        self.assertEqual(len(calls), 1)

    def test_stale_catalog_returns_immediately_and_failed_refresh_preserves_it(self):
        clock = {"now": 1000.0}
        callbacks = []
        caller = FakeMcpCaller(
            [
                {
                    "products": [
                        {
                            "product_id": "cached-nl",
                            "name": "Cached Netherlands",
                            "country": "NL",
                            "category": "shopping",
                            "currency": "EUR",
                        }
                    ]
                },
                ValueError("upstream unavailable"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            client = McpBitrefillClient(
                api_key="key_123",
                call_tool=caller,
                catalog_cache_path=Path(tmpdir) / "catalog.json",
                catalog_cache_ttl_seconds=600,
                catalog_refresh_runner=callbacks.append,
                now_provider=lambda: clock["now"],
            )
            client.list_products(
                country="NL",
                category="",
                start=0,
                limit=8,
                include_test_products=False,
            )
            clock["now"] = 1601.0

            started = time.monotonic()
            stale = client.list_products(
                country="NL",
                category="",
                start=0,
                limit=8,
                include_test_products=False,
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.05)
            self.assertEqual([row["productId"] for row in stale], ["cached-nl"])
            self.assertEqual(len(caller.calls), 1)
            self.assertEqual(len(callbacks), 1)

            callbacks.pop(0)()
            stale_after_failure = client.list_products(
                country="NL",
                category="",
                start=0,
                limit=8,
                include_test_products=False,
            )

        self.assertEqual(
            [row["productId"] for row in stale_after_failure],
            ["cached-nl"],
        )
        self.assertEqual(len(caller.calls), 2)
        self.assertEqual(len(callbacks), 1)

    def test_catalog_cache_ignores_oversized_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "catalog.json"
            cache_path.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
            caller = FakeMcpCaller(
                [
                    {
                        "products": [
                            {
                                "product_id": "live-nl",
                                "name": "Live Netherlands",
                                "country": "NL",
                                "category": "shopping",
                                "currency": "EUR",
                            }
                        ]
                    }
                ]
            )

            client = McpBitrefillClient(
                api_key="key_123",
                call_tool=caller,
                catalog_cache_path=cache_path,
                now_provider=lambda: 1000.0,
            )
            products = client.list_products(
                country="NL",
                category="",
                start=0,
                limit=8,
                include_test_products=False,
            )

        self.assertEqual([row["productId"] for row in products], ["live-nl"])
        self.assertEqual(len(caller.calls), 1)

    def test_catalog_cache_ignores_invalid_persistence(self):
        poisoned_product = {
            "productId": "poisoned",
            "name": "Poisoned",
            "country": "NL",
            "currency": "EUR",
            "category": "shopping",
            "categories": ["shopping"],
            "productType": "gift_card",
            "recipientType": "none",
            "requiredRecipientFields": [],
            "packages": [],
            "inStock": True,
            "requiresPrepayment": False,
        }
        invalid_payloads = {
            "malformed": "{not-json",
            "future timestamp": json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "country": "NL",
                            "includeTestProducts": False,
                            "storedAt": 2000.0,
                            "products": [poisoned_product],
                        }
                    ],
                }
            ),
            "too many products": json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "country": "NL",
                            "includeTestProducts": False,
                            "storedAt": 1000.0,
                            "products": [poisoned_product] * 201,
                        }
                    ],
                }
            ),
        }

        for label, contents in invalid_payloads.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                cache_path = Path(tmpdir) / "catalog.json"
                cache_path.write_text(contents, encoding="utf-8")
                caller = FakeMcpCaller(
                    [
                        {
                            "products": [
                                {
                                    "product_id": "live-nl",
                                    "name": "Live Netherlands",
                                    "country": "NL",
                                    "category": "shopping",
                                    "currency": "EUR",
                                }
                            ]
                        }
                    ]
                )

                client = McpBitrefillClient(
                    api_key="key_123",
                    call_tool=caller,
                    catalog_cache_path=cache_path,
                    catalog_cache_ttl_seconds=600,
                    now_provider=lambda: 1000.0,
                )
                products = client.list_products(
                    country="NL",
                    category="",
                    start=0,
                    limit=8,
                    include_test_products=False,
                )

            self.assertEqual([row["productId"] for row in products], ["live-nl"])
            self.assertEqual(len(caller.calls), 1)

    def test_catalog_snapshot_survives_client_reconstruction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "catalog.json"
            first_caller = FakeMcpCaller(
                [
                    {
                        "products": [
                            {
                                "product_id": "cached-nl",
                                "name": "Cached Netherlands",
                                "country": "NL",
                                "category": "shopping",
                                "currency": "EUR",
                            }
                        ]
                    }
                ]
            )
            first_client = McpBitrefillClient(
                api_key="key_123",
                call_tool=first_caller,
                catalog_cache_path=cache_path,
                catalog_cache_ttl_seconds=600,
                now_provider=lambda: 1000.0,
            )
            first_client.list_products(
                country="NL",
                category="",
                start=0,
                limit=8,
                include_test_products=False,
            )

            second_caller = FakeMcpCaller([])
            second_client = McpBitrefillClient(
                api_key="key_123",
                call_tool=second_caller,
                catalog_cache_path=cache_path,
                catalog_cache_ttl_seconds=600,
                now_provider=lambda: 1000.0,
            )
            products = second_client.list_products(
                country="NL",
                category="",
                start=0,
                limit=8,
                include_test_products=False,
            )

        self.assertEqual([row["productId"] for row in products], ["cached-nl"])
        self.assertEqual(second_caller.calls, [])

    def test_list_reuses_one_fresh_country_snapshot_for_categories_and_pages(self):
        caller = FakeMcpCaller(
            [
                {
                    "products": [
                        {
                            "product_id": "p1",
                            "name": "Food One",
                            "country": "NL",
                            "category": "food",
                            "currency": "EUR",
                        },
                        {
                            "product_id": "p2",
                            "name": "Games Two",
                            "country": "NL",
                            "category": "games",
                            "currency": "EUR",
                        },
                        {
                            "product_id": "p3",
                            "name": "Food Three",
                            "country": "NL",
                            "category": "restaurants",
                            "currency": "EUR",
                        },
                        {
                            "product_id": "p4",
                            "name": "Shopping Four",
                            "country": "NL",
                            "category": "shopping",
                            "currency": "EUR",
                        },
                    ]
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            client = McpBitrefillClient(
                api_key="key_123",
                call_tool=caller,
                catalog_cache_path=Path(tmpdir) / "catalog.json",
                catalog_cache_ttl_seconds=600,
                now_provider=lambda: 1000.0,
            )

            first = client.list_products(
                country="NL,XI",
                category="",
                start=0,
                limit=2,
                include_test_products=False,
            )
            food = client.list_products(
                country="NL,XI",
                category="food,restaurants",
                start=0,
                limit=8,
                include_test_products=False,
            )
            second_page = client.list_products(
                country="NL,XI",
                category="",
                start=2,
                limit=2,
                include_test_products=False,
            )

        self.assertEqual([row["productId"] for row in first], ["p1", "p2"])
        self.assertEqual([row["productId"] for row in food], ["p1", "p3"])
        self.assertEqual(
            [row["productId"] for row in second_page],
            ["p3", "p4"],
        )
        self.assertEqual(len(caller.calls), 1)

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

    def test_list_uses_local_mcp_filter_for_local_and_international_catalog(self):
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
                        {
                            "product_id": "food-europe",
                            "name": "Food Europe",
                            "countries": ["CZ", "DE", "NL"],
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

        self.assertEqual([item["productId"] for item in products], ["food-europe"])
        self.assertEqual(
            [arguments for _, arguments in caller.calls],
            [
                {
                    "query": "*",
                    "country": "CZ",
                    "include_test_products": False,
                    "per_page": 100,
                },
            ],
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

    def test_quote_accepts_price_equal_to_live_cap_and_rejects_price_above_it(self):
        product = {
            "product_id": "large-gift-card-us",
            "name": "Large Gift Card",
            "country": "US",
            "currency": "USD",
            "recipient_type": "none",
            "packages": [
                {
                    "package_id": "large-gift-card-us<&>1000",
                    "package_value": "1000",
                    "price": "1000.00",
                },
                {
                    "package_id": "large-gift-card-us<&>1000.01",
                    "package_value": "1000.01",
                    "price": "1000.01",
                },
            ],
        }
        client = McpBitrefillClient(
            api_key="key_123",
            max_purchase_usd="1000.00",
            call_tool=FakeMcpCaller([product, product]),
        )

        quote = client.quote_product(
            product_id="large-gift-card-us",
            package_id="large-gift-card-us<&>1000",
            country="US",
            recipient={},
        )
        self.assertEqual(quote["priceUsd"], "1000.00")

        with self.assertRaisesRegex(
            ValueError,
            r"exceeds live Bitrefill max \$1000\.00",
        ):
            client.quote_product(
                product_id="large-gift-card-us",
                package_id="large-gift-card-us<&>1000.01",
                country="US",
                recipient={},
            )


APPROVED_QUOTE = {
    "quoteId": "quote_live_1",
    "productId": "steam-usa",
    "name": "Steam USA",
    "productType": "gift_card",
    "packageId": "steam-usa<&>50",
    "packageValue": "50",
    "priceUsd": "50.00",
    "totalUsd": "50.50",
    "recipientType": "none",
}


class FakeTreasuryClient:
    def __init__(self):
        self.transfers = []
        self.token_transfers = []

    def transfer_usdc(self, *, to_address, amount, chain="base"):
        self.transfers.append(
            {"to_address": to_address, "amount": amount, "chain": chain}
        )
        return {"ok": True, "txId": "0xUSDC"}

    def transfer_token_exact(
        self,
        *,
        token_address,
        to_address,
        amount_atomic,
        chain,
        idempotency_key,
    ):
        self.token_transfers.append(
            {
                "token_address": token_address,
                "to_address": to_address,
                "amount_atomic": amount_atomic,
                "chain": chain,
                "idempotency_key": idempotency_key,
            }
        )
        return {"ok": True, "txId": "0xUSDC"}


class BitrefillMcpPurchaseTests(unittest.TestCase):
    def test_balance_purchase_uses_buy_and_invoice_mcp_tools(self):
        caller = FakeMcpCaller(
            [
                {
                    "invoice_id": "inv_1",
                    "status": "complete",
                    "payment_link": "MARKER-PAYMENT-LINK",
                    "command": "MARKER-COMMAND",
                    "stdout": "MARKER-STDOUT",
                    "stderr": "MARKER-STDERR",
                    "credentials": "MARKER-CREDENTIALS",
                },
                {
                    "invoice_id": "inv_1",
                    "status": "complete",
                    "payment_link": "MARKER-PAYMENT-LINK",
                    "command": "MARKER-COMMAND",
                    "stdout": "MARKER-STDOUT",
                    "stderr": "MARKER-STDERR",
                    "credentials": "MARKER-CREDENTIALS",
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
                    {"product_id": "steam-usa", "package_value": "50"}
                ],
                "payment_method": "balance",
                "return_payment_link": False,
            },
        )
        self.assertEqual(result["provider"], "bitrefill-mcp")
        self.assertEqual(result["paymentMethod"], "balance")
        self.assertEqual(result["invoiceId"], "inv_1")
        self.assertEqual(result["orderId"], "ord_1")
        self.assertEqual(result["redemption"]["value"]["code"], "SECRET-CODE")
        self.assertEqual(checkpoints[0]["invoiceId"], "inv_1")
        self.assertNotIn("SECRET-CODE", str(checkpoints))
        for marker in (
            "MARKER-PAYMENT-LINK",
            "MARKER-COMMAND",
            "MARKER-STDOUT",
            "MARKER-STDERR",
            "MARKER-CREDENTIALS",
        ):
            self.assertNotIn(marker, str(checkpoints))
            self.assertNotIn(marker, str(result))

    def test_missing_invoice_id_logs_the_response_shape(self):
        caller = FakeMcpCaller(
            [
                {
                    "unexpected_envelope": {
                        "identifier": "inv_secret_value",
                        "payment_info": {"address": "0xSECRETADDRESS"},
                    }
                }
            ]
        )
        client = McpBitrefillClient(
            api_key="key_123",
            max_purchase_usd="50.00",
            call_tool=caller,
        )
        logger = logging.getLogger("sign402_gateway.bitrefill_mcp")

        with self.assertLogs(logger, level="ERROR") as captured:
            with self.assertRaisesRegex(ValueError, "invoice id is missing"):
                client.prepare_purchase(quote=APPROVED_QUOTE, recipient={})

        joined = "\n".join(captured.output)
        self.assertIn("unexpected_envelope", joined)
        self.assertIn("payment_info", joined)
        self.assertIn("identifier", joined)
        self.assertNotIn("inv_secret_value", joined)
        self.assertNotIn("0xSECRETADDRESS", joined)

    def test_purchase_never_sends_the_deprecated_package_id_field(self):
        caller = FakeMcpCaller(
            [
                {"invoice_id": "inv_1", "status": "complete"},
                {
                    "invoice_id": "inv_1",
                    "status": "complete",
                    "orders": [{"order_id": "ord_1", "status": "delivered"}],
                },
            ]
        )
        client = McpBitrefillClient(
            api_key="key_123",
            max_purchase_usd="50.00",
            call_tool=caller,
        )

        client.buy_product(quote=APPROVED_QUOTE, recipient={})

        item = caller.calls[0][1]["cart_items"][0]
        self.assertNotIn("package_id", item)
        self.assertEqual(item["package_value"], "50")

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

    def _invoice(self, **overrides):
        invoice = {
            "invoice_id": "inv_prepare",
            "status": "unpaid",
            "payment_method": "usdc_base",
            "cart_items": [
                {
                    "product_id": APPROVED_QUOTE["productId"],
                    "package_value": APPROVED_QUOTE["packageValue"],
                }
            ],
            "payment_info": self._payment_info(amount="50.50"),
        }
        invoice.update(overrides)
        return invoice

    def test_prepare_unwraps_the_live_response_envelope(self):
        # The live MCP server returns the invoice under `response`, alongside
        # `agent_instructions`, instead of at the top level.
        caller = FakeMcpCaller(
            [
                {
                    "agent_instructions": "pay the invoice",
                    "response": self._invoice(),
                }
            ]
        )
        client = self._client(caller, FakeTreasuryClient())

        prepared = client.prepare_purchase(quote=APPROVED_QUOTE, recipient={})

        self.assertEqual(prepared["invoiceId"], "inv_prepare")

    def test_prepare_accepts_payment_info_identified_only_by_contract(self):
        # The live invoice carries `contractAddress` and `altcoinPrice` but no
        # `currency` field.
        caller = FakeMcpCaller(
            [
                self._invoice(
                    payment_info={
                        "address": "0xBitrefill",
                        "altcoinPrice": "50.50",
                        "contractAddress": BASE_USDC_MAINNET,
                        "paymentUri": "ethereum:0xBitrefill@8453",
                    }
                )
            ]
        )
        client = self._client(caller, FakeTreasuryClient())

        prepared = client.prepare_purchase(quote=APPROVED_QUOTE, recipient={})

        self.assertEqual(prepared["paymentAmount"], "50.50")
        self.assertEqual(prepared["paymentAsset"], "USDC")

    def test_prepare_rejects_payment_info_that_identifies_no_token(self):
        caller = FakeMcpCaller(
            [
                self._invoice(
                    payment_info={
                        "address": "0xBitrefill",
                        "altcoinPrice": "50.50",
                    }
                )
            ]
        )
        client = self._client(caller, FakeTreasuryClient())

        with self.assertRaisesRegex(ValueError, "payment token"):
            client.prepare_purchase(quote=APPROVED_QUOTE, recipient={})

    def test_prepare_rejects_payment_info_for_another_token(self):
        caller = FakeMcpCaller(
            [
                self._invoice(
                    payment_info={
                        "address": "0xBitrefill",
                        "altcoinPrice": "50.50",
                        "contractAddress": (
                            "0x4200000000000000000000000000000000000006"
                        ),
                    }
                )
            ]
        )
        client = self._client(caller, FakeTreasuryClient())

        with self.assertRaisesRegex(ValueError, "not Base USDC"):
            client.prepare_purchase(quote=APPROVED_QUOTE, recipient={})

    def test_prepare_creates_and_validates_invoice_before_treasury_transfer(self):
        caller = FakeMcpCaller([self._invoice()])
        treasury = FakeTreasuryClient()
        client = self._client(caller, treasury)

        prepared = client.prepare_purchase(
            quote=APPROVED_QUOTE,
            recipient={},
        )

        self.assertEqual([name for name, _ in caller.calls], ["buy-products"])
        self.assertEqual(
            caller.calls[0][1]["cart_items"],
            [{"product_id": "steam-usa", "package_value": "50"}],
        )
        self.assertNotIn("package_id", caller.calls[0][1]["cart_items"][0])
        self.assertEqual(treasury.transfers, [])
        self.assertEqual(
            prepared,
            {
                "invoiceId": "inv_prepare",
                "status": "unpaid",
                "productId": "steam-usa",
                "packageValue": "50",
                "paymentMethod": "usdc_base",
                "paymentAmount": "50.50",
                "paymentAsset": "USDC",
                "paymentNetwork": "base",
            },
        )
        self.assertNotIn("0xBitrefill", str(prepared))

    def test_prepare_rejects_invoice_mismatches_before_treasury_transfer(self):
        invalid_invoices = {
            "missing invoice id": self._invoice(invoice_id=""),
            "missing cart item": self._invoice(cart_items=[]),
            "wrong payment method": self._invoice(payment_method="balance"),
            "wrong product": self._invoice(
                cart_items=[
                    {
                        "product_id": "other-product",
                        "package_value": "50",
                    }
                ]
            ),
            "wrong package": self._invoice(
                cart_items=[
                    {
                        "product_id": "steam-usa",
                        "package_value": "25",
                    }
                ]
            ),
            "wrong asset": self._invoice(
                payment_info=self._payment_info(currency="BTC")
            ),
            "wrong network": self._invoice(
                payment_info=self._payment_info(network="ethereum")
            ),
            "above approved total": self._invoice(
                payment_info=self._payment_info(amount="50.51")
            ),
        }

        for label, invoice in invalid_invoices.items():
            with self.subTest(label=label):
                treasury = FakeTreasuryClient()
                client = self._client(FakeMcpCaller([invoice]), treasury)

                with self.assertRaises(ValueError):
                    client.prepare_purchase(
                        quote=APPROVED_QUOTE,
                        recipient={},
                    )

                self.assertEqual(treasury.transfers, [])

    def test_complete_reloads_and_revalidates_same_prepared_invoice(self):
        prepared_invoice = self._invoice()
        reloaded_invoice = self._invoice(
            status="complete",
            orders=[{"order_id": "ord_prepare", "status": "delivered"}],
        )
        caller = FakeMcpCaller([prepared_invoice, reloaded_invoice])
        treasury = FakeTreasuryClient()
        client = self._client(caller, treasury)

        prepared = client.prepare_purchase(
            quote=APPROVED_QUOTE,
            recipient={},
        )
        result = client.complete_purchase(
            quote=APPROVED_QUOTE,
            prepared=prepared,
        )

        self.assertEqual(
            [name for name, _ in caller.calls],
            ["buy-products", "get-invoice-by-id"],
        )
        self.assertEqual(result["invoiceId"], "inv_prepare")

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
            treasury.token_transfers,
            [
                {
                    "token_address": BASE_USDC_MAINNET,
                    "to_address": "0xBitrefill",
                    "amount_atomic": "50010000",
                    "chain": "base",
                    "idempotency_key": "bitrefill-pay:inv_2",
                }
            ],
        )
        self.assertEqual(treasury.transfers, [])
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
        self.assertEqual(
            treasury.token_transfers[0]["amount_atomic"],
            "50010000",
        )

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
                self.assertEqual(treasury.token_transfers, [])

    def test_paid_checkpoint_prevents_second_invoice_payment(self):
        caller = FakeMcpCaller(
            [
                self._invoice(invoice_id="inv_once"),
                self._invoice(invoice_id="inv_once"),
                self._invoice(
                    invoice_id="inv_once",
                    status="complete",
                    orders=[{"order_id": "ord_once", "status": "delivered"}],
                ),
                self._invoice(
                    invoice_id="inv_once",
                    status="complete",
                    orders=[{"order_id": "ord_once", "status": "delivered"}],
                ),
            ]
        )
        treasury = FakeTreasuryClient()
        client = self._client(caller, treasury)
        checkpoints = []

        prepared = client.prepare_purchase(
            quote=APPROVED_QUOTE,
            recipient={},
        )
        client.complete_purchase(
            quote=APPROVED_QUOTE,
            prepared=prepared,
            checkpoint_callback=checkpoints.append,
        )
        paid_checkpoint = checkpoints[-1]
        result = client.complete_purchase(
            quote=APPROVED_QUOTE,
            prepared=paid_checkpoint,
            checkpoint_callback=checkpoints.append,
        )

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(len(treasury.token_transfers), 1)
        self.assertEqual(
            treasury.token_transfers[0]["idempotency_key"],
            "bitrefill-pay:inv_once",
        )

    def test_provider_payment_detected_status_never_rebroadcasts(self):
        caller = FakeMcpCaller(
            [
                self._invoice(
                    invoice_id="inv_detected",
                    status="payment_detected",
                ),
                self._invoice(
                    invoice_id="inv_detected",
                    status="complete",
                    orders=[
                        {"order_id": "ord_detected", "status": "delivered"}
                    ],
                ),
            ]
        )
        treasury = FakeTreasuryClient()
        client = self._client(caller, treasury)
        prepared = {
            "invoiceId": "inv_detected",
            "status": "unpaid",
            "productId": "steam-usa",
            "packageValue": "50",
            "paymentMethod": "usdc_base",
            "paymentAmount": "50.50",
            "paymentAsset": "USDC",
            "paymentNetwork": "base",
        }

        result = client.complete_purchase(
            quote=APPROVED_QUOTE,
            prepared=prepared,
        )

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(treasury.token_transfers, [])
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
