# Bitrefill Invoice-First Funding Design

**Date:** 2026-07-26

## Goal

Prevent a Bitrefill provider rejection from moving user funds, prevent a retry
from charging the user twice, and retain enough safe diagnostics to identify a
provider failure without writing bearer-value data to logs or SQLite.

## Scope

This change covers managed-wallet Bitrefill purchases paid through the CDP
wallet on Base. It preserves the existing 1% service fee, the approved maximum
token spend, post-approval token repricing, and the guarded pre-swap token
return.

The USDC from the 2026-07-26 failed Wolt attempt is not reusable credit. It was
returned separately to the user-selected Base address and the historical quote
was marked `REFUNDED`.

## Invariants

1. Bitrefill must create an unpaid invoice before the user's wallet transfers
   any token.
2. No invoice ID means no user transfer and no CDP swap.
3. The invoice product, denomination, payment method, network, asset, and amount
   must match the approved quote before funding starts.
4. The Bitrefill invoice amount must not exceed the quote's approved USDC total.
5. A provider rejection during invoice creation is a no-funds-moved failure,
   not reconciliation.
6. A confirmed user transfer, swap, or USDC payment is checkpointed before the
   next external step.
7. Retrying or reconciling an existing invoice must never issue the same
   on-chain payment twice.
8. Payment links, redemption codes, eSIM activation data, private keys, API
   keys, wallet secrets, raw provider bodies, and crypto payment addresses must
   not be written to logs.
9. User-facing errors and persisted failure fields remain generic.

## Architecture

### Split provider preparation from payment

The Bitrefill client gains two explicit operations:

- `prepare_purchase(...)` creates the unpaid invoice with
  `package_value`, validates the returned invoice ID, and emits a safe
  checkpoint containing only the invoice ID, status, order IDs, payment amount,
  asset, and network.
- `complete_purchase(...)` reloads the prepared invoice by ID, revalidates its
  payment requirements, pays it through the supplied treasury operation, and
  polls the same invoice through delivery.

The raw payment address exists only in memory inside `complete_purchase`.
Payment links and raw provider responses are neither persisted nor logged.

The test Bitrefill client implements the same interface without real network or
wallet side effects.

### Orchestration order

For a managed-wallet purchase after approval and execution repricing:

1. Persist `USER_APPROVED` with the approved commitment and execution pricing.
2. Call provider preparation and persist `INVOICE_CREATED`.
3. Validate the prepared invoice against the approved quote.
4. Transfer the exact repriced payment-token amount from the user wallet to CDP
   and checkpoint the transfer transaction.
5. Swap on CDP when the selected token is not Base USDC and checkpoint the swap.
6. Pay the already-created invoice with exact Base USDC using an idempotency key
   derived from the invoice ID and checkpoint the confirmed transaction.
7. Poll that same invoice until delivered or a terminal provider state.

The existing outer wallet runner must call preparation before
`user_funding_runner`. The fulfillment runner must accept only a prepared
invoice belonging to the same quote.

### State model

Add `INVOICE_CREATED` between `USER_APPROVED` and the first funding state.
Its safe metadata contains:

- `invoiceId`
- normalized invoice status
- product ID and package value copied from the committed quote
- payment amount
- payment asset (`USDC`)
- payment network (`base`)

No address or payment link is stored in this checkpoint.

If preparation fails, transition to `FULFILLMENT_FAILED` and return a generic
provider error. Because no funds moved, do not use
`RECONCILIATION_REQUIRED`.

Failures after a confirmed user transfer remain
`RECONCILIATION_REQUIRED` unless the existing proven `pre_swap` return path can
return the exact original ERC-20 amount.

### Idempotent USDC payment

Direct Bitrefill USDC payment uses the exact atomic invoice amount and a stable
idempotency key:

`bitrefill-pay:<invoiceId>`

The transfer must wait for a successful Base receipt before being recorded as
paid. Reconciliation first checks the stored payment transaction and current
invoice status. It must not broadcast another transfer when a transaction hash
already exists or when the invoice reports payment detected, confirmed,
pending, or complete.

### Existing CDP balance

CDP wallet balance is treasury state, not anonymous user credit. A new purchase
does not consume an unexplained historical balance as a substitute for its own
funding checkpoint. Historical reconciliation is handled explicitly per quote.

The service fee portion remaining after the invoice payment stays treasury
revenue under the existing 1% fee behavior.

## Safe diagnostics

Raw provider error content is removed from logging.

Provider diagnostics use an allowlist:

- normalized `error_code` or `code`
- bounded human-readable `message` after bearer-value filtering
- HTTP or provider status
- provider request/trace ID
- local quote ID, product ID, and package value

If the body cannot be safely parsed, log only the exception type, body byte
length, and SHA-256 fingerprint. Do not log the body.

The message filter rejects or replaces:

- URLs and payment links
- EVM addresses and transaction calldata
- redemption/code/PIN fields
- eSIM activation values and QR payloads
- secret-looking key/value pairs
- exact values of secret environment variables

Context fields pass through the same sanitizer as error messages.

## Error handling

- Invoice creation rejected: `FULFILLMENT_FAILED`, no funds moved.
- Invoice validation mismatch: `FULFILLMENT_FAILED`, no funds moved.
- User transfer rejected before broadcast: generic funding failure; no provider
  payment.
- Proven pre-swap failure after ERC-20 transfer: return the exact original token
  using the existing idempotent return flow.
- Ambiguous or post-swap failure: `RECONCILIATION_REQUIRED`; never guess and
  never automatically rebroadcast.
- USDC payment reverted: `RECONCILIATION_REQUIRED`.
- Polling timeout after confirmed USDC payment: retain invoice ID and payment
  transaction, then reconcile by reading invoice status only.

## Testing

The implementation follows red-green TDD and includes:

1. A regression test proving provider invoice creation occurs before
   `user_funding_runner`.
2. A regression test proving provider rejection moves no user funds and performs
   no swap.
3. Invoice validation tests for wrong product, denomination, asset, network,
   amount, and missing invoice ID.
4. A test proving `package_value` is sent and deprecated `package_id` is absent.
5. Idempotency tests proving one invoice can broadcast at most one USDC payment.
6. Reconciliation tests proving a stored payment transaction is polled rather
   than paid again.
7. Failure-stage tests preserving the current exact-token pre-swap return.
8. Logging tests proving useful error codes survive while payment links,
   addresses, redemption values, eSIM data, and environment secrets do not.
9. Full Python and Node suites.

Production verification is read-only: exact deployed commit, process restart,
health checks, and synthetic tests. No live Bitrefill purchase is performed as
part of deployment verification.

## Acceptance criteria

- A Bitrefill invoice-creation rejection cannot transfer or swap user funds.
- A retry cannot pay the same invoice twice.
- A polling timeout after payment can be reconciled without another transfer.
- The 1% service fee and 5% token-spend ceiling remain unchanged.
- No bearer-value provider data appears in responses, SQLite failure metadata,
  or logs.
- All local and server test suites pass.
