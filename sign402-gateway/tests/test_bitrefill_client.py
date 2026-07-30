import unittest

import sign402_gateway.bitrefill as bitrefill
from sign402_gateway.bitrefill import TestBitrefillClient


class BitrefillClientTests(unittest.TestCase):
    def test_live_rest_client_is_not_available(self):
        self.assertFalse(hasattr(bitrefill, "LiveBitrefillClient"))
        self.assertFalse(hasattr(bitrefill, "MAX_BITREFILL_RESPONSE_BYTES"))

    def test_test_catalog_list_filters_country_and_category_then_slices(self):
        client = TestBitrefillClient()

        products = client.list_products(
            country="cz,uS",
            category="restaurants,REFILL",
            start=0,
            limit=1,
            include_test_products=True,
        )

        self.assertEqual(
            [product["productId"] for product in products],
            ["test-phone-refill"],
        )
        self.assertEqual(
            client.list_products(
                country="US",
                category="gift_card",
                start=0,
                limit=10,
                include_test_products=False,
            ),
            [],
        )

    def test_test_catalog_list_accepts_official_giftcard_category_alias(self):
        products = TestBitrefillClient().list_products(
            country="US",
            category="giftcard",
            start=0,
            limit=10,
            include_test_products=True,
        )

        self.assertEqual(
            [product["productId"] for product in products],
            ["test-gift-card-link", "test-gift-card-code"],
        )
        self.assertEqual(
            {product["category"] for product in products},
            {"gift_card"},
        )

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

    def test_test_purchase_client_supports_prepare_then_complete(self):
        client = TestBitrefillClient()
        quote = {
            "quoteId": "quote_1",
            "productId": "test-gift-card-code",
            "packageId": "1",
            "packageValue": "1",
            "priceUsd": "1.00",
            "totalUsd": "1.01",
        }

        prepared = client.prepare_purchase(quote=quote, recipient={})
        result = client.complete_purchase(
            quote=quote,
            prepared=prepared,
        )

        self.assertEqual(prepared["productId"], "test-gift-card-code")
        self.assertEqual(prepared["packageValue"], "1")
        self.assertEqual(result["invoiceId"], prepared["invoiceId"])
        self.assertTrue(result["ok"])


class BitrefillClientContractTests(unittest.TestCase):
    """Both implementations must stay swappable behind one protocol."""

    def test_the_test_client_accepts_a_buyer_email(self):
        client = TestBitrefillClient()
        quote = client.quote_product(
            product_id="test-gift-card-code",
            package_id="1",
            country="US",
            recipient={},
        )

        prepared = client.prepare_purchase(
            quote={**quote, "quoteId": "quote_1"},
            recipient={},
            buyer_email="buyer@example.com",
        )

        self.assertTrue(str(prepared["invoiceId"]))


if __name__ == "__main__":
    unittest.main()
