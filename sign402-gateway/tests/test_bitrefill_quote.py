import unittest

from sign402_gateway.bitrefill_quote import (
    SINGIT_DECIMALS,
    build_purchase_commitment,
    build_quote,
    build_real_rate_quote,
    hash_purchase_commitment,
)


class BitrefillQuoteTests(unittest.TestCase):
    def test_build_quote_accepts_explicit_phone_refill_snapshot(self):
        quote = build_quote(
            request={
                "productId": "test-phone-refill",
                "packageId": "1",
                "country": "US",
            },
            product={
                "productId": "test-phone-refill",
                "name": "Test Phone Refill",
                "productType": "phone_refill",
                "packageId": "1",
                "packageValue": "1",
                "country": "US",
                "currency": "USD",
                "priceUsd": "1.00",
            },
            singit_usd_price="0.01",
            margin_bps=500,
            quote_id="quote_fixed",
            now_epoch=1_719_000_000,
        )

        self.assertEqual(quote["productId"], "test-phone-refill")
        self.assertEqual(quote["productType"], "phone_refill")
        self.assertEqual(quote["packageId"], "1")
        self.assertEqual(quote["singitAmount"], "105")

    def test_build_quote_converts_usd_price_to_singit_atomic_with_margin(self):
        quote = build_quote(
            request={
                "productId": "test-gift-card-code",
                "packageId": "25",
                "country": "US",
            },
            product={
                "productId": "test-gift-card-code",
                "name": "Test Gift Card Code",
                "productType": "gift_card",
                "packageId": "25",
                "country": "US",
                "currency": "USD",
                "packageValue": "25",
                "priceUsd": "25.00",
            },
            singit_usd_price="0.01",
            margin_bps=500,
            quote_id="quote_fixed",
            now_epoch=1_719_000_000,
            ttl_seconds=120,
        )

        self.assertEqual(quote["quoteId"], "quote_fixed")
        self.assertEqual(quote["productId"], "test-gift-card-code")
        self.assertEqual(quote["singitAmount"], "2625")
        self.assertEqual(quote["maxSingitAtomic"], str(2625 * 10**SINGIT_DECIMALS))
        self.assertEqual(quote["expiresAtEpoch"], 1_719_000_120)
        self.assertIn("Test Gift Card Code", quote["quoteText"])

    def test_build_quote_rounds_micro_purchase_up_to_eleven_singit(self):
        quote = build_quote(
            request={
                "productId": "bitrefill-giftcard-usd",
                "packageId": "0.1",
                "country": "US",
            },
            product={
                "productId": "bitrefill-giftcard-usd",
                "name": "Bitrefill Gift Card (USD)",
                "productType": "gift_card",
                "packageId": "0.1",
                "country": "US",
                "currency": "USD",
                "packageValue": "0.1",
                "priceUsd": "0.10",
            },
            singit_usd_price="0.01",
            margin_bps=500,
            quote_id="quote_micro",
            now_epoch=1_719_000_000,
        )

        self.assertEqual(quote["singitAmount"], "11")
        self.assertEqual(quote["maxSingitAtomic"], str(11 * 10**SINGIT_DECIMALS))

    def test_build_real_rate_quote_uses_bankr_pricing_result(self):
        quote = build_real_rate_quote(
            request={
                "productId": "bitrefill-giftcard-usd",
                "packageId": "0.1",
                "country": "US",
            },
            product={
                "productId": "bitrefill-giftcard-usd",
                "name": "Bitrefill Gift Card (USD)",
                "productType": "gift_card",
                "packageId": "0.1",
                "packageValue": "0.1",
                "country": "US",
                "currency": "USD",
                "priceUsd": "0.10",
            },
            pricing={
                "pricingMode": "bankr_real_rate",
                "targetUsdc": "0.10",
                "bufferedTargetUsdc": "0.11",
                "requiredSingit": "25000",
                "requiredSingitAtomic": "25000000000000000000000",
                "expectedUsdc": "0.111",
                "minUsdc": "0.109",
            },
            quote_id="quote_real_1",
            now_epoch=1_719_000_000,
        )

        self.assertEqual(quote["quoteId"], "quote_real_1")
        self.assertEqual(quote["pricingMode"], "bankr_real_rate")
        self.assertEqual(quote["singitAmount"], "25000")
        self.assertEqual(quote["maxSingitAtomic"], "25000000000000000000000")
        self.assertEqual(quote["requiredUsdc"], "0.10")
        self.assertIn("real-rate", quote["quoteText"])

    def test_purchase_commitment_hash_is_stable_and_hides_recipient(self):
        quote = {
            "quoteId": "quote_fixed",
            "productId": "test-gift-card-code",
            "productType": "gift_card",
            "packageId": "25",
            "packageValue": "25",
            "priceUsd": "25.00",
            "maxSingitAtomic": "2625000000000000000000",
            "expiresAt": "2024-06-20T12:02:00Z",
        }

        commitment = build_purchase_commitment(
            quote,
            recipient={"email": "buyer@example.com"},
        )
        payment_hash = hash_purchase_commitment(commitment)

        self.assertEqual(commitment["type"], "singit-bitrefill-purchase")
        self.assertEqual(commitment["productType"], "gift_card")
        self.assertEqual(commitment["packageId"], "25")
        self.assertEqual(commitment["priceUsd"], "25.00")
        self.assertEqual(commitment["recipientCommitment"][:7], "sha256:")
        self.assertNotIn("buyer@example.com", str(commitment))
        self.assertEqual(len(payment_hash), 64)
        self.assertEqual(payment_hash, hash_purchase_commitment(commitment))

    def test_quote_accepts_normalized_product_country(self):
        quote = build_quote(
            request={"productId": "test-gift-de", "packageId": "25", "country": "DE"},
            product={
                "productId": "test-gift-de",
                "name": "Test Gift DE",
                "productType": "gift_card",
                "packageId": "25",
                "packageValue": "25",
                "country": "DE",
                "currency": "EUR",
                "priceUsd": "25",
            },
            singit_usd_price="0.01",
            margin_bps=500,
            quote_id="quote_fixed",
            now_epoch=1_719_000_000,
            ttl_seconds=120,
        )

        self.assertEqual(quote["country"], "DE")


if __name__ == "__main__":
    unittest.main()
