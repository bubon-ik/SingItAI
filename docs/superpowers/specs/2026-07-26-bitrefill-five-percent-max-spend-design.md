# Bitrefill Five-Percent Maximum Spend Design

## Goal

Prevent a managed-wallet Bitrefill purchase from moving a user's payment
tokens to the CDP wallet when the current swap cannot produce enough USDC,
without making the platform absorb exchange-rate movement or swap slippage.

The user approves a maximum source-token spend with five percent of headroom.
Immediately before any token transfer, Sign402 recalculates the exact source
amount required at the current guaranteed swap floor. It transfers and swaps
only that exact amount. Any unused headroom remains in the user's wallet.

## Incident and Root Cause

The failed Wolt purchase required `23.9976 USDC`. Its initial swap quote
reported:

- expected output: `24.036302 USDC`;
- guaranteed minimum output: `23.795602 USDC`.

The quote builder sized the SINGIT amount against optimistic `toAmount`.
Execution later enforced `minToAmount >= requiredUsdc`, but only after the
approved SINGIT had moved from the user wallet to CDP. The swap correctly
refused to run, leaving the source tokens on CDP and the order in
`RECONCILIATION_REQUIRED`.

The one-percent Bitrefill service fee was calculated correctly and is not the
cause of the failure.

## User-Visible Pricing Contract

For payment tokens that require a swap:

1. The initial quote contains an estimated source-token spend calculated
   against the provider's guaranteed `minToAmount`.
2. Sign402 derives an approved maximum source-token spend by adding
   `500 bps` (five percent) to the estimate and rounding upward to the token's
   atomic precision.
3. The approval message shows both:
   - `Estimated spend: <amount> <symbol>`;
   - `Maximum spend: <amount> <symbol>`.
4. The approval commitment binds the payment token, its decimals, the
   estimated amount, the maximum atomic amount, the product, recipient, USD
   total, and quote expiry.
5. Five percent is a maximum exchange-rate allowance, not an additional
   service fee. The Bitrefill service fee remains exactly one percent.

For Base USDC, no swap is required. Estimated spend and maximum spend both
equal the exact USD total; no five-percent allowance is added.

The five-percent allowance is configured as
`SIGN402_BITREFILL_MAX_REPRICE_BPS=500`. Startup rejects negative values and
values greater than `500`. Production therefore cannot silently authorize more
than the user-approved policy ceiling.

## Quote-Time Pricing

`RealRateSingitPricer` must use a quote's guaranteed output as the search
criterion:

```text
guaranteed output = minToAmount when present, otherwise toAmount
```

Every high-bound search, binary-search comparison, proportional minimization,
final confirmation, and previous-quantum check uses the guaranteed output.
`toAmount` remains diagnostic expected output only.

`buffer_bps` stays zero in the production pricer. The five-percent approval
headroom is a separate user-visible maximum and must not be folded into the USD
target or represented as a service fee.

If the user's current withdrawable balance cannot cover the initial estimate,
quote creation fails. If it covers the estimate but not the full five-percent
maximum, the approved maximum is capped at the current withdrawable balance.

## Execution-Time Repricing

After the approval provider returns a valid approval and before
`user_funding_runner` is called:

1. Resolve the same committed payment token and current withdrawable balance.
2. Request a fresh real-rate price for the exact committed USD total.
3. Calculate the fresh required source-token amount using `minToAmount`.
4. Require the fresh token address and decimals to match the commitment.
5. Require:

```text
fresh required atomic amount <= approved maximum atomic amount
fresh required atomic amount <= current withdrawable balance
```

6. Persist a sanitized execution-pricing snapshot containing the fresh amount,
   expected USDC, guaranteed minimum USDC, timestamp, token address, decimals,
   and approved maximum.
7. Pass an execution quote containing the fresh exact amount to both the user
   transfer and CDP swap.

The user transfer and CDP swap must consume the same fresh atomic amount.
Neither component may fall back to the initial estimated amount or an
environment-selected token.

For Base USDC, execution verifies the exact committed USDC amount and current
balance without requesting a swap quote.

## Reprice-Required Result

If fresh pricing is unavailable, the guaranteed output is insufficient, the
fresh amount exceeds the approved maximum, the token binding changes, or the
balance is no longer sufficient:

- no user transfer is attempted;
- no CDP swap is attempted;
- no Bitrefill invoice or order is created;
- the spend reservation is released;
- the quote moves to `QUOTE_EXPIRED` so the same approval cannot be retried;
- the response is:

```json
{
  "ok": false,
  "decision": "reprice_required",
  "telegramText": "The exchange rate changed. No funds were moved. Request a new quote and confirm it again."
}
```

Upstream provider details remain redacted from user-facing text and persisted
metadata.

## Narrow Race and Automatic Return

A price can still move between the execution-time reprice and CDP's internal
pre-swap floor check. The CDP service must distinguish failures that provably
happen before any swap transaction is broadcast from failures with an unknown
or post-broadcast stage.

For a proven `pre_swap` failure after the user transfer:

1. Do not call Bitrefill.
2. Return the exact transferred source-token atomic amount from CDP to the
   committed user wallet.
3. Verify the return transaction receipt.
4. Persist only a sanitized return snapshot: transaction hash, network, token,
   atomic amount, CDP source, and user destination.
