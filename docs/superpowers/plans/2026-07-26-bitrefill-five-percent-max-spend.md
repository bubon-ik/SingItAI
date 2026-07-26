# Bitrefill Five-Percent Maximum Spend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reprice a managed-wallet Bitrefill purchase after approval but before funding, debit only the fresh exact source-token amount inside a five-percent approved maximum, and safely return tokens after a proven pre-swap failure.

**Architecture:** Quote-time pricing searches against guaranteed `minToAmount` and records separate estimated and maximum source-token amounts. A dedicated execution repricer resolves the committed token again after approval, produces an exact execution quote bounded by the approved maximum and current balance, and runs before the user transfer. CDP failures carry a structured stage; only a proven `pre_swap` failure can invoke the exact, idempotent source-token return path.

**Tech Stack:** Python 3.14, `decimal.Decimal`, standard-library `unittest`, SQLite, Node.js 22, `node:test`, Coinbase CDP SDK, viem, Base Mainnet, systemd.

## Global Constraints

- Work only on `x402Bnkr`; do not merge or switch to `main`.
- Keep the Bitrefill service fee at exactly `100 bps` (one percent).
- The user-visible reprice allowance defaults to and may never exceed `500 bps`.
- Five percent is an approved maximum, not an amount automatically debited.
- No user transfer may occur before execution repricing succeeds.
- Actual source-token atomic spend must not exceed the committed maximum or current balance.
- Base USDC receives no five-percent allowance and requires no swap quote.
- Automatic return is allowed only when no swap broadcast is provable.
- Never retry an ambiguous swap or return automatically.
- Do not expose wallet private keys, CDP credentials, recipients, fulfillment tokens, or redemption data.
- Deployment verification must not perform a live purchase, transfer, swap, or return.

---

### Task 1: Price Against Guaranteed Swap Output

**Files:**
- Modify: `sign402-gateway/sign402_gateway/real_rate_pricing.py`
- Test: `sign402-gateway/tests/test_real_rate_pricing.py`

**Interfaces:**
- Produces: `_guaranteed_to_amount(quote: dict[str, Any]) -> Decimal`
- Preserves: `price_for_usdc(...) -> dict[str, Any]`
- Guarantees: returned `minUsdc >= bufferedTargetUsdc`

- [ ] **Step 1: Write the failing floor-sizing tests**

Add tests that use the existing linear fake quote client whose
`minToAmount` is `toAmount * 0.99`:

```python
def test_zero_buffer_sizes_against_guaranteed_minimum(self):
    client = LinearQuoteClient(rate="0.000001")
    pricer = RealRateSingitPricer(
        quote_client=client,
        from_token="0xSINGIT",
        buffer_bps=0,
        max_singit="2000000",
    )

    result = pricer.price_for_usdc("1")

    self.assertGreaterEqual(Decimal(result["minUsdc"]), Decimal("1"))
    self.assertGreater(Decimal(result["requiredSingit"]), Decimal("1000000"))


def test_missing_minimum_falls_back_to_expected_output(self):
    client = QuoteClientWithoutMinimum(rate="0.000001")
    pricer = RealRateSingitPricer(
        quote_client=client,
        from_token="0xSINGIT",
        buffer_bps=0,
        max_singit="1000000",
    )

    result = pricer.price_for_usdc("1")

    self.assertEqual(result["expectedUsdc"], "1")
    self.assertEqual(result["minUsdc"], "1")
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_real_rate_pricing.RealRateSingitPricerTests.test_zero_buffer_sizes_against_guaranteed_minimum \
  tests.test_real_rate_pricing.RealRateSingitPricerTests.test_missing_minimum_falls_back_to_expected_output -v
```

Expected: the first test fails because current search accepts
`toAmount >= target` while `minToAmount < target`.

- [ ] **Step 3: Implement guaranteed-output selection**

Add:

```python
def _guaranteed_to_amount(quote: dict[str, Any]) -> Decimal:
    value = quote.get("minToAmount")
    if value is None or str(value).strip() == "":
        value = quote["toAmount"]
    return _finite_decimal(value, field="guaranteed swap output")
```

