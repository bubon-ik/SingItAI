# Bankr Real-Rate SINGIT Bitrefill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Price Bitrefill purchases from Bankr's real SINGIT→USDC swap route, collect that SINGIT through the existing Bankr x402 endpoint, swap SINGIT to USDC, then pay Bitrefill.

**Architecture:** Add a focused Bankr swap module, a real-rate pricing module, and a pre-Bitrefill funding step in the existing fulfillment runner. Keep the current Bankr x402 endpoint, but make quote preparation return dynamic real-rate SINGIT amounts and make fulfillment swap after verified SINGIT settlement before creating/paying a Bitrefill invoice.

**Tech Stack:** Python 3.14, `unittest`, Bankr CLI, Bankr x402 Cloud custom-token endpoint, Base ERC-20 logs, Bitrefill REST v2, Node test runner for endpoint handler.

---

## File Structure

- Create `sign402-gateway/sign402_gateway/bankr_swap.py`
  - Owns Bankr CLI swap/quote parsing and execution.
  - No Bitrefill logic.
- Create `sign402-gateway/sign402_gateway/real_rate_pricing.py`
  - Owns bounded search that calculates required SINGIT for target USDC.
  - Depends on an injected quote client.
- Modify `sign402-gateway/sign402_gateway/bitrefill_quote.py`
  - Add a real-rate quote builder while preserving fixed-price quote behavior.
- Modify `sign402-gateway/sign402_gateway/bitrefill_runner.py`
  - Wire optional real-rate pricing into quote creation.
  - Add optional funding step before Bitrefill purchase.
  - Ensure swap failures do not create/pay Bitrefill invoices.
- Modify `sign402-gateway/sign402_gateway/server.py`
  - Build Bankr swap clients from env.
  - Build real-rate quote provider when `SIGN402_BITREFILL_PRICING_MODE=bankr_real_rate`.
  - Build funding runner when real-rate mode is enabled.
- Modify `singit-risk-check/bankr.x402.json`
  - Raise `buy-bitrefill.price` to a configured maximum SINGIT cap, because `paymentScheme=upto` must cover dynamic real-rate amounts.
  - Update description/schema to clarify real-rate settlement.
- Modify `singit-risk-check/x402/buy-bitrefill/index.mjs`
  - Return `pricingMode` and dynamic settle amount in the response.
- Modify `singit-risk-check/x402/buy-bitrefill/index.ts`
  - Keep TypeScript source in sync with `.mjs`.
- Add tests:
  - `sign402-gateway/tests/test_bankr_swap.py`
  - `sign402-gateway/tests/test_real_rate_pricing.py`
  - Extend `sign402-gateway/tests/test_bitrefill_quote.py`
  - Extend `sign402-gateway/tests/test_bitrefill_runner.py`
  - Extend `sign402-gateway/tests/test_gateway_server.py`
  - Extend `singit-risk-check/tests/buy-bitrefill.test.mjs`

## Task 1: Bankr swap parser and CLI client

**Files:**
- Create: `sign402-gateway/sign402_gateway/bankr_swap.py`
- Create: `sign402-gateway/tests/test_bankr_swap.py`

- [ ] **Step 1: Write failing parser tests**

Create `sign402-gateway/tests/test_bankr_swap.py`:

```python
import subprocess
import unittest
from unittest.mock import patch

from sign402_gateway.bankr_swap import (
    BankrSwapClient,
    parse_bankr_swap_quote,
    parse_bankr_transaction_hash,
)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["bankr"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class BankrSwapTests(unittest.TestCase):
    def test_parse_quote_only_output(self):
        quote = parse_bankr_swap_quote(
            """
- Resolving 0xc2c1e0b7C401e6217193732272444D928646eba3 → USDC on base...
- Fetching quote for 11 0xc2c1…eba3 → USDC...

You pay:  11 SINGIT ($0.00)
You receive:  ~0.000004 USDC ($0.00)
  Min received:       0.000004 USDC
"""
        )

        self.assertEqual(quote["fromAmount"], "11")
        self.assertEqual(quote["fromToken"], "SINGIT")
        self.assertEqual(quote["toAmount"], "0.000004")
        self.assertEqual(quote["toToken"], "USDC")
        self.assertEqual(quote["minToAmount"], "0.000004")

    def test_parse_transaction_hash_accepts_tx_hash_line(self):
        self.assertEqual(
            parse_bankr_transaction_hash(
                "Swap successful\nTx Hash:  0x8eb6fe0859bf2fe1726322e251c9bc18ef2033bc443285436ee33636d10b04d6"
            ),
            "0x8eb6fe0859bf2fe1726322e251c9bc18ef2033bc443285436ee33636d10b04d6",
        )

    def test_quote_runs_bankr_swap_quote_only(self):
        with patch(
            "subprocess.run",
            return_value=completed(
                stdout="You pay:  25 SINGIT ($0.00)\nYou receive:  ~0.10 USDC ($0.10)\n  Min received:       0.095 USDC"
            ),
        ) as run:
            client = BankrSwapClient(bankr_cli="/tmp/bankr")
            quote = client.quote(
                from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
                to_token="USDC",
                amount="25",
                chain="base",
            )

        self.assertEqual(quote["toAmount"], "0.10")
        self.assertIn("--quote-only", run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][:3], ["/tmp/bankr", "wallet", "swap"])

    def test_swap_runs_bankr_swap_without_quote_only(self):
        with patch(
            "subprocess.run",
            return_value=completed(
                stdout="Swap successful\nTx Hash:  0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
        ) as run:
            client = BankrSwapClient(bankr_cli="/tmp/bankr")
            result = client.swap(
                from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
                to_token="USDC",
                amount="25",
                chain="base",
            )

        self.assertEqual(result["txId"], "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertNotIn("--quote-only", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest tests.test_bankr_swap -v
```

Expected: import fails because `sign402_gateway.bankr_swap` does not exist.

- [ ] **Step 3: Implement `bankr_swap.py`**

Create `sign402-gateway/sign402_gateway/bankr_swap.py`:

```python
import re
import subprocess
from typing import Any


TX_HASH_RE = re.compile(r"\bTx Hash:\s*(0x[a-fA-F0-9]{64})\b")
RECEIVE_RE = re.compile(r"You receive:\s*~?\s*([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z0-9_.$-]+)")
PAY_RE = re.compile(r"You pay:\s*([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z0-9_.$-]+)")
MIN_RE = re.compile(r"Min received:\s*([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z0-9_.$-]+)")


def parse_bankr_transaction_hash(stdout: str) -> str | None:
    match = TX_HASH_RE.search(stdout)
    return match.group(1) if match else None


def parse_bankr_swap_quote(stdout: str) -> dict[str, str]:
    pay = PAY_RE.search(stdout)
    receive = RECEIVE_RE.search(stdout)
    minimum = MIN_RE.search(stdout)
    if not pay or not receive:
        raise ValueError("Bankr swap quote output did not include pay/receive amounts")
    return {
        "fromAmount": pay.group(1),
        "fromToken": pay.group(2),
        "toAmount": receive.group(1),
        "toToken": receive.group(2),
        "minToAmount": minimum.group(1) if minimum else receive.group(1),
    }


class BankrSwapClient:
    def __init__(self, *, bankr_cli: str):
        self.bankr_cli = bankr_cli

    def quote(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        command = self._command(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            chain=chain,
        ) + ["--quote-only"]
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or result.stdout.strip() or "Bankr swap quote failed")
        quote = parse_bankr_swap_quote(result.stdout)
        quote.update({"ok": True, "command": command, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
        return quote

    def swap(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        command = self._command(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            chain=chain,
        )
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=180)
        payload = {
            "ok": result.returncode == 0,
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "txId": parse_bankr_transaction_hash(result.stdout),
        }
        if result.returncode != 0:
            raise ValueError(payload["stderr"] or payload["stdout"] or "Bankr swap failed")
        return payload

    def _command(self, *, from_token: str, to_token: str, amount: str, chain: str) -> list[str]:
        return [
            self.bankr_cli,
            "wallet",
            "swap",
            "--from",
            str(from_token),
            "--to",
            str(to_token),
            "--amount",
            str(amount),
            "--chain",
            str(chain),
        ]
```

- [ ] **Step 4: Run tests and verify GREEN**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest tests.test_bankr_swap -v
```

Expected: all `test_bankr_swap` tests pass.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/bankr_swap.py sign402-gateway/tests/test_bankr_swap.py
git commit -m "Add Bankr swap client"
```

## Task 2: Real-rate pricing search

**Files:**
- Create: `sign402-gateway/sign402_gateway/real_rate_pricing.py`
- Create: `sign402-gateway/tests/test_real_rate_pricing.py`

