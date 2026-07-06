import unittest
from decimal import Decimal

from sign402_gateway.real_rate_pricing import RealRateSingitPricer


class LinearQuoteClient:
    def __init__(self, rate: Decimal):
        self.rate = rate
        self.amounts = []
        self.calls = []

    def quote(self, *, from_token, to_token, amount, chain, decimals=18):
        self.amounts.append(Decimal(str(amount)))
        self.calls.append(
            {
                "from_token": from_token,
                "to_token": to_token,
                "amount": amount,
                "chain": chain,
                "decimals": decimals,
            }
        )
        out = Decimal(str(amount)) * self.rate
        return {
            "ok": True,
            "fromAmount": str(amount),
            "fromToken": "SINGIT",
            "toAmount": format(out, "f"),
            "toToken": "USDC",
            "minToAmount": format(out * Decimal("0.99"), "f"),
        }


class MinAmountQuoteClient(LinearQuoteClient):
    def __init__(self, rate: Decimal, minimum: Decimal):
        super().__init__(rate)
        self.minimum = minimum

    def quote(self, *, from_token, to_token, amount, chain, decimals=18):
        parsed = Decimal(str(amount))
        self.amounts.append(parsed)
        self.calls.append(
            {
                "from_token": from_token,
                "to_token": to_token,
                "amount": amount,
                "chain": chain,
                "decimals": decimals,
            }
        )
        if parsed < self.minimum:
            raise ValueError("Quote failed: API error (500): No quote available")
        out = parsed * self.rate
        return {
            "ok": True,
            "fromAmount": str(amount),
            "fromToken": "SINGIT",
            "toAmount": format(out, "f"),
            "toToken": "USDC",
            "minToAmount": format(out * Decimal("0.99"), "f"),
        }


class SparseQuoteClient(LinearQuoteClient):
    def __init__(self, rate: Decimal, failing_amounts: set[Decimal]):
        super().__init__(rate)
        self.failing_amounts = failing_amounts

    def quote(self, *, from_token, to_token, amount, chain, decimals=18):
        parsed = Decimal(str(amount))
        if parsed in self.failing_amounts:
            self.amounts.append(parsed)
            self.calls.append(
                {
                    "from_token": from_token,
                    "to_token": to_token,
                    "amount": amount,
                    "chain": chain,
                    "decimals": decimals,
                }
            )
            raise ValueError(
                "Quote failed: API error (500): An error has occurred. Please try again later."
            )
        return super().quote(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            chain=chain,
            decimals=decimals,
        )


class WalletApiMinAmountQuoteClient(LinearQuoteClient):
    """Mimics BankrWalletApiClient: raises its own 5xx message format."""

    def __init__(self, rate: Decimal, minimum: Decimal):
        super().__init__(rate)
        self.minimum = minimum

    def quote(self, *, from_token, to_token, amount, chain, decimals=18):
        parsed = Decimal(str(amount))
        self.amounts.append(parsed)
        self.calls.append(
            {
                "from_token": from_token,
                "to_token": to_token,
                "amount": amount,
                "chain": chain,
                "decimals": decimals,
            }
        )
        if parsed < self.minimum:
            raise ValueError("Bankr Wallet API error 500: Internal Server Error")
        out = parsed * self.rate
        return {
            "ok": True,
            "fromAmount": str(amount),
            "fromToken": "SINGIT",
            "toAmount": format(out, "f"),
            "toToken": "USDC",
            "minToAmount": format(out * Decimal("0.99"), "f"),
        }


class WalletApiAuthErrorQuoteClient(LinearQuoteClient):
    def quote(self, *, from_token, to_token, amount, chain, decimals=18):
        self.amounts.append(Decimal(str(amount)))
        self.calls.append(
            {
                "from_token": from_token,
                "to_token": to_token,
                "amount": amount,
                "chain": chain,
                "decimals": decimals,
            }
        )
        raise ValueError("Bankr Wallet API error 401: unauthorized")


class RealRatePricingTests(unittest.TestCase):
    def test_tolerates_wallet_api_5xx_for_small_amounts(self):
        client = WalletApiMinAmountQuoteClient(Decimal("0.01"), minimum=Decimal("5"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="100000",
        )

        result = pricer.price_for_usdc("0.10")

        self.assertEqual(result["pricingMode"], "bankr_real_rate")

    def test_does_not_requote_the_same_amount_within_one_call(self):
        client = LinearQuoteClient(Decimal("0.01"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="100000",
        )

        pricer.price_for_usdc("0.10")

        self.assertEqual(
            len(client.amounts),
            len(set(client.amounts)),
            msg=f"same amount quoted more than once: {client.amounts}",
        )

    def test_propagates_wallet_api_auth_error(self):
        client = WalletApiAuthErrorQuoteClient(Decimal("0.01"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
        )

        with self.assertRaisesRegex(ValueError, "401"):
            pricer.price_for_usdc("0.10")


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

    def test_price_for_usdc_accepts_per_call_token_and_decimals(self):
        client = LinearQuoteClient(Decimal("0.01"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xSINGIT",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="10000",
        )

        result = pricer.price_for_usdc("10", from_token="0xCUSTOM", decimals=8)

        self.assertTrue(all(c["from_token"] == "0xCUSTOM" for c in client.calls))
        self.assertTrue(all(c["decimals"] == 8 for c in client.calls))
        self.assertEqual(
            result["requiredSingitAtomic"],
            str(int(Decimal(result["requiredSingit"]) * Decimal(10) ** 8)),
        )
        self.assertEqual(result["fromToken"], "0xCUSTOM")

    def test_price_for_usdc_defaults_to_constructor_token_and_18_decimals(self):
        client = LinearQuoteClient(Decimal("0.01"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xSINGIT",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="10000",
        )

        result = pricer.price_for_usdc("10")

        self.assertTrue(all(c["from_token"] == "0xSINGIT" for c in client.calls))
        self.assertTrue(all(c["decimals"] == 18 for c in client.calls))

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

    def test_skips_bankr_no_quote_amounts_while_searching(self):
        client = MinAmountQuoteClient(rate=Decimal("0.02"), minimum=Decimal("8"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="1000",
            search_iterations=4,
        )

        result = pricer.price_for_usdc("0.10")

        self.assertEqual(result["requiredSingit"], "8")
        self.assertGreaterEqual(Decimal(result["expectedUsdc"]), Decimal("0.11"))
        self.assertIn(Decimal("1"), client.amounts)
        self.assertIn(Decimal("8"), client.amounts)

    def test_skips_transient_bankr_500_amounts_while_searching(self):
        client = SparseQuoteClient(rate=Decimal("0.02"), failing_amounts={Decimal("6")})
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="1000",
            search_iterations=4,
        )

        result = pricer.price_for_usdc("0.10")

        self.assertGreaterEqual(Decimal(result["expectedUsdc"]), Decimal("0.11"))
        self.assertIn(Decimal("6"), client.amounts)
        self.assertTrue(any(amount > Decimal("6") for amount in client.amounts))

    def test_jumps_from_tiny_working_quote_toward_target(self):
        client = MinAmountQuoteClient(rate=Decimal("0.000001"), minimum=Decimal("2"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="1000000",
            search_iterations=4,
        )

        result = pricer.price_for_usdc("0.10")

        self.assertGreaterEqual(Decimal(result["expectedUsdc"]), Decimal("0.11"))
        self.assertIn(Decimal("2"), client.amounts)
        self.assertTrue(any(amount > Decimal("100000") for amount in client.amounts))
        self.assertNotIn(Decimal("65536"), client.amounts)

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
