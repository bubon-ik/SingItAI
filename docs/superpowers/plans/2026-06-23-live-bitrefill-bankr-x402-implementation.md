# Live Bitrefill via Bankr x402 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable real Bitrefill fulfillment only after a successful Bankr x402 SINGIT payment.

**Architecture:** Hermes/Telegram and local callers still use `/agent/search-bitrefill`, `/agent/quote-bitrefill`, and `/agent/buy-bitrefill`. `/agent/buy-bitrefill` asks Firefly for hardware confirmation, then pays the Bankr x402 `buy-bitrefill` endpoint in SINGIT. Bankr calls `/internal/fulfill-bitrefill` with a shared bearer secret; only that internal fulfillment step may call Bitrefill live API.

**Tech Stack:** Python stdlib HTTP client, Bitrefill REST API v2, existing SQLite commerce store, existing Bankr CLI x402 client, unittest.

---

### Task 1: Add live Bitrefill REST client tests

**Files:**
- Modify: `sign402-gateway/tests/test_bitrefill_client.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`

- [ ] Add tests proving live search and product details normalize Bitrefill REST payloads into the existing gateway catalog shape.
- [ ] Add a test proving `buy_product` creates a balance invoice with `auto_pay: true`, retrieves the first order, and returns invoice/order references without logging recipient data.
- [ ] Add a test proving `buy_product` refuses quotes above `SIGN402_BITREFILL_LIVE_MAX_USD`.
- [ ] Replace the factory test that currently rejects live mode with one that returns `LiveBitrefillClient`.

### Task 2: Implement live client

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`

- [ ] Add a tiny injectable JSON transport so unit tests do not hit the network.
- [ ] Add `LiveBitrefillClient` with Bearer auth, base URL defaulting to `https://api.bitrefill.com/v2`, balance payment, and max-USD guard.
- [ ] Normalize products, packages, recipient requirements, invoice status, and order redemption into existing gateway fields.
- [ ] Keep full redemption material inside the commerce store result, but do not include recipient secrets in provider result strings.

### Task 3: Wire env factory

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`

- [ ] `SIGN402_BITREFILL_MODE=test` remains default.
- [ ] `SIGN402_BITREFILL_MODE=live` requires `BITREFILL_API_KEY`.
- [ ] Optional envs: `SIGN402_BITREFILL_BASE_URL`, `SIGN402_BITREFILL_LIVE_MAX_USD`.
- [ ] Live mode returns `LiveBitrefillClient` instead of failing.

### Task 4: Verify

**Commands:**

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_bitrefill_client tests.test_gateway_server -v
python3 -m unittest discover -s tests -v
```

**Expected:** All tests pass. No live network call is made by tests.

### Task 5: Real purchase runbook

**Required before real purchase:**

- `BITREFILL_API_KEY` is set locally.
- Bitrefill account balance is funded.
- `SIGN402_BITREFILL_MODE=live`.
- `SIGN402_BITREFILL_LIVE_MAX_USD` is a small cap, initially `5`.
- Bankr encrypted env has current `SIGN402_GATEWAY_INTERNAL_URL`.
- Gateway and Bankr share `SIGN402_BANKR_FULFILLMENT_SECRET`.
- User confirms product, recipient, Bitrefill price, and max SINGIT before Firefly approval.