- [ ] **Step 1: Write failing pricing tests**

Create `sign402-gateway/tests/test_real_rate_pricing.py`:

```python
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
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest tests.test_real_rate_pricing -v
```

Expected: import fails because `sign402_gateway.real_rate_pricing` does not exist.

- [ ] **Step 3: Implement `real_rate_pricing.py`**

Create `sign402-gateway/sign402_gateway/real_rate_pricing.py`:

```python
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
        buffered_target = (target * (Decimal(10_000 + self.buffer_bps) / Decimal(10_000))).quantize(
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
        best_quote = high_quote
        for _ in range(max(1, self.search_iterations)):
            mid = (low + high) / 2
            quote = self._quote(mid)
            if Decimal(quote["toAmount"]) >= buffered_target:
                best_amount = mid
                best_quote = quote
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
            "requiredSingitAtomic": str(int(rounded_singit * (Decimal(10) ** SINGIT_DECIMALS))),
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
```

- [ ] **Step 4: Run tests and verify GREEN**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest tests.test_real_rate_pricing -v
```

Expected: all real-rate pricing tests pass.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/real_rate_pricing.py sign402-gateway/tests/test_real_rate_pricing.py
git commit -m "Price Bitrefill quotes from Bankr swap route"
```

## Task 3: Real-rate Bitrefill quotes

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_quote.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/tests/test_bitrefill_quote.py`
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py`

- [ ] **Step 1: Add failing real-rate quote builder test**

Append to `BitrefillQuoteTests` in `sign402-gateway/tests/test_bitrefill_quote.py`:

```python
def test_build_real_rate_quote_uses_bankr_pricing_result(self):
    quote = build_real_rate_quote(
        request={"productId": "bitrefill-giftcard-usd", "packageId": "0.1", "country": "US"},
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
```

Update imports:

```python
from sign402_gateway.bitrefill_quote import build_quote, build_real_rate_quote, ...
```

- [ ] **Step 2: Add failing quote service test**

Append to `BitrefillRunnerTests` in `sign402-gateway/tests/test_bitrefill_runner.py`:

```python
class FixedRealRatePricer:
    def price_for_usdc(self, target_usdc):
        return {
            "pricingMode": "bankr_real_rate",
            "targetUsdc": str(target_usdc),
            "bufferedTargetUsdc": "0.11",
            "requiredSingit": "25000",
            "requiredSingitAtomic": "25000000000000000000000",
            "expectedUsdc": "0.111",
            "minUsdc": "0.109",
        }


def test_quote_service_can_use_real_rate_pricer(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
        service = BitrefillQuoteService(
            bitrefill_client=TestBitrefillClient(),
            store=store,
            singit_usd_price_provider=lambda: "0.01",
            real_rate_pricer=FixedRealRatePricer(),
            quote_id_provider=lambda: "quote_real_1",
            now_provider=lambda: 1_719_000_000,
        )

        quote = service.quote({"productId": "test-gift-card-code", "packageId": "1", "country": "US"})

        self.assertEqual(quote["pricingMode"], "bankr_real_rate")
        self.assertEqual(quote["singitAmount"], "25000")
        self.assertEqual(store.get_quote("quote_real_1")["quote"]["maxSingitAtomic"], "25000000000000000000000")
```