Replace every search/minimization/final comparison against
`Decimal(quote["toAmount"])` with `_guaranteed_to_amount(quote)`.
Keep `_next_high_amount` proportional estimation based on guaranteed output as
well. Return:

```python
"expectedUsdc": final_quote["toAmount"],
"minUsdc": format(_guaranteed_to_amount(final_quote), "f"),
```

- [ ] **Step 4: Run focused and module tests**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest tests.test_real_rate_pricing -v
```

Expected: all real-rate pricing tests pass.

- [ ] **Step 5: Commit**

```bash
git add \
  sign402-gateway/sign402_gateway/real_rate_pricing.py \
  sign402-gateway/tests/test_real_rate_pricing.py
git commit -m "fix: price Bitrefill swaps by guaranteed output"
```

---

### Task 2: Bind Estimate and Five-Percent Maximum in the Quote

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_quote.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_bitrefill_quote.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`

**Interfaces:**
- Produces: `_approved_maximum_atomic(estimated_atomic, balance_atomic, bps) -> int`
- Extends: `build_real_rate_quote(..., max_reprice_bps: int = 500)`
- Adds quote fields: `estimatedPaymentTokenAmount`, `estimatedPaymentTokenAtomic`, `maxPaymentTokenAmount`, `maxPaymentTokenAtomic`, `maxRepriceBps`

- [ ] **Step 1: Write failing maximum and USDC tests**

Add quote tests:

```python
def test_real_rate_quote_adds_five_percent_approved_maximum(self):
    quote = build_real_rate_quote(
        request={
            "productId": "test-gift-card",
            "packageId": "1",
            "country": "US",
        },
        product={
            "productId": "test-gift-card",
            "name": "Test Gift Card",
            "productType": "gift_card",
            "packageId": "1",
            "packageValue": "1.00",
            "priceUsd": "1.00",
            "country": "US",
            "currency": "USD",
        },
        pricing={
            "pricingMode": "bankr_real_rate",
            "targetUsdc": "1.01",
            "bufferedTargetUsdc": "1.01",
            "requiredAmount": "100",
            "requiredAmountAtomic": "100000000000000000000",
            "expectedUsdc": "1.02",
            "minUsdc": "1.01",
        },
        payment_token={
            "address": "0xc2c1e0b7C401e6217193732272444D928646eba3",
            "symbol": "SINGIT",
            "decimals": 18,
            "balance": "1000",
            "native": False,
        },
        max_reprice_bps=500,
        quote_id="quote_1",
        now_epoch=1_719_000_000,
    )

    self.assertEqual(quote["estimatedPaymentTokenAmount"], "100")
    self.assertEqual(quote["estimatedPaymentTokenAtomic"], "100000000000000000000")
    self.assertEqual(quote["maxPaymentTokenAmount"], "105")
    self.assertEqual(quote["maxPaymentTokenAtomic"], "105000000000000000000")
    self.assertEqual(quote["maxRepriceBps"], 500)
```

Add cases proving upward atomic rounding, maximum capped to wallet balance, and
Base USDC maximum equal to estimate.

- [ ] **Step 2: Verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_quote \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_direct_usdc_quote_has_no_reprice_allowance -v
```

Expected: failures because the new quote fields and constructor option do not
exist.

- [ ] **Step 3: Implement bounded maximum calculation**

Add:

```python
MAX_REPRICE_BPS = 500


def _approved_maximum_atomic(
    estimated_atomic: int,
    *,
    balance_atomic: int,
    bps: int,
) -> int:
    if not 0 <= int(bps) <= MAX_REPRICE_BPS:
        raise ValueError("Bitrefill max reprice bps must be from 0 to 500")
    increased = (
        estimated_atomic * (10_000 + int(bps)) + 9_999
    ) // 10_000
    return min(increased, balance_atomic)
