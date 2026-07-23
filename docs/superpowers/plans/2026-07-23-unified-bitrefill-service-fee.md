# Unified 2% Bitrefill Service Fee Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Charge exactly one transparent 2% service fee on every Bitrefill purchase, with no minimum fee and no application-level pricing buffer.

**Architecture:** A shared Decimal-based helper calculates the fee and total in USD. Fixed SINGIT, direct USDC, and real-rate token quotes all convert the same `totalUsd`; quote commitments, approval copy, and spend controls bind that complete charge.

**Tech Stack:** Python 3.11+, `decimal.Decimal`, `unittest`, existing Sign402 gateway modules and SQLite quote store.

## Global Constraints

- `SERVICE_FEE_BPS` is exactly `200`.
- There is no minimum fee.
- The old fixed-route 5% margin and real-rate 10% pricing buffer must not remain active.
- Round only upward at the payment token's atomic-unit boundary.
- Keep Bitrefill affiliate revenue outside the user-facing calculation.
- Preserve unrelated working-tree changes in `sign402-gateway/.env.example`, `sign402-gateway/sign402_gateway/server.py`, and `sign402-gateway/tests/test_gateway_server.py`.
- Never create or pay a Bitrefill order while testing this change.

---

### Task 1: Shared fee calculation and fixed SINGIT quotes

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_quote.py`
- Test: `sign402-gateway/tests/test_bitrefill_quote.py`

**Interfaces:**
- Produces: `SERVICE_FEE_BPS: int = 200`
- Produces: `calculate_service_fee(price_usd: Any) -> tuple[Decimal, Decimal]`, returning `(service_fee_usd, total_usd)`
- Produces quote fields: `serviceFeeBps`, `serviceFeeUsd`, `totalUsd`

- [ ] **Step 1: Write failing fee and fixed-quote tests**

Add tests that import `SERVICE_FEE_BPS` and `calculate_service_fee`, then assert the no-minimum micro-price behavior and atomic precision:

```python
def test_service_fee_is_two_percent_without_a_minimum(self):
    fee, total = calculate_service_fee("0.10")

    self.assertEqual(SERVICE_FEE_BPS, 200)
    self.assertEqual(fee, Decimal("0.002"))
    self.assertEqual(total, Decimal("0.102"))