- [ ] **Step 3: Run tests and verify RED**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_quote.BitrefillQuoteTests.test_build_real_rate_quote_uses_bankr_pricing_result \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_quote_service_can_use_real_rate_pricer -v
```

Expected: `build_real_rate_quote` import fails and `BitrefillQuoteService.__init__` does not accept `real_rate_pricer`.

- [ ] **Step 4: Implement real-rate quote builder**

In `sign402-gateway/sign402_gateway/bitrefill_quote.py`, add:

```python
def build_real_rate_quote(
    *,
    request: dict[str, Any],
    product: dict[str, Any],
    pricing: dict[str, Any],
    quote_id: str | None = None,
    now_epoch: int | None = None,
    ttl_seconds: int = DEFAULT_QUOTE_TTL_SECONDS,
) -> dict[str, Any]:
    product_id = str(product["productId"])
    package_id = str(product["packageId"])
    if str(request.get("productId", "")).strip() != product_id:
        raise ValueError("selected Bitrefill product does not match request")
    if str(request.get("packageId", "")).strip() != package_id:
        raise ValueError("selected Bitrefill package does not match request")
    price_usd = Decimal(str(product.get("priceUsd", product.get("packageValue", ""))))
    if price_usd <= 0:
        raise ValueError("product priceUsd must be positive")
    required_singit = Decimal(str(pricing["requiredSingit"]))
    if required_singit <= 0:
        raise ValueError("requiredSingit must be positive")
    started_at = int(now_epoch if now_epoch is not None else time.time())
    expires_at_epoch = started_at + int(ttl_seconds)
    product_name = str(product.get("name") or product_id)
    value = str(product.get("packageValue", "")).strip()
    if not value:
        raise ValueError("packageValue is required")

    return {
        "quoteId": quote_id or new_quote_id(),
        "productId": product_id,
        "productName": product_name,
        "productType": str(product["productType"]),
        "packageId": package_id,
        "country": str(product.get("country") or request.get("country", "")).upper(),
        "currency": str(product.get("currency", "USD")),
        "packageValue": value,
        "priceUsd": f"{price_usd:.2f}",
        "pricingMode": "bankr_real_rate",
        "requiredUsdc": str(pricing["targetUsdc"]),
        "bufferedTargetUsdc": str(pricing["bufferedTargetUsdc"]),
        "expectedUsdc": str(pricing["expectedUsdc"]),
        "minUsdc": str(pricing["minUsdc"]),
        "singitAmount": format_decimal(required_singit),
        "maxSingitAtomic": str(pricing["requiredSingitAtomic"]),
        "createdAtEpoch": started_at,
        "expiresAtEpoch": expires_at_epoch,
        "expiresAt": iso_from_epoch(expires_at_epoch),
        "quoteText": (
            f"{product_name} ${value}: pay {format_decimal(required_singit)} SINGIT "
            f"at the real-rate Bankr route for about {pricing['expectedUsdc']} USDC. "
            f"Quote expires in {ttl_seconds}s."
        ),
    }
```

Update `build_purchase_commitment`:

```python
    if quote.get("pricingMode"):
        commitment["pricingMode"] = str(quote["pricingMode"])
    if quote.get("requiredUsdc"):
        commitment["requiredUsdc"] = str(quote["requiredUsdc"])
```

- [ ] **Step 5: Wire `BitrefillQuoteService`**

In `sign402-gateway/sign402_gateway/bitrefill_runner.py`, update imports:

```python
from .bitrefill_quote import build_purchase_commitment, build_quote, build_real_rate_quote, ...
```

Update constructor:

```python
        real_rate_pricer: Any | None = None,
```

Store:

```python
        self.real_rate_pricer = real_rate_pricer
```

In `quote()`, replace the current `build_quote(...)` call with:

```python
        if self.real_rate_pricer is not None:
            pricing = self.real_rate_pricer.price_for_usdc(product["priceUsd"])
            quote = build_real_rate_quote(
                request=payload,
                product=product,
                pricing=pricing,
                quote_id=self.quote_id_provider(),
                now_epoch=self.now_provider(),
            )
        else:
            quote = build_quote(
                request=payload,
                product=product,
                singit_usd_price=self.singit_usd_price_provider(),
                quote_id=self.quote_id_provider(),
                now_epoch=self.now_provider(),
            )
```

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_quote.BitrefillQuoteTests.test_build_real_rate_quote_uses_bankr_pricing_result \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_quote_service_can_use_real_rate_pricer -v
```

Expected: both tests pass.

- [ ] **Step 7: Run existing quote/runner tests**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest tests.test_bitrefill_quote tests.test_bitrefill_runner -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add sign402-gateway/sign402_gateway/bitrefill_quote.py sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_bitrefill_quote.py sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "Create real-rate Bitrefill quotes"
```

## Task 4: Swap SINGIT before Bitrefill fulfillment

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py`

- [ ] **Step 1: Add failing funding runner tests**

Append to `BitrefillRunnerTests`:

```python
class FakeFundingRunner:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, quote):
        self.calls.append(quote)
        if self.fail:
            raise RuntimeError("swap route failed")
        return {"ok": True, "txId": "0xSWAP", "expectedUsdc": quote.get("expectedUsdc", "0.11")}


def test_fulfillment_swaps_singit_before_bitrefill_purchase(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
        store.save_quote({
            "quoteId": "quote_1",
            "productId": "test-gift-card-code",
            "productName": "Test Gift Card Code",
            "productType": "gift_card",
            "packageId": "1",
            "packageValue": "1",
            "priceUsd": "1.00",
            "pricingMode": "bankr_real_rate",
            "expectedUsdc": "1.10",
            "maxSingitAtomic": "25000000000000000000000",
            "singitAmount": "25000",
            "expiresAtEpoch": 1_719_000_120,
        })
        store.advance_state(
            "quote_1",
            "FIREFLY_APPROVED",
            {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
        )
        funding = FakeFundingRunner()
        bitrefill = Mock(
            **{"buy_product.return_value": {"ok": True, "orderId": "order_1", "status": "delivered"}}
        )
        runner = BitrefillFulfillmentRunner(
            store=store,
            bitrefill_client=bitrefill,
            funding_runner=funding,
            now_provider=lambda: 1_719_000_001,
        )

        runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

        self.assertEqual(funding.calls[0]["quoteId"], "quote_1")
        bitrefill.buy_product.assert_called_once()
        self.assertEqual(store.get_quote("quote_1")["metadata"]["bankrSwap"]["txId"], "0xSWAP")


def test_fulfillment_does_not_buy_bitrefill_when_swap_fails(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
        store.save_quote({
            "quoteId": "quote_1",
            "productId": "test-gift-card-code",
            "productName": "Test Gift Card Code",
            "productType": "gift_card",
            "packageId": "1",
            "packageValue": "1",
            "priceUsd": "1.00",
            "pricingMode": "bankr_real_rate",
            "expectedUsdc": "1.10",
            "maxSingitAtomic": "25000000000000000000000",
            "singitAmount": "25000",
            "expiresAtEpoch": 1_719_000_120,
        })
        store.advance_state(
            "quote_1",
            "FIREFLY_APPROVED",
            {"fulfillmentTokenHash": hashlib.sha256(b"valid_token").hexdigest()},
        )
        bitrefill = Mock()
        runner = BitrefillFulfillmentRunner(
            store=store,
            bitrefill_client=bitrefill,
            funding_runner=FakeFundingRunner(fail=True),
            now_provider=lambda: 1_719_000_001,
        )

        with self.assertRaisesRegex(RuntimeError, "swap route failed"):
            runner.fulfill({"quoteId": "quote_1", "fulfillmentToken": "valid_token"})

        bitrefill.buy_product.assert_not_called()
        record = store.get_quote("quote_1")
        self.assertEqual(record["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(record["metadata"]["fundingError"], "swap route failed")
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_fulfillment_swaps_singit_before_bitrefill_purchase \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_fulfillment_does_not_buy_bitrefill_when_swap_fails -v
```

Expected: `BitrefillFulfillmentRunner.__init__` does not accept `funding_runner`.

- [ ] **Step 3: Implement optional funding step**

In `BitrefillFulfillmentRunner.__init__`, add:

```python
        funding_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
```

Store:

```python
        self.funding_runner = funding_runner
```

In `fulfill()`, after `try_mark_fulfilling()` and before `bitrefill_client.buy_product(...)`, add:

```python
        if self.funding_runner is not None:
            try:
                funding_result = self.funding_runner(record["quote"])
                self.store.advance_state(quote_id, "FULFILLING", {"bankrSwap": funding_result})
            except Exception as exc:
                self.store.advance_state(
                    quote_id,
                    "RECONCILIATION_REQUIRED",
                    {"fundingError": str(exc)},
                )
                raise
```

- [ ] **Step 4: Run focused tests and verify GREEN**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_fulfillment_swaps_singit_before_bitrefill_purchase \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_fulfillment_does_not_buy_bitrefill_when_swap_fails -v
```

Expected: both tests pass.

- [ ] **Step 5: Run runner suite**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest tests.test_bitrefill_runner -v
```

Expected: all runner tests pass.

- [ ] **Step 6: Commit**

```bash
git add sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "Swap SINGIT before Bitrefill fulfillment"
```

## Task 5: Bankr real-rate funding runner and env wiring

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`

- [ ] **Step 1: Add failing tests for funding runner**

Add imports in `test_gateway_server.py`:

```python
from sign402_gateway.server import BankrSingitToUsdcFundingRunner, build_real_rate_pricer_from_env
```

Add tests:

```python
def test_bankr_singit_to_usdc_funding_runner_swaps_quote_singit(self):
    swap_client = Mock(
        **{
            "swap.return_value": {
                "ok": True,
                "txId": "0xSWAP",
                "stdout": "Swap successful",
            }
        }
    )
    runner = BankrSingitToUsdcFundingRunner(
        swap_client=swap_client,
        from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
        to_token="USDC",
        chain="base",
    )

    result = runner(
        {
            "quoteId": "quote_1",
            "pricingMode": "bankr_real_rate",
            "singitAmount": "25000",
            "expectedUsdc": "0.11",
        }
    )

    self.assertEqual(result["txId"], "0xSWAP")
    swap_client.swap.assert_called_once_with(
        from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
        to_token="USDC",
        amount="25000",
        chain="base",
    )


