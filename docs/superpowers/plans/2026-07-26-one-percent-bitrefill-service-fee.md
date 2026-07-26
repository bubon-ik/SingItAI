# One Percent Bitrefill Service Fee Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the single Bitrefill service fee from 2% to 1% for every new quote while leaving swap, wallet, buffer, and limit behavior unchanged.

**Architecture:** Keep `SERVICE_FEE_BPS` as the single calculation source. Update quote behavior first, then make approval and operator-facing percentage labels derive from committed or canonical basis points so displayed copy cannot drift from calculation.

**Tech Stack:** Python 3, `Decimal`, standard-library `unittest`

## Global Constraints

- New Bitrefill quotes use exactly 100 basis points.
- There is no minimum service fee.
- Direct USDC, SINGIT, and arbitrary-token routes use the same fee.
- Existing persisted quotes are not migrated or recalculated.
- Do not alter `buffer_bps`, wallet funding, fulfillment, spending-limit values, or product caps.

---

### Task 1: One Percent Quote Calculation

**Files:**
- Modify: `sign402-gateway/tests/test_bitrefill_quote.py`
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_quote.py`

**Interfaces:**
- Consumes: `calculate_service_fee(price_usd: Any) -> tuple[Decimal, Decimal]`
- Produces: quote fields `serviceFeeBps=100`, `serviceFeeUsd`, and `totalUsd`

- [ ] **Step 1: Change quote behavior tests to require 1%**

Update the fee test to use literal, independently calculated expectations:

```python
def test_service_fee_is_one_percent_without_a_minimum(self):
    fee, total = quote_module.calculate_service_fee("0.10")

    self.assertEqual(fee, Decimal("0.001"))
    self.assertEqual(total, Decimal("0.101"))
```

Update fixed quote expectations for a `0.10 USD` product:

```python
self.assertEqual(quote["serviceFeeBps"], 100)
self.assertEqual(quote["serviceFeeUsd"], "0.001")
self.assertEqual(quote["totalUsd"], "0.101")
self.assertEqual(quote["singitAmount"], "10.1")
```

Update the fixed-price `1.00 USD` expectation from `102` to `101` SINGIT and
the `25.00 USD` expectation from `2550` to `2525` SINGIT. Update real-rate
fixtures so `targetUsdc`, `bufferedTargetUsdc`, `expectedUsdc`, `minUsdc`,
`serviceFeeUsd`, `totalUsd`, and `requiredUsdc` use `0.101`.

Update direct-USDC and selected-token runner tests so a `1.00 USD` product
expects:

```python
self.assertEqual(quote["serviceFeeBps"], 100)
self.assertEqual(quote["serviceFeeUsd"], "0.01")
self.assertEqual(quote["totalUsd"], "1.01")
self.assertEqual(quote["paymentTokenAmount"], "1.01")
self.assertEqual(quote["maxPaymentTokenAtomic"], "1010000")
self.assertEqual(quote["requiredUsdc"], "1.01")
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_quote \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_quote_service_prices_authenticated_users_selected_wallet_token \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_quote_service_uses_usdc_directly_without_requesting_a_swap_quote -v
```

Expected: failures showing the implementation still calculates 200 basis
points and totals `0.102` / `1.02`.

- [ ] **Step 3: Apply the minimal production change**

In `sign402_gateway/bitrefill_quote.py`:

```python
SERVICE_FEE_BPS = 100
```

Do not change `calculate_service_fee`; its existing basis-point formula remains
the single source for fixed and real-rate totals.

- [ ] **Step 4: Run quote and routing tests and verify GREEN**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_quote \
  tests.test_bitrefill_runner -v
```

Expected: all tests pass after remaining fee-derived fixtures are updated to
the new literal outcomes.

### Task 2: Fee Labels Follow the Effective Rate

**Files:**
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`

**Interfaces:**
- Consumes: quote field `serviceFeeBps` and canonical `SERVICE_FEE_BPS`
- Produces: approval and spending-limit copy showing `1%`

- [ ] **Step 1: Change presentation tests to require 1%**

For approval context, use a committed one-percent quote:

```python
{
    "priceUsd": "1.00",
    "serviceFeeBps": 100,
    "serviceFeeUsd": "0.01",
    "totalUsd": "1.01",
}
```

Assert:

```python
self.assertIn("Service fee (1%): 0.01 USD", lines)
self.assertIn("Total: 1.01 USD", lines)
```

Update the spending-limits text assertion to:

```text
Bitrefill product maximum: 1000 USD before the 1% service fee.
```

- [ ] **Step 2: Run presentation tests and verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_bitrefill_approval_names_selected_token_and_amount \
  tests.test_gateway_server.GatewayServerTests.test_agent_spending_limits_returns_effective_limits -v
```

Expected: failures because production copy still contains the literal `2%`.

- [ ] **Step 3: Derive approval percentage from the quote**

In `_bitrefill_approval_context_lines`, calculate:

```python
fee_percent = format_decimal(
    Decimal(str(quote.get("serviceFeeBps", 0))) / Decimal(100)
)
```

Use it in the label:

```python
f"Service fee ({fee_percent}%): {_format_amount(str(quote.get('serviceFeeUsd', '')))} USD"
```

This preserves accurate display for old persisted quotes carrying
`serviceFeeBps=200`.

- [ ] **Step 4: Derive operator copy from the canonical rate**

Import `SERVICE_FEE_BPS` into `server.py`. In
`_spending_limits_telegram_text`, derive:

```python
service_fee_percent = format_decimal(
    Decimal(SERVICE_FEE_BPS) / Decimal(100)
)
```

Render:

```python
f"Bitrefill product maximum: {bitrefill_max} USD before the "
f"{service_fee_percent}% service fee."
```

- [ ] **Step 5: Run full relevant verification**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_quote \
  tests.test_bitrefill_runner \
  tests.test_gateway_server -v
```

Expected: all three modules pass with zero failures.

- [ ] **Step 6: Inspect the final diff**

Run:

```bash
git diff --check
git diff -- \
  sign402-gateway/sign402_gateway/bitrefill_quote.py \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/tests/test_bitrefill_quote.py \
  sign402-gateway/tests/test_bitrefill_runner.py \
  sign402-gateway/tests/test_gateway_server.py
```

Confirm the diff changes only the fee calculation, fee-derived expectations,
and fee labels. Do not commit implementation unless explicitly requested.
