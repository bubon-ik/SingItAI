import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import ANY, Mock

from sign402_gateway.bitrefill import TestBitrefillClient
from sign402_gateway.bitrefill_runner import (
    BITREFILL_BROWSE_CATEGORIES,
    BitrefillCatalogService,
    BitrefillFulfillmentRunner,
    BitrefillProductDetailsService,
    BitrefillPurchaseRunner,
    BitrefillQuoteService,
    BitrefillSearchService,
    BitrefillSettlementPreparationRunner,
    WalletPaymentTokenResolver,
    WalletBitrefillPurchaseRunner,
    _bitrefill_approval_context_lines,
    lookup_bitrefill_order,
)
from sign402_gateway.commerce_store import BitrefillCommerceStore


class PendingThenDeliveredBitrefillClient:
    def __init__(self):
        self.buy_calls = 0
        self.refresh_calls = 0

    def buy_product(self, *, quote, recipient, checkpoint_callback=None):
        self.buy_calls += 1
        return {
            "ok": True,
            "provider": "bitrefill-live",
            "invoiceId": "invoice_1",
            "orderId": "order_1",
            "status": "created",
            "redemption": {
                "type": "bitrefill",
                "label": "Bitrefill redemption",
                "value": None,
            },
        }

    def refresh_purchase(self, provider_result, quote):
        self.refresh_calls += 1
        return {
            "ok": True,
            "provider": "bitrefill-live",
            "invoiceId": provider_result["invoiceId"],
            "orderId": provider_result["orderId"],
            "status": "delivered",
            "redemption": {
                "type": "bitrefill",
                "label": "Bitrefill redemption",
                "value": {"code": "READY-123"},
            },
        }


class FixedRealRatePricer:
    def price_for_usdc(self, target_usdc):
        return {
            "pricingMode": "bankr_real_rate",
            "targetUsdc": str(target_usdc),
            "bufferedTargetUsdc": "0.11",
            "requiredSingit": "25000",
            "requiredSingitAtomic": "25000000000000000000000",
            "expectedUsdc": "0.111",
            "minUsdc": "0.109",
        }


class FixedWalletTokenPricer:
    def __init__(self):
        self.calls = []

    def price_for_usdc(self, target_usdc, **kwargs):
        self.calls.append((str(target_usdc), kwargs))
        return {
            "pricingMode": "bankr_real_rate",
            "targetUsdc": str(target_usdc),
            "bufferedTargetUsdc": "0.11",
            "requiredAmount": "0.11",
            "requiredAmountAtomic": "110000",
            "expectedUsdc": "0.111",
            "minUsdc": "0.109",
        }


class FakeFundingRunner:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, quote):
        self.calls.append(quote)
        if self.fail:
            raise RuntimeError("swap route failed")
        return {
            "ok": True,
            "txId": "0xSWAP",
            "expectedUsdc": quote.get("expectedUsdc", "0.11"),
        }


class FakeUserFundingRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, *, telegram_user_id, quote, recipient):
        self.calls.append(
            {
                "telegram_user_id": telegram_user_id,
                "quote": quote,
                "recipient": recipient,
            }
        )
        return {
            "ok": True,
            "mode": "user_wallet_transfer_to_cdp_swap",
            "fromWallet": "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C",
            "toWallet": "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            "transfer": {"ok": True, "txId": "0xUSERTRANSFER"},
            "funding": {"ok": True, "txId": "0xCDPSWAP"},
        }


