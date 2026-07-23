import unittest
from decimal import Decimal

from sign402_gateway import bitrefill_quote as quote_module
from sign402_gateway.bitrefill_quote import (
    SINGIT_DECIMALS,
    build_purchase_commitment,
    build_quote,
    build_real_rate_quote,
    hash_purchase_commitment,
)


class BitrefillQuoteTests(unittest.TestCase):
    def test_service_fee_is_two_percent_without_a_minimum(self):
        fee, total = quote_module.calculate_service_fee("0.10")

        self.assertEqual(quote_module.SERVICE_FEE_BPS, 200)
        self.assertEqual(fee, Decimal("0.002"))
        self.assertEqual(total, Decimal("0.102"))

    def test_build_quote_charges_two_percent_with_atomic_precision(self):
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
            quote_id="quote_micro",
            now_epoch=1_719_000_000,
        )

        self.assertEqual(quote["serviceFeeBps"], 200)
        self.assertEqual(quote["serviceFeeUsd"], "0.002")
        self.assertEqual(quote["totalUsd"], "0.102")
        self.assertEqual(quote["singitAmount"], "10.2")
        self.assertEqual(
            quote["maxSingitAtomic"],
            str(int(Decimal("10.2") * 10**SINGIT_DECIMALS)),
        )
        self.assertNotIn("marginBps", quote)

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
            quote_id="quote_fixed",
            now_epoch=1_719_000_000,
        )

        self.assertEqual(quote["productId"], "test-phone-refill")
        self.assertEqual(quote["productType"], "phone_refill")
        self.assertEqual(quote["packageId"], "1")
        self.assertEqual(quote["singitAmount"], "102")

    def test_build_quote_converts_usd_price_to_singit_atomic_with_service_fee(self):
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
            quote_id="quote_fixed",
            now_epoch=1_719_000_000,
            ttl_seconds=120,
        )

        self.assertEqual(quote["quoteId"], "quote_fixed")
        self.assertEqual(quote["productId"], "test-gift-card-code")
        self.assertEqual(quote["singitAmount"], "2550")
        self.assertEqual(quote["maxSingitAtomic"], str(2550 * 10**SINGIT_DECIMALS))
        self.assertEqual(quote["expiresAtEpoch"], 1_719_000_120)
        self.assertIn("Test Gift Card Code", quote["quoteText"])

    def test_build_quote_keeps_micro_purchase_at_atomic_precision(self):
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
            quote_id="quote_micro",
            now_epoch=1_719_000_000,
        )

        self.assertEqual(quote["singitAmount"], "10.2")
        self.assertEqual(
            quote["maxSingitAtomic"],
            str(int(Decimal("10.2") * 10**SINGIT_DECIMALS)),
        )

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

    def test_build_real_rate_quote_binds_selected_payment_token(self):
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
                "requiredAmount": "0.11",
                "requiredAmountAtomic": "110000",
                "expectedUsdc": "0.111",
                "minUsdc": "0.109",
            },
            payment_token={
                "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "symbol": "USDC",
                "decimals": 6,
                "native": False,
            },
            quote_id="quote_usdc",
            now_epoch=1_719_000_000,
        )

        self.assertEqual(quote["paymentTokenSymbol"], "USDC")
        self.assertEqual(quote["paymentTokenAmount"], "0.11")
        self.assertEqual(quote["maxPaymentTokenAtomic"], "110000")
        self.assertNotIn("maxSingitAtomic", quote)

        commitment = build_purchase_commitment(quote)
        self.assertEqual(
            commitment["paymentTokenAddress"],
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
        self.assertEqual(commitment["maxPaymentTokenAtomic"], "110000")

    def test_purchase_commitment_hash_is_stable_and_hides_recipient(self):
        quote = {
            "quoteId": "quote_fixed",
            "productId": "test-gift-card-code",
            "productType": "gift_card",
            "packageId": "25",
            "packageValue": "25",
            "priceUsd": "25.00",
            "serviceFeeBps": 200,
            "serviceFeeUsd": "0.5",
            "totalUsd": "25.5",
            "maxSingitAtomic": "2550000000000000000000",
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
        self.assertEqual(commitment["serviceFeeBps"], 200)
        self.assertEqual(commitment["serviceFeeUsd"], "0.5")
        self.assertEqual(commitment["totalUsd"], "25.5")
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
            quote_id="quote_fixed",
            now_epoch=1_719_000_000,
            ttl_seconds=120,
        )

        self.assertEqual(quote["country"], "DE")


if __name__ == "__main__":
    unittest.main()