def test_bankr_singit_to_usdc_funding_runner_rejects_fixed_price_quote(self):
    runner = BankrSingitToUsdcFundingRunner(
        swap_client=Mock(),
        from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
        to_token="USDC",
        chain="base",
    )

    with self.assertRaisesRegex(ValueError, "bankr_real_rate"):
        runner({"quoteId": "quote_1", "singitAmount": "11"})


def test_real_rate_pricer_env_builder_requires_max_singit(self):
    with self.assertRaisesRegex(ValueError, "SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER"):
        build_real_rate_pricer_from_env(
            {
                "SIGN402_BITREFILL_PRICING_MODE": "bankr_real_rate",
            }
        )
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_bankr_singit_to_usdc_funding_runner_swaps_quote_singit \
  tests.test_gateway_server.GatewayServerTests.test_bankr_singit_to_usdc_funding_runner_rejects_fixed_price_quote \
  tests.test_gateway_server.GatewayServerTests.test_real_rate_pricer_env_builder_requires_max_singit -v
```

Expected: imports fail because runner and builder do not exist.

- [ ] **Step 3: Implement funding runner and pricer builder**

In `server.py`, import:

```python
from .bankr_swap import BankrSwapClient
from .real_rate_pricing import RealRateSingitPricer
```

Add:

```python
class BankrSingitToUsdcFundingRunner:
    def __init__(self, *, swap_client: Any, from_token: str, to_token: str = "USDC", chain: str = "base"):
        self.swap_client = swap_client
        self.from_token = from_token
        self.to_token = to_token
        self.chain = chain

    def __call__(self, quote: dict[str, Any]) -> dict[str, Any]:
        if quote.get("pricingMode") != "bankr_real_rate":
            raise ValueError("Bankr real-rate funding requires pricingMode=bankr_real_rate")
        amount = str(quote.get("singitAmount", "")).strip()
        if not amount:
            raise ValueError("quote singitAmount is required for Bankr swap")
        result = self.swap_client.swap(
            from_token=self.from_token,
            to_token=self.to_token,
            amount=amount,
            chain=self.chain,
        )
        return {
            **result,
            "pricingMode": "bankr_real_rate",
            "fromToken": self.from_token,
            "toToken": self.to_token,
            "chain": self.chain,
            "amount": amount,
            "expectedUsdc": str(quote.get("expectedUsdc", "")),
        }
```

Add:

```python
def build_real_rate_pricer_from_env(env: dict[str, str] | None = None):
    values = os.environ if env is None else env
    mode = values.get("SIGN402_BITREFILL_PRICING_MODE", "fixed").strip().lower()
    if mode != "bankr_real_rate":
        return None
    max_singit = values.get("SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER", "").strip()
    if not max_singit:
        raise ValueError("SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER is required for bankr_real_rate")
    bankr_cli = values.get("SIGN402_BANKR_CLI", DEFAULT_BANKR_CLI)
    swap_client = BankrSwapClient(bankr_cli=bankr_cli)
    return RealRateSingitPricer(
        quote_client=swap_client,
        from_token=values.get("SIGN402_BANKR_SWAP_FROM_TOKEN", DEFAULT_SINGIT_TOKEN_ADDRESS),
        to_token=values.get("SIGN402_BANKR_SWAP_TO_TOKEN", "USDC"),
        chain=values.get("SIGN402_BANKR_SWAP_CHAIN", "base"),
        buffer_bps=int(values.get("SIGN402_BITREFILL_USDC_BUFFER_BPS", "1000")),
        max_singit=max_singit,
    )
```

- [ ] **Step 4: Wire `build_server()`**

In `build_server()`:

```python
    real_rate_pricer = build_real_rate_pricer_from_env()
```

Pass to quote service:

```python
        real_rate_pricer=real_rate_pricer,
