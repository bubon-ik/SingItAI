# Merchant-Scoped Daily Budget — Venice AI Chat Design

**Goal:** Let a user approve one standing policy — "up to $5 per day, Venice AI
only" — and then chat freely without another approval, until the daily cap is
reached or the policy expires.

**Why:** Every current spend path asks for approval per payment. A chat cannot
work that way; nobody confirms a message on a hardware device. This is the first
flow where the policy layer does the job it was designed for.

**Scope:** Venice AI chat only. Bitrefill, x402 tool purchases and LLM-credit
top-ups are unchanged.

---

## Global Constraints

- The policy is approved once through the existing approval channel before any
  spend, and names the merchant, the daily cap, and the expiry on screen.
- A payment is allowed only when the 402 challenge's `payTo` matches the address
  bound at approval time. Domain and URL are never sufficient.
- A `payTo` change pauses the policy. It never auto-approves against a new
  address.
- Spending never exceeds the daily cap, the per-prefund cap, or the policy
  expiry, whichever binds first.
- `SIGN402_PURCHASES_PAUSED=1` stops all chat settlement.
- Unspent prefunded credit is user funds. It is refundable and must survive a
  restart.
- The user is never silently refused. Every stop has a message and a next step.
- Never log or persist prompt text, model responses, wallet secrets, or the
  Venice payment envelope beyond what settlement requires.
- Chat content is not stored server-side beyond the active session buffer.

---

## Policy Shape

```json
{
  "version": "1",
  "policyId": "policy-venice-daily-001",
  "agentId": "singit-chat",
  "allowedPurpose": "ai_chat",
  "merchant": {
    "name": "Venice AI",
    "network": "eip155:8453",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x…",
    "payToBoundAt": 1786400000,
    "resourceHost": "api.venice.ai"
  },
  "window": {
    "kind": "utc_day",
    "maxSpendAtomicPerWindow": "5000000"
  },
  "prefund": {
    "chunkAtomic": "500000",
    "maxOutstandingAtomic": "1000000"
  },
  "expiresAt": 1789000000,
  "nonce": "venice-daily-001"
}
```

`payTo` is the binding. `resourceHost` is advisory, used only for display and
for detecting an obviously wrong endpoint before a payment is attempted.

### Approval screen

Order matters. The user is granting a standing authorization, and the screen
must say so rather than looking like a single purchase.

```
Standing approval — not a one-off
Venice AI  (0x1234…abcd)
Up to $5.00 per day
Resets 00:00 UTC · Expires 10 Sep
OK / CANCEL
```

On Trezor the context is three lines. Use:

```
AI CHAT $5/DAY
VENICE 0x…abcd
UNTIL 10 SEP
```

The word `/DAY` on the device screen is what distinguishes this from a single
payment. It is not optional.

---

## Settlement Model

Per-message on-chain settlement is rejected: at ~$0.003 a message it adds
seconds of latency and overhead comparable to the price of the message itself.

Instead the gateway prefunds in chunks and meters locally.

```
policy approved
  → prefund chunk ($0.50) paid to Venice via x402
  → each message debits the local ledger
  → chunk exhausted → next chunk, if the daily window allows
  → day rolls over at 00:00 UTC → counter resets
```

**Outstanding credit** is prefunded-but-unconsumed value. It is capped by
`maxOutstandingAtomic` so a paused or abandoned session cannot strand more than
one extra chunk.

**A prefund counts against the daily window at the moment it is paid**, not as
it is consumed. This is the conservative direction: the user can never be
surprised by a spend larger than the cap they approved.

**Marketing constraint.** Because settlement is batched, the product may say
"you pay for what you use" and may display a per-message price. It may not say
each message is its own on-chain transaction. That claim would be false.

---

## Components

### Policy store

New table keyed by policy hash, holding: bound `payTo`, window start, atomic
spent in the current window, outstanding prefunded credit, paused flag, pause
reason, last settlement reference.

Window rollover is computed on read: if `now >= window_start + 86400`, advance
the window start to the current UTC day and zero the spent counter before any
budget check. Correctness does not depend on a scheduler running.

### Chat runner

Per user message:

1. Reject if globally paused, policy paused, expired, or the daily cap is spent.
2. If local credit covers the estimated cost, debit and call Venice. Done.
3. Otherwise attempt a prefund:
   a. Request the resource; read the 402 challenge.
   b. Verify `payTo`, network, and asset against the bound policy. Any mismatch
      pauses the policy and stops.
   c. Check chunk size against remaining daily budget and outstanding cap.
   d. Settle, record the chunk, then call Venice.
