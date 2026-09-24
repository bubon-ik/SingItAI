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
