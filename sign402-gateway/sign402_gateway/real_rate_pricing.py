from decimal import Decimal, ROUND_CEILING, getcontext
from typing import Any

from .numeric import format_decimal


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
        search_iterations: int = 8,
    ):
        self.quote_client = quote_client
        self.from_token = from_token
        self.to_token = to_token
        self.chain = chain
        self.buffer_bps = int(buffer_bps)
        self.max_singit = Decimal(str(max_singit))
        self.search_iterations = int(search_iterations)
        self._quote_cache: dict[str, dict[str, Any]] = {}

    def price_for_usdc(
        self,
        target_usdc: str,
        *,
        from_token: str | None = None,
        decimals: int | None = None,
    ) -> dict[str, Any]:
        token = str(from_token or self.from_token)
        token_decimals = int(decimals) if decimals is not None else SINGIT_DECIMALS
        self._quote_cache = {}
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
        high_quote = self._quote_or_none(high, token, token_decimals)
        while high_quote is None or Decimal(high_quote["toAmount"]) < buffered_target:
            low = high
            high = self._next_high_amount(
                current=high,
                quote=high_quote,
                buffered_target=buffered_target,
            )
            if high > self.max_singit:
                raise ValueError("required SINGIT exceeds configured maximum")
            high_quote = self._quote_or_none(high, token, token_decimals)

        best_amount = high
        for _ in range(max(1, self.search_iterations)):
            mid = (low + high) / 2
            quote = self._quote_or_none(mid, token, token_decimals)
            if quote is None:
                low = mid
                continue
            if Decimal(quote["toAmount"]) >= buffered_target:
                best_amount = mid
                high = mid
            else:
                low = mid

        rounded_singit = best_amount.quantize(Decimal("1"), rounding=ROUND_CEILING)
        if rounded_singit > self.max_singit:
            raise ValueError("required SINGIT exceeds configured maximum")
        rounded_singit = self._minimize_integer_amount(
            amount=rounded_singit,
            buffered_target=buffered_target,
            token=token,
            token_decimals=token_decimals,
        )
        final_quote = self._quote(rounded_singit, token, token_decimals)
        if Decimal(final_quote["toAmount"]) < buffered_target:
            raise ValueError("Bankr quote did not meet target USDC after rounding")

        return {
            "pricingMode": "bankr_real_rate",
            "targetUsdc": format(target, "f"),
            "bufferedTargetUsdc": format(buffered_target, "f"),
            "requiredSingit": format_decimal(rounded_singit),
            "requiredSingitAtomic": str(
                int(rounded_singit * (Decimal(10) ** token_decimals))
            ),
            "expectedUsdc": final_quote["toAmount"],
            "minUsdc": final_quote.get("minToAmount", final_quote["toAmount"]),
            "fromToken": token,
            "toToken": self.to_token,
            "chain": self.chain,
            "quote": final_quote,
        }

    def _quote(self, amount: Decimal, token: str, token_decimals: int) -> dict[str, Any]:
        # Within one price_for_usdc call the search re-visits some amounts
        # (e.g. the final confirmation re-quotes the converged size); memoize
        # by the exact amount string sent to the client to avoid duplicate
        # Bankr round-trips on this blocking pricing path.
        amount_key = format_decimal(amount)
        key = f"{token}:{token_decimals}:{amount_key}"
        cached = self._quote_cache.get(key)
        if cached is not None:
            return cached
        result = self.quote_client.quote(
            from_token=token,
            to_token=self.to_token,
            amount=amount_key,
            chain=self.chain,
            decimals=token_decimals,
        )
        self._quote_cache[key] = result
        return result

    def _quote_or_none(
        self, amount: Decimal, token: str, token_decimals: int
    ) -> dict[str, Any] | None:
        try:
            return self._quote(amount, token, token_decimals)
        except Exception as exc:
            if _is_skippable_quote_error(str(exc)):
                return None
            raise

    def _next_high_amount(
        self,
        *,
        current: Decimal,
        quote: dict[str, Any] | None,
        buffered_target: Decimal,
    ) -> Decimal:
        doubled = current * 2
        if quote is None:
            return doubled
        received = Decimal(str(quote.get("toAmount", "0")))
        if received <= 0:
            return doubled
        estimated = (
            current
            * (buffered_target / received)
            * Decimal("1.05")
        ).quantize(Decimal("1"), rounding=ROUND_CEILING)
        return max(doubled, estimated)

    def _minimize_integer_amount(
        self,
        *,
        amount: Decimal,
        buffered_target: Decimal,
        token: str,
        token_decimals: int,
    ) -> Decimal:
        if amount <= 1:
            return amount
        previous = amount - 1
        quote = self._quote_or_none(previous, token, token_decimals)
        if quote is not None and Decimal(quote["toAmount"]) >= buffered_target:
            return previous
        return amount


def _is_skippable_quote_error(message: str) -> bool:
    """True when a quote error means 'no route at this size', not a fatal failure.

    Bankr returns a server-side error for amounts it cannot route, and the CLI
    and Wallet API surface it with different wording. Both must be treated the
    same so the binary search can step to another size instead of either
    aborting (Wallet API path) or silently inflating the price; genuine client
    errors (auth, bad request) still propagate.
    """
    lowered = message.lower()
    if "no quote available" in lowered or "no route" in lowered or "no liquidity" in lowered:
        return True
    # CLI: "Quote failed: API error (5xx)"; Wallet API: "Bankr Wallet API error 5xx: ..."
    if "api error (5" in lowered or "wallet api error 5" in lowered:
        return True
    return False