```

For swapped payment tokens, derive `balance_atomic` from the resolved balance
and decimals and add the five fields. Preserve `paymentTokenAmount` as the
estimated amount for compatibility, but make approval copy use
`maxPaymentTokenAmount`.

For Base USDC, set maximum equal to estimate and `maxRepriceBps` to `0`.

- [ ] **Step 4: Bind estimate and maximum in the commitment and copy**

Extend `build_purchase_commitment`:

```python
"estimatedPaymentTokenAtomic": str(quote["estimatedPaymentTokenAtomic"]),
"maxPaymentTokenAtomic": str(quote["maxPaymentTokenAtomic"]),
"maxRepriceBps": int(quote["maxRepriceBps"]),
```

Change approval lines to:

```python
f"Estimated spend: {_format_amount(quote['estimatedPaymentTokenAmount'])} {payment_symbol}",
f"Maximum spend: {_format_amount(quote['maxPaymentTokenAmount'])} {payment_symbol}",
```

Legacy managed-wallet quotes without the new fields must raise
`ValueError("quote does not contain a bounded payment-token maximum")`.

- [ ] **Step 5: Run quote and runner modules**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_quote \
  tests.test_bitrefill_runner -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add \
  sign402-gateway/sign402_gateway/bitrefill_quote.py \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/tests/test_bitrefill_quote.py \
  sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "feat: bind bounded Bitrefill maximum spend"
```

---

### Task 3: Reprice After Approval and Before User Funding

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`

**Interfaces:**
- Produces: `WalletBitrefillExecutionPricer.__call__(telegram_user_id: str, quote: dict) -> dict`
- Extends: `WalletBitrefillPurchaseRunner(..., execution_pricer=...)`
- Adds execution fields: `actualPaymentTokenAmount`, `actualPaymentTokenAtomic`, plus sanitized `executionPricing`

- [ ] **Step 1: Write the failing no-transfer tests**

Add tests for:

```python
def test_wallet_reprice_above_approved_maximum_moves_no_funds(self):
    execution_pricer = Mock(side_effect=RepriceRequiredError())
    user_funding = Mock()
    fulfillment = Mock()
    runner = make_approved_wallet_runner(
        execution_pricer=execution_pricer,
        user_funding_runner=user_funding,
        fulfillment_runner=fulfillment,
    )

    result = runner.buy({"quoteId": "quote_1", "telegramUserId": "u1"})

    self.assertFalse(result["ok"])
    self.assertEqual(result["decision"], "reprice_required")
    self.assertIn("No funds were moved", result["telegramText"])
    user_funding.assert_not_called()
    fulfillment.assert_not_called()
    self.assertEqual(store.get_quote("quote_1")["state"], "QUOTE_EXPIRED")
```

Add separate tests for token drift, decimal drift, insufficient current
balance, unavailable fresh quote, and a fresh amount inside the maximum. The
success case must assert both funding runners receive the fresh exact amount,
not the estimate or maximum.

- [ ] **Step 2: Verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_wallet_reprice_above_approved_maximum_moves_no_funds \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_wallet_reprice_inside_maximum_debits_only_fresh_amount -v
```

Expected: errors because execution repricing is not wired.

- [ ] **Step 3: Implement the execution repricer**

Add:

```python
class RepriceRequiredError(ValueError):
    pass


class WalletBitrefillExecutionPricer:
    def __init__(
        self,
        *,
        real_rate_pricer,
        payment_token_resolver,
        now_provider=now_epoch,
    ):
        self.real_rate_pricer = real_rate_pricer
        self.payment_token_resolver = payment_token_resolver
        self.now_provider = now_provider

    def __call__(self, telegram_user_id: str, quote: dict[str, Any]) -> dict[str, Any]:
        token = self.payment_token_resolver.resolve(
            telegram_user_id,
            {"address": quote["paymentTokenAddress"]},
        )
        if token["address"].casefold() != str(quote["paymentTokenAddress"]).casefold():
            raise RepriceRequiredError("payment token changed")
        decimals = int(quote["paymentTokenDecimals"])
        if int(token["decimals"]) != decimals:
            raise RepriceRequiredError("payment token decimals changed")
        maximum_atomic = int(quote["maxPaymentTokenAtomic"])
        balance_atomic = int(
            Decimal(token["balance"]) * (Decimal(10) ** decimals)
        )
        pricing_address = (
            COINBASE_NATIVE_TOKEN_ADDRESS
            if token["native"]
            else token["address"]
        )
        try:
            if token["address"].casefold() == BASE_USDC_MAINNET.casefold():
                pricing = _price_direct_usdc(
                    quote["totalUsd"],
                    decimals=decimals,
                    balance=token["balance"],
                )
            else:
                allowed_atomic = min(maximum_atomic, balance_atomic)
                pricing = self.real_rate_pricer.price_for_usdc(
                    quote["totalUsd"],
                    from_token=pricing_address,
                    decimals=decimals,
                    max_amount=format_decimal(
                        Decimal(allowed_atomic) / (Decimal(10) ** decimals)
                    ),
                )
        except (ArithmeticError, TypeError, ValueError):
            raise RepriceRequiredError("fresh pricing is unavailable") from None
        actual_atomic = int(pricing["requiredAmountAtomic"])
        if actual_atomic > maximum_atomic or actual_atomic > balance_atomic:
            raise RepriceRequiredError("fresh price exceeds approved maximum")
        execution_quote = deepcopy(quote)
        execution_quote["actualPaymentTokenAmount"] = str(pricing["requiredAmount"])
        execution_quote["actualPaymentTokenAtomic"] = str(actual_atomic)
        execution_quote["executionPricing"] = {
            "paymentTokenAddress": token["address"],
            "paymentTokenDecimals": decimals,
            "actualPaymentTokenAmount": str(pricing["requiredAmount"]),
            "actualPaymentTokenAtomic": str(actual_atomic),
            "expectedUsdc": str(pricing["expectedUsdc"]),
            "minUsdc": str(pricing["minUsdc"]),
            "approvedMaximumAtomic": str(maximum_atomic),
            "pricedAtEpoch": int(self.now_provider()),
        }
        return execution_quote
```

Use `_price_direct_usdc` for Base USDC. For other tokens call
`real_rate_pricer.price_for_usdc` with the committed address, decimals, and:

```python
max_amount = min(
    Decimal(token["balance"]),
    Decimal(quote["maxPaymentTokenAtomic"])
    / (Decimal(10) ** int(quote["paymentTokenDecimals"])),
)
```

Catch pricing/binding/cap failures and raise `RepriceRequiredError` without
including provider details.

- [ ] **Step 4: Invoke repricing after approval**

In `WalletBitrefillPurchaseRunner`, call execution repricing immediately after
hash approval validation and before `user_funding_runner`. On
`RepriceRequiredError`, advance to `QUOTE_EXPIRED`, release the reservation,
and return the exact `reprice_required` response from the spec.

Persist:

```python
execution_pricing = execution_quote["executionPricing"]
self.store.advance_state(
    quote_id,
    "USER_APPROVED",
    {
        "paymentHash": payment_hash,
        "paymentCommitment": commitment,
        "executionPricing": execution_pricing,
        "walletCheckout": wallet_checkout,
        "fulfillmentTokenHash": token_hash,
        "recipient": recipient,
    },
)
```

Pass the execution quote to `user_funding_runner`. Persist
`executionPricing` before invoking fulfillment. Extend
`BitrefillFulfillmentRunner` so it reconstructs an effective execution quote
from the committed quote plus the persisted, bounded actual fields; funding
runners must prefer `actualPaymentTokenAmount`/`actualPaymentTokenAtomic`.
Reject missing, malformed, token-mismatched, or above-maximum execution
metadata before requesting a CDP swap. This keeps the fulfillment-token API
small while guaranteeing the transfer and swap consume the same fresh amount.

- [ ] **Step 5: Wire production dependencies**

Construct one `WalletPaymentTokenResolver` and reuse it for quote-time and
execution-time pricing. Add `max_reprice_bps` parsing:

```python
max_reprice_bps = _bounded_int_env(
    "SIGN402_BITREFILL_MAX_REPRICE_BPS",
    default=500,
    minimum=0,
    maximum=500,
)
```

Pass the value into `BitrefillQuoteService` and inject
`WalletBitrefillExecutionPricer` into `WalletBitrefillPurchaseRunner`.

- [ ] **Step 6: Run runner and server suites**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_runner \
  tests.test_gateway_server -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/tests/test_bitrefill_runner.py \
  sign402-gateway/tests/test_gateway_server.py
