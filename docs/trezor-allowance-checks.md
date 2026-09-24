# Trezor allowance — checks

Every entry: the command, the output, and one line of conclusion, written as each
check runs — the same rule as `docs/checks.md` on `main`. Design:
[trezor-allowance-v1.md](trezor-allowance-v1.md).

## T1 — Trezor signs a USDC `approve` through Suite MCP, 24 September

**Status: PASS.** Run on the real device from the owner's Mac. Design:
[trezor-allowance-v1.md](trezor-allowance-v1.md).

`approve_preview` on the `trezor-local-sidecar` branch signs one USDC
`approve` with `broadcast: false` and never calls `trezor_push_transaction`. The
spender is the burn address. Run twice, at the default and at a realistic grant:

```
$ .venv/bin/python -m trezor_sidecar.approve_preview
  signer     0xB80b…4558  (your paired account)          (truncated here)
  token      USDC on Base (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913)
  action     approve (permission, not a transfer)
  spender    0x000000000000000000000000000000000000dEaD
  allowance  1.00 USDC
Signed on device. Not broadcast.
  r: string(66 chars)
  s: string(66 chars)
  serializedTx: string(362 chars)
  v: string(3 chars)

$ SIGN402_TREZOR_PREVIEW_AMOUNT_ATOMIC=300000000 .venv/bin/python -m trezor_sidecar.approve_preview
  allowance  300.00 USDC
Signed on device. Not broadcast.
  (same response shape)
```

The owner reported that Trezor Suite and the device both displayed the
transaction, and confirmed it on the device both times.

Nothing reached the chain. USDC `allowance(signer, 0x…dEaD)` read afterwards:

```
eth_call USDC.allowance(0xB80b…4558, 0x…dEaD) at block 51706193
→ 0x0000000000000000000000000000000000000000000000000000000000000000
```

Conclusion: Suite MCP accepts `approve` calldata through the same
`trezor_send_transaction` route the sidecar already uses for `transfer`, and the
device signs it. The one-signature grant is technically possible. What the
screen showed is T2, recorded separately.

---

## T2 — what the Trezor shows for that `approve`, 24 September

**Status: PASS, as reported by the owner.** Same tool as T1, rerun at 300.00 USDC
and read on the device screen itself, not only in Trezor Suite.

Reported by the owner:

- The device labelled the operation as an **approve / allowance**, not as a
  plain call to a contract.
- The **spender address** was shown.
- The **amount** was shown.
- Trezor Suite, on the Mac, separately showed an approve of 300.

Not recorded: whether the spender was shown in full or shortened, the exact
format of the amount (`300 USDC` or a raw integer), and whether any warning
screen appeared. No photograph was taken.

Conclusion: the grant is not a blind signature. The owner can see on the device
itself that they are granting an allowance, to which address, for how much —
which is the condition trezor-allowance-v1.md sets for proceeding with a
one-signature grant. The first real grant should be photographed, closing the
three details above for the record.

---

## T3a — limiter automated suite and mutation check, 24 September

**Status: PASS.** Foundry 1.5.1, solc 0.8.24, local EVM. No network, no
dependencies: `agent-allowance/` on the `trezor-local-sidecar` branch.

```
$ forge clean && forge test
Ran 20 tests for test/AgentAllowance.t.sol:AgentAllowanceTest
  … 19 unit tests + 1 fuzz test (1000 runs) …
Suite result: ok. 20 passed; 0 failed; 0 skipped
Ran 6 tests for test/AgentAllowance.invariants.t.sol:AgentAllowanceInvariants
  … each invariant: runs: 256, calls: 51200 …
Suite result: ok. 6 passed; 0 failed; 0 skipped
Ran 2 test suites: 26 tests passed, 0 failed, 0 skipped (26 total tests)
```

A passing suite proves nothing unless it can fail, so each check in `spend`
was removed in turn and the suite rerun, with the invariant failure cache cleared
between runs:

| Mutation | Caught by |
| --- | --- |
| Coverage guard forced to fail | all 6 invariants (the `afterInvariant` guard runs) |
| No daily cap check | daily-cap invariant, daily-cap unit test |
| No `ref` replay check | replay invariant, grant invariant, unit test |
| No agent check | agent invariant, grant invariant, unit test |
| No per-purchase check | per-purchase invariant, fuzz test, unit test |
| Day never resets | midnight reset and midnight burst unit tests |
| Pause ignored | pause unit test |
| Expiry ignored | expiry unit test |
| Payee checks removed | payee unit test |
| Budget not recorded | daily-cap invariant, two unit tests |

