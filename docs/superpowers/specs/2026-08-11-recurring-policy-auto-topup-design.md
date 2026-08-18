# Recurring Policy Auto Top-Up Design

**Goal:** Let a user sign one recurring spending policy so the gateway tops up
LLM credits automatically when the balance falls below a threshold, without an
interactive approval at the moment of spend.

**Why:** Every existing spend path requires a human approval per payment. That
makes automatic spending impossible by construction. Credits run out at
unpredictable times, which is exactly the case where a pre-signed policy is
necessary rather than decorative.

**Scope:** LLM credits top-up only. Bitrefill purchases stay interactive.

---

## Global Constraints

- A recurring policy must be approved through the existing approval channel
  before any automatic execution.
- Automatic execution never asks for approval; it reports after the fact.
- No automatic execution may exceed the per-execution cap, the per-period
  budget, or the per-day execution count.
- `SIGN402_PURCHASES_PAUSED=1` must stop all automatic execution.
- A user pause must take effect before the next check, without a restart.
- Automatic execution reuses the existing top-up path. No new payment primitive.
- Never log or persist API keys, wallet secrets, or private keys.
- A failed automatic execution must not retry inside the same check interval.
- Two overlapping checks must never produce two top-ups for one trigger.

---

## Policy Shape

The current policy is one-shot: `maxBudgetAtomic` is consumed and the policy is
finished. A recurring policy adds a period and a counter that resets.

```json
{
  "version": "1",
  "agentId": "hermes-demo",
  "policyId": "policy-llm-auto-001",
  "allowedPurpose": "bankr_llm_credits_topup",
  "asset": "0xc2c1e0b7C401e6217193732272444D928646eba3",
  "recurrence": {
    "periodSeconds": 2592000,
    "maxBudgetAtomicPerPeriod": "10000000000000000000000",
    "maxExecutionsPerDay": 3
  },
  "maxPerPaymentAtomic": "5000000000000000000000",
  "trigger": {
    "kind": "credit_balance_below",
    "thresholdUsd": "10.00",
    "topUpUsd": "25.00"
  },
  "expiresAt": 1793500000,
  "nonce": "llm-auto-001"
}
```

The approval screen must show, in this order: purpose, top-up amount, trigger
threshold, budget per period, period length, and expiry. The user is approving
a standing authorization, so the screen must say so explicitly rather than
looking like a single purchase.

---

## Components

### Policy store extension

`commerce_store.py` gains a recurring-policy table holding the policy hash, the
approval reference, the current period start, atomic spent-in-period, execution
count for the current UTC day, a paused flag, and the last execution outcome.

Period rollover is computed on read, not by a timer: if
`now >= period_start + period_seconds`, the period start advances and the spent
counter resets before any budget check. This keeps correctness independent of
scheduler reliability.

### Trigger checker

A single background loop inside the gateway process, interval from
`SIGN402_AUTO_TOPUP_CHECK_SECONDS` (default 300).

For each active recurring policy:

1. Skip if globally paused, user-paused, expired, or day-count exhausted.
2. Read the current credit balance through the existing credits path.
3. Skip if balance is at or above `thresholdUsd`.
4. Compute the top-up amount and its atomic funding cost.
5. Skip if it exceeds `maxPerPaymentAtomic` or the remaining period budget.
6. Acquire the per-policy execution lock.
7. Execute through the existing top-up path with a deterministic idempotency
   key derived from policy hash, period start, and execution index.
8. Persist the outcome, increment counters, release the lock.
9. Notify the user.

The lock is a row-level claim in SQLite, not an in-process mutex, so a restart
mid-execution cannot double-spend.

### Notification

After each automatic execution, send a message on the user's linked channel:
amount, resulting credit balance, remaining period budget, and two actions —
`Pause auto top-up` and `Show history`.

A failed execution notifies once per failure reason per period, not per check,
so a persistent failure does not become a message flood.

---

## Failure States

| Situation | State | Funds moved | Retry |
|---|---|---|---|
| Balance read fails | `CHECK_FAILED` | no | next interval |
| Budget or cap exceeded | `SKIPPED_CAP` | no | next period |
| Funding token insufficient | `FUNDING_FAILED` | no | next interval |
| Provider rejects top-up | `TOPUP_FAILED` | no | next interval |
| Ambiguous failure after on-chain action | `RECONCILIATION_REQUIRED` | maybe | never automatic |

`RECONCILIATION_REQUIRED` pauses the policy and requires the user to resume it.

---

## Kill Switches

Three independent levels, all of which must work without a deploy:

1. `SIGN402_PURCHASES_PAUSED=1` — stops everything, already exists.
2. Per-policy pause from the notification button.
3. Policy expiry — every recurring policy must carry `expiresAt`. A policy
   without an expiry is rejected at approval time.

---

## Configuration

```env
SIGN402_AUTO_TOPUP_ENABLED=0
SIGN402_AUTO_TOPUP_CHECK_SECONDS=300
SIGN402_AUTO_TOPUP_MAX_EXECUTIONS_PER_DAY=3
SIGN402_AUTO_TOPUP_MAX_USD_PER_EXECUTION=25.00
```

Ships disabled. Enabled per environment after the manual verification below.

---

## Verification

Automated tests use injected balance readings and a stub top-up path. They
never call a live provider.

Required coverage:

- period rollover resets the spent counter and not the day counter;
- a policy at its period budget is skipped, not partially executed;
- two concurrent checks produce exactly one execution;
- a restart between lock acquisition and execution does not double-spend;
- global pause and per-policy pause both stop execution before any funding;
- an expired policy never executes;
- a policy without `expiresAt` is rejected at approval.

Manual verification runs with a low `maxPerPaymentAtomic`, a threshold set just
above the current balance to force one trigger, and an explicit check that a
second trigger does not fire within the same period.

---

## Open Questions

1. Should the top-up amount be fixed, or sized to reach a target balance?
   Fixed is simpler and easier to show on an approval screen.
2. Should a recurring policy be re-confirmed on some cadence even when unused?
   A standing authorization that nobody has seen for months is a trust problem
   independent of its caps.
3. Does the Trezor path need a different approval screen for recurrence, given
   the device shows a fixed three-line context?
