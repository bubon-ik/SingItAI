# Unified 2% Bitrefill Service Fee Design

**Date:** 2026-07-23

## Goal

Charge one transparent 2% service fee on every Bitrefill purchase, regardless of the payment token or pricing route. Do not apply a minimum fee or any additional pricing buffer.

## Pricing Rule

The Bitrefill product's USD price remains the face cost used to fulfill the order.

```text
serviceFeeUsd = priceUsd * 200 / 10,000
totalUsd = priceUsd + serviceFeeUsd
```

The calculation must retain sufficient decimal precision for the payment token. Rounding happens only upward at the selected token's atomic-unit boundary so that the quote can cover `totalUsd`. There is no whole-token rounding and no minimum service fee.

Examples:

| Product price | Service fee | Total |
| ---: | ---: | ---: |
| $0.10 | $0.002 | $0.102 |
| $10.00 | $0.20 | $10.20 |
| $50.00 | $1.00 | $51.00 |

Bitrefill affiliate revenue is operator revenue and does not change the user-facing calculation.

## Architecture

One shared pricing helper owns the service-fee calculation and exposes the configured `200` basis points. Both quote routes use its result:

- The fixed SINGIT route converts `totalUsd` to SINGIT and rounds only to the token's atomic unit.
- The real-rate route requests enough of the selected payment token to produce `totalUsd` after route-reported swap fees and slippage.
- Direct USDC quotes require exactly `totalUsd`, rounded upward to USDC's atomic unit.

The existing 5% fixed-route margin and 10% real-rate buffer are removed. Route-reported network fees, protocol fees, and slippage remain part of the conversion quote; the application adds no second markup or hidden buffer.

## Quote and Approval Data

Every quote records:

- `priceUsd`: Bitrefill product cost;
- `serviceFeeBps`: `200`;
- `serviceFeeUsd`: the 2% fee;
- `totalUsd`: product cost plus service fee;
- the existing selected-token amount and atomic maximum.

The purchase commitment binds `serviceFeeBps`, `serviceFeeUsd`, and `totalUsd` in addition to the existing product and token fields. User spending-policy checks use `totalUsd`, not only the product cost, so approval covers the complete charge.

## User-Facing Receipt

Before approval, the receipt shows the three monetary components explicitly:

```text
Product: 10.00 USD
Service fee (2%): 0.20 USD
Total: 10.20 USD
Pay: <quoted token amount> <symbol>
```

No message describes the service fee as a swap buffer. Affiliate revenue is not shown because it is not charged to the user.

## Error Handling

- Reject non-positive product prices.
- Reject quotes when the selected-token route cannot guarantee at least `totalUsd`.
- Reject quotes when token precision or balance cannot cover the atomic-unit-rounded payment amount.
- If the exchange rate moves beyond the quoted limit, expire or reject the quote and require a new approval; do not silently increase the charge.

## Testing

Tests cover:

- the shared 2% calculation without a minimum fee;
- a $0.10 purchase producing a $0.002 fee and $0.102 total;
- fixed SINGIT quotes using atomic precision instead of whole-token rounding;
- direct USDC quotes charging the same 2%;
- real-rate token quotes targeting the same 2% total with no extra application buffer;
- quote and purchase-commitment fee fields;
- receipt text showing product price, fee, and total;
- spending-policy checks using the total charge;
- regression coverage proving the old 5% margin and 10% buffer are absent.

## Scope

This change affects Bitrefill purchase pricing only. It does not change Bitrefill affiliate configuration, catalog prices, redemption delivery, refunds, unrelated paid endpoints, or payment-provider fee schedules.
