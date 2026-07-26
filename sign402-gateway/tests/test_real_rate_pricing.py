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
    def test_rounds_selected_token_to_its_decimal_precision(self):
        client = LinearQuoteClient(Decimal("1"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
            buffer_bps=1000,
            max_singit="1000000",
        )

        result = pricer.price_for_usdc(
            "1.00",
            from_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            decimals=6,
            max_amount="5",
        )

        self.assertEqual(result["requiredAmount"], "1.1")
        self.assertEqual(result["requiredAmountAtomic"], "1100000")

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


    def test_finds_required_singit_for_target_usdc_with_no_application_buffer(self):
        client = LinearQuoteClient(Decimal("0.01"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
            to_token="USDC",
            chain="base",
            buffer_bps=0,
            max_singit="1000",
        )

        result = pricer.price_for_usdc("0.102")

        self.assertEqual(result["pricingMode"], "bankr_real_rate")
        self.assertEqual(result["targetUsdc"], "0.102")
        self.assertEqual(result["bufferedTargetUsdc"], "0.102000")
        self.assertGreaterEqual(Decimal(result["expectedUsdc"]), Decimal("0.102"))
        self.assertEqual(result["requiredSingitAtomic"], "10200000000000000000")

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

    def test_quotes_exact_wallet_balance_before_rejecting(self):
        balance = Decimal("20750000.123456789")
        client = MinAmountQuoteClient(
            rate=Decimal("0.0000014"),
            minimum=Decimal("20000000"),
        )
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xSINGIT",
            to_token="USDC",
            chain="base",
            buffer_bps=0,
            max_singit="2000000000",
        )

        result = pricer.price_for_usdc(
            "24.48",
            max_amount=format(balance, "f"),
        )

        required = Decimal(result["requiredAmount"])
        self.assertLess(required, balance)
        self.assertGreaterEqual(Decimal(result["expectedUsdc"]), Decimal("24.48"))
        self.assertEqual(client.amounts.count(balance), 1)
        self.assertTrue(all(amount <= balance for amount in client.amounts))

    def test_reports_insufficient_balance_after_exact_cap_quote(self):
        balance = Decimal("5")
        client = LinearQuoteClient(Decimal("0.01"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xTOKEN",
            to_token="USDC",
            chain="base",
            buffer_bps=0,
        )

        with self.assertRaisesRegex(
            ValueError,
            "^selected payment token balance is insufficient at the current swap rate$",
        ):
            pricer.price_for_usdc("0.10", max_amount=format(balance, "f"))

        self.assertEqual(client.amounts.count(balance), 1)
        self.assertTrue(all(amount <= balance for amount in client.amounts))

    def test_reports_unavailable_quote_after_exact_cap_attempt(self):
        balance = Decimal("5")
        client = WalletApiMinAmountQuoteClient(
            rate=Decimal("0.01"),
            minimum=Decimal("8"),
        )
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xTOKEN",
            to_token="USDC",
            chain="base",
            buffer_bps=0,
        )

        with self.assertRaisesRegex(
            ValueError,
            "^unable to obtain a swap quote for the selected payment token balance$",
        ):
            pricer.price_for_usdc("0.10", max_amount=format(balance, "f"))

        self.assertEqual(client.amounts.count(balance), 1)
        self.assertTrue(all(amount <= balance for amount in client.amounts))

    def test_never_quotes_above_sub_unit_wallet_balance(self):
        balance = Decimal("0.5")
        client = LinearQuoteClient(Decimal("1"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xTOKEN",
            to_token="USDC",
            chain="base",
            buffer_bps=0,
        )

        result = pricer.price_for_usdc("0.25", max_amount=format(balance, "f"))

        self.assertEqual(result["requiredAmount"], "0.25")
        self.assertEqual(client.amounts.count(balance), 1)
        self.assertTrue(all(amount <= balance for amount in client.amounts))


if __name__ == "__main__":
    unittest.main()