10 of 10 mutations killed.

Two defects in the tests themselves were found this way and fixed before this
record. The first mutation run parsed invariant failures wrongly and reported
the replay and agent invariants as blind; they were not. It also showed,
correctly, that random time jumps almost never fit ten purchases into one UTC
day, so the daily-cap invariant held without being exercised. A `burst` action
in the handler now makes up to fifteen maximum-size purchases in one block, and
an `afterInvariant` guard fails any run in which no purchase succeeded.

Conclusion: the limiter enforces what trezor-allowance-v1.md specifies, in a
local EVM. Not covered here: a deployment on Base Sepolia (T3b), real USDC's
behaviour instead of the mock, and an external review.

---

## T4 rehearsal — the whole runbook on a local fork of Base mainnet, 24 September

**Status: PASS.** `anvil --fork-url https://mainnet.base.org` at block 51708086,
chain ID 8453: the real USDC contract and the real Trezor address, with nothing
sent to Base. Anvil's public test keys stood in for the agent and guardian; the
Trezor address was impersonated for `approve`. Every command in
[trezor-allowance-t4-runbook.md](trezor-allowance-t4-runbook.md), in order:

| Step | Expected | Observed |
| --- | --- | --- |
| Deploy with test policy | address | `0xB3B2…f165` (fork only) |
| Grant 1.00 | allowance 1.00, 0.50 left today | `1000000`, `500000` |
| Spend 0.31 | `PerPurchaseCapExceeded` | `0x80b5d3c8` |
| Spend from the owner | `NotAgent` | `0x0d9ab13f` |
| Pay the owner | `InvalidPayee` | `0xb387a238` |
| Pause from the agent | `NotGuardian` | `0xef6d0f02` |
| Pause from the guardian (call only) | no error | `0x` |
| Real spend 0.30 | agent +0.30, owner −0.30, limiter 0 | `300000`, `6526336 → 6226336`, `0` |
| Second 0.30 | `DailyCapExceeded` | `0xcc70389d` |
| Replay the paid ref | `RefUsed` | `0xac0292f0` |
| Spend 0.20 (call only) | no error | `0x` |
| Revoke, then 0.20 again | reverts in USDC | `ERC20: transfer amount exceeds allowance` |

The sidecar's `allowance status`, `grant 1.00` and `revoke` were then run against
the same fork, with a test key signing in place of the device and
`eth_sendRawTransaction` in place of Suite's broadcast. Status read every limiter
field; grant printed the limiter and links, verified the signed bytes, broadcast,
waited for the receipt and read back an allowance of 1 USDC; `grant 1.01` was
refused before signing (`SIGN402_TREZOR_POC_MAX_USD` = 1.00); revoke read back 0.

Conclusion: the runbook's commands and expected results match real USDC on Base
state. What the rehearsal cannot show is the device itself, Suite's broadcast
path, and mainnet gas — which is what T4 on mainnet is for.

The same day, `agent-allowance/script/t4-mainnet.sh` was run end to end against
a fresh fork, with test keys for the agent and guardian, the Trezor address
impersonated for grant and revoke, and publication skipped. All 9 checks
passed, the 0.30 was returned, and a second run resumed without deploying,
granting or purchasing again.

## T4, first mainnet attempt — stopped before the device, 24 September

The limiter was deployed and published (`exact_match` on Sourcify) at
`0xB9bD6FD8a3F8831DDDCb56BA8562e1080Db465f3`, deploy transaction
`0x60477303e55749d658369f8931967408d47593e608f6dfb7937ee6f49b5445ab`. `grant`
then stopped with "That contract does not answer like an AgentAllowance limiter".
Nothing was signed; the device was not reached.

Cause, reproduced with the public Base RPC: the inspection checked the chain ID
before every read — about twenty requests in a burst — and the endpoint began
refusing after the fifth field. The refusal was then reported as a wrong
contract. The first five fields had read back correctly.

Fixed in the sidecar: the chain is confirmed once per client, refused reads are
retried with backoff, a refused receipt poll counts as pending, and a read that
keeps failing says it is the RPC and that nothing was signed. The same limiter
then read back in full through the public endpoint: owner, token, agent,
guardian, caps 500000 and 300000, not paused, 0.50 left today.