4. Debit actual cost once Venice returns usage.
5. Append the footer: cost of this message, remaining daily budget.

The prefund holds a per-policy row lock in SQLite. Two concurrent messages must
produce one prefund, and a restart between lock and settlement must not double
pay.

### payTo watcher

Poll the x402-list changes feed for `payto_changed`. On a change affecting a
bound merchant:

1. Pause every policy bound to the old address.
2. Notify: "Venice AI changed its payout address. Chat is paused until you
   approve the new one."
3. Require a fresh approval showing the new address. Never migrate silently.

Also treat an unexpected `payTo` seen live in a 402 challenge as the same event,
since the feed may lag.

---

## User-Facing Behaviour

**First entry**

> AI without an account, without logs. Paid from your wallet, fractions of a
> cent per message.
> Daily limit: `$1` `$5` `$10`

Five free messages before the first approval, funded by the operator. The cost
is negligible and it removes the "pay before you have tried it" barrier.

**During chat**

Footer under each answer: `$0.003 · $4.87 left today`.

**At 80% of the daily cap**

> You've used $4 of today's $5.

Once per window, not per message.

**At the cap**

> Today's $5 is spent. Resets at 00:00 UTC.
> `Raise limit` `Wait until tomorrow`

Raising the limit requires a new approval. It never silently continues.

**Never** show the words x402, facilitator, settlement, or prefund to the user.

---

## Failure States

| Situation | State | User sees | Funds |
|---|---|---|---|
| Daily cap reached | `WINDOW_EXHAUSTED` | cap message + raise option | none moved |
| `payTo` mismatch or changed | `MERCHANT_CHANGED` | paused, re-approve | none moved |
| Venice returns 5xx or times out | `PROVIDER_UNAVAILABLE` | retry suggestion | credit intact |
| Prefund settlement fails | `PREFUND_FAILED` | try again shortly | none moved |
| Settled but response never arrived | `RECONCILIATION_REQUIRED` | paused, support | credit preserved |
| Policy expired | `EXPIRED` | re-approve prompt | credit refundable |

`RECONCILIATION_REQUIRED` pauses the policy and requires manual resolution. It
is never retried automatically.

---

## Refunds

Outstanding credit belongs to the user. On pause, expiry, or user revocation the
balance is displayed and remains claimable. If Venice supports credit withdrawal
it is returned; if not, it stays spendable against the same merchant when the
policy is renewed. It is never quietly written off.

---

## Kill Switches

1. `SIGN402_PURCHASES_PAUSED=1` — global, already exists.
2. Per-policy pause from the chat footer or the wallet menu.
3. `expiresAt` — mandatory. A policy without an expiry is rejected at approval.

---

## Configuration

```env
SIGN402_AI_CHAT_ENABLED=0
SIGN402_AI_CHAT_MERCHANT_PAYTO=0x...
SIGN402_AI_CHAT_PREFUND_CHUNK_ATOMIC=500000
SIGN402_AI_CHAT_MAX_OUTSTANDING_ATOMIC=1000000
SIGN402_AI_CHAT_DEFAULT_DAILY_CAP_ATOMIC=5000000
SIGN402_AI_CHAT_FREE_MESSAGES=5
```

Ships disabled.

---

## Verification

Automated tests use a stub Venice and injected 402 challenges. They never call
the live provider.

Required coverage:

- window rollover zeroes the spent counter and preserves outstanding credit;
- a prefund that would exceed the daily cap is refused before any settlement;
- two concurrent messages produce exactly one prefund;
- a restart between lock acquisition and settlement does not double pay;
- a 402 challenge with an unexpected `payTo` pauses and moves no funds;
- a `payto_changed` event pauses every affected policy;
- an expired policy refuses before the 402 request is even made;
- a policy without `expiresAt` is rejected at approval;
- free messages are not billed and do not consume the window;
- global pause stops settlement before any funding.

Manual verification runs with a $0.10 daily cap and a $0.02 chunk, confirms the
cap message appears, and confirms a second day resets the counter.

---

## Open Questions

1. Should the daily cap be per-merchant or shared across all chat merchants once
   a second provider is added? Per-merchant is clearer on the approval screen;
   shared is easier to reason about as a budget.
2. Should unused credit roll into the next day, or expire with the window?
   Rolling is friendlier; expiring makes the daily cap mean exactly one thing.
3. Does the five free messages allowance need abuse protection beyond the
   existing per-user rate limit?
