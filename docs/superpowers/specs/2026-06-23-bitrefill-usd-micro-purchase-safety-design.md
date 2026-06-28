# Bitrefill USD Micro-Purchase Safety Design

## Goal

Enable a real $0.10 purchase of `Bitrefill Gift Card (USD)` through the existing Firefly and Bankr x402 flow without treating Bitrefill's satoshi-denominated catalog `price` as USD or allowing an unexpectedly large USDC treasury transfer.

## Scope

- Support the minimum custom value from the USD gift card's `range` metadata.
- Price that custom value from its USD face value, not from Bitrefill's satoshi `price` or `price_rate`.
- Reject any USDC invoice payment above the configured live USD cap before calling the treasury client.
- Keep the live test cap at $0.20.
- Do not change non-USD or fixed-package pricing in this patch. Exact invoice-backed quotes for the universal catalog remain separate work.

## Data Flow

1. Product normalization adds a synthetic package for the range minimum: package ID and value `0.1`, USD price `0.10`.
2. Quote generation produces a maximum of 11 SINGIT at $0.01 per SINGIT with the existing 5% margin and ceiling rounding.
3. Firefly confirms the quote commitment.
4. Bankr settles the SINGIT-priced endpoint.
5. Bitrefill returns a `usdc_base` invoice.
6. The gateway checks the exact invoice payment against the $0.20 live cap before any Bankr USDC transfer.
7. If within the cap, the treasury sends the exact invoice amount and fulfillment continues.

## Failure Handling

- Missing or invalid range metadata does not create a synthetic package.
- Non-USD range products are not assigned a USD face-value price by this patch.
- Invoice payment above the live cap raises an error and the treasury client is not called.
- All existing quote expiry, Firefly approval, fulfillment-token, and order-retrieval protections remain unchanged.

## Verification

- Unit test: the real Bitrefill USD gift-card response shape exposes a `$0.10` package.
- Unit test: a USDC invoice above the configured cap is rejected with zero treasury transfers.
- Unit test: a `$0.10` quote results in 11 SINGIT.
- Full gateway Python test suite and Bankr handler tests remain green.