git commit -m "feat: reprice Bitrefill before wallet debit"
```

---

### Task 4: Preserve CDP Pre-Swap Stage and Add Idempotent Token Return

**Files:**
- Create: `cdp-x402-service/src/staged-swap.mjs`
- Create: `cdp-x402-service/src/token-return.mjs`
- Modify: `cdp-x402-service/src/index.mjs`
- Create: `cdp-x402-service/test/staged-swap.test.mjs`
- Create: `cdp-x402-service/test/token-return.test.mjs`

**Interfaces:**
- Produces: `StagedCdpError(stage: string, cause: Error)`
- Produces: `executeStagedSwap({ getPrice, assertFloor, swap, minUsdc })`
- Produces: `returnErc20({ account, publicClient, token, to, amountAtomic, network, idempotencyKey })`
- CLI: `transfer-token --token --to --amount-atomic --chain --idempotency-key`

- [ ] **Step 1: Write staged-swap tests**

Add:

```javascript
test("floor failure is classified pre_swap and never calls swap", async () => {
  let swapCalls = 0;
  await assert.rejects(
    executeStagedSwap({
      minUsdc: "23.9976",
      getPrice: async () => ({ liquidityAvailable: true, minToAmount: 23795602n }),
      assertFloor: () => { throw new Error("below floor"); },
      swap: async () => { swapCalls += 1; },
    }),
    (error) => error.stage === "pre_swap",
  );
  assert.equal(swapCalls, 0);
});

test("swap call failure has no safe pre_swap stage", async () => {
  await assert.rejects(
    executeStagedSwap({
      minUsdc: "1",
      getPrice: async () => ({ liquidityAvailable: true, minToAmount: 1000000n }),
      assertFloor: () => {},
      swap: async () => { throw new Error("ambiguous"); },
    }),
    (error) => error.stage === "",
  );
});
```

- [ ] **Step 2: Write token-return validation tests**

Test exact token, destination, atomic amount, network, and idempotency key
passed to `account.sendTransaction`, followed by
`publicClient.waitForTransactionReceipt({hash})`. Test a reverted receipt,
invalid address, zero/negative amount, and an empty or overlong idempotency key
fail without reporting success.

- [ ] **Step 3: Verify Node tests RED**

Run:

```bash
cd cdp-x402-service
npm test -- --test-name-pattern="pre_swap|token return"
```

Expected: module-not-found failures.

- [ ] **Step 4: Implement staged swap**

Implement:

```javascript
export class StagedCdpError extends Error {
  constructor(message, { stage = "", cause } = {}) {
    super(message, { cause });
    this.name = "StagedCdpError";
    this.stage = stage;
  }
}

export async function executeStagedSwap({ getPrice, assertFloor, swap, minUsdc }) {
  if (minUsdc) {
    try {
      const price = await getPrice();
      assertFloor(price, minUsdc);
    } catch (cause) {
      throw new StagedCdpError("CDP pre-swap validation failed", {
        stage: "pre_swap",
        cause,
      });
    }
  }
  try {
    return await swap();
  } catch (cause) {
    throw new StagedCdpError("CDP swap result is ambiguous", { cause });
  }
}
```

At the CLI boundary, emit one safe JSON error object on stderr:

```json
{"ok":false,"error":"CDP wallet service failed","stage":"pre_swap"}
```

Do not emit the upstream stack or credentials.

- [ ] **Step 5: Implement exact idempotent token return**

Validate inputs with viem `isAddress` and a decimal-digit atomic amount. Encode
`erc20.transfer(to, amount)` and call:

```javascript
const result = await account.sendTransaction({
  network,
  transaction: {
    to: token,
    data: encodeFunctionData({
      abi: erc20Abi,
      functionName: "transfer",
      args: [to, amount],
    }),
  },
  idempotencyKey,
});
const receipt = await publicClient.waitForTransactionReceipt({
  hash: result.transactionHash,
});
if (receipt.status !== "success") {
  throw new Error("CDP token return transaction reverted");
}
```

Return only safe fields including `transactionHash` after the successful
receipt. The CLI must not print success before receipt verification.

- [ ] **Step 6: Run the complete Node suite**

Run:

```bash
cd cdp-x402-service
npm test
```

Expected: all Node tests pass.

- [ ] **Step 7: Commit**

```bash
git add \
  cdp-x402-service/src/index.mjs \
  cdp-x402-service/src/staged-swap.mjs \
  cdp-x402-service/src/token-return.mjs \
  cdp-x402-service/test/staged-swap.test.mjs \
  cdp-x402-service/test/token-return.test.mjs
