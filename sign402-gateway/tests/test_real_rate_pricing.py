import unittest
from decimal import Decimal

from sign402_gateway.real_rate_pricing import RealRateSingitPricer


class LinearQuoteClient:
    def __init__(self, rate: Decimal):
        self.rate = rate
        self.amounts = []

    def quote(self, *, from_token, to_token, amount, chain):
        self.amounts.append(Decimal(str(amount)))
        out = Decimal(str(amount)) * self.rate
        return {
            "ok": True,
            "fromAmount": str(amount),
            "fromToken": "SINGIT",
            "toAmount": format(out, "f"),
            "toToken": "USDC",
            "minToAmount": format(out * Decimal("0.99"), "f"),
        }


class RealRatePricingTests(unittest.TestCase):
    def test_finds_required_singit_for_target_usdc_with_buffer(self):
        client = LinearQuoteClient(Decimal("0.01"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="1000",
        )

        result = pricer.price_for_usdc("0.10")

        self.assertEqual(result["pricingMode"], "bankr_real_rate")
        self.assertGreaterEqual(Decimal(result["expectedUsdc"]), Decimal("0.11"))
        self.assertLessEqual(Decimal(result["requiredSingit"]), Decimal("11.01"))
        self.assertEqual(result["requiredSingitAtomic"], "11000000000000000000")

    def test_rejects_when_required_singit_exceeds_cap(self):
        client = LinearQuoteClient(Decimal("0.000001"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="100",
        )

        with self.assertRaisesRegex(ValueError, "required SINGIT exceeds"):
            pricer.price_for_usdc("0.10")

    def test_rejects_zero_or_negative_target(self):
        pricer = RealRateSingitPricer(
            quote_client=LinearQuoteClient(Decimal("0.01")),
            from_token="token",
            to_token="USDC",
            chain="base",
        )

        with self.assertRaisesRegex(ValueError, "target USDC must be positive"):
            pricer.price_for_usdc("0")


if __name__ == "__main__":
    unittest.main()
