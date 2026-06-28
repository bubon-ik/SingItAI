# Buffered Treasury Bitrefill USDC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users pay only SINGIT through Bankr x402 while the service fulfills real Bitrefill orders by paying `usdc_base` invoices from the Bankr treasury wallet.

**Architecture:** `/agent/buy-bitrefill` still performs Firefly confirmation and pays the SINGIT Bankr x402 endpoint. The protected `/internal/fulfill-bitrefill` then creates a Bitrefill `usdc_base` invoice, sends exact USDC from the Bankr wallet to the invoice address, polls Bitrefill, and returns a redacted success result. SINGIT-to-USDC rebalancing is a separate later operation, not in the user-facing purchase path.

**Tech Stack:** Python stdlib, Bitrefill REST API v2, Bankr CLI `wallet transfer`, existing SQLite store, existing unittest suite.

---

### Task 1: Treasury client tests

**Files:**
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`

- [ ] Add a test that `BankrTreasuryClient.transfer_usdc` invokes `bankr --ni wallet transfer --to <address> --amount <amount> --token USDC --chain base`.
- [ ] Add a test that a failed transfer raises an error containing stdout/stderr.

### Task 2: Bitrefill `usdc_base` fulfillment tests

**Files:**
- Modify: `sign402-gateway/tests/test_bitrefill_client.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`

- [ ] Add a test that live Bitrefill creates an invoice with `payment_method: "usdc_base"` and `refund_address`, calls treasury transfer with exact `payment.address` and `payment.price`, polls `/invoices/{id}`, then fetches `/orders/{id}`.
- [ ] Add a test that missing invoice payment address is rejected before treasury transfer.
- [ ] Keep balance mode tests passing for fallback/local comparison.

### Task 3: Env wiring

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`

- [ ] `SIGN402_BITREFILL_PAYMENT_METHOD=balance` keeps current behavior.
- [ ] `SIGN402_BITREFILL_PAYMENT_METHOD=usdc_base` requires `SIGN402_TREASURY_REFUND_ADDRESS`.
- [ ] In live mode, inject `BankrTreasuryClient` into `LiveBitrefillClient`.

### Task 4: Verification

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest discover -s tests -v
cd "/Users/mp/Documents/Berlin Hack/singit-risk-check"
node --test tests/buy-bitrefill.test.mjs
```

Expected: all tests pass. No real USDC transfer is executed in tests.