class BitrefillRunnerTests(unittest.TestCase):
    def test_bitrefill_approval_names_selected_token_and_amount(self):
        lines = _bitrefill_approval_context_lines(
            {
                "productName": "Bitrefill Gift Card",
                "priceUsd": "1.00",
                "paymentTokenSymbol": "USDC",
                "paymentTokenAmount": "1.10",
                "expiresAtEpoch": 220,
            },
            source_wallet="0x1111111111111111111111111111111111111111",
            now_epoch_value=100,
        )

        self.assertIn("Payment token: USDC", lines)
        self.assertIn("Maximum spend: 1.1 USDC", lines)

    def test_wallet_payment_token_resolver_uses_server_inventory_metadata(self):
        resolver = WalletPaymentTokenResolver(
            lambda user_id: {
                "ok": True,
                "tokens": [
                    {
                        "symbol": "USDC",
                        "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "balance": "4.82",
                        "decimals": 6,
                        "verified": True,
                    }
                ],
            }
        )

        token = resolver.resolve(
            "1045618308",
            {
                "address": "0x833589fcD6edb6E08f4c7C32D4f71b54bdA02913",
                "symbol": "FAKE",
                "decimals": 18,
            },
        )

        self.assertEqual(token["symbol"], "USDC")
        self.assertEqual(token["decimals"], 6)
        self.assertEqual(token["balance"], "4.82")

    def test_wallet_payment_token_resolver_rejects_token_outside_user_wallet(self):
        resolver = WalletPaymentTokenResolver(
            lambda user_id: {"ok": True, "tokens": []}
        )

        with self.assertRaisesRegex(ValueError, "not available in this wallet"):
            resolver.resolve(
                "1045618308",
                {"address": "0x2222222222222222222222222222222222222222"},
            )

    def test_quote_service_prices_authenticated_users_selected_wallet_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            pricer = FixedWalletTokenPricer()
            resolver = WalletPaymentTokenResolver(
                lambda user_id: {
                    "ok": True,
                    "tokens": [
                        {
                            "symbol": "SINGIT",
                            "contractAddress": "0x1111111111111111111111111111111111111111",
                            "balance": "9180933.33",
                            "decimals": 18,
                            "verified": True,
                        }
                    ],
                }
            )
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                real_rate_pricer=pricer,
                payment_token_resolver=resolver,
                quote_id_provider=lambda: "quote_usdc",
                now_provider=lambda: 1_719_000_000,
            )

            quote = service.quote(
                {
                    "productId": "test-gift-card-link",
                    "packageId": "1",
                    "country": "US",
                    "telegramUserId": "1045618308",
                    "paymentToken": {
                        "address": "0x1111111111111111111111111111111111111111"
                    },
                }
            )

            self.assertEqual(quote["paymentTokenSymbol"], "SINGIT")
            self.assertEqual(
                pricer.calls,
                [
                    (
                        "1.00",
                        {
                            "from_token": "0x1111111111111111111111111111111111111111",
                            "decimals": 18,
                            "max_amount": "9180933.33",
                        },
                    )
                ],
            )

    def test_quote_service_uses_usdc_directly_without_requesting_a_swap_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            pricer = Mock()
            pricer.price_for_usdc.side_effect = AssertionError(
                "USDC must not be quoted against itself"
            )
            resolver = WalletPaymentTokenResolver(
                lambda user_id: {
                    "ok": True,
                    "tokens": [
                        {
                            "symbol": "USDC",
                            "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                            "balance": "4.82",
                            "decimals": 6,
                            "verified": True,
                        }
                    ],
                }
            )
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                real_rate_pricer=pricer,
                payment_token_resolver=resolver,
                quote_id_provider=lambda: "quote_direct_usdc",
                now_provider=lambda: 1_719_000_000,
            )

            quote = service.quote(
                {
                    "productId": "test-gift-card-link",
                    "packageId": "1",
                    "country": "US",
                    "telegramUserId": "1045618308",
                    "paymentToken": {
                        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
                    },
                }
            )

            pricer.price_for_usdc.assert_not_called()
            self.assertEqual(quote["paymentTokenSymbol"], "USDC")
            self.assertEqual(quote["paymentTokenAmount"], "1")
            self.assertEqual(quote["maxPaymentTokenAtomic"], "1000000")
            self.assertEqual(quote["requiredUsdc"], "1")

    def test_quote_service_rejects_direct_usdc_when_wallet_balance_is_too_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=BitrefillCommerceStore(Path(tmp) / "orders.sqlite3"),
                singit_usd_price_provider=lambda: "0.01",
                real_rate_pricer=FixedWalletTokenPricer(),
                payment_token_resolver=WalletPaymentTokenResolver(
                    lambda user_id: {
                        "ok": True,
                        "tokens": [
                            {
                                "symbol": "USDC",
                                "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                                "balance": "0.50",
                                "decimals": 6,
                                "verified": True,
                            }
                        ],
                    }
                ),
            )

            with self.assertRaisesRegex(ValueError, "USDC balance is insufficient"):
                service.quote(
                    {
                        "productId": "test-gift-card-link",
                        "packageId": "1",
                        "country": "US",
                        "telegramUserId": "1045618308",
                        "paymentToken": {
                            "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
                        },
                    }
                )

    def test_quote_service_requires_payment_token_for_authenticated_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=BitrefillCommerceStore(Path(tmp) / "orders.sqlite3"),
                singit_usd_price_provider=lambda: "0.01",
                real_rate_pricer=FixedWalletTokenPricer(),
                payment_token_resolver=WalletPaymentTokenResolver(
                    lambda user_id: {"ok": True, "tokens": []}
                ),
            )

            with self.assertRaisesRegex(ValueError, "paymentToken is required"):
                service.quote(
                    {
                        "productId": "test-gift-card-link",
                        "packageId": "1",
                        "country": "US",
                        "telegramUserId": "1045618308",
                    }
                )

    def test_catalog_service_maps_filters_and_returns_page_metadata(self):
        products = [{"productId": f"product-{index}"} for index in range(9)]
        client = Mock()
        client.list_products.return_value = products
        service = BitrefillCatalogService(bitrefill_client=client)

        page = service(
            {
                "country": "CZ",
                "category": "Food",
                "start": 8,
                "limit": 8,
                "includeInternational": True,
                "includeTestProducts": False,
            }
        )

        client.list_products.assert_called_once_with(
            country="CZ,XI",
            category="food,restaurants,food-delivery,groceries",
            start=8,
            limit=9,
            include_test_products=False,
        )
        self.assertEqual(
            page,
            {
                "ok": True,
                "products": products[:8],
                "start": 8,
                "limit": 8,
                "hasPrevious": True,
                "hasNext": True,
            },
        )

    def test_catalog_service_maps_all_supported_categories(self):
        self.assertEqual(
            BITREFILL_BROWSE_CATEGORIES,
            {
                "all": "",
                "shopping": "retail,ecommerce,gifts,electronics,apparel",
                "food": "food,restaurants,food-delivery,groceries",
                "games": "games",
                "mobile": "refill,phone,data,bundles",
                "travel": "travel,flights,experiences",
                "entertainment": "entertainment,streaming,music",
            },
        )

    def test_catalog_service_validates_country_category_and_pagination(self):
        service = BitrefillCatalogService(bitrefill_client=Mock())
        invalid_payloads = [
            ({"country": "C"}, "country"),
            ({"country": "ČZ"}, "country"),
            ({"country": "CZ1"}, "country"),
            ({"country": "CZ", "category": "unknown"}, "category"),
            ({"country": "CZ", "start": -1}, "start"),
            ({"country": "CZ", "limit": 0}, "limit"),
            ({"country": "CZ", "limit": 21}, "limit"),
            ({"country": "CZ", "start": True}, "start"),
            ({"country": "CZ", "limit": False}, "limit"),
        ]

        for overrides, error in invalid_payloads:
            payload = {"country": "CZ", "category": "all", "start": 0, "limit": 8}
            payload.update(overrides)
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, error):
                    service(payload)

        service.bitrefill_client.list_products.assert_not_called()

    def test_catalog_service_rejects_non_boolean_flags(self):
        client = Mock()
        client.list_products.return_value = []
        service = BitrefillCatalogService(bitrefill_client=client)

        for flag in ("includeInternational", "includeTestProducts"):
            for value in ("false", 0):
                payload = {
                    "country": "CZ",
                    "category": "all",
                    "start": 0,
                    "limit": 8,
                    flag: value,
                }
                with self.subTest(flag=flag, value=value):
                    with self.assertRaisesRegex(ValueError, flag):
                        service(payload)

        client.list_products.assert_not_called()

    def test_catalog_services_search_and_return_product_details(self):
        client = TestBitrefillClient()

        search = BitrefillSearchService(bitrefill_client=client)(
            {"query": "phone", "country": "US", "includeTestProducts": True}
        )
        details = BitrefillProductDetailsService(bitrefill_client=client)(
            {"productId": "test-phone-refill", "country": "US"}
        )

        self.assertEqual(search["products"][0]["productId"], "test-phone-refill")
        self.assertEqual(details["recipientType"], "phone")

    def test_search_service_can_search_all_countries(self):
        client = Mock()
        client.search_products.return_value = [
            {
                "productId": "bitrefill-giftcard-usd",
                "name": "Bitrefill Gift Card (USD)",
                "country": "US",
            }
        ]
        service = BitrefillSearchService(bitrefill_client=client)

        result = service(
            {
                "query": "Bitrefill Gift Card",
                "country": "CZ",
                "searchAllCountries": True,
            }
        )

        self.assertEqual(result["products"][0]["productId"], "bitrefill-giftcard-usd")
        client.search_products.assert_called_once_with(
            query="Bitrefill Gift Card",
            country="",
            category="",
            product_type="",
            include_test_products=False,
        )

    def test_quote_service_quotes_selected_phone_refill(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_phone",
                now_provider=lambda: 1_719_000_000,
            )

            quote = service.quote(
                {
                    "productId": "test-phone-refill",
                    "packageId": "1",
                    "country": "US",
                    "recipient": {"phone": "+12025550123"},
                }
            )

            self.assertEqual(quote["productId"], "test-phone-refill")
            self.assertEqual(quote["packageId"], "1")

    def test_quote_service_saves_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )

            quote = service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})

            self.assertEqual(quote["quoteId"], "quote_1")
            self.assertEqual(store.get_quote("quote_1")["state"], "QUOTED")

    def test_quote_service_uses_configured_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
                ttl_seconds=900,
            )

            quote = service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})

            self.assertEqual(quote["expiresAtEpoch"], 1_719_000_900)
            self.assertIn("Quote expires in 900s", quote["quoteText"])

    def test_quote_service_can_use_real_rate_pricer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                real_rate_pricer=FixedRealRatePricer(),
                quote_id_provider=lambda: "quote_real_1",
                now_provider=lambda: 1_719_000_000,
            )

            quote = service.quote(
                {"productId": "test-gift-card-code", "packageId": "1", "country": "US"}
            )

            self.assertEqual(quote["pricingMode"], "bankr_real_rate")
            self.assertEqual(quote["singitAmount"], "25000")
            self.assertEqual(
                store.get_quote("quote_real_1")["quote"]["maxSingitAtomic"],
                "25000000000000000000000",
            )

    def test_runner_requires_firefly_before_bankr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            firefly.approve_payment_hash.return_value = {
                "approved": False,
                "approvedHash": "",
                "raw": "<CANCEL",
            }
            bankr = Mock()

            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
            )

            result = runner.buy({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})

            self.assertFalse(result["ok"])
            self.assertEqual(result["decision"], "rejected_by_firefly")
            bankr.assert_not_called()

    def test_runner_checks_reserve_before_firefly_or_bankr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote(
                {"productId": "test-gift-card-link", "packageId": "1", "country": "US"}
            )
            firefly = Mock()
            bankr = Mock()
            guard = Mock(side_effect=ValueError("insufficient USDC reserve"))
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                pre_payment_guard=guard,
            )

            with self.assertRaisesRegex(ValueError, "insufficient USDC reserve"):
                runner.buy({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})

            guard.assert_called_once_with(quote)
            firefly.approve_payment_hash.assert_not_called()
            bankr.assert_not_called()
            self.assertEqual(store.get_quote("quote_1")["state"], "QUOTED")

    def test_runner_calls_bankr_after_firefly_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr = Mock(return_value={"ok": True, "status": 200, "body": {"ok": True, "orderId": "order_1"}})

            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "fulfill_secret_1",
            )
            expected_hash = runner.payment_hash_for_quote(quote, recipient={"email": "buyer@example.com"})
            firefly.approve_payment_hash.return_value = {"approved": True, "approvedHash": expected_hash}

            result = runner.buy({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})

            self.assertTrue(result["ok"])
            self.assertEqual(result["fulfillmentToken"], "fulfill_secret_1")
            bankr.assert_called_once_with(
                "https://x402.bankr.bot/wallet/buy-bitrefill",
                request_body={"quoteId": "quote_1", "fulfillmentToken": "fulfill_secret_1"},
            )
            metadata = store.get_quote("quote_1")["metadata"]
            self.assertEqual(
                metadata["fulfillmentTokenHash"],
                hashlib.sha256(b"fulfill_secret_1").hexdigest(),
            )
            self.assertEqual(metadata["recipient"], {"email": "buyer@example.com"})
            self.assertNotIn("fulfill_secret_1", str(metadata))
            self.assertIn(
                "✅ Test Gift Card Link $1 is ready.",
                result["telegramText"],
            )
            self.assertIn("paid with SINGIT", result["telegramText"])
            self.assertNotIn("x402", result["telegramText"].lower())
            self.assertNotIn("invoice", result["telegramText"].lower())
            self.assertNotIn("usdc", result["telegramText"].lower())

    def test_runner_blocks_bitrefill_fulfillment_without_verified_singit_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr = Mock(return_value={"ok": True, "status": 200, "body": {"ok": True}, "transactionHash": None})
            fulfillment = Mock()
            settlement_verifier = Mock(side_effect=ValueError("SINGIT settlement transaction hash is missing"))
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "fulfill_secret_1",
                settlement_verifier=settlement_verifier,
                fulfillment_runner=fulfillment,
            )
            expected_hash = runner.payment_hash_for_quote(quote, recipient={})
            firefly.approve_payment_hash.return_value = {"approved": True, "approvedHash": expected_hash}

            with self.assertRaisesRegex(ValueError, "SINGIT settlement transaction hash is missing"):
                runner.buy({"quoteId": "quote_1"})

            bankr.assert_called_once()
            settlement_verifier.assert_called_once()
            fulfillment.assert_not_called()
            record = store.get_quote("quote_1")
            self.assertEqual(record["state"], "RECONCILIATION_REQUIRED")
            self.assertEqual(
                record["metadata"]["singitSettlementError"],
                "SINGIT settlement transaction hash is missing",
            )

    def test_runner_fulfills_bitrefill_only_after_verified_singit_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr_result = {
                "ok": True,
                "status": 200,
                "body": {"ok": True, "quoteId": "quote_1", "status": "singit_settlement_requested"},
                "transactionHash": "0xSINGITTX",
            }
            bankr = Mock(return_value=bankr_result)
            settlement_verifier = Mock(return_value={"transactionHash": "0xSINGITTX", "amountAtomic": quote["maxSingitAtomic"]})
            fulfillment = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=TestBitrefillClient(),
                now_provider=lambda: 1_719_000_002,
            )
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "fulfill_secret_1",
                settlement_verifier=settlement_verifier,
                fulfillment_runner=fulfillment,
            )
            expected_hash = runner.payment_hash_for_quote(quote, recipient={})
            firefly.approve_payment_hash.return_value = {"approved": True, "approvedHash": expected_hash}

            result = runner.buy({"quoteId": "quote_1"})

            self.assertTrue(result["ok"])
            self.assertEqual(result["decision"], "approved_and_executed")
            self.assertEqual(result["bitrefill"]["orderId"], store.get_quote("quote_1")["metadata"]["bitrefill"]["orderId"])
            self.assertEqual(store.get_quote("quote_1")["state"], "DELIVERED")
            settlement_verifier.assert_called_once_with(bankr_result=bankr_result, quote=quote)

    def test_runner_rejects_expired_quote_before_firefly_or_bankr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr = Mock()
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_121,
            )

            with self.assertRaisesRegex(ValueError, "quote expired"):
                runner.buy({"quoteId": "quote_1"})

            self.assertEqual(store.get_quote("quote_1")["state"], "QUOTE_EXPIRED")
            firefly.approve_payment_hash.assert_not_called()
            bankr.assert_not_called()

    def test_wallet_runner_fulfills_without_bankr_x402_payment(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_wallet_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote(
                {"productId": "test-gift-card-link", "packageId": "1", "country": "US"}
            )
            funding = FakeFundingRunner()
            fulfillment = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=TestBitrefillClient(),
                funding_runner=funding,
                now_provider=lambda: 1_719_000_002,
            )
            user_funding = FakeUserFundingRunner()
            approval = Mock()
            runner = WalletBitrefillPurchaseRunner(
                store=store,
                approval_client=approval,
                fulfillment_runner=fulfillment,
                user_funding_runner=user_funding,
                source_wallet_provider=lambda user_id: "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C",
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "wallet_fulfill_secret_1",
            )
            expected_hash = runner.payment_hash_for_quote(
                quote,
                recipient={"email": "buyer@example.com"},
            )
            approval.return_value = {"approved": True, "approvedHash": expected_hash}

            result = runner.buy(
                {
                    "quoteId": "quote_wallet_1",
                    "recipient": {"email": "buyer@example.com"},
                    "telegramUserId": "1045618308",
                }
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["decision"], "approved_and_fulfilled")
            self.assertEqual(result["fulfillmentToken"], "wallet_fulfill_secret_1")
            self.assertEqual(result["walletCheckout"]["paymentApprovalHash"], expected_hash)
            self.assertEqual(result["walletCheckout"]["userFunding"]["fromWallet"], "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C")
            self.assertIn("Paid from 0xAc4a...f45C", result["telegramText"])
            self.assertIn("Use /last_purchase to reveal your code", result["telegramText"])
            self.assertNotIn("bankr", result)
            self.assertEqual(len(funding.calls), 1)
            self.assertEqual(len(user_funding.calls), 1)
            self.assertEqual(user_funding.calls[0]["telegram_user_id"], "1045618308")
            self.assertEqual(user_funding.calls[0]["quote"]["quoteId"], "quote_wallet_1")
            record = store.get_quote("quote_wallet_1")
            self.assertEqual(record["state"], "DELIVERED")
            self.assertEqual(record["metadata"]["recipient"], {"email": "buyer@example.com"})
            self.assertEqual(record["metadata"]["walletCheckout"]["approval"]["approved"], True)
            self.assertEqual(record["metadata"]["walletCheckout"]["userFunding"]["transfer"]["txId"], "0xUSERTRANSFER")
            self.assertNotIn("wallet_fulfill_secret_1", str(record["metadata"]))
            self.assertEqual(approval.call_args.kwargs["telegram_user_id"], "1045618308")
            context_lines = approval.call_args.kwargs["context_lines"]
            self.assertIn("Action: BUY BITREFILL", context_lines)
            self.assertIn("Product: Test Gift Card Link", context_lines)
            self.assertIn("Cost: 1 USD", context_lines)
            self.assertIn("Max spend: 105 SINGIT", context_lines)
            self.assertIn("Paid from: 0xAc4a...f45C", context_lines)
            self.assertIn("Expires: 2 minutes", context_lines)
            self.assertIn("Spent: 105 SINGIT", result["telegramText"])
            self.assertIn("Transfer tx: https://basescan.org/tx/0xUSERTRANSFER", result["telegramText"])

    def test_wallet_runner_rejects_unconfirmed_checkout_before_fulfillment(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_wallet_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote(
                {"productId": "test-gift-card-link", "packageId": "1", "country": "US"}
            )
            fulfillment = Mock()
            approval = Mock(return_value={"approved": False, "approvedHash": ""})
            runner = WalletBitrefillPurchaseRunner(
                store=store,
                approval_client=approval,
                fulfillment_runner=fulfillment,
                now_provider=lambda: 1_719_000_001,
            )

            result = runner.buy({"quoteId": "quote_wallet_1"})

            self.assertFalse(result["ok"])
            self.assertEqual(result["decision"], "rejected_by_user")
            fulfillment.assert_not_called()
            self.assertEqual(store.get_quote("quote_wallet_1")["state"], "USER_REJECTED")

    def test_runner_rejects_replay_of_non_quoted_order_before_firefly_or_bankr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state("quote_1", "DELIVERED")
            firefly = Mock()
            bankr = Mock()
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
            )

            with self.assertRaisesRegex(ValueError, "quote is not purchasable"):
                runner.buy({"quoteId": "quote_1"})

            firefly.approve_payment_hash.assert_not_called()
            bankr.assert_not_called()

    def test_runner_marks_reconciliation_required_when_bankr_call_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            firefly = Mock()
            bankr = Mock(side_effect=RuntimeError("bankr timeout"))
            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
                now_provider=lambda: 1_719_000_001,
                fulfillment_token_provider=lambda: "fulfill_secret_1",
            )
            expected_hash = runner.payment_hash_for_quote(quote, recipient={})
            firefly.approve_payment_hash.return_value = {
                "approved": True,
                "approvedHash": expected_hash,
            }

            with self.assertRaisesRegex(RuntimeError, "bankr timeout"):
                runner.buy({"quoteId": "quote_1"})

            record = store.get_quote("quote_1")
            self.assertEqual(record["state"], "RECONCILIATION_REQUIRED")
            self.assertEqual(record["metadata"]["bankrError"], "bankr timeout")

    def test_fulfillment_runner_buys_once_and_rejects_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"fulfill_secret_1").hexdigest()},
            )

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=TestBitrefillClient(),
                now_provider=lambda: 1_719_000_001,
            )
            result1 = runner.fulfill(
                {
                    "quoteId": "quote_1",
                    "fulfillmentToken": "fulfill_secret_1",
                    "recipient": {"email": "buyer@example.com"},
                }
            )

            self.assertTrue(result1["ok"])
            self.assertEqual(result1["quoteId"], "quote_1")
            self.assertIn("orderId", result1)
            self.assertNotIn("redemption", result1)
            self.assertNotIn("buyer@example.com", str(result1))
            with self.assertRaisesRegex(ValueError, "already fulfilled"):
                runner.fulfill(
                    {"quoteId": "quote_1", "fulfillmentToken": "fulfill_secret_1"}
                )

    def test_fulfillment_rejects_quote_that_expires_before_provider_purchase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            bitrefill = Mock()

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_121,
            )

            with self.assertRaisesRegex(ValueError, "quote expired"):
                runner.fulfill({"quoteId": "quote_1"})

            self.assertEqual(store.get_quote("quote_1")["state"], "QUOTE_EXPIRED")
            bitrefill.buy_product.assert_not_called()

    def test_fulfillment_rejects_token_not_bound_to_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
            )
            bitrefill = Mock()

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_001,
            )

            with self.assertRaisesRegex(ValueError, "invalid fulfillment token"):
                runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "wrong_token"})

            bitrefill.buy_product.assert_not_called()

    def test_fulfillment_uses_firefly_approved_recipient_from_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {
                    "fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest(),
                    "recipient": {"email": "approved@example.com"},
                },
            )
            bitrefill = Mock(
                **{
                    "buy_product.return_value": {
                        "ok": True,
                        "orderId": "order_1",
                        "status": "delivered",
                    }
                }
            )

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_001,
            )
            runner.fulfill(
                {
                    "quoteId": "quote_1",
                    "fulfillmentToken": "valid_token",
                    "recipient": {"email": "attacker@example.com"},
                }
            )

            bitrefill.buy_product.assert_called_once_with(
                quote=quote,
                recipient={"email": "approved@example.com"},
                checkpoint_callback=ANY,
            )

    def test_fulfillment_swaps_singit_before_bitrefill_purchase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "productType": "gift_card",
                    "packageId": "1",
                    "packageValue": "1",
                    "priceUsd": "1.00",
                    "pricingMode": "bankr_real_rate",
                    "expectedUsdc": "1.10",
                    "maxSingitAtomic": "25000000000000000000000",
                    "singitAmount": "25000",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
            )
            funding = FakeFundingRunner()
            bitrefill = Mock(
                **{
                    "buy_product.return_value": {
                        "ok": True,
                        "orderId": "order_1",
                        "status": "delivered",
                    }
                }
            )
            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                funding_runner=funding,
                now_provider=lambda: 1_719_000_001,
            )

            runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

            self.assertEqual(funding.calls[0]["quoteId"], "quote_1")
            bitrefill.buy_product.assert_called_once()
            self.assertEqual(store.get_quote("quote_1")["metadata"]["bankrSwap"]["txId"], "0xSWAP")

    def test_fulfillment_does_not_buy_bitrefill_when_swap_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "productType": "gift_card",
                    "packageId": "1",
                    "packageValue": "1",
                    "priceUsd": "1.00",
                    "pricingMode": "bankr_real_rate",
                    "expectedUsdc": "1.10",
                    "maxSingitAtomic": "25000000000000000000000",
                    "singitAmount": "25000",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
            )
            bitrefill = Mock()
            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                funding_runner=FakeFundingRunner(fail=True),
                now_provider=lambda: 1_719_000_001,
            )

            with self.assertRaisesRegex(RuntimeError, "swap route failed"):
                runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

            bitrefill.buy_product.assert_not_called()
            record = store.get_quote("quote_1")
            self.assertEqual(record["state"], "RECONCILIATION_REQUIRED")
            self.assertEqual(record["metadata"]["fundingError"], "swap route failed")

    def test_settlement_preparation_returns_real_rate_pricing_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "productType": "gift_card",
                    "packageId": "1",
                    "packageValue": "1",
                    "priceUsd": "1.00",
                    "pricingMode": "bankr_real_rate",
                    "expectedUsdc": "1.10",
                    "maxSingitAtomic": "25000000000000000000000",
                    "singitAmount": "25000",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
            )
            runner = BitrefillSettlementPreparationRunner(
                store=store,
                now_provider=lambda: 1_719_000_001,
            )

            result = runner.prepare({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

            self.assertEqual(result["pricingMode"], "bankr_real_rate")
            self.assertEqual(result["settleAmountAtomic"], "25000000000000000000000")

    def test_order_lookup_redacts_private_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "packageValue": "25",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "DELIVERED",
                {
                    "recipient": {"email": "private@example.com"},
                    "fulfillmentTokenHash": "a" * 64,
                    "bankr": {"stdout": "private diagnostic"},
                    "bitrefill": {"orderId": "order_1", "status": "delivered"},
                },
            )

            from sign402_gateway.bitrefill_runner import lookup_bitrefill_order

            result = lookup_bitrefill_order(store, "quote_1")

            self.assertEqual(result["quoteId"], "quote_1")
            self.assertEqual(result["state"], "DELIVERED")
            self.assertEqual(result["orderId"], "order_1")
            self.assertNotIn("private@example.com", str(result))
            self.assertNotIn("fulfillmentTokenHash", result)
            self.assertNotIn("metadata", result)

    def test_order_lookup_can_reveal_redemption_when_recipient_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "packageValue": "25",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "DELIVERED",
                {
                    "recipient": {"email": "buyer@example.com"},
                    "bitrefill": {
                        "orderId": "order_1",
                        "status": "delivered",
                        "redemption": {
                            "type": "bitrefill",
                            "label": "Bitrefill redemption",
                            "value": {"code": "SECRET-CODE"},
                        },
                    },
                },
            )

            from sign402_gateway.bitrefill_runner import lookup_bitrefill_order

            result = lookup_bitrefill_order(
                store,
                "quote_1",
                include_redemption=True,
                recipient={"email": "buyer@example.com"},
            )

            self.assertEqual(result["redemption"]["value"]["code"], "SECRET-CODE")
            self.assertEqual(
                result["telegramText"],
                "✅ Test Gift Card Code $25 is ready.\nCode: SECRET-CODE",
            )

    def test_order_lookup_requires_fulfillment_token_when_no_recipient_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "packageValue": "25",
                    "productType": "gift_card",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "DELIVERED",
                {
                    "fulfillmentTokenHash": hashlib.sha256(b"reveal_tok").hexdigest(),
                    "bitrefill": {
                        "orderId": "order_1",
                        "status": "delivered",
                        "redemption": {
                            "type": "bitrefill",
                            "label": "Bitrefill redemption",
                            "value": {"code": "SECRET-CODE"},
                        },
                    },
                },
            )

            from sign402_gateway.bitrefill_runner import lookup_bitrefill_order

            with self.assertRaises(ValueError):
                lookup_bitrefill_order(store, "quote_1", include_redemption=True)

            with self.assertRaises(ValueError):
                lookup_bitrefill_order(
                    store,
                    "quote_1",
                    include_redemption=True,
                    fulfillment_token="wrong_tok",
                )

            result = lookup_bitrefill_order(
                store,
                "quote_1",
                include_redemption=True,
                fulfillment_token="reveal_tok",
            )
            self.assertEqual(result["redemption"]["value"]["code"], "SECRET-CODE")

    def test_pending_order_refreshes_without_repurchase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-code", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {
                    "fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest(),
                    "recipient": {"email": "buyer@example.com"},
                },
            )
            bitrefill = PendingThenDeliveredBitrefillClient()
            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_001,
            )

            first_result = runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})
            refreshed = lookup_bitrefill_order(
                store,
                "quote_1",
                include_redemption=True,
                recipient={"email": "buyer@example.com"},
                bitrefill_client=bitrefill,
            )

            self.assertEqual(first_result["status"], "created")
            self.assertEqual(refreshed["state"], "DELIVERED")
            self.assertEqual(refreshed["redemption"]["value"]["code"], "READY-123")
            self.assertEqual(bitrefill.buy_calls, 1)
            self.assertEqual(bitrefill.refresh_calls, 1)

    def test_order_lookup_rejects_redemption_reveal_for_wrong_recipient(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote(
                {
                    "quoteId": "quote_1",
                    "productId": "test-gift-card-code",
                    "productName": "Test Gift Card Code",
                    "packageValue": "25",
                    "expiresAtEpoch": 1_719_000_120,
                }
            )
            store.advance_state(
                "quote_1",
                "DELIVERED",
                {
                    "recipient": {"email": "buyer@example.com"},
                    "bitrefill": {
                        "orderId": "order_1",
                        "status": "delivered",
                        "redemption": {"value": {"code": "SECRET-CODE"}},
                    },
                },
            )

            from sign402_gateway.bitrefill_runner import lookup_bitrefill_order

            with self.assertRaisesRegex(ValueError, "recipient does not match"):
                lookup_bitrefill_order(
                    store,
                    "quote_1",
                    include_redemption=True,
                    recipient={"email": "attacker@example.com"},
                )

    def test_fulfillment_records_provider_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=TestBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"productId": "test-gift-card-link", "packageId": "1", "country": "US"})
            store.advance_state(
                "quote_1",
                "FIREFLY_APPROVED",
                {
                    "fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest(),
                    "recipient": {"email": "approved@example.com"},
                },
            )
            bitrefill = Mock()
            bitrefill.buy_product.side_effect = RuntimeError("provider unavailable")

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(
                store=store,
                bitrefill_client=bitrefill,
                now_provider=lambda: 1_719_000_001,
            )

            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

            record = store.get_quote("quote_1")
            self.assertEqual(record["state"], "FULFILLMENT_FAILED")
            self.assertEqual(record["metadata"]["fulfillmentError"], "provider unavailable")


if __name__ == "__main__":
    unittest.main()
