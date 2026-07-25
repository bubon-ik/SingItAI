# Configurable Bitrefill Spending Limits Design

## Goal

Allow a user to choose practical per-transaction and daily spending limits,
including limits large enough for a $1,000 Bitrefill product, without allowing
the user to exceed operator-controlled production ceilings or removing the
last protection against an unexpectedly expensive Bitrefill quote or invoice.

## Selected Model

The existing `/limits <per-transaction> <daily>` command remains the source of
each user's personal limits. For example:

```text
/limits 50 1000
```

means:

- at most 50 USDC of total charged value in one purchase;
- at most 1,000 USDC of total charged value per UTC day.

The operator controls higher, non-user-editable production ceilings. The
initial production values will be:

```text
SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX=1020000000
SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC=5000000000
```

These values represent 1,020 USDC per transaction and 5,000 USDC per UTC day.
Existing user limits are preserved. A stored limit above a subsequently
lowered operator ceiling continues to be clamped on read.

Bitrefill retains a separate provider disaster cap:

```text
SIGN402_BITREFILL_LIVE_MAX_USD=1000.00
```

This cap applies to the Bitrefill product price and invoice amount before the
Sign402 service fee. It protects the legacy/operator purchase path as well as
per-user purchases and is checked independently of `/limits`.

## Amount Semantics

Personal limits and operator wallet ceilings apply to `totalUsd`, including
the 2% Sign402 service fee. The Bitrefill disaster cap applies to the product
price before that fee.

Therefore:

- a Bitrefill product priced at $1,000 has a total charged value of 1,020 USDC;
- the user must set a per-transaction limit of at least 1,020 USDC;
- the operator per-transaction ceiling of 1,020 USDC permits that purchase;
- the Bitrefill disaster cap of $1,000 permits the underlying product price;
- the user's remaining daily limit must also be at least 1,020 USDC.

The effective authorization remains bounded by the personal per-transaction
limit, remaining personal daily budget, operator ceilings, Bitrefill disaster
cap, available balance, supported denomination, swap liquidity, gas, and
explicit purchase approval.

## Purchase Flow

The existing fail-closed ordering remains unchanged:

1. Bitrefill validates the selected product, recipient fields, and the $1,000
   provider disaster cap while creating the quote.
2. The gateway computes `totalUsd`, including the service fee.
3. The gateway checks the user's per-transaction limit and remaining daily
   budget against `totalUsd`.
4. At buy time, the gateway checks the limits again and atomically reserves the
   full daily-budget amount while approval is pending.
5. The user explicitly approves the exact committed purchase.
6. Funding and fulfillment run only after approval.
7. The Bitrefill invoice is checked against both the approved quote and the
   $1,000 provider disaster cap before any treasury transfer.
8. A successful purchase settles the reservation; rejection, timeout, or
   failure releases it.

Parallel pending purchases therefore cannot bypass the daily limit.

## User-Facing Limit Display

The `/limits` response will distinguish the three concepts that are currently
easy to confuse:

- `Your spending limits`: the user's effective per-transaction and daily
  limits;
- `Platform maximums`: the operator's per-transaction and daily ceilings;
- `Bitrefill product maximum`: the provider disaster cap before the service
  fee.

The response will state that fees count toward the user's limits. It will no
longer describe operator defaults as if they were hard maximums.

When a requested user limit exceeds an operator ceiling, the existing update
request remains rejected and the error identifies the applicable platform
maximum. A Bitrefill product above the disaster cap remains rejected before
approval or payment.

## Configuration and Deployment

The repository will document the three explicit production settings above.
No API keys, wallet secrets, approval tokens, recipient data, redemption data,
or other bearer-value information will be added to version control.

Deployment updates `/etc/sign402-gateway.env`, restarts
`sign402-gateway.service`, and verifies:

- the service is active and has not entered a restart loop;
- the health endpoint succeeds;
- `/limits 50 1000` is accepted;
- `/limits 1020 5000` is accepted;
- a per-transaction value above 1,020 or a daily value above 5,000 is rejected;
- a Bitrefill quote at or below $1,000 reaches the personal-limit check;
- a Bitrefill quote above $1,000 is rejected before approval or payment.

Production purchase probes must stop before `buy-products`; verification must
not create a real charge.

## Test Coverage

Gateway tests will prove:

- the limit display exposes personal limits, operator ceilings, and the
  Bitrefill product cap with unambiguous labels;
- fees are described as counting toward personal limits;
- 1,020/5,000 is accepted when it equals the operator ceilings;
- values above either operator ceiling are rejected;
- existing stored values are clamped when an operator lowers a ceiling;
- a $1,000 Bitrefill product can pass the provider cap while its 1,020 USDC
  total remains subject to personal limits;
- a product above $1,000 remains blocked before approval and payment;
- reservations still prevent parallel purchases from exceeding the daily cap.

Hermes plugin tests will verify that the revised `/limits` text is returned
without changing the command syntax.

## Non-Goals

- Removing all operator controls or making live Bitrefill unlimited.
- Allowing a user to exceed the configured production ceilings.
- Changing the 2% service fee.
- Changing approval binding, funding, settlement, redemption delivery, or
  purchase rate limits.
- Automatically increasing a user's stored limits.