git commit -m "feat: classify and return failed CDP funding"
```

---

### Task 5: Automatically Return Only Proven Pre-Swap Transfers

**Files:**
- Modify: `sign402-gateway/sign402_gateway/commerce_store.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_commerce_store.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`

**Interfaces:**
- Adds state: `REFUNDED`
- Produces: `CdpWalletServiceError(message: str, stage: str = "")`
- Produces: `CdpWalletClient.return_token(...) -> dict`
- Extends: `WalletBitrefillPurchaseRunner(..., return_runner=...)`

- [ ] **Step 1: Write failing stage parsing and return tests**

Test that `CdpWalletClient` parses safe JSON stderr and exposes
`stage == "pre_swap"`. Plain text, malformed JSON, process timeout, and an
error after `account.swap` must produce `stage == ""`.

Test `return_token` builds:

```text
transfer-token
--token <committed token>
--to <committed user wallet>
--amount-atomic <exact transferred amount>
--chain base
--idempotency-key bitrefill-return:<quoteId>
```

- [ ] **Step 2: Write failing purchase-runner return tests**

Add:

```python
def test_proven_pre_swap_failure_returns_exact_transfer_once(self):
    fulfillment = Mock(side_effect=CdpWalletServiceError("failed", stage="pre_swap"))
    return_runner = Mock(return_value={"ok": True, "txId": "0xRETURN"})
    runner = make_approved_wallet_runner(
        fulfillment_runner=fulfillment,
        return_runner=return_runner,
    )

    result = runner.buy({"quoteId": "quote_1", "telegramUserId": "u1"})

    self.assertFalse(result["ok"])
    self.assertEqual(result["decision"], "refunded_after_rate_change")
    return_runner.assert_called_once_with(
        quote_id="quote_1",
        token_address="0xc2c1e0b7C401e6217193732272444D928646eba3",
        to_address="0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C",
        amount_atomic="101000000000000000000",
        chain="base",
    )
    self.assertEqual(store.get_quote("quote_1")["state"], "REFUNDED")
```

Add unknown-stage and ambiguous-return tests proving no automatic retry and
`RECONCILIATION_REQUIRED`.

- [ ] **Step 3: Verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_cdp_wallet_client_preserves_pre_swap_stage \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_proven_pre_swap_failure_returns_exact_transfer_once \
  tests.test_commerce_store.BitrefillCommerceStoreTests.test_refunded_is_terminal -v
```

Expected: failures because the error type, return runner, and state do not
exist.

- [ ] **Step 4: Implement structured CDP errors and return client**

Parse only the safe JSON envelope:

```python
class CdpWalletServiceError(ValueError):
    def __init__(self, message: str, *, stage: str = ""):
        super().__init__(message)
        self.stage = stage if stage == "pre_swap" else ""
```

Add `return_token` using exact atomic amount and deterministic idempotency key.
Never retry inside the client.

In `BitrefillFulfillmentRunner`, catch `CdpWalletServiceError` separately,
persist the redacted funding failure, and re-raise the same typed exception so
the wallet purchase runner can make the stage-aware return decision. Continue
flattening all other provider exceptions.

- [ ] **Step 5: Add terminal refunded state and sanitized snapshot**

Add `"REFUNDED": 904` after reconciliation in `STATE_ORDER`; move
`REFUND_REQUIRED` to `905`. Add a sanitizer that accepts only:

```python
{
    "transactionHash",
    "network",
    "token",
    "amountAtomic",
    "from",
    "to",
}
```

Persist it under `tokenReturn`.

- [ ] **Step 6: Implement guarded automatic return**

Only catch `CdpWalletServiceError` with `stage == "pre_swap"` after a persisted
user transfer. Call the return runner once. On confirmed success, set
`REFUNDED`; on any return exception, preserve reconciliation and a redacted
`returnError`.

Unknown-stage failures follow the existing reconciliation path and must not
call the return runner.

