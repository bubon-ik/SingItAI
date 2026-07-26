# One Percent Bitrefill Service Fee Design

## Goal

Reduce the single transparent Bitrefill service fee from 2% to 1% for every
new Bitrefill quote, regardless of the selected payment token or pricing route.

## Scope

- Set the canonical Bitrefill service fee to 100 basis points.
- Continue calculating the fee without a minimum:
  `serviceFeeUsd = priceUsd * 100 / 10,000`.
- Continue calculating `totalUsd = priceUsd + serviceFeeUsd`.
- Apply the same total to direct USDC, SINGIT, and arbitrary-token quotes.
- Show the effective 1% fee in approval and operator-facing text.
- Preserve existing quote fields and purchase-commitment binding:
  `serviceFeeBps`, `serviceFeeUsd`, and `totalUsd`.

This change does not alter swap pricing, `buffer_bps`, wallet funding,
fulfillment, spending-limit values, product caps, or existing persisted quotes.

## Implementation

`SERVICE_FEE_BPS` remains the single calculation source and changes from 200 to
100. Fee display text derives its percentage from each quote's
`serviceFeeBps`, preventing the label from drifting from the committed amount.
Operator copy that describes the product cap before the fee is updated to 1%.

## Behavior

For a product priced at `1.00 USD`, a new quote contains:

```text
serviceFeeBps = 100
serviceFeeUsd = 0.01
totalUsd = 1.01
```

Approval, spend enforcement, and recorded spend continue using `totalUsd`.
Micro-price purchases retain exact decimal calculation and no minimum fee.

## Compatibility

Previously persisted quotes remain readable and retain their committed 2% fee.
No stored quote is migrated or recalculated. New quotes use 1%.

## Testing

- Change fee tests first and verify they fail against the 2% implementation.
- Cover the canonical basis-point value and micro-price calculation.
- Cover fixed-price, real-rate token, and direct-USDC quote totals.
- Cover approval and operator-facing fee labels.
- Run the complete Bitrefill quote, runner, and gateway test modules.
