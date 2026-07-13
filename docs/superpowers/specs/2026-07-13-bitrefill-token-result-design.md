# Bitrefill token-aware fulfillment result design

## Problem

Wallet-funded Bitrefill purchases can be quoted in a selected payment token.
Those quotes contain `maxPaymentTokenAtomic` and do not contain the legacy
`maxSingitAtomic` field. `BitrefillFulfillmentRunner._redacted_result` currently
reads `maxSingitAtomic` unconditionally after Bitrefill has already delivered
the order. A successful token-funded purchase therefore raises `KeyError`, is
reported as failed, and is moved to `RECONCILIATION_REQUIRED` even though its
funding transaction and redemption are stored.

## Result contract

`BitrefillFulfillmentRunner` will preserve the existing response for legacy
SINGIT quotes:

- `settleAmountAtomic` equals `maxSingitAtomic`;
- `maxSingitAtomic` remains present.

For token-neutral quotes containing `maxPaymentTokenAtomic`, the response will:

- set `settleAmountAtomic` to `maxPaymentTokenAtomic`;
- include `maxPaymentTokenAtomic`;
- include `paymentTokenSymbol` when it is present in the quote;
- omit `maxSingitAtomic` rather than mislabel another token's amount as SINGIT.

The common response fields `ok`, `quoteId`, `orderId`, and `status` remain
unchanged.

## Data flow and compatibility

The change is limited to the redacted fulfillment response created after the
provider order has been persisted. It does not change quote commitments,
approval hashes, wallet transfers, Bitrefill requests, database state ordering,
or redemption storage. Existing legacy consumers continue receiving the same
SINGIT fields. Token-aware consumers receive an accurately named maximum amount.

## Error handling

If neither maximum field is present, the runner will raise a clear validation
error before constructing a misleading response. Existing fulfillment and
reconciliation behavior remains unchanged for genuine provider or funding
failures.

## Verification

Add a regression test that creates a delivered token-funded quote containing
`maxPaymentTokenAtomic` but no `maxSingitAtomic`, then verifies that fulfillment
returns the token-aware amount fields without raising. Keep the existing SINGIT
fulfillment test to prove backward compatibility, and run the full gateway test
suite.