The script's own reads got the same retry. Its first version returned success
for every `cast call`, reverts included — the exit status after `if …; fi`
without an `else` is 0 — which a fork rehearsal caught as seven false failures
before the script was run again on mainnet. After the fix: 9 of 9 checks on the
fork, and a deliberately wrong expected selector is reported as a failure.

## T4 — the allowance on Base mainnet, 24 September

**Status: PASS.** `agent-allowance/script/t4-mainnet.sh`, resumed after the
attempt above, run by the owner on the Mac with the Trezor. Every transaction
was then read back from Base independently of the script.

Addresses: owner (Trezor) `0xB80b5Ca13583fB7E0236db4bD8834B9035654558`, agent
`0x2d45184b8d2F32bC3F1e7aa71972a2350462a668`, guardian
`0xacE450136feeE5358F703B30951C9BaEAfCA9EdB`, limiter
[`0xB9bD6FD8a3F8831DDDCb56BA8562e1080Db465f3`](https://base.blockscout.com/address/0xB9bD6FD8a3F8831DDDCb56BA8562e1080Db465f3?tab=contract)
(Sourcify `exact_match`). Policy: daily cap 0.50, per purchase 0.30, expiry
2026-09-30 23:54 UTC.

| Step | Block | Signed by | Transaction | On chain |
| --- | --- | --- | --- | --- |
| Deploy | 51709168 | agent | [`0x60477303…45ab`](https://basescan.org/tx/0x60477303e55749d658369f8931967408d47593e608f6dfb7937ee6f49b5445ab) | limiter created, 701,733 gas |
| Grant | 51709697 | **Trezor** | [`0x0bd2a4af…e986`](https://basescan.org/tx/0x0bd2a4af2006457441f54d388edfb685d3616c93102b3f0b35a038032e74e986) | `Approval(owner, limiter, 1000000)` |
| Purchase | 51709708 | **agent, no device** | [`0x2dad4c47…e8d6`](https://basescan.org/tx/0x2dad4c47b28b00a296b92c6270d3f2e1a34a31ee68837d4b45227e9d3f89e8d6) | `spend`; USDC `Transfer(owner → agent, 300000)` |
| Revoke | 51709727 | **Trezor** | [`0xca19d88d…23aa`](https://basescan.org/tx/0xca19d88d1984630a7918ec11e4121278a2aeb341fbb3eda15c7b598f2db023aa) | `Approval(owner, limiter, 0)` |
| Return | 51709758 | agent | [`0xfcf893ee…6397`](https://basescan.org/tx/0xfcf893ee9d90c4f2871c7cf5593c535e2da5ab074d5dbd3e6885801d61826397) | USDC `Transfer(agent → owner, 300000)` |

Both device transactions were signed on the Trezor, checked by the sidecar
against exactly the requested `approve` before broadcast, and matched it on
chain. The purchase moved money from the Trezor address with the Trezor not
involved — the point of the design.

Checks by `eth_call` against mainnet, nothing sent:

| Case | Expected | Observed |
| --- | --- | --- |
| 0.31 over the per-purchase cap | `PerPurchaseCapExceeded` | `0x80b5d3c8` |
| the owner calling `spend` | `NotAgent` | `0x0d9ab13f` |
| paying the owner | `InvalidPayee` | `0xb387a238` |
| the agent pausing | `NotGuardian` | `0xef6d0f02` |
| the guardian pausing | no revert | no revert |
| a second 0.30 the same day | `DailyCapExceeded` | `0xcc70389d` |
| 0.20 inside the day and the allowance | no revert | no revert |
| replaying the paid reference | `RefUsed` | `0xac0292f0` |
| 0.20 after the revoke | USDC refuses | `ERC20: transfer amount exceeds allowance` |

State read afterwards: allowance 0; `spentToday` 300000; `usedRef(t4-1)` true;
not paused; owner 6.526336 USDC (as before the test), agent 0, limiter 0. Gas for
the whole test: about 0.0000007 ETH from the Trezor address and 0.0000049 ETH
from the agent.

**What the run showed that nothing before it could.** Three values the script
printed straight after a transaction were stale: "Allowance now 0" after the
grant, "agent USDC: 0" after the purchase, "Allowance now 0.7" after the revoke.
The public endpoint is load-balanced, and the read after a receipt can land on a
node a few blocks behind. The checks themselves ran on the correct state, but
could equally have landed on a stale node. Fixed after the run: `grant` and
`revoke` wait up to 30 s for the RPC to show the mined allowance and otherwise
say the node is behind and not to sign again; the script waits for the expected
allowance, `spentToday` and agent balance before any check that depends on them,
and counts a value that never appears as a failure. Rehearsed on a fork: 9/9.

**Still open.** The device screens were not photographed, so the three T2 details
— full or shortened spender, amount format, warning screens — remain unrecorded.
T6, Bitrefill crediting an invoice paid through the limiter, is next on Base.

## T7, first mainnet attempt — stopped before the first agent transaction, 24 September

The grant went through: the Trezor signed `approve(limiter, 1000000)`, the sidecar
checked the bytes and broadcast
[`0x66a47d18…5a82`](https://basescan.org/tx/0x66a47d1822c3396d6c1f410f384e25ffbf605658d33cb68be96a052bae0b5a82),
and the allowance read back as 1 USDC — the settle-wait fix from T4 working. The
first purchase then failed before sending anything: cast refused the keystore
password passed as `/dev/fd/63` ("does not exist"; it wants a regular file). Read
back afterwards: agent nonce unchanged at 3, owner 6.526336 USDC, allowance
1000000, `spentToday` 300000 from T4.

The fork rehearsal had not caught it because it supplied the agent's key directly
and never took the password path. Fixed: the password goes into a regular file in
a `mktemp -d` directory (700, file 600) removed on any exit, and the rehearsal now
imports the agent into a password-protected keystore and runs exactly the owner's
path. It passed; a wrong password stops before any transaction.

## T7 — x402 on Base mainnet, paid by the agent, funded by the limiter, 24 September

**Status: PASS, verified from the chain.** `agent-allowance/script/t7-x402.sh`
against the owner's own services on Bankr x402 Cloud (x402 v2, `exact`, EIP-3009,
facilitator `api.bankr.bot`), with the T4 limiter re-granted 1 USDC from the Trezor
([`0x66a47d18…5a82`](https://basescan.org/tx/0x66a47d1822c3396d6c1f410f384e25ffbf605658d33cb68be96a052bae0b5a82)).
Float target 0.05, low-water 0.01, exact funding above 0.01.

| Purchase | HTTP | Funding by the limiter | Settlement on chain |
| --- | --- | --- | --- |
| `vet-service?slug=treza`, 0.005 | 200, review delivered | refill 0.05, [`0x7377411c…0832`](https://basescan.org/tx/0x7377411cea4feab76798d6a1f248b0cc76a9b9f6244ca95631f5f5ef6aa50832), block 51711184 | [`0x7cc1e86d…f7ab`](https://basescan.org/tx/0x7cc1e86d7f6c677a8a94fb9425bd04a1b052052dcd9ad162c38f4f2a422ff7ab), block 51711193: agent → `0x8AEE…01a0`, 5000 |
| `vet-service?slug=venice`, 0.005 | 404, "service not found" | none: paid from the float | **none** — not delivered, not charged |
| `vet-shortlist?limit=3`, 0.02 | 200, shortlist delivered | exact 0.02, [`0xee2ccf70…a282`](https://basescan.org/tx/0xee2ccf7092e41945eb834758ffd10e397500d4db77684807c5cf2f653b7aa282), block 51711206 | [`0x05e11d6d…891a`](https://basescan.org/tx/0x05e11d6de7592cf6251bfa563597792970faded03fe00e1ef7ddb9655c3a891a), block 51711215: agent → `0x8AEE…01a0`, 20000 |
| Float returned | — | — | [`0x8c98edd2…0cf4`](https://basescan.org/tx/0x8c98edd20e857dff63a787d0a94bf7eed8bd203567214d2afa779ceb77970cf4): agent → owner, 45000 |
| Revoke (Trezor) | — | — | [`0x8f412244…32ff`](https://basescan.org/tx/0x8f412244a37cc5671bcf388c6eaac2ec87d798b174eeaed34f98256f776932ff): allowance 0 |

Both settlements were submitted by Bankr's facilitator `0x4a15…a584` to the payTo
contract, which pulled the USDC with the agent's signed authorization. The owner
lost exactly 0.025 (6.526336 → 6.501336), the price of the two delivered calls.
Every x402 payment was signed by the agent key; the Trezor signed only the grant
and the revoke. That is the lane working as designed.

**The script reported 3 failures; none was real.** It took the settlement hash
from the x402 client's result, and Bankr returns no settlement header
(`paymentResponse: null`), so two paid calls looked unsettled; the 404 was counted
as a failure too. Settlement is now read from the chain — a USDC `Transfer` from
the agent to `payTo` of exactly the price, mined after the payment was sent — and
four outcomes are distinguished: delivered and paid, and not delivered and not
charged, pass; charged without delivery, or delivery without a settlement, fail.
Rehearsed on a fork with a stub that behaves like Bankr: all four purchases
classified correctly; with settlement suppressed, every delivered call fails.

Not shown by this run: a *paid* purchase from the float with no refill. The
venice call exercised that path but was not charged. The mechanism is the same as
the first payment, which also came out of the float rather than an exact amount.
The fixed script targets treza twice to show it on the next run.

## T6 — a Bitrefill purchase through x402, on Base mainnet, 24 September

**Status: PASS, verified from the chain.** `t6-bitrefill.sh grant 0.10`, then
`buy hediyen-kart-all-access-turkey 1` (Hediyen Kart All Access Turkey, 1 TRY,
0.02 USDC), then `revoke`, on the T4 limiter. The owner chose Bitrefill's x402
route for this instead of its MCP server; the owner confirmed the product and
price before the order existed.

| Step | Block | Signed by | Transaction | On chain |
| --- | --- | --- | --- | --- |
| Grant | 51713080 | **Trezor** | [`0x9884cc11…d389`](https://basescan.org/tx/0x9884cc11b82210315546e255a23e0f1b662494680cc5f69b5f46746e5fb7d389) | `Approval(owner, limiter, 100000)` |
| Funding | 51713109 | agent | [`0x46109346…440b`](https://basescan.org/tx/0x46109346573a08d7e791b2f0d82b9ce9419eec071cd8a1fc5b0043a7edce440b) | `spend`: USDC owner → agent, 20000 |
| Payment | 51713115 | Bitrefill's facilitator, with the agent's authorization | [`0x29a761fc…5dcd`](https://basescan.org/tx/0x29a761fcb79eedba5a81d026146c0adbd2bb1dac36f758f09028d6cd97f35dcd) | USDC agent → `0x480C…846A` (Bitrefill's published x402 address), 20000 |
| Revoke | 51713136 | **Trezor** | [`0x23b319f4…671f`](https://basescan.org/tx/0x23b319f42147ea8755bc410eb9ddd4149f4fd71b5b7a536dfb169197bd4b671f) | `Approval(owner, limiter, 0)` |

Invoice `4d730b74-9a2f-4f27-85cb-82221a75f097`, created 02:05 UTC, delivered
02:06 UTC, 35 seconds after the order. Owner 6.501336 → 6.481336 USDC; agent 0;
`usedRef(keccak("bitrefill:<invoice>"))` true, so the same invoice cannot be
funded twice.

The purchase log holds the invoice id, product, amount, payment method, the two
transaction hashes and the times — nothing from the redemption. No file under
`~/.sign402-trezor-poc` mentions a redemption, serial or barcode. The code was
printed in the owner's terminal only.

What this run settled that none before it could: the order-creation, pay-route
and delivery shapes of Bitrefill's x402 API, which had not been exercised — the
pay route's recipient matched the address Bitrefill publishes, its amount matched
the confirmed price, and `redemption_info` came back with the invoice status under
the agent's sign-in token without a second signature.

Two things the owner met on the way, both fixed or explained: the Claude Code
terminal panel cannot execute files under `~/Documents` (macOS privacy; the
macOS Terminal can), and the pre-grant read of the limiter sat silent for about
fifteen seconds on the public endpoint — it now says what it is doing first.


## Phase 1 — the gateway deploys limiters, 24 September

**Status: PASS on a local fork of Base mainnet; not yet run on mainnet.**

`AllowanceService.setup` against `anvil --fork-url https://mainnet.base.org`, with an
anvil test key as the operator's gas key and the owner's real Trezor address as
owner:

- `setup 100 10 30`: agent key created and stored encrypted, agent funded by the
  service's own quote, limiter deployed and verified in 4.4 s; status read from
  the chain ("waiting for a grant from your Trezor", owner 6.481336 USDC).
- The same caps again deployed nothing; new caps (`50 5 7`) deployed a second
  limiter and marked the first `SUPERSEDED`; an unlisted Telegram user was refused.
- `export_artifact.py --check --onchain` on both: each runs exactly the tested
  code, immutables masked. Owner and guardian read back as configured.

Two defects the rehearsal found in the new code, fixed before this record:

1. The gateway's JSON-RPC client folds every node error into "request failed", so
   a refused deployment was reported as "Base RPC is not answering" and retried.
   The lane now has its own client that keeps the node's message: refusals are
   named and not retried, transport failures are retried, and an "already known"
   broadcast counts as sent.
2. The agent was topped up to a fixed 0.0002 ETH. The fork's node quotes a 1 gwei
   priority fee (mainnet quotes 0.001 gwei), the deployment's maximum cost
   exceeded it, and the node refused it. The service now quotes the deployment
   first and funds to twice its cost, with a 0.002 ETH ceiling.

Sourcify's API accepted the publication request for the T4 limiter (409
`already_verified`, exact matches), confirming the request shape; publication of a
new deployment is first exercised on mainnet.

Gateway suite: 1242/1242 (spending-memory at the pinned 443743e).


## Phase 2 — grant, revoke and pause through the whole chain, 24 September

**Status: PASS on a local fork of Base mainnet; not yet run with the device.**

One run through every real component but the device: the gateway's
`AllowanceService`, the broker over loopback HTTP with its store, a companion
enrolled for the owner, the sidecar service with its chain checks, and the fork.
The device was a test key signing with the fork's real nonce and fees — the one
stand-in.

| Step | Result |
| --- | --- |
| `setup 100 10 30` | limiter deployed and verified for the owner |
| `grant 250` | job through broker and companion; sidecar check passed; signed; gateway checked the bytes, broadcast, allowance read back 250 USDC: **done** |
| grant to a limiter owned by another address (the T4 limiter) | **refused on the owner's computer before the device** (`limiter_invalid`); the device was not asked; Telegram text names it as a possible attack |
| `revoke` | approve of 0 signed and sent: allowance 0, **done** |
| `pause` | guardian funded from the operator's gas key, `pause()` sent, limiter reads paused and 0 left today; no device |

Device prompts in the whole run: two — the grant and the revoke.

Unit coverage: gateway 1257/1257 (42 allowance tests: signed-approve check,
every refusal before the device, the owner's-computer errors named, transient
failures kept waiting, a lost broadcast offered again and landing once, an
owner without gas, a reverted approve, revoke of a superseded limiter, pause);
sidecar 361/361 (15 device-path tests and the broker migration). Removing the
gateway's check of the signed bytes, or its check that the paired Trezor is the
owner on file, or the sidecar's pre-device limiter check, each fails its tests.


## Phase 3 — purchases on the lane, 24 September

**Status: PASS in tests and on a local fork of Base mainnet; Bitrefill sign-in and
quote live; not yet a live purchase through the gateway.**

**x402 on a fork.** The service deployed a limiter for the owner's real Trezor
address (6.48 USDC on the fork), the allowance was set by impersonation (the grant
path is phase 2's), and a stand-in facilitator moved the agent's USDC to the
seller as a real one does:

| Purchase | Funding | Settlement |
| --- | --- | --- |
| 0.005, empty float | refill to 0.20 | found |
| 0.005 again, same seller | float, no transaction | found — a different transfer from the first |
| 0.12 | exactly 0.12 | found |

Owner −0.32 (0.20 refill + 0.12), agent float 0.19, limiter `spentToday` 0.32,
three settlements counted.

**Bitrefill, live, free.** The gateway's own Sign-In-With-X with a throwaway key
got a token from `api.bitrefill.com/x402`; the quote for the T6 card read 0.02
USDC; search returned products. The sign-in message equals the skill's
`siwx_build_message.js` output byte for byte.

**Found by the tests, fixed before any run:** a second identical micro-payment
matched the first's settlement, because the search window can start at the block
where the first landed. Counted settlements are now stored and excluded.

Mutations: without settlement de-duplication the float and restart tests fail;
deciding the lane after the seller and owner are asked fails the refusal test.
Gateway suite 1283/1283.


## Phase 4 — Telegram commands, 24 September

**Status: PASS in tests; not yet run in Telegram.**

Plugin: every command maps to exactly one gateway action with the parsed
arguments and the user's own token; malformed arguments print usage and call
nothing; a quote ends with `/allowance_buy <code>`; a refusal reaches the user in
the gateway's words; a buy-tool purchase refused by the lane shows its reason.
The tests had to authorise the test user through `SIGN402_TELEGRAM_ALLOWED_USERS`
exactly as production does — the plugin drops everyone else before any command.

Gateway: "not enabled for this account" is a 400, not a 403 — the plugin reads
401/403 as its own credentials failing and would have shown the wrong reason;
`/limits` appends the limiter's status for users on the lane.

Plugin suite 283/283, gateway suite 1284/1284.


## Phase 5 — a real theft and the watcher, 24 September

**Status: PASS on a local fork of Base mainnet; not yet running in production.**

A limiter deployed by the service for the owner's real Trezor address, 1 USDC
allowed (by impersonation), and the watcher on the fork:

| Step | Result |
| --- | --- |
| an ordinary x402 purchase | refill to the float; the watcher reported "0.2 USDC moved from your Trezor to your agent"; not paused |
| the agent key, as a thief would use it, sends `spend(attacker, 0.25)` | 0.25 reached the attacker |
| the next watcher pass | **alarm, and a real guardian `pause()` on chain**; `paused()` reads true |
| the thief tries again | **refused by the contract** (`IsPaused`, `0x1309a563`); the attacker still holds 0.25 |

The loss was one per-purchase amount, as the design says it would be.

Unit coverage: eight watcher tests — a spend to the agent is reported, to anyone
else pauses, a burst pauses, no event is reported twice, agent outflows are
matched to settlements or returns or raised after the grace period, a settlement
counted late is not an alarm, a failed pause says to revoke now, and a failed
Telegram notice never logs the bot token. Removing rule 1 fails two of them.

## T8 — the lane in production, through Telegram, 24 September (in progress)

Deployed with `scripts/deploy-trezor-allowance.sh` (f381590, then fixes up to
7612ecb), owner-only. Gas funder `0x894C…dd4e`, guardian `0x9a29…C3Ef`.

| Step | Result | On chain |
| --- | --- | --- |
| `/allowance_setup 1 0.3 7` | limiter `0x4F35812DE41a76b24EEE61C630fCc5c85DcFa46B` | runtime code = artifact (immutables masked), Sourcify `exact_match`; owner `0xB80b…4558`, agent `0x7870…1b1D`, caps 1 / 0.3 USDC, expiry 2026-10-01 15:41 UTC |
| `/allowance_grant 1`, confirmed on the Trezor | granted | [`0xda1bbd0d…a4ad`](https://basescan.org/tx/0xda1bbd0dff501f36ccb893b94c20ffda29dc127642165fcbd49390727921a4ad), block 51737723: `approve(limiter, 1000000)` from the Trezor address |
| `buy crypto news` (Otto AI, 0.001) | delivered, "paid from your Trezor allowance (refill 0.2 USDC)" | refill [`0xffaaf82e…3756`](https://basescan.org/tx/0xffaaf82e0aee3d0f5d2fa37ce2136bc8df5e50d16e2caf9e88d05228a4553756), block 51737981: `Spent` 0.2 to the agent; settlement [`0x946ceb1a…676f`](https://basescan.org/tx/0x946ceb1ab7077fd6787114d2ce3e5aa5c1f2045fe758edb89cb5e0d409be676f), block 51737984: agent → `0x0e84…b808`, 1000 |
| Watcher | reported the refill after the 2feb739 fix | — |
| Bitrefill x402 | paused by the owner at the iMessage approval for the new merchant `bitrefill:x402`; nothing paid | — |
| Revoke | not yet | — |

Found and fixed during the run: an empty gas wallet surfaced as Base's raw
`OutOfFunds` (056d00c); the grant reply named a command that does not exist, and
revoke did not return the float (254cef8); the production RPC (Alchemy free tier)
allows 10-block `eth_getLogs` and its HTTP 400 read as an outage, so the watcher
saw nothing (2feb739); both Bitrefill routes shared one merchant name, so the
x402 address read as payout drift (7612ecb).
