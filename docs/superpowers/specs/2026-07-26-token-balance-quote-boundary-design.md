# Token-Balance Quote Boundary Fix Design

## Goal

Allow a selected wallet token to fund a Bitrefill purchase when its exact
available balance can satisfy the purchase total, even if smaller Bankr quote
probes have no route and the next exponential-search probe would exceed that
balance.

The fix must never quote, reserve, swap, or spend more than the selected
token's available balance. It must preserve the existing Bitrefill product
cap, user spending limits, platform ceilings, service fee, approval binding,
and purchase-safety checks.

## Root Cause

The Bitrefill runner passes the selected token's available wallet balance to
`RealRateSingitPricer.price_for_usdc()` as `max_amount`.

The pricer currently grows its search amount exponentially. When the next
candidate is greater than `max_amount`, it immediately raises:

```text
required SINGIT exceeds configured maximum
```

It does this without trying the exact `max_amount` boundary. This creates a
false rejection when:

- Bankr cannot route one or more smaller probe amounts;
- the next doubled or estimated probe crosses the wallet balance;
- a quote for the exact available balance would meet the target.

The error text is also misleading in this path because `max_amount` is the
selected token's wallet balance, not the configured operator maximum.

## Selected Behavior

`max_amount` remains a hard per-call upper bound. When it is provided, the
search treats it as the selected token's available balance.

The search will:

1. Never request a quote greater than the active amount cap, including its
   initial probe.
2. Continue its existing exponential or estimated search while the next
   candidate is within the cap.
3. When the next candidate would cross the cap, quote the exact cap once
   instead of rejecting immediately.
4. If the cap quote meets the buffered USDC target, use it as the upper bound
   and continue the existing binary search and final minimization. The result
   is the smallest usable amount the current quote-search resolution can find,
   rather than automatically spending the full balance.
5. If the cap quote returns less than the buffered target, reject with the
   balance-specific error.
6. If the cap quote is unavailable because Bankr returns a skippable no-route
   or server error, reject with a quote-unavailable error instead of claiming
   that the balance itself is insufficient.

Both failure branches are terminal. The exact cap is requested at most once,
including when that request fails and therefore is not stored in the existing
successful-quote cache. Rejection happens without starting an approval, swap,
transfer, invoice, or Bitrefill fulfillment.

## Failure Messages

When an exact-cap quote succeeds but its output is below the target, the pricer
will raise:

```text
selected payment token balance is insufficient at the current swap rate
```

When the exact-cap quote is unavailable, including a skippable route error or
temporary Bankr 5xx, the pricer will instead raise:

```text
unable to obtain a swap quote for the selected payment token balance
```

This avoids misreporting a provider or routing problem as proof that the
wallet has insufficient value. Non-skippable authentication, validation, and
client errors continue to propagate unchanged.

When no per-call `max_amount` is supplied and the constructor-level
`max_singit` cap is exhausted, the existing message remains:

```text
required SINGIT exceeds configured maximum
```

This distinction preserves the existing operator-cap diagnostic while giving
wallet users an accurate explanation.

`max_singit` applies only when no selected-wallet `max_amount` is supplied.
An explicit `max_amount` replaces it because arbitrary payment tokens use
different units and cannot share a SINGIT-denominated cap. The token-agnostic
Bitrefill product cap, user spending limits, and platform USDC ceilings remain
the shared operator controls.

## Unchanged Purchase Semantics

This change affects quote discovery only. It does not:

- increase or bypass the selected token balance;
- alter the 2% service fee or the buffered USDC target;
- change `/limits`, daily reservations, or platform ceilings;
- change the Bitrefill product maximum;
- loosen token allowlisting, gas reservation, approval binding, or replay
  protection;
- create a purchase before explicit approval;
- change funding, settlement, fulfillment, or redemption delivery.

The lowest applicable product, spending, platform, balance, liquidity, gas,
and approval limit continues to win.

## Test Coverage

Focused pricing tests will prove:

- smaller quote probes may return a skippable no-route error, the next normal
  probe may exceed the wallet balance, and an exact-balance quote that covers
  the target is accepted;
- after that boundary quote succeeds, the returned required token amount is
  no greater than the balance and the search attempts to minimize it;
- an exact-balance quote that does not cover the target produces the new
  balance-specific error;
- an unavailable quote at the exact balance produces the quote-unavailable
  error rather than an insufficient-balance claim;
- the exact balance is requested exactly once in both the successful and
  unavailable-quote boundary cases;
- a constructor-level configured cap still produces the existing configured
  maximum error;
- no quote request, including the initial probe, is ever greater than an
  explicit `max_amount`;
- an explicit `max_amount` below the normal starting probe of one token unit
  is used as the initial boundary and is never exceeded.

The existing real-rate pricing and Bitrefill runner suites will remain green.
The complete gateway test suite will run before the implementation is called
finished.

All automated verification uses fake quote clients. It must not create a real
Bitrefill order, approval, swap, transfer, or charge.

## Deployment and Verification

The implementation will be committed on the isolated
`codex/token-balance-quote-boundary` branch. After review and merge, production
deployment will:

1. fast-forward the server checkout to the reviewed commit;
2. run focused pricing and Bitrefill runner tests on the server;
3. restart only `sign402-gateway.service`, because the Hermes plugin is
   unchanged;
4. verify the gateway service is active, has not entered a restart loop, and
   returns a healthy response;
5. leave purchase verification at the quote boundary and never call the live
   buy or fulfillment operation.

No production secret or wallet balance will be written to the repository,
test fixtures, command logs, or commit history.

## Non-Goals

- Changing Bankr's liquidity or minimum routable amount.
- Guaranteeing that every token balance has a viable swap route.
- Raising product, spending, daily, operator, or wallet limits.
- Automatically selecting a different token after a quote failure.
- Performing a live purchase as a deployment test.
