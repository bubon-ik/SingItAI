from decimal import Decimal, InvalidOperation, ROUND_CEILING, getcontext
from typing import Any

from .numeric import format_decimal


SINGIT_DECIMALS = 18
getcontext().prec = 50


def _finite_decimal(value: Any, *, field: str) -> Decimal:
    """Parse a caller-supplied amount, rejecting NaN/Infinity and junk.

    ``Decimal(str(...))`` happily accepts ``"NaN"`` and ``"Infinity"``. An
    infinite spend cap silently disables the wallet-balance ceiling this pricer
    exists to enforce, and both forms make later comparisons raise
    ``InvalidOperation`` (an ArithmeticError) instead of the ``ValueError`` the
    quote path reports to the user.
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a decimal number") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal number")
    return parsed


class RealRateSingitPricer:
    def __init__(
        self,
        *,
        quote_client: Any,
        from_token: str,
        to_token: str = "USDC",
        chain: str = "base",
        buffer_bps: int = 0,
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
        max_amount: str | None = None,
    ) -> dict[str, Any]:
        token = str(from_token or self.from_token)
        token_decimals = int(decimals) if decimals is not None else SINGIT_DECIMALS
        amount_quantum = Decimal(1).scaleb(-token_decimals)
        balance_cap = max_amount is not None
        amount_cap = (
            _finite_decimal(max_amount, field="payment token balance")
            if balance_cap
            else _finite_decimal(self.max_singit, field="configured maximum amount")
        )
        cap_exhausted_message = (
            "selected payment token balance is insufficient at the current swap rate"
            if balance_cap
            else "required SINGIT exceeds configured maximum"
        )
        if amount_cap <= 0:
            raise ValueError("payment token balance must be positive")
        self._quote_cache = {}
        target = _finite_decimal(target_usdc, field="target USDC")
        if target <= 0:
            raise ValueError("target USDC must be positive")
        buffered_target = (
            target * (Decimal(10_000 + self.buffer_bps) / Decimal(10_000))
        ).quantize(
            Decimal("0.000001"),
            rounding=ROUND_CEILING,
        )
        low = Decimal("0")
        high = min(Decimal("1"), amount_cap)
        high_quote = self._quote_or_none(high, token, token_decimals)
        while high_quote is None or Decimal(high_quote["toAmount"]) < buffered_target:
            if high >= amount_cap:
                if balance_cap and high_quote is None:
                    raise ValueError(
                        "unable to obtain a swap quote for the selected payment token balance"
                    )
                raise ValueError(cap_exhausted_message)
            low = high
            high = min(
                self._next_high_amount(
                    current=high,
                    quote=high_quote,
                    buffered_target=buffered_target,
                ),
                amount_cap,
            )
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

        rounded_singit = best_amount.quantize(amount_quantum, rounding=ROUND_CEILING)
        if rounded_singit > amount_cap:
            raise ValueError(cap_exhausted_message)
        rounded_singit = self._minimize_amount(
            amount=rounded_singit,
            buffered_target=buffered_target,
            token=token,
            token_decimals=token_decimals,
            quantum=amount_quantum,
        )
        final_quote = self._quote(rounded_singit, token, token_decimals)
        if Decimal(final_quote["toAmount"]) < buffered_target:
            raise ValueError("Bankr quote did not meet target USDC after rounding")

        amount_text = format_decimal(rounded_singit)
        amount_atomic = str(
            int(rounded_singit * (Decimal(10) ** token_decimals))
        )
        return {
            "pricingMode": "bankr_real_rate",
            "targetUsdc": format(target, "f"),
            "bufferedTargetUsdc": format(buffered_target, "f"),
            "requiredAmount": amount_text,
            "requiredAmountAtomic": amount_atomic,
            "requiredSingit": amount_text,
            "requiredSingitAtomic": amount_atomic,
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

    def _minimize_amount(
        self,
        *,
        amount: Decimal,
        buffered_target: Decimal,
        token: str,
        token_decimals: int,
        quantum: Decimal,
    ) -> Decimal:
        quote = self._quote(amount, token, token_decimals)
        received = Decimal(str(quote.get("toAmount", "0")))
        if received > 0:
            proportional = (amount * buffered_target / received).quantize(
                quantum,
                rounding=ROUND_CEILING,
            )
            if Decimal("0") < proportional < amount:
                proportional_quote = self._quote_or_none(
                    proportional,
                    token,
                    token_decimals,
                )
                if (
                    proportional_quote is not None
                    and Decimal(proportional_quote["toAmount"]) >= buffered_target
                ):
                    amount = proportional
        if amount <= quantum:
            return amount
        previous = amount - quantum
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
