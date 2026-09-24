# Trezor allowance v1 — a spending limit the agent cannot exceed

Design specification. Nothing in this document is implemented. Every number,
address and screen description below is a requirement, not an observation; the
checks in the last section are what turns any of it into a fact.

## What this is

Today the gateway pays from a custodial wallet: the spending key is encrypted
on the server and decrypted to sign. The README says so plainly, and a server
compromise is the risk that follows. The Ledger lane (`docs/ledger-v1.md` on `main`)
narrows it by asking a human to approve individual escalated purchases, but the
payment itself is still signed by the server's key, and the owner has to be
present for each approval.

This lane removes the custodial key from the purchase path instead of guarding
it. The owner's USDC stay at the owner's own Trezor-controlled address. The
owner signs one on-chain `approve` giving a small limiter contract permission to
move up to a fixed total. The limiter enforces a daily cap, a per-purchase cap
and an expiry before it moves anything. The agent's key can trigger the limiter
and can do nothing else.

The device is needed to grant, to change the policy, and to revoke. It is not
needed to buy.

## Scope

One owner, Base mainnet, USDC at `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`,
the Bitrefill purchase lane, a Trezor at `m/44'/60'/0'/0/0`. The existing
`trezor-local-sidecar` sidecar is the only component that talks to the
device, over Trezor Suite MCP on `127.0.0.1:21340`, exactly as it does now.

