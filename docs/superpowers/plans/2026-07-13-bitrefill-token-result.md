# Bitrefill Token-Aware Fulfillment Result Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return a successful, accurately named fulfillment result after a Bitrefill purchase funded with USDC, ETH, or another selected wallet token.

**Architecture:** Keep the existing `BitrefillFulfillmentRunner` boundary and make only its redacted response token-aware. Select the atomic maximum field from the immutable quote, preserve the legacy SINGIT response contract, and reject malformed quotes that contain neither supported maximum.

**Tech Stack:** Python 3, `unittest`, SQLite-backed `BitrefillCommerceStore`.

## Global Constraints

- Do not change quote commitments, approval hashes, wallet transfers, Bitrefill requests, database state ordering, or redemption storage.
- Legacy SINGIT responses retain `settleAmountAtomic` and `maxSingitAtomic`.
- Token-funded responses use `settleAmountAtomic`, `maxPaymentTokenAtomic`, and optional `paymentTokenSymbol`, with no misleading `maxSingitAtomic`.
- Common fields `ok`, `quoteId`, `orderId`, and `status` remain unchanged.

---

### Task 1: Make the fulfillment result token-aware

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py:792-801`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`

**Interfaces:**
- Consumes: `BitrefillFulfillmentRunner._redacted_result(quote: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]`.
- Produces: a legacy SINGIT result when `maxSingitAtomic` exists, or a token-aware result when `maxPaymentTokenAtomic` exists.

- [ ] **Step 1: Write a failing token-funded fulfillment test**

Add a test that saves a valid gift-card quote with `maxPaymentTokenAtomic="100000"`, `paymentTokenSymbol="USDC"`, and no `maxSingitAtomic`; advances it to `USER_APPROVED` with a valid fulfillment token hash; fulfills it through a mock provider returning a delivered redemption; and asserts:

```python
self.assertEqual(result["settleAmountAtomic"], "100000")
self.assertEqual(result["maxPaymentTokenAtomic"], "100000")
self.assertEqual(result["paymentTokenSymbol"], "USDC")
self.assertNotIn("maxSingitAtomic", result)
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. python3 -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_fulfillment_runner_returns_token_aware_atomic_amount -v
```

Expected: `ERROR` with `KeyError: 'maxSingitAtomic'` from `_redacted_result`.

- [ ] **Step 3: Implement the minimal token-aware response**

Build the common redacted result first. If `maxPaymentTokenAtomic` is present, add:

```python
{
    "settleAmountAtomic": str(quote["maxPaymentTokenAtomic"]),
    "maxPaymentTokenAtomic": str(quote["maxPaymentTokenAtomic"]),
}
```

Add `paymentTokenSymbol` only when the quote contains a non-empty symbol. Otherwise, if `maxSingitAtomic` is present, preserve:

```python
{
    "settleAmountAtomic": str(quote["maxSingitAtomic"]),
    "maxSingitAtomic": str(quote["maxSingitAtomic"]),
}
```

If neither maximum exists, raise `ValueError("quote settlement maximum is missing")`.

- [ ] **Step 4: Verify GREEN and backward compatibility**

Run the focused test:

```bash
cd sign402-gateway
PYTHONPATH=. python3 -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_fulfillment_runner_returns_token_aware_atomic_amount -v
```

Expected: `OK`.

Then run the full gateway suite:

```bash
cd sign402-gateway
PYTHONPATH=. python3 -m unittest discover -s tests -q
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 5: Commit and publish**

```bash
git add \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/tests/test_bitrefill_runner.py \
  docs/superpowers/plans/2026-07-13-bitrefill-token-result.md
git commit -m "Fix token-funded Bitrefill results"
git push singitai x402Bnkr
```