- [ ] **Step 7: Run full affected Python modules**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_commerce_store \
  tests.test_bitrefill_quote \
  tests.test_bitrefill_runner \
  tests.test_gateway_server \
  tests.test_real_rate_pricing -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add \
  sign402-gateway/sign402_gateway/commerce_store.py \
  sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/tests/test_commerce_store.py \
  sign402-gateway/tests/test_gateway_server.py \
  sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "feat: safely return failed Bitrefill funding"
```

---

### Task 6: Full Verification, Push, and Production Deployment

**Files:**
- Verify: all changed Python, Node, test, spec, and plan files
- Deploy: `/home/hermes/apps/sign402`
- Restart: `sign402-gateway.service` only

**Interfaces:**
- Consumes: reviewed `x402Bnkr` commit
- Produces: exact deployed commit with healthy gateway

- [ ] **Step 1: Run all local suites**

Run:

```bash
cd cdp-x402-service
npm test

cd ../sign402-gateway
../payment-executor/.venv/bin/python -m unittest discover -s tests -v
```

Expected: every Node and Python test passes with zero failures.

- [ ] **Step 2: Inspect scope and secrets**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -n 12
rg -n \
  'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|CDP_API_KEY_SECRET=|SIGN402_WALLET_MASTER_KEY=|fulfillmentToken"[[:space:]]*:' \
  cdp-x402-service/src \
  sign402-gateway/sign402_gateway \
  docs/superpowers/specs/2026-07-26-bitrefill-five-percent-max-spend-design.md \
  docs/superpowers/plans/2026-07-26-bitrefill-five-percent-max-spend.md
```

Expected: clean diff check, only intended files changed, and no secrets.

- [ ] **Step 3: Verify regression mutation**

Temporarily replace guaranteed-output selection with `toAmount`, run
`test_zero_buffer_sizes_against_guaranteed_minimum`, and confirm it fails.
Restore the implementation and rerun the test to confirm it passes. Do not
commit the mutation.

- [ ] **Step 4: Push exact branch**

Run:

```bash
reviewed_commit=$(git rev-parse HEAD)
git push singitai x402Bnkr
test "$(git ls-remote singitai refs/heads/x402Bnkr | awk 'NR == 1 {print $1}')" = "$reviewed_commit"
```

Expected: remote `x402Bnkr` equals the reviewed local commit.

- [ ] **Step 5: Fast-forward and test the exact production tree**

Run:

```bash
ssh hermes@164.68.104.44 sh -s -- "$reviewed_commit" <<'REMOTE'
set -eu
reviewed_commit=$1
cd /home/hermes/apps/sign402
test -z "$(git status --porcelain)"
git fetch origin x402Bnkr
test "$(git rev-parse FETCH_HEAD)" = "$reviewed_commit"
git merge --ff-only "$reviewed_commit"
test "$(git rev-parse HEAD)" = "$reviewed_commit"
cd cdp-x402-service
npm test
cd ../sign402-gateway
.venv/bin/python -m unittest discover -s tests -q
REMOTE
```

Expected: exact fast-forward and all server-side tests pass.

- [ ] **Step 6: Restart gateway and verify stability**

Use `sudo systemctl restart sign402-gateway` when non-interactive sudo is
available. Otherwise, after verifying `User=hermes`, `Restart=always`, and
`KillSignal=15`, send one `SIGTERM` to the current PID and let systemd restart
it.

Verify:

```bash
systemctl show sign402-gateway \
  -p ActiveState -p SubState -p MainPID -p NRestarts
curl -fsS http://127.0.0.1:8099/health |
python3 -c 'import json,sys; assert json.load(sys.stdin)["ok"]; print("health ok")'
```

Wait five seconds and require the PID to remain unchanged and health to pass
again. Do not restart `hermes-gateway`.

- [ ] **Step 7: Perform read-only production checks**

Request a non-purchasing quote through the internal quote path and verify it
contains distinct estimate and maximum fields with a maximum no greater than
five percent above estimate. Run the synthetic reprice-above-maximum unit test
on the deployed tree and confirm zero transfer calls.

Do not approve, transfer, swap, create a Bitrefill invoice, or buy a product.
