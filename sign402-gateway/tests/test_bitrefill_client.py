import unittest

from sign402_gateway.bitrefill import LiveBitrefillClient, TestBitrefillClient


class FakeBitrefillTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, *, query=None, body=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": query or {},
                "body": body,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected Bitrefill call: {method} {path}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeTreasuryClient:
    def __init__(self):
        self.transfers = []

    def transfer_usdc(self, *, to_address, amount, chain="base"):
        self.transfers.append(
            {
                "to_address": to_address,
                "amount": amount,
                "chain": chain,
            }
        )
        return {"ok": True, "txId": "0xUSDC"}


class BitrefillClientTests(unittest.TestCase):
    def test_test_catalog_searches_multiple_product_types(self):
        client = TestBitrefillClient()

        all_products = client.search_products(
            query="",
            country="US",
            category="",
            product_type="",
            include_test_products=True,
        )
        phone_products = client.search_products(
            query="",
            country="US",
            category="",
            product_type="phone_refill",
            include_test_products=True,
        )

        self.assertEqual(
            {product["productId"] for product in all_products},
            {"test-gift-card-link", "test-gift-card-code", "test-phone-refill"},
        )
        self.assertEqual(
            [product["productId"] for product in phone_products],
            ["test-phone-refill"],
        )

    def test_phone_refill_details_expose_packages_and_recipient_requirement(self):
        details = TestBitrefillClient().get_product_details(
            product_id="test-phone-refill",
            country="US",
        )

        self.assertEqual(details["productType"], "phone_refill")
        self.assertEqual(details["recipientType"], "phone")
        self.assertIn("phone", details["requiredRecipientFields"])
        self.assertTrue(details["packages"])

    def test_quote_product_validates_package_and_phone(self):
        selected = TestBitrefillClient().quote_product(
            product_id="test-phone-refill",
            package_id="1",
            country="US",
            recipient={"phone": "+12025550123"},
        )

        self.assertEqual(selected["productId"], "test-phone-refill")
        self.assertEqual(selected["packageId"], "1")
        self.assertEqual(selected["priceUsd"], "1.00")

    def test_quote_product_requires_phone_for_phone_refill(self):
        with self.assertRaisesRegex(ValueError, "recipient.phone is required"):
            TestBitrefillClient().quote_product(
                product_id="test-phone-refill",
                package_id="1",
                country="US",
                recipient={},
            )

    def test_test_purchase_returns_non_value_redemption_reference(self):
        client = TestBitrefillClient()
        result = client.buy_product(
            quote={
                "quoteId": "quote_1",
                "productId": "test-gift-card-code",
                "packageId": "1",
                "packageValue": "1",
                "priceUsd": "1.00",
            },
            recipient={"email": "buyer@example.com"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "bitrefill-test")
        self.assertIn("orderId", result)
        self.assertNotIn("buyer@example.com", str(result))

    def test_live_search_normalizes_bitrefill_products(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": [
                        {
                            "id": "amazon_com-usa",
                            "name": "Amazon.com Gift Card",
                            "country_code": "US",
                            "currency": "USD",
                            "recipient_type": "none",
                            "in_stock": True,
                            "packages": [
                                {
                                    "id": "amazon_com-usa<&>5",
                                    "value": "5",
                                    "price": 5,
                                }
                            ],
                        }
                    ]
                }
            ]
        )
        client = LiveBitrefillClient(api_key="key_123", request_json=transport)

        products = client.search_products(
            query="Amazon",
            country="US",
            category="",
            product_type="",
            include_test_products=False,
        )

        self.assertEqual(products[0]["productId"], "amazon_com-usa")
        self.assertEqual(products[0]["country"], "US")
        self.assertEqual(products[0]["packages"][0]["packageId"], "amazon_com-usa<&>5")
        self.assertEqual(products[0]["packages"][0]["priceUsd"], "5.00")
        self.assertEqual(
            transport.calls[0],
            {
                "method": "GET",
                "path": "/products/search",
                "query": {
                    "q": "Amazon",
                    "limit": "50",
                    "include_test_products": "false",
                },
                "body": None,
            },
        )

    def test_live_usd_range_exposes_minimum_face_value(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "bitrefill-giftcard-usd",
                        "name": "Bitrefill Gift Card (USD)",
                        "country_code": "US",
                        "currency": "USD",
                        "recipient_type": "none",
                        "in_stock": True,
                        "range": {
                            "min": 0.1,
                            "max": 2000,
                            "step": 0.01,
                            "price_rate": 1600.413832903551,
                        },
                        "packages": [
                            {
                                "id": "bitrefill-giftcard-usd<&>1",
                                "value": "1",
                                "price": 1601,
                                "amount": 1,
                            }
                        ],
                    }
                }
            ]
        )
        client = LiveBitrefillClient(api_key="key_123", request_json=transport)

        product = client.get_product_details(
            product_id="bitrefill-giftcard-usd",
            country="US",
        )

        packages = {package["packageId"]: package for package in product["packages"]}
        self.assertEqual(packages["0.1"]["value"], "0.1")
        self.assertEqual(packages["0.1"]["priceUsd"], "0.10")
        self.assertEqual(packages["bitrefill-giftcard-usd<&>1"]["priceUsd"], "1.00")

    def test_live_quote_requires_phone_for_phone_refill(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "tmobile_usa",
                        "name": "T-Mobile USA",
                        "country_code": "US",
                        "currency": "USD",
                        "recipient_type": "phone_number",
                        "in_stock": True,
                        "packages": [
                            {"id": "tmobile_usa<&>5", "value": "5", "price": 5}
                        ],
                    }
                }
            ]
        )
        client = LiveBitrefillClient(api_key="key_123", request_json=transport)

        with self.assertRaisesRegex(ValueError, "recipient.phone is required"):
            client.quote_product(
                product_id="tmobile_usa",
                package_id="tmobile_usa<&>5",
                country="US",
                recipient={},
            )

    def test_live_buy_uses_balance_invoice_after_singit_settlement(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "complete",
                        "orders": [{"id": "order_1"}],
                    }
                },
                {
                    "data": {
                        "id": "order_1",
                        "status": "delivered",
                        "redemption_info": {
                            "code": "SECRET-CODE-123",
                            "instructions": "Redeem at merchant",
                        },
                    }
                },
            ]
        )
        client = LiveBitrefillClient(
            api_key="key_123",
            max_purchase_usd="5.00",
            request_json=transport,
        )

        result = client.buy_product(
            quote={
                "quoteId": "quote_live_1",
                "productId": "amazon_com-usa",
                "productType": "gift_card",
                "packageId": "amazon_com-usa<&>5",
                "packageValue": "5",
                "priceUsd": "5.00",
            },
            recipient={"email": "buyer@example.com"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "bitrefill-live")
        self.assertEqual(result["invoiceId"], "invoice_1")
        self.assertEqual(result["orderId"], "order_1")
        self.assertEqual(result["redemption"]["value"]["code"], "SECRET-CODE-123")
        self.assertNotIn("buyer@example.com", str(result))
        self.assertEqual(
            transport.calls[0]["body"],
            {
                "products": [
                    {
                        "product_id": "amazon_com-usa",
                        "package_id": "amazon_com-usa<&>5",
                        "quantity": 1,
                    }
                ],
                "payment_method": "balance",
                "auto_pay": True,
            },
        )

    def test_live_buy_uses_usdc_base_invoice_and_treasury_transfer(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "unpaid",
                        "payment": {
                            "address": "0xBitrefillInvoice",
                            "price": 5.01,
                            "currency": "USDC",
                        },
                        "orders": [{"id": "order_1"}],
                    }
                },
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "complete",
                        "orders": [{"id": "order_1"}],
                    }
                },
                {
                    "data": {
                        "id": "order_1",
                        "status": "delivered",
                        "redemption_info": {"code": "SECRET-CODE-123"},
                    }
                },
            ]
        )
        treasury = FakeTreasuryClient()
        client = LiveBitrefillClient(
            api_key="key_123",
            max_purchase_usd="5.50",
            payment_method="usdc_base",
            refund_address="0xTreasuryRefund",
            treasury_client=treasury,
            invoice_poll_attempts=2,
            invoice_poll_interval_seconds=0,
            request_json=transport,
        )

        result = client.buy_product(
            quote={
                "quoteId": "quote_live_1",
                "productId": "amazon_com-usa",
                "productType": "gift_card",
                "packageId": "amazon_com-usa<&>5",
                "packageValue": "5",
                "priceUsd": "5.00",
            },
            recipient={"email": "buyer@example.com"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["invoiceId"], "invoice_1")
        self.assertEqual(result["orderId"], "order_1")
        self.assertEqual(result["treasuryPayment"]["txId"], "0xUSDC")
        self.assertEqual(
            treasury.transfers,
            [
                {
                    "to_address": "0xBitrefillInvoice",
                    "amount": "5.01",
                    "chain": "base",
                }
            ],
        )
        self.assertEqual(
            transport.calls[0]["body"],
            {
                "products": [
                    {
                        "product_id": "amazon_com-usa",
                        "package_id": "amazon_com-usa<&>5",
                        "quantity": 1,
                    }
                ],
                "payment_method": "usdc_base",
                "refund_address": "0xTreasuryRefund",
            },
        )
        self.assertEqual(transport.calls[1]["path"], "/invoices/invoice_1")
        self.assertEqual(transport.calls[2]["path"], "/orders/order_1")
        self.assertNotIn("buyer@example.com", str(result))

    def test_live_buy_rejects_invoice_above_quoted_price_tolerance(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "unpaid",
                        "payment": {
                            "address": "0xBitrefillInvoice",
                            "price": 5.40,
                            "currency": "USDC",
                        },
                        "orders": [{"id": "order_1"}],
                    }
                }
            ]
        )
        treasury = FakeTreasuryClient()
        client = LiveBitrefillClient(
            api_key="key_123",
            max_purchase_usd="5.50",
            payment_method="usdc_base",
            refund_address="0xTreasuryRefund",
            treasury_client=treasury,
            invoice_poll_attempts=2,
            invoice_poll_interval_seconds=0,
            request_json=transport,
        )

        with self.assertRaises(ValueError):
            client.buy_product(
                quote={
                    "quoteId": "quote_live_1",
                    "productId": "amazon_com-usa",
                    "productType": "gift_card",
                    "packageId": "amazon_com-usa<&>5",
                    "packageValue": "5",
                    "priceUsd": "5.00",
                },
                recipient={"email": "buyer@example.com"},
            )

        self.assertEqual(treasury.transfers, [])

    def test_live_buy_waits_until_gift_card_redemption_is_available(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "unpaid",
                        "payment": {
                            "address": "0xBitrefillInvoice",
                            "price": 100000,
                            "currency": "USDC",
                        },
                    }
                },
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "all_delivered",
                        "orders": [{"id": "order_1"}],
                    }
                },
                {
                    "data": {
                        "id": "order_1",
                        "status": "created",
                        "redemption_info": None,
                    }
                },
                {
                    "data": {
                        "id": "order_1",
                        "status": "delivered",
                        "redemption_info": {"code": "READY-123"},
                    }
                },
            ]
        )
        client = LiveBitrefillClient(
            api_key="key",
            max_purchase_usd="0.20",
            payment_method="usdc_base",
            refund_address="0xRefund",
            treasury_client=FakeTreasuryClient(),
            invoice_poll_attempts=2,
            invoice_poll_interval_seconds=0,
            request_json=transport,
        )

        result = client.buy_product(
            quote={
                "quoteId": "quote_1",
                "productId": "bitrefill-giftcard-usd",
                "productType": "gift_card",
                "packageId": "0.1",
                "packageValue": "0.1",
                "priceUsd": "0.10",
            },
            recipient={},
        )

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["redemption"]["value"]["code"], "READY-123")
        self.assertEqual(transport.calls[2]["path"], "/orders/order_1")
        self.assertEqual(transport.calls[3]["path"], "/orders/order_1")

    def test_live_buy_checkpoints_invoice_and_treasury_payment(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "unpaid",
                        "payment": {
                            "address": "0xBitrefillInvoice",
                            "price": 100000,
                            "currency": "USDC",
                        },
                    }
                },
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "complete",
                        "orders": [{"id": "order_1"}],
                    }
                },
                {
                    "data": {
                        "id": "order_1",
                        "status": "delivered",
                        "redemption_info": {"code": "READY-123"},
                    }
                },
            ]
        )
        checkpoints = []
        client = LiveBitrefillClient(
            api_key="key",
            max_purchase_usd="0.20",
            payment_method="usdc_base",
            refund_address="0xRefund",
            treasury_client=FakeTreasuryClient(),
            invoice_poll_attempts=2,
            invoice_poll_interval_seconds=0,
            request_json=transport,
        )

        client.buy_product(
            quote={
                "quoteId": "quote_1",
                "productId": "bitrefill-giftcard-usd",
                "productType": "gift_card",
                "packageId": "0.1",
                "packageValue": "0.1",
                "priceUsd": "0.10",
            },
            recipient={},
            checkpoint_callback=checkpoints.append,
        )

        self.assertEqual(checkpoints[0]["invoiceId"], "invoice_1")
        self.assertEqual(checkpoints[0]["payment"]["address"], "0xBitrefillInvoice")
        self.assertEqual(checkpoints[1]["treasuryPayment"]["txId"], "0xUSDC")

    def test_live_refresh_purchase_uses_existing_invoice_and_order_without_transfer(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "complete",
                        "orders": [{"id": "order_1"}],
                    }
                },
                {
                    "data": {
                        "id": "order_1",
                        "status": "delivered",
                        "redemption_info": {"code": "READY-123"},
                    }
                },
            ]
        )
        treasury = FakeTreasuryClient()
        client = LiveBitrefillClient(
            api_key="key",
            max_purchase_usd="0.20",
            payment_method="usdc_base",
            refund_address="0xRefund",
            treasury_client=treasury,
            invoice_poll_attempts=2,
            invoice_poll_interval_seconds=0,
            request_json=transport,
        )

        result = client.refresh_purchase(
            {
                "invoiceId": "invoice_1",
                "orderId": "order_1",
                "treasuryPayment": {"txId": "0xUSDC"},
            },
            {
                "quoteId": "quote_1",
                "productId": "bitrefill-giftcard-usd",
                "productType": "gift_card",
                "packageId": "0.1",
                "packageValue": "0.1",
                "priceUsd": "0.10",
            },
        )

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["redemption"]["value"]["code"], "READY-123")
        self.assertEqual(treasury.transfers, [])
        self.assertEqual(transport.calls[0]["path"], "/invoices/invoice_1")
        self.assertEqual(transport.calls[1]["path"], "/orders/order_1")

    def test_live_buy_converts_usdc_base_atomic_invoice_price_before_transfer(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "invoice_micro",
                        "status": "unpaid",
                        "payment": {
                            "address": "0xBitrefillInvoice",
                            "price": 100000,
                            "currency": "USDC",
                        },
                        "orders": [{"id": "order_micro"}],
                    }
                },
                {
                    "data": {
                        "id": "invoice_micro",
                        "status": "complete",
                        "orders": [{"id": "order_micro"}],
                    }
                },
                {
                    "data": {
                        "id": "order_micro",
                        "status": "delivered",
                        "redemption_info": {"code": "MICRO-CODE-123"},
                    }
                },
            ]
        )
        treasury = FakeTreasuryClient()
        client = LiveBitrefillClient(
            api_key="key_123",
            max_purchase_usd="0.20",
            payment_method="usdc_base",
            refund_address="0xTreasuryRefund",
            treasury_client=treasury,
            invoice_poll_attempts=2,
            invoice_poll_interval_seconds=0,
            request_json=transport,
        )

        result = client.buy_product(
            quote={
                "quoteId": "quote_micro",
                "productId": "bitrefill-giftcard-usd",
                "productType": "gift_card",
                "packageId": "0.1",
                "packageValue": "0.1",
                "priceUsd": "0.10",
            },
            recipient={},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["invoiceId"], "invoice_micro")
        self.assertEqual(result["orderId"], "order_micro")
        self.assertEqual(
            treasury.transfers,
            [
                {
                    "to_address": "0xBitrefillInvoice",
                    "amount": "0.100000",
                    "chain": "base",
                }
            ],
        )

    def test_live_buy_rejects_usdc_invoice_without_payment_address(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "invoice_1",
                        "status": "unpaid",
                        "payment": {"price": 5.01, "currency": "USDC"},
                        "orders": [{"id": "order_1"}],
                    }
                }
            ]
        )
        treasury = FakeTreasuryClient()
        client = LiveBitrefillClient(
            api_key="key_123",
            max_purchase_usd="5.50",
            payment_method="usdc_base",
            refund_address="0xTreasuryRefund",
            treasury_client=treasury,
            request_json=transport,
        )

        with self.assertRaisesRegex(ValueError, "payment address"):
            client.buy_product(
                quote={
                    "quoteId": "quote_live_1",
                    "productId": "amazon_com-usa",
                    "productType": "gift_card",
                    "packageId": "amazon_com-usa<&>5",
                    "packageValue": "5",
                    "priceUsd": "5.00",
                },
                recipient={},
            )

        self.assertEqual(treasury.transfers, [])

    def test_live_buy_rejects_usdc_invoice_above_configured_cap_before_transfer(self):
        transport = FakeBitrefillTransport(
            [
                {
                    "data": {
                        "id": "invoice_oversized",
                        "status": "unpaid",
                        "payment": {
                            "address": "0xBitrefillInvoice",
                            "price": 0.21,
                            "currency": "USDC",
                        },
                    }
                }
            ]
        )
        treasury = FakeTreasuryClient()
        client = LiveBitrefillClient(
            api_key="key_123",
            max_purchase_usd="0.20",
            payment_method="usdc_base",
            refund_address="0xTreasuryRefund",
            treasury_client=treasury,
            request_json=transport,
        )

        with self.assertRaisesRegex(ValueError, "exceeds live Bitrefill max"):
            client.buy_product(
                quote={
                    "quoteId": "quote_micro",
                    "productId": "bitrefill-giftcard-usd",
                    "productType": "gift_card",
                    "packageId": "0.1",
                    "packageValue": "0.1",
                    "priceUsd": "0.10",
                },
                recipient={},
            )

        self.assertEqual(treasury.transfers, [])

    def test_live_buy_refuses_quotes_above_configured_cap(self):
        client = LiveBitrefillClient(
            api_key="key_123",
            max_purchase_usd="5.00",
            request_json=FakeBitrefillTransport([]),
        )

        with self.assertRaisesRegex(ValueError, "exceeds live Bitrefill max"):
            client.buy_product(
                quote={
                    "quoteId": "quote_live_1",
                    "productId": "amazon_com-usa",
                    "productType": "gift_card",
                    "packageId": "amazon_com-usa<&>10",
                    "packageValue": "10",
                    "priceUsd": "10.00",
                },
                recipient={},
            )


if __name__ == "__main__":
    unittest.main()