5. Move the order to a terminal `REFUNDED` state.
6. Tell the user that the exchange rate changed and the tokens were returned.

For an unknown or post-broadcast failure, automatic return is forbidden
because the swap may already have moved funds. The order remains
`RECONCILIATION_REQUIRED` for operator investigation.

If a proven pre-swap return itself fails or has an ambiguous result, the order
also remains `RECONCILIATION_REQUIRED`. Sign402 must never issue a second
automatic return without first reconciling the on-chain result.

## State and Persistence

Add `REFUNDED` as a terminal commerce state ordered after
`RECONCILIATION_REQUIRED`. A refunded record carries:

- the original approval commitment;
- the exact execution-pricing snapshot;
- the user-to-CDP transfer transaction hash;
- the CDP-to-user return transaction hash;
- no plaintext fulfillment token, recipient, wallet secret, provider response,
  or redemption data.

`QUOTE_EXPIRED`, `REFUNDED`, and `RECONCILIATION_REQUIRED` are not purchasable.
A user must request a new quote after any of these terminal outcomes.

## Component Changes

### `sign402_gateway.real_rate_pricing`

- Centralize guaranteed-output selection.
- Size and minimize source amounts using `minToAmount`.
- Preserve `expectedUsdc` from `toAmount` and `minUsdc` from the guaranteed
  output.

### `sign402_gateway.bitrefill_quote`

- Store estimated source amount and approved maximum source amount separately.
- Add the bounded five-percent maximum without changing the one-percent
  service fee.

### `sign402_gateway.bitrefill_runner`

- Bind both estimated and maximum amounts in the approval commitment.
- Reprice after approval and before user funding.
- Transfer and swap only the fresh exact amount.
- Return `reprice_required` without moving funds when the fresh amount cannot
  fit inside the approved maximum.
- Perform a single automatic source-token return only for a proven pre-swap
  failure.

### `sign402_gateway.server`

- Build the execution repricer with the same token resolver and CDP quote
  client used at quote time.
- Parse and bound `SIGN402_BITREFILL_MAX_REPRICE_BPS`.
- Preserve structured CDP failure stages instead of flattening every failure
  into an untyped `ValueError`.
- Expose a CDP source-token return method used only by the guarded refund path.

### `cdp-x402-service`

- Emit a machine-readable `pre_swap` stage for liquidity and floor failures
  that occur before `account.swap`.
- Leave post-call and ambiguous failures unclassified.
- Add a generic CDP ERC-20 transfer command accepting an exact atomic amount,
  token contract, destination, network, and caller-provided idempotency key.
- Return and verify a transaction hash without logging CDP credentials.

## Safety Invariants

- No transfer occurs before execution-time repricing succeeds.
- Actual source-token spend never exceeds the approved maximum atomic amount.
- Actual source-token spend never exceeds the current wallet balance.
- The token contract and decimals cannot change after approval.
- The platform does not subsidize exchange-rate movement or swap slippage.
- The one-percent service fee remains unchanged.
- A failed pre-transfer reprice creates no on-chain transaction.
- Automatic return is allowed only when no swap broadcast is provable.
- An ambiguous transaction result is never retried automatically.
- A stale approval cannot be reused for a new quote.

## Testing

Python regression tests must prove:

- quote sizing rejects `toAmount >= target` when `minToAmount < target`;
- quote sizing selects an amount whose `minToAmount >= target`;
- the five-percent maximum is rounded upward and capped by balance;
- Base USDC has no five-percent allowance;
- approval copy distinguishes estimate from maximum;
- repricing within the maximum transfers only the fresh exact amount;
- repricing above the maximum moves no funds and returns
  `reprice_required`;
- token or decimal drift moves no funds;
- an insufficient current balance moves no funds;
- a proven pre-swap failure returns the exact transferred amount once;
- an unknown-stage failure never triggers automatic return;
- a failed or ambiguous return remains reconciliation-required;
- legacy quotes without the new fields fail closed rather than receiving an
  implicit larger approval.

Node regression tests must prove:

- floor/liquidity failures before `account.swap` report `pre_swap`;
- failures from or after `account.swap` do not claim `pre_swap`;
- the generic CDP token return uses the exact token, destination, atomic amount,
  network, and idempotency key;
- invalid addresses, amounts, or idempotency keys fail before any transaction.

The complete Python gateway and Node CDP service suites must pass before
deployment.

## Deployment and Verification

Deploy the reviewed commit to `x402Bnkr`, fast-forward the production checkout
to that exact commit, and restart only `sign402-gateway`.

Verification must include:

- exact production commit equality;
- all automated suites passing on the deployed tree;
- `sign402-gateway` active with a stable PID;
- local health returning `ok: true`;
- a read-only quote for a non-USDC token showing estimate and five-percent
  maximum;
- a synthetic or mocked reprice-above-maximum test proving zero transfer calls.

Do not perform a live purchase, transfer, swap, or return as a deployment test.

## Out of Scope

- Changing the one-percent Bitrefill service fee.
- Making the platform absorb slippage or exchange-rate losses.
- Reusing an expired approval.
- Exact-output swap integration with another exchange provider.
- Refunding realized USDC surplus after a successful exact-input swap.
- Retrying or modifying the already refunded Wolt order.
