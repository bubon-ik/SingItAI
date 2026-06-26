from decimal import Decimal, ROUND_CEILING, getcontext
from typing import Any


SINGIT_DECIMALS = 18
getcontext().prec = 50


class RealRateSingitPricer:
    def __init__(
        self,
        *,
        quote_client: Any,
        from_token: str,
        to_token: str = "USDC",
        chain: str = "base",
        buffer_bps: int = 1000,
        max_singit: str = "1000000",
        search_iterations: int = 24,
    ):
        self.quote_client = quote_client
        self.from_token = from_token
        self.to_token = to_token
        self.chain = chain
        self.buffer_bps = int(buffer_bps)
        self.max_singit = Decimal(str(max_singit))
        self.search_iterations = int(search_iterations)

    def price_for_usdc(self, target_usdc: str) -> dict[str, Any]:
        target = Decimal(str(target_usdc))
        if target <= 0:
            raise ValueError("target USDC must be positive")
        buffered_target = (
            target * (Decimal(10_000 + self.buffer_bps) / Decimal(10_000))
        ).quantize(
            Decimal("0.000001"),
            rounding=ROUND_CEILING,
        )
        low = Decimal("0")
        high = Decimal("1")
        high_quote = self._quote(high)
        while Decimal(high_quote["toAmount"]) < buffered_target:
            low = high
            high *= 2
            if high > self.max_singit:
                raise ValueError("required SINGIT exceeds configured maximum")
            high_quote = self._quote(high)

        best_amount = high
        for _ in range(max(1, self.search_iterations)):
            mid = (low + high) / 2
            quote = self._quote(mid)
            if Decimal(quote["toAmount"]) >= buffered_target:
                best_amount = mid
                high = mid
            else:
                low = mid

        rounded_singit = best_amount.quantize(Decimal("1"), rounding=ROUND_CEILING)
        if rounded_singit > self.max_singit:
            raise ValueError("required SINGIT exceeds configured maximum")
        final_quote = self._quote(rounded_singit)
        if Decimal(final_quote["toAmount"]) < buffered_target:
            raise ValueError("Bankr quote did not meet target USDC after rounding")

        return {
            "pricingMode": "bankr_real_rate",
            "targetUsdc": format(target, "f"),
            "bufferedTargetUsdc": format(buffered_target, "f"),
            "requiredSingit": format_decimal(rounded_singit),
            "requiredSingitAtomic": str(
                int(rounded_singit * (Decimal(10) ** SINGIT_DECIMALS))
            ),
            "expectedUsdc": final_quote["toAmount"],
            "minUsdc": final_quote.get("minToAmount", final_quote["toAmount"]),
            "fromToken": self.from_token,
            "toToken": self.to_token,
            "chain": self.chain,
            "quote": final_quote,
        }

    def _quote(self, amount: Decimal) -> dict[str, Any]:
        return self.quote_client.quote(
            from_token=self.from_token,
            to_token=self.to_token,
            amount=format_decimal(amount),
            chain=self.chain,
        )


def format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text