```

Build funding runner:

```python
    funding_runner = None
    if real_rate_pricer is not None:
        funding_runner = BankrSingitToUsdcFundingRunner(
            swap_client=BankrSwapClient(bankr_cli=DEFAULT_BANKR_CLI),
            from_token=os.getenv("SIGN402_BANKR_SWAP_FROM_TOKEN", DEFAULT_SINGIT_TOKEN_ADDRESS),
            to_token=os.getenv("SIGN402_BANKR_SWAP_TO_TOKEN", "USDC"),
            chain=os.getenv("SIGN402_BANKR_SWAP_CHAIN", "base"),
        )
```

Pass to fulfillment runner:

```python
        funding_runner=funding_runner,
```

- [ ] **Step 5: Run focused tests and verify GREEN**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_bankr_singit_to_usdc_funding_runner_swaps_quote_singit \
  tests.test_gateway_server.GatewayServerTests.test_bankr_singit_to_usdc_funding_runner_rejects_fixed_price_quote \
  tests.test_gateway_server.GatewayServerTests.test_real_rate_pricer_env_builder_requires_max_singit -v
```

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "Wire Bankr real-rate SINGIT funding"
```

## Task 6: Update Bankr endpoint metadata and tests

**Files:**
- Modify: `singit-risk-check/bankr.x402.json`
- Modify: `singit-risk-check/x402/buy-bitrefill/index.mjs`
- Modify: `singit-risk-check/x402/buy-bitrefill/index.ts`
- Modify: `singit-risk-check/tests/buy-bitrefill.test.mjs`

- [ ] **Step 1: Add failing endpoint test for pricingMode**

In `singit-risk-check/tests/buy-bitrefill.test.mjs`, update the successful test's mocked gateway payload:

```js
body: JSON.stringify({
  ok: true,
  quoteId: "quote_1",
  status: "ready_for_singit_settlement",
  pricingMode: "bankr_real_rate",
  settleAmountAtomic: "25000000000000000000000",
})
```

Add assertions:

```js
assert.equal(response.headers.get("X-402-Settle-Amount"), "25000000000000000000000");
assert.equal(payload.pricingMode, "bankr_real_rate");
assert.equal(payload.settleAmountAtomic, "25000000000000000000000");
```

- [ ] **Step 2: Run Node test and verify RED**

```bash
cd singit-risk-check
node --test tests/buy-bitrefill.test.mjs
```

Expected: response body lacks `pricingMode` and `settleAmountAtomic`.

- [ ] **Step 3: Update endpoint handler**

In both `index.mjs` and `index.ts`, change successful response body to:

```js
return Response.json(
  {
    ok: true,
    quoteId,
    orderId: payload.orderId,
    status: payload.status || "ready_for_singit_settlement",
    pricingMode: payload.pricingMode || "fixed",
    settleAmountAtomic: String(settleAmount),
  },
  { headers },
);
```

- [ ] **Step 4: Update `bankr.x402.json`**

Change `buy-bitrefill`:

```json
"description": "Collect real-rate SINGIT settlement for a Sign402 Bitrefill purchase",
"price": "1000000000",
"paymentScheme": "upto",
```

Add output properties:

```json
"pricingMode": {
  "type": "string",
  "description": "Pricing mode used by the gateway, normally bankr_real_rate"
},
"settleAmountAtomic": {
  "type": "string",
  "description": "Dynamic SINGIT atomic amount requested for this quote"
}
```

- [ ] **Step 5: Run Node tests and verify GREEN**

```bash
cd singit-risk-check
node --test tests/buy-bitrefill.test.mjs
```

Expected: all Node endpoint tests pass.

- [ ] **Step 6: Commit**

```bash
git add singit-risk-check/bankr.x402.json singit-risk-check/x402/buy-bitrefill/index.mjs singit-risk-check/x402/buy-bitrefill/index.ts singit-risk-check/tests/buy-bitrefill.test.mjs
git commit -m "Expose real-rate settlement in Bankr endpoint"
```

## Task 7: Full verification and no-spend quote-only probe

**Files:**
- No production file changes expected.

- [ ] **Step 1: Run full Python suite**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest discover -s tests -v
```

Expected: all Python tests pass.

- [ ] **Step 2: Run Firefly bridge suite**

```bash
cd sign402-bridge
../payment-executor/.venv/bin/python -m unittest discover -s tests -v
```

Expected: all Firefly bridge tests pass.

- [ ] **Step 3: Run Node suite**

```bash
cd singit-risk-check
node --test tests/buy-bitrefill.test.mjs
```