def test_build_quote_charges_two_percent_with_atomic_precision(self):
    quote = build_quote(
        request={"productId": "bitrefill-giftcard-usd", "packageId": "0.1", "country": "US"},
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
    self.assertEqual(quote["maxSingitAtomic"], str(Decimal("10.2") * 10**SINGIT_DECIMALS).split(".")[0])
    self.assertNotIn("marginBps", quote)
```

Update existing fixed-quote expectations from 5% values to 2% values and remove explicit `margin_bps=500` arguments.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_quote.BitrefillQuoteTests.test_service_fee_is_two_percent_without_a_minimum \
  tests.test_bitrefill_quote.BitrefillQuoteTests.test_build_quote_charges_two_percent_with_atomic_precision -v
```

Expected: FAIL because the constant/helper and new quote fields do not exist and the old quote rounds to whole SINGIT.

- [ ] **Step 3: Implement the shared calculation and fixed quote**

In `bitrefill_quote.py`, replace `DEFAULT_MARGIN_BPS` and the per-call margin with:

```python
SERVICE_FEE_BPS = 200


def calculate_service_fee(price_usd: Any) -> tuple[Decimal, Decimal]:
    price = Decimal(str(price_usd))
    if price <= 0:
        raise ValueError("product priceUsd must be positive")
    fee = price * Decimal(SERVICE_FEE_BPS) / Decimal(10_000)
    return fee, price + fee
```

Have `build_quote` call this helper, convert `total_usd` to SINGIT, and quantize to `Decimal(1).scaleb(-SINGIT_DECIMALS)` with `ROUND_CEILING`. Store the new fee fields with `format_decimal` and remove `marginBps`.

- [ ] **Step 4: Bind the fee fields in purchase commitments**

Add the fields unconditionally from a newly generated quote:

```python
commitment.update(
    {
        "serviceFeeBps": int(quote["serviceFeeBps"]),
        "serviceFeeUsd": str(quote["serviceFeeUsd"]),
        "totalUsd": str(quote["totalUsd"]),
    }
)
```

Extend the commitment test to assert all three values.

- [ ] **Step 5: Run the quote test module and verify GREEN**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest tests.test_bitrefill_quote -v
```

Expected: all `BitrefillQuoteTests` pass.

- [ ] **Step 6: Commit the self-contained quote change**

```bash
git add sign402-gateway/sign402_gateway/bitrefill_quote.py sign402-gateway/tests/test_bitrefill_quote.py
git commit -m "feat: apply two percent Bitrefill service fee"
```

---

### Task 2: Apply the same total to real-rate tokens and direct USDC

**Files:**
- Modify: `sign402-gateway/sign402_gateway/real_rate_pricing.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_real_rate_pricing.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`

**Interfaces:**
- Consumes: `calculate_service_fee(price_usd) -> (fee, total)`
- Produces: `RealRateSingitPricer(..., buffer_bps=0)` as the default
- Produces: all real-rate `targetUsdc` and direct-USDC required amounts equal to `totalUsd`

- [ ] **Step 1: Write failing direct-USDC and real-rate service tests**

Change the direct-USDC quote test to expect:

```python
self.assertEqual(quote["serviceFeeUsd"], "0.02")
self.assertEqual(quote["totalUsd"], "1.02")
self.assertEqual(quote["paymentTokenAmount"], "1.02")
self.assertEqual(quote["maxPaymentTokenAtomic"], "1020000")
self.assertEqual(quote["requiredUsdc"], "1.02")
```

Change `FixedWalletTokenPricer` assertions so its recorded target is `1.02`, and add a real-rate quote assertion that no second buffer is present:

```python
self.assertEqual(pricer.calls[0][0], "1.02")
self.assertEqual(quote["requiredUsdc"], "1.02")
self.assertEqual(quote["bufferedTargetUsdc"], "1.02")
```

Add a server construction test asserting a missing `SIGN402_BITREFILL_USDC_BUFFER_BPS` produces `pricer.buffer_bps == 0`.

- [ ] **Step 2: Run focused routing tests and verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_quote_service_uses_usdc_directly_without_requesting_a_swap_quote \
  tests.test_real_rate_pricing.RealRatePricingTests.test_finds_required_singit_for_target_usdc_with_no_application_buffer -v
```

Expected: FAIL because direct USDC still charges face value and the real-rate default still adds 1,000 bps.

- [ ] **Step 3: Price every route from the shared total**

Import `calculate_service_fee` into `bitrefill_runner.py`. Immediately after Bitrefill returns the product, calculate:

```python
service_fee_usd, total_usd = calculate_service_fee(product["priceUsd"])
total_usd_text = format_decimal(total_usd)
```

Pass `total_usd_text` to `_price_direct_usdc` and every `price_for_usdc` call. Keep `product["priceUsd"]` unchanged so fulfillment still uses the provider's product cost. Have `build_real_rate_quote` calculate and attach the fee fields and reject pricing data whose `targetUsdc` differs from `totalUsd`.

- [ ] **Step 4: Remove the application pricing buffer and whole-token rounding**

In `real_rate_pricing.py`:

```python
buffer_bps: int = 0
```

Always calculate the quantum from the resolved token decimals:

```python
token_decimals = int(decimals) if decimals is not None else SINGIT_DECIMALS
amount_quantum = Decimal(1).scaleb(-token_decimals)
```

Retain `bufferedTargetUsdc` for compatibility, but with a zero buffer it must equal `targetUsdc`.

In `build_real_rate_pricer_from_env`, stop reading `SIGN402_BITREFILL_USDC_BUFFER_BPS` for user pricing and construct the production pricer with `buffer_bps=0`. This prevents a stale deployment variable from adding a second markup.

- [ ] **Step 5: Update real-rate tests to the no-buffer contract**

Rename the buffer-specific test to `test_finds_required_singit_for_target_usdc_with_no_application_buffer`, construct with `buffer_bps=0`, call `result = pricer.price_for_usdc("0.102")`, and assert:

```python
self.assertEqual(result["targetUsdc"], "0.102")
self.assertEqual(result["bufferedTargetUsdc"], "0.102000")
self.assertGreaterEqual(Decimal(result["expectedUsdc"]), Decimal("0.102"))
self.assertEqual(result["requiredSingitAtomic"], "10200000000000000000")
```

Update tests that intentionally exercise buffer mechanics to pass their buffer explicitly; update default-behavior tests to expect zero.

- [ ] **Step 6: Run pricing and routing modules and verify GREEN**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_real_rate_pricing \
  tests.test_bitrefill_runner \
  tests.test_gateway_server -v
```

Expected: all three modules pass without changing the user's existing kill-switch behavior.

- [ ] **Step 7: Commit only Task 2 paths and preserved server hunks**

Before staging, inspect `git diff` and ensure the pre-existing purchase-pause changes remain intact. Stage the complete files only after confirming both sets of changes are wanted in the same working tree:

```bash
git diff -- sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git add sign402-gateway/sign402_gateway/real_rate_pricing.py \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/tests/test_real_rate_pricing.py \
  sign402-gateway/tests/test_bitrefill_runner.py
git add -p sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "feat: unify Bitrefill token pricing"
```

---

### Task 3: Show the fee and enforce the total charge

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`

**Interfaces:**
- Consumes quote fields: `priceUsd`, `serviceFeeBps`, `serviceFeeUsd`, `totalUsd`
- Produces approval context lines for product price, service fee, and total
- Produces spend-limit amounts based on `totalUsd`

- [ ] **Step 1: Write failing approval-copy tests**

Extend `test_bitrefill_approval_names_selected_token_and_amount` with a quote containing the fee fields and assert:

```python
self.assertIn("Product price: 1 USD", lines)
self.assertIn("Service fee (2%): 0.02 USD", lines)
self.assertIn("Total: 1.02 USD", lines)
self.assertNotIn("Cost: 1 USD", lines)
```

- [ ] **Step 2: Write failing spend-limit tests**

In gateway tests, construct a Bitrefill quote with `priceUsd="1.00"` and `totalUsd="1.02"`, invoke the quote/buy handlers, and assert `_bitrefill_spend_requirement` receives `"1.02"` for pre-quote enforcement, buy-time enforcement, and recorded spend.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_bitrefill_approval_names_selected_token_and_amount \
  tests.test_gateway_server.GatewayServerTests.test_bitrefill_spend_controls_use_total_usd -v
```

Expected: FAIL because the receipt shows only `priceUsd` and the server passes face price to spending controls.

- [ ] **Step 4: Implement transparent approval copy**

Replace the single cost line in `_bitrefill_approval_context_lines` with:

```python
f"Product price: {_format_amount(str(quote.get('priceUsd', '')))} USD",
f"Service fee (2%): {_format_amount(str(quote.get('serviceFeeUsd', '')))} USD",
f"Total: {_format_amount(str(quote.get('totalUsd', '')))} USD",
```

Keep selected-token maximum spend and quote expiry lines unchanged.

- [ ] **Step 5: Enforce and record `totalUsd`**

At each Bitrefill spend-control call in `server.py`, replace the face-only argument with the backward-compatible complete charge:

```python
_bitrefill_spend_requirement(result.get("totalUsd") or result["priceUsd"])
```

For quote dictionaries, use:

```python
_bitrefill_spend_requirement(quote.get("totalUsd") or quote["priceUsd"])
```

- [ ] **Step 6: Run receipt and gateway tests and verify GREEN**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_runner \
  tests.test_gateway_server -v
```

Expected: both modules pass.

- [ ] **Step 7: Commit the receipt and policy integration**

Use interactive staging for the already-dirty server and gateway test files:

```bash
git add sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_bitrefill_runner.py
git add -p sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "feat: disclose Bitrefill fee in approvals"
```

---

### Task 4: Full regression verification

**Files:**
- Verify: `sign402-gateway/sign402_gateway/bitrefill_quote.py`
- Verify: `sign402-gateway/sign402_gateway/real_rate_pricing.py`
- Verify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Verify: `sign402-gateway/sign402_gateway/server.py`
- Verify: related test modules

- [ ] **Step 1: Prove old application markups are absent**

Run:

```bash
rg -n 'DEFAULT_MARGIN_BPS = 500|margin_bps=500|SIGN402_BITREFILL_USDC_BUFFER_BPS.*, "1000"' sign402-gateway
```

Expected: no matches in active application or tests. Historical docs may still describe the old design and do not affect runtime.

- [ ] **Step 2: Run the complete gateway suite**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest discover -s tests -v
```

Expected: exit code 0 with zero failures and zero errors.

- [ ] **Step 3: Verify formatting and inspect the final diff**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors; only intended fee changes plus the user's pre-existing unrelated changes are present.

- [ ] **Step 4: Review the requirement checklist**

Confirm from tests and diff:

- every quote has `serviceFeeBps=200`;
- no minimum fee exists;
- fixed SINGIT uses atomic precision;
- direct USDC and real-rate tokens target `totalUsd`;
- no application-level 10% pricing buffer remains;
- commitments bind fee and total;
- approval copy discloses fee and total;
- spend limits use the total;
- no purchase API was invoked.

- [ ] **Step 5: Commit any final test-only corrections**

If verification required corrections, stage only those exact paths and commit:

```bash
git add sign402-gateway/tests/test_bitrefill_quote.py \
  sign402-gateway/tests/test_real_rate_pricing.py \
  sign402-gateway/tests/test_bitrefill_runner.py
git add -p sign402-gateway/tests/test_gateway_server.py
git commit -m "test: cover unified Bitrefill fee"
```
