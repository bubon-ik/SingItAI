# Bitrefill USD Micro-Purchase Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely quote and fulfill a real $0.10 Bitrefill USD gift card without misreading satoshi catalog prices or overpaying a USDC invoice.

**Architecture:** Extend `LiveBitrefillClient` normalization only for USD variable-value products by exposing the range minimum at face value. Add a hard pre-transfer check that compares the exact USDC invoice amount to `max_purchase_usd`; existing quote, Firefly, Bankr, and fulfillment components remain unchanged.

**Tech Stack:** Python 3.14, `unittest`, Bitrefill REST v2, Bankr CLI, SQLite commerce store.

---

### Task 1: Reproduce USD range normalization

**Files:**
- Modify: `sign402-gateway/tests/test_bitrefill_client.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`

- [ ] Add a test using the real response shape (`currency: USD`, `range.min: 0.1`, packages with satoshi `price`) and assert that package `0.1` has `priceUsd == "0.10"` while package `1` is not interpreted as `$1601`.
- [ ] Run `python3 -m unittest tests.test_bitrefill_client.BitrefillClientTests.test_live_usd_range_exposes_minimum_face_value -v` and confirm it fails because the minimum package is absent.
- [ ] Pass product currency and range metadata into package normalization; synthesize the USD range-minimum package and use USD `amount` for packaged face values in range products.
- [ ] Re-run the focused test and confirm it passes.

### Task 2: Prevent oversized treasury transfers

**Files:**
- Modify: `sign402-gateway/tests/test_bitrefill_client.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`

- [ ] Add a test where a `$0.10` quote receives a `$0.21` USDC invoice under a `$0.20` cap; assert a `ValueError` and zero treasury transfers.
- [ ] Run the focused test and confirm it fails because the current code calls the treasury client.
- [ ] Before `transfer_usdc`, reject `payment_amount > max_purchase_usd` with the existing live-cap error wording.
- [ ] Re-run the focused test and confirm it passes.

### Task 3: Verify the micro-quote and regressions

**Files:**
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py` only if the existing quote tests do not already cover ceiling rounding.

- [ ] Add or run a focused quote test proving `$0.10 / $0.01 * 1.05` rounds up to `11 SINGIT`.
- [ ] Run `python3 -m unittest discover -s tests -v` from `sign402-gateway` and require zero failures.
- [ ] Run `node --test tests/buy-bitrefill.test.mjs` from `singit-risk-check` and require zero failures.
- [ ] Restart the live gateway with `SIGN402_BITREFILL_LIVE_MAX_USD=0.20`, search the USD gift card, and request a fresh `$0.10` quote without buying it.
- [ ] Present the exact product, SINGIT maximum, USDC safety cap, recipient, and expiry for explicit user confirmation before `/agent/buy-bitrefill`.