x402 resources on Base are in v1 too, through the agent key as payer (see
[x402](#x402-the-agent-pays-the-limiter-funds-it)). Not in v1: Solana (see
[Solana](#solana-not-in-v1));
multiple owners; multiple agents against one allowance; an upgradeable or
governable limiter; recovering tokens sent to the limiter by mistake.

## The three addresses

| Role | Key lives | Holds | Can do |
| --- | --- | --- | --- |
| Owner | Trezor, never exported | The USDC | Grant, change policy, revoke, pause |
| Agent | Gateway host, hot | Only ETH for gas | Call `spend` within the policy |
| Limiter | No key — a contract | Nothing, ever | Move owner's USDC, only through `spend` |

The limiter is deployed by the agent key, not the device. Every parameter is a
constructor argument and immutable, so a limiter deployed with the wrong owner
is worthless rather than dangerous, and the owner's first signature is the grant
itself.

## The limiter

Minimal, immutable, no proxy, no admin, no setter, no token custody. Source:
`agent-allowance/src/AgentAllowance.sol` on the `trezor-local-sidecar`
branch.

**Immutable, set at deployment:** `token`, `owner`, `agent`, `guardian`,
`dailyCap`, `perPurchaseCap`, `expiry` (unix seconds).

**Mutable:** `paused` (one way only), `day` (the current UTC day index),
`spentToday`, `usedRef` (a mapping of consumed purchase references).

**Why nothing is settable.** T2 established that the Trezor renders an ERC-20
`approve` readably: the operation, the spender and the amount. It did not
establish anything about a call to our own contract, and a call to an unknown
contract is exactly what a hardware wallet shows as raw data. An owner-only
`setPolicy` would therefore be a blind signature on the one function that
decides how much the agent may spend. So there is none: every device
interaction in this design is an `approve`, which the owner can read.

**`spend(address payee, uint256 amount, bytes32 ref)`** — the only function that
moves money. In order:

1. `msg.sender == agent`, else revert.
2. `!paused` and `block.timestamp < expiry`, else revert.
3. `payee` is not zero, not the limiter and not the owner, else revert. Paying
   the limiter would strand the tokens; paying the owner is never a purchase.
4. `amount > 0` and `amount <= perPurchaseCap`, else revert.
5. `ref != 0` and `!usedRef[ref]`, else revert.
6. If `block.timestamp / 86400 != day`, count today's spending from 0.
7. Today's spending plus `amount` `<= dailyCap`, else revert.
8. Write `usedRef[ref]`, `day` and `spentToday`.
9. `token.transferFrom(owner, payee, amount)`, requiring `true`.
10. Emit `Spent(ref, payee, amount, spentToday)`.

State is written before the transfer, so a payee that re-enters cannot spend the
same budget twice. USDC has no transfer hook today; the ordering does not depend
on that staying true.

A failed transfer reverts the whole call, so a failed purchase consumes neither
budget nor `ref`, and the same purchase can be retried.

**`pause()`** — `guardian` or `owner`, and **permanent**. The guardian is a
second hot key held by the operator; it can stop spending and nothing else, so
the server can contain an incident at 3am without the owner finding the drawer.
There is no unpause: resuming means a new limiter and a new `approve`, which the
owner reads on the device. An unpause from the device would be the blind call
this design avoids, and an unpause from a hot key would make the kill switch
reversible by whoever tripped it.

**Read-only:** `remainingToday()` (zero once paused or expired),
`allowanceLeft()` (the ERC-20 allowance left), and the public immutables. The
gateway quotes from these, so an owner sees a refusal with a reason instead of
a reverted transaction.

### Two decisions worth naming

**The daily window resets at UTC midnight, not on a rolling 24 hours.** A rolling
window needs a ring buffer of timestamped spends and costs gas on every purchase.
The cost of the cheap version is real and must be stated to the owner: a burst at
23:59 followed by a burst at 00:01 spends two daily caps in two minutes. The
total is still bounded by the ERC-20 allowance, which is the number that actually
limits the worst case.

**`ref` is consumed on-chain.** The gateway already guarantees at-most-once
submission in its own store (`sign402-gateway/sign402_gateway/ledger_payments.py` on `main`),
but that store is on the machine we are assuming can be compromised. One storage
slot per purchase buys a second wall: a retried, replayed or duplicated `spend`
for a purchase already paid reverts on-chain. `ref` is the same reference the
Ledger lane already computes over the frozen order, so a receipt is comparable
across both lanes.

## Granting, changing and revoking

**Grant** is one transaction from the device: `approve(limiter, total)` on USDC.
It is a permission, not a transfer: the owner's balance does not change when it
is mined, and nothing is sent to the limiter, then or ever. Each purchase pulls
exactly its own amount from the owner's address to the payee in one transaction.
An owner whose balance is below a purchase simply cannot make it; the allowance
reserves nothing and creates no debt.
`total` is the owner's absolute risk budget — not the daily cap, and never
`type(uint256).max`. With a $100 daily cap, an owner who approves $300 has
decided that a full compromise for three days is the worst outcome they accept.

**Change the policy** is two device transactions, both readable `approve`s:
deploy a new limiter with the new caps (agent key, no device), `approve` it, and
`approve` the old one down to zero. Changing caps is rare; reading what one signs
is not optional.

**Revoke** is one transaction: `approve(limiter, 0)`. It is enforced by the USDC
contract, so it works if the limiter has a bug, if the gateway is hostile, and if
this project no longer exists. That property is the reason the limiter holds no
tokens: there must be nothing to strand.

**What the device must show.** The grant is the single moment of consent for
everything that follows, so a blind signature here is worse than the per-purchase
blind signature the Ledger lane already refused. The Trezor screen must render at
minimum the spender address and the exact amount. If it does not — check T2 —
this design does not proceed as written, and the alternatives are a smaller
`total` re-granted often, or a different rail.

## Gateway integration

A new `allowance_payments.py` replaces the custodial payer for this lane only.
Per purchase:

1. Quote, freeze the intent, compute `ref` over the frozen order.
2. `_reserve_user_wallet_spend` stays the single in-process chokepoint. The
   on-chain caps are a second wall, not a replacement: the server-side limits
   still exist and still refuse first, with a readable message.
3. Spending-memory (`sign402-gateway/sign402_gateway/decide.py` on `main`)
   still decides `PAY / ESCALATE / BLOCK`. It is the soft layer — it decides
   *what* to buy; the contract decides *how much is possible*. A prompt can
   mislead the first and cannot touch the second.
4. Preflight the contract reads. Refuse with the reason (`per-purchase cap`,
   `daily cap`, `allowance exhausted`, `expired`, `paused`) before broadcasting.
5. Build `spend(payee, amount, ref)`, sign with the agent key, broadcast, wait
   for the receipt.
6. The receipt is the transaction hash. It is what `/last_purchase` shows and
   what settles the order downstream.

Proof of payment changes shape. The sidecar's `verify_signed_usdc_transfer`
accepts only a raw transaction sent to the USDC contract, with `transfer`
calldata, signed by the Trezor address. A `spend` fails all three: it is sent to
the limiter, with `spend` calldata, signed by the agent key. This lane verifies
the mined receipt instead: a USDC `Transfer(owner, payee, amount)` log emitted
by the token contract in a transaction to the configured limiter.

`/limits` reads `remainingToday()`, `allowanceLeft()` and the caps from
the chain. The numbers an owner sees stop being a claim by our server.

The agent key needs ETH for gas and must hold no USDC. A balance monitor that
warns before gas runs out belongs in this lane, because an agent that cannot pay
gas looks exactly like an agent that is refusing to buy.

## x402: the agent pays, the limiter funds it

The Bitrefill lane pays by a plain USDC transfer to an invoice address — the
sidecar already builds exactly that calldata (`encode_usdc_transfer` in
`trezor-sidecar/trezor_sidecar/base.py`), so `spend` pays the invoice directly.

x402 cannot be paid that way. The `exact` scheme on EVM wants an authorization
signed by the payer for every payment — EIP-3009 `transferWithAuthorization` for
USDC — which the seller's facilitator then settles from the payer's address. If
the payer were the owner's Trezor address, every x402 call would need the device.

So the payer is the **agent key**, and the limiter funds it. To the seller and
the facilitator the agent is an ordinary payer, so this works with any x402
service unchanged. The limiter needs no change: the agent's address is a valid
`spend` payee. The owner's USDC still rest at the owner's address; what the
agent holds is what the limiter released to it, inside the same caps.

Two ways to fund it, both used:

**The float, for micro-payments.** The agent keeps a small USDC balance. When a
payment would take it below the low-water mark, `spend(agent, refill, ref)`
tops it up to the target first. Many x402 calls, one `spend`. Without it, a
$0.01 query would carry a `spend` of its own costing a comparable amount in gas,
and every call would wait for a block and for the facilitator's node to see it.

**Exact funding, for anything above the float threshold.** `spend(agent,
amount, ref)` for exactly the quoted amount, then the x402 payment. The agent
holds nothing beyond the seconds between the two.

| Setting | T7 value | Meaning |
| --- | --- | --- |
| Float target | 0.05 USDC | Refill up to this |
| Float low-water | 0.01 USDC | Refill when a payment would leave less than this |
| Float threshold | 0.01 USDC | Payments above it are funded exactly instead |

Every refill and every exact funding is a `spend`, so the per-purchase cap bounds
each one and the daily cap bounds their sum. The `ref` of an exact funding
commits the resource and the quoted amount; a refill's commits a counter.

Before any funding, the x402 terms are read from the seller's `402` and shown:
network, asset, amount, recipient. The payment is then signed only for those
terms — `cdp-x402-service buy-user` refuses anything else — so a seller that
changes the price or the recipient between the quote and the payment gets
nothing.

What this costs, stated as plainly as the rest:

- **Money passes through the hot key.** The float sits there permanently, up to
  its target; exact funding for seconds. A stolen agent key takes the float at
  once, and then whatever the caps allow, as before. The float target is
  therefore a second number the owner chooses, next to the caps.
- **The agent paying itself is the design here,** not a hole: the x402 lane is
  exactly `spend` to the agent. The caps are the only bound, which the threat
  model already says for every lane.
- **The facilitator's node must see the funding.** After a `spend` to the agent,
  the facilitator verifies the agent's balance on its own node, which can be
  blocks behind (T4 saw public nodes lag). The payment waits until the balance is
  visible, and retries once on an insufficient-balance refusal. The float makes
  this rare.
- **A failed payment leaves USDC with the agent.** It stays in the float for the
  next call, and is returned to the owner when the float is closed.
- **Settlement is read from the chain, never from the seller.** A seller need not
  return a settlement header, and one that did could be wrong. Paid means a USDC
  `Transfer` from the agent to `payTo` of exactly the price, mined after the
  payment was sent. A non-2xx answer with no such transfer is "not delivered, not
  charged" — correct; either one without the other is a failure.

## Threat model

**A fully compromised gateway** — agent key, guardian key, decrypted everything
— can spend up to the daily cap, every day, until the owner revokes. It cannot
exceed the per-purchase cap, cannot exceed the approved total, cannot change the
policy, and cannot touch anything else at the owner's address. That is the
guarantee: a bounded, chosen, on-chain-auditable loss, not zero loss. Any
description of this lane that implies zero is wrong.

**Where the money can go.** v1 does not restrict the payee. A compromised agent
can pay itself, $10 at a time, up to the cap. A payee allowlist would close this,
but invoice addresses rotate, so the list would need a device signature to
update — which rebuilds the problem this lane exists to solve. The open
alternative is a co-signature from a merchant registry key inside `spend`; it is
not in v1 and the gap is stated rather than papered over.

**If the limiter has a bug.** Deployed code cannot be changed — there is no
proxy, no admin and no upgrade path — so "the limiter is hacked" means one thing:
a mistake in its logic lets someone get past a check. A missing sender check, a
wrong day calculation, a broken `ref` guard.

The ceiling on that loss is the remaining ERC-20 allowance, and it is enforced by
the USDC contract, not by ours. However broken the limiter is, USDC will not let
it pull more than the owner approved. The rest of the balance, the owner's other
tokens and the Trezor key are out of its reach.

| What happened | Maximum loss | How fast |
| --- | --- | --- |
| Agent key stolen, limiter correct | Daily cap per day, per-purchase cap per call | Slowly, visibly; the watcher can pause |
| Bug in the limiter, checks bypassed | The whole remaining allowance | **One transaction, at once** |
| Trezor or seed compromised | Everything | Outside this design |
| Circle freezes or pauses USDC | Not ours to bound | As for anyone holding USDC |

The uncomfortable row is the second one. A bug is worse than a stolen agent key:
the key is bounded by the daily cap and slow enough to catch; a bug can take the
whole allowance in one transaction, before any watcher reacts. Pausing may not
help, because a bug in the checks may be a bug in the pause check too. The one
exit that always works is `approve(limiter, 0)` from the device, and by then it
is after the fact.

What follows from that:

1. **The approved total is the owner's real risk, and should be chosen as one.**
   "What am I prepared to lose in the worst case", not "what is convenient".
   Fifty re-granted weekly beats a thousand for a year.
2. **The limiter stays minimal.** Under a hundred lines of logic, no
   dependencies. `SafeERC20` exists for tokens that do not return a boolean;
   the token here is fixed to USDC, which returns `true` or reverts, so a checked
   call does the same job without importing a library. Less code, fewer places
   for the mistake.
3. **Every refusal is a test, and the invariants are fuzzed.** Beyond the unit
   tests in T3, Foundry invariant tests over millions of random call sequences:
   spent in a day never exceeds the cap; nobody but `agent` moves money; the
   limiter's own token balance is always zero; a `ref` pays at most once.
4. **Audit before real amounts.** Tests and an external code review are enough
   for first runs at $50. They are not enough for money anyone would miss.
5. **Or do not write the limiter.** Safe's Allowance Module is audited and has
   been in use for years, and "X per period" is what it does. It trades our bug
   risk for theirs, at a cost: the funds live at the Safe's address rather than
   the Trezor's own, and "no more than $10 per purchase" is not expressible in it
   without additional code.

**A lookalike limiter.** The limiter is deployed by the agent key, and the device
shows the spender only as an address. A compromised gateway could present the
address of a contract that is not this limiter — no caps, no checks — and the
screen would look the same. The approved amount still bounds the loss, because
USDC enforces it; the caps would not. Before the first grant, the owner checks on
a second device that the spender address is a verified `AgentAllowance` on
BaseScan with the expected `owner`, `agent`, caps and expiry. The bot should show
that link, but the check is only worth anything if it is not done through the bot.

**Blind signing** is the one failure that breaks the model rather than bounding
it: an owner who cannot read the grant is consenting to an amount they did not
see. Check T2 gates the design.

**The owner's address is now a known agent-funded target.** It holds the balance,
not just the cap. Nothing here protects against a compromised Trezor or seed.

## Solana, not in v1

SPL Token has delegation in the token program itself: `approve` sets a delegate
and an amount, the tokens stay in the owner's token account, and the delegate
transfers within the limit. The ceiling therefore needs no program at all. The
daily reset and the per-purchase cap do — either a small Anchor program holding
the same policy as the limiter here, or Squads V4 spending limits, which provide
amount-per-period natively at the cost of the funds living in a Squads vault
rather than the owner's own account.

The choice cannot be made before check T5: whether a Trezor will sign those
instructions at all, and readably. Until then the gateway continues to never
select the Solana leg in x402 offers, which it already does deliberately.

To avoid writing the policy twice, the gateway gets one chain-agnostic rail
interface — `grant`, `spend`, `revoke`, `remaining` — with Base and Solana as
adapters. The caps and their meaning are defined once, here.

## T0 — how each lane pays today

Answered from the gateway code on `main` on 23 September 2026, no device and no network.

| Lane | Payer today | Transport | Fits `spend` |
| --- | --- | --- | --- |
| Bitrefill, `usdc_base` (`bitrefill_mcp.py`, `_pay_usdc_invoice`) | Operator treasury via Bankr (`BankrTreasuryClient.transfer_token_exact`) | Plain USDC `transfer` to the invoice's `payment["address"]` | **Yes, directly** |
| `/agent/buy-tool` x402, including the Ledger lane (`UserWalletX402Buyer` → `cdp-x402-service`, `@x402/evm` `ExactEvmScheme`) | The owner's custodial wallet, key decrypted per payment | x402 exact: a payer-signed authorization per payment | No — just-in-time funding |
| The Graph (`onchain_data.py`) | Gateway account, budgeted to the user | x402 exact | No — just-in-time funding |
| Web search (`web_search.py`) | Gateway account | x402 exact | No — just-in-time funding |
| Bankr LLM credits, SINGIT, Bankr x402 calls | Bankr's own wallet via the Bankr CLI | Bankr's | Out of scope |
| Algorand payment executor | Demo only | — | Out of scope |

Conclusions:

- **v1 = Bitrefill is confirmed as the right scope.** It is the only lane that pays
  by a plain transfer, so `spend` replaces the payer and nothing else.
- **The allowance lane changes who pays Bitrefill.** Today it is the operator's
  treasury; in this lane it is the owner's Trezor address. Bitrefill should not
  care who pays an invoice as long as the transfer arrives — T6 confirms that.
- **Invoice addresses come from each invoice** (`payment["address"]`). That is
  consistent with a fresh address per invoice but does not prove it. The payee
  allowlist question stays open until a few real invoices are compared.
- **Funding the agent for x402 is weaker, and should be described as such.**
  `spend` pays the agent's own session address, which then signs the x402
  payment. In that path the agent paying itself is the design, not a hole, and
  the caps are the only bound. The money still rests at the owner's address
  until the purchase.

## Checks

Written as each check runs, in [trezor-allowance-checks.md](trezor-allowance-checks.md), in the format of `docs/checks.md` on `main`:
the command, the output, one line of conclusion. T1 and T2 come first because
they can end this design.

| ID | Check | Needs | Blocks |
| --- | --- | --- | --- |
| T0 | Which gateway lanes pay by plain transfer and which need a signed payload | Code only | **Done** — see above |
| T1 | Trezor Suite MCP accepts `approve` calldata and the device signs it | Device, no funds, nothing broadcast | **Pass**, 24 September |
| T2 | What the Trezor screen shows for that approve: spender and exact amount, or a blind hash | Device, same run as T1 | **Pass**, as reported, 24 September |
| T3a | Limiter automated suite: every refusal as a unit test, fuzzed amounts, six invariants, and every check removed in turn to prove the suite catches it | Local EVM | **Pass**, 24 September, 10/10 mutations caught |
| T3b | The same limiter on Base Sepolia | Testnet | **Skipped** by the owner's decision, 24 September: covered by T4 on mainnet, where the worst case is the 1.00 USDC grant |
| T4 | Mainnet run: deploy, publish source, grant 1.00 from the device, one real 0.30 purchase, every refusal by `eth_call`, revoke, prove the same purchase now fails in USDC. Procedure: [trezor-allowance-t4-runbook.md](trezor-allowance-t4-runbook.md) | Device, 1.00 USDC at risk, 0.30 moved between the owner's own addresses | **Pass**, 24 September, 9/9 |
| T5 | Whether a Trezor signs an SPL `approve`, and what it displays | Device, Solana | The Solana variant |
| T7 | x402 on Base mainnet through the limiter: a float refill and a 0.005 payment to `vet-service`, a second 0.005 from the float with no refill, a 0.02 payment to `vet-shortlist` funded exactly, each settlement read back on chain, the float returned and the allowance revoked. Procedure: `agent-allowance/script/t7-x402.sh` | Device, the owner's own x402 services, cents | **Pass**, 24 September |
| T6 | A Bitrefill purchase through x402, paid by the agent and funded by the limiter: sign-in with the agent key, the owner's confirmation before the order exists, exact funding, payment to Bitrefill's published x402 address only, settlement read on chain, the code shown in the terminal and written nowhere. The owner chose this over Bitrefill's MCP route and invoice payment by `transferFrom` (24 September), which drops the question of whether Bitrefill credits a contract-sent transfer. Procedure: `agent-allowance/script/t6-bitrefill.sh grant / buy / revoke` | Device, a product of a few cents | **Pass**, 24 September |

T3 is also the automated suite: the reverts are the specification, and a limiter
that passes only the happy path has not been tested at all.

**T1 and T2 procedure.** On the `trezor-local-sidecar` branch,
`python -m trezor_sidecar.approve_preview` signs one USDC `approve` on the device
and stops. It never broadcasts and never calls `trezor_push_transaction`. The
spender is the burn address `0x…dEaD`, whose key nobody holds, so even a signed
transaction that escaped and was mined would grant an allowance nobody can use.
`approve` needs no balance, so the paired account does not need funds.
`SIGN402_TREZOR_PREVIEW_AMOUNT_ATOMIC=300000000` previews a realistic 300 USDC
grant. Confirming and rejecting on the device are both valid results; what
matters is what the screen showed, written down in trezor-allowance-checks.md.

If the tool fails with "Trezor Suite is unavailable", run
`python -m trezor_sidecar.transfer_preview` as the control. If the transfer
preview works and the approve preview does not, Suite refuses `approve` calldata
and T1 has failed.

**T6 is a real Bitrefill purchase** and follows the repository rule for them: the
`bitrefill` skill and MCP route, with the exact product, amount and payment method
shown and confirmed by the owner before the order is created.