Expected: all Node tests pass.

- [ ] **Step 4: Run no-spend Bankr quote-only probe**

Use a small quote-only command. This must not execute a swap:

```bash
cd "/Users/mp/Documents/Berlin Hack"
/Users/mp/Documents/Berlin\ Hack/.tools/bankr-cli/node_modules/.bin/bankr wallet swap \
  --from 0xc2c1e0b7C401e6217193732272444D928646eba3 \
  --to USDC \
  --amount 11 \
  --chain base \
  --quote-only
```

Expected: command prints a Bankr quote and does not submit a transaction.

- [ ] **Step 5: Run `git diff --check`**

```bash
cd "/Users/mp/Documents/Berlin Hack"
git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 6: Commit final verification fixes if needed**

Only if Step 1-5 required small fixes:

```bash
git add sign402-gateway singit-risk-check
git commit -m "Verify real-rate SINGIT Bitrefill flow"
```

## Task 8: Live-readiness checklist, no automatic purchase

**Files:**
- No code changes expected unless verification exposes a bug.

- [ ] **Step 1: Check Bankr endpoint deploy target**

```bash
cd singit-risk-check
/Users/mp/Documents/Berlin\ Hack/.tools/bankr-cli/node_modules/.bin/bankr x402 deploy buy-bitrefill
```

Expected: endpoint deploys and reports the same `https://x402.bankr.bot/.../buy-bitrefill` URL.

- [ ] **Step 2: Start gateway in real-rate mode**

Use the existing local key file and explicit max cap:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
BITREFILL_API_KEY="$(<.bitrefill-api-key)" \
FIREFLY_PORT=/dev/cu.usbmodem11301 \
SIGN402_GATEWAY_PORT=8099 \
SIGN402_BITREFILL_MODE=live \
SIGN402_BITREFILL_PAYMENT_METHOD=usdc_base \
SIGN402_BITREFILL_PRICING_MODE=bankr_real_rate \
SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER=1000000000 \
SIGN402_BANKR_SWAP_FROM_TOKEN=0xc2c1e0b7C401e6217193732272444D928646eba3 \
SIGN402_BANKR_SWAP_TO_TOKEN=USDC \
SIGN402_BANKR_SWAP_CHAIN=base \
SIGN402_BITREFILL_USDC_BUFFER_BPS=1000 \
SIGN402_TREASURY_REFUND_ADDRESS=0x3b3e349e6cfee692b69d2c63ce86f7d444667d98 \
SIGN402_BANKR_WALLET_ADDRESS=0x3b3e349e6cfee692b69d2c63ce86f7d444667d98 \
SIGN402_BANKR_FULFILLMENT_SECRET=secret_zX1K5TauOX-HIXprez__4BpJWJX3LNnGTQkyIT-XcmY \
SIGN402_BANKR_BITREFILL_URL=https://x402.bankr.bot/0x3b3e349e6cfee692b69d2c63ce86f7d444667d98/buy-bitrefill \
/opt/homebrew/opt/python@3.14/bin/python3.14 -c 'import sys; sys.path[:0]=["/Users/mp/Documents/Berlin Hack/payment-executor/.venv/lib/python3.14/site-packages","/Users/mp/Documents/Berlin Hack/sign402-gateway","/Users/mp/Documents/Berlin Hack/sign402-bridge","/Users/mp/Documents/Berlin Hack/live-demo","/Users/mp/Documents/Berlin Hack/payment-executor","/Users/mp/Documents/Berlin Hack/demo-resource-server"]; from sign402_gateway.server import main; main()'
```

Expected: gateway starts and `/health` returns `ok: true`.

- [ ] **Step 3: Create a no-spend real-rate Bitrefill quote**

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/quote-bitrefill \
  -H "Content-Type: application/json" \
  -d '{"productId":"bitrefill-giftcard-usd","packageId":"0.1","country":"US","recipient":{}}'
```

Expected: response includes:

```json
{
  "pricingMode": "bankr_real_rate",
  "requiredUsdc": "0.10",
  "singitAmount": "...",
  "maxSingitAtomic": "..."
}
```

- [ ] **Step 4: Stop before live purchase**

Do not run `/agent/buy-bitrefill` automatically.

Ask the user to confirm a live purchase with:

```text
подтверждаю покупку Bitrefill Gift Card USD на $0.10, максимум <quoted SINGIT> SINGIT, real-rate Bankr swap, USDC target $0.10
```

