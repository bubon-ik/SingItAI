# T4 runbook — the allowance on Base mainnet, for cents

Design: [trezor-allowance-v1.md](trezor-allowance-v1.md). The owner chose to skip
Base Sepolia (T3b) and run this on mainnet directly: the worst case is bounded by
the grant, which here is **1.00 USDC**, and the one real purchase moves **0.30
USDC** from the Trezor address to the agent's own address — both the owner's.

Every transaction below is run by the owner. Passwords, keystore files and
private keys never go into a chat, an issue or this repository.

## One command

`agent-allowance/script/t4-mainnet.sh` runs every step below in order and
records the results in `~/.sign402-trezor-poc/t4-log.md`:

```bash
cd "/Users/mp/Documents/Berlin Hack/.worktrees/trezor-local-sidecar/agent-allowance" && ./script/t4-mainnet.sh
```

It stops for exactly the owner's actions: choosing and typing the two key
passwords, sending 0.0002 ETH to the agent from Trezor Suite (it waits for the
funds to arrive), checking the published source on the phone, two confirmations
on the Trezor, and a `y` before the one real purchase. It is resumable — rerun
it after any stop and it skips what is done, from the addresses and hashes it
keeps in `~/.sign402-trezor-poc/t4.env`. Neither file holds a password or key.

The rest of this document is what the script does, step by step, for running
it by hand or reading what happened.

## Test policy

| Parameter | Value | Why |
| --- | --- | --- |
| Grant (`approve`) | 1.00 USDC | Worst case of the whole test |
| Daily cap | 0.50 USDC | Below the grant, so the daily cap is the binding limit |
| Per purchase | 0.30 USDC | One real purchase fits; a second does not fit the day |
| Expiry | now + 7 days | Long enough to finish, short enough to lapse on its own |
| Payee | the agent's own address | The money stays the owner's |

Refusals are checked with `cast call`: an `eth_call` that runs the transaction
against the live chain and sends nothing. Only four transactions are real:
deploy, grant, one purchase, revoke.

Error selectors, for reading `cast call` output (`execution reverted, data: "0x…"`):

| Selector | Error |
| --- | --- |
| `0x0d9ab13f` | `NotAgent()` |
| `0xef6d0f02` | `NotGuardian()` |
| `0x1309a563` | `IsPaused()` |
| `0x203d82d8` | `Expired()` |
| `0xb387a238` | `InvalidPayee()` |
| `0x1f2a2005` | `ZeroAmount()` |
| `0xe97d945b` | `InvalidRef()` |
| `0xac0292f0` | `RefUsed()` |
| `0x80b5d3c8` | `PerPurchaseCapExceeded()` |
| `0xcc70389d` | `DailyCapExceeded()` |

## Two terminals

**Terminal A — the agent side**, in the contract directory:

```bash
cd "/Users/mp/Documents/Berlin Hack/.worktrees/trezor-local-sidecar/agent-allowance"
export RPC=https://mainnet.base.org
export USDC=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
export OWNER=0xB80b5Ca13583fB7E0236db4bD8834B9035654558
```

**Terminal B — the Trezor side**, with Trezor Suite open, MCP on, device unlocked:

```bash
cd "/Users/mp/Documents/Berlin Hack/.worktrees/trezor-local-sidecar/trezor-sidecar"
set -a && source "$HOME/.config/sign402-trezor-poc/sidecar.env" && set +a
```

`SIGN402_TREZOR_POC_ENABLED=1` and `SIGN402_TREZOR_POC_MAX_USD` of at least
`1.00` must be set in that file. `grant` refuses anything above the maximum.

## 1. Two hot keys (A)

```bash
mkdir -p ~/.foundry/keystores
cast wallet new ~/.foundry/keystores sign402-agent
cast wallet new ~/.foundry/keystores sign402-guardian
```

Each asks for a password and prints an address. Then:

```bash
export AGENT=0x…      # sign402-agent
export GUARDIAN=0x…   # sign402-guardian
```

The agent key will hold only gas. The guardian key needs no funds for this test.

## 2. Gas for the agent

From Trezor Suite, send **0.0002 ETH on Base** to `$AGENT`. The Trezor address
keeps enough ETH for the grant and the revoke. Check:

```bash
cast balance $AGENT --rpc-url $RPC --ether
```

## 3. Deploy the limiter (A)

```bash
EXPIRY=$(( $(date +%s) + 7*86400 ))
forge create src/AgentAllowance.sol:AgentAllowance --rpc-url $RPC --account sign402-agent --broadcast --constructor-args $USDC $OWNER $AGENT $GUARDIAN 500000 300000 $EXPIRY
export LIMITER=0x…    # "Deployed to"
```

## 4. Publish the source (A)

Sourcify needs no API key:

```bash
forge verify-contract --chain 8453 --watch $LIMITER src/AgentAllowance.sol:AgentAllowance
```

Then, **on the phone, not on this Mac**, open
`https://base.blockscout.com/address/<LIMITER>?tab=contract` and check it is a
verified `AgentAllowance` whose `owner` is the Trezor address, `token` is USDC,
`agent` and `guardian` are the two new keys, `dailyCap` is 500000,
`perPurchaseCap` is 300000. This is the lookalike-limiter check.

## 5. Status and grant (B)

```bash
.venv/bin/python -m trezor_sidecar.allowance status 0x<LIMITER>
.venv/bin/python -m trezor_sidecar.allowance grant 0x<LIMITER> 1.00
```

`grant` refuses before touching the device if the limiter has no code, another
owner, another token, or is paused or expired. On the Trezor, confirm only if it
shows an approve, the limiter address in full, and 1 USDC. **Photograph every
screen** — this closes the three gaps left open in T2.

After signing, the command checks that the signed bytes are exactly that
`approve` from the Trezor address on Base, broadcasts, and waits for the receipt.

## 6. Refusals, free (A)

Nothing below is sent.

```bash
cast call $LIMITER "spend(address,uint256,bytes32)" $AGENT 310000 $(cast keccak t4-a) --from $AGENT --rpc-url $RPC
```
→ `0x80b5d3c8` `PerPurchaseCapExceeded`

```bash
cast call $LIMITER "spend(address,uint256,bytes32)" $AGENT 300000 $(cast keccak t4-a) --from $OWNER --rpc-url $RPC
```
→ `0x0d9ab13f` `NotAgent` — not even the owner can spend through it

```bash
cast call $LIMITER "spend(address,uint256,bytes32)" $OWNER 100000 $(cast keccak t4-a) --from $AGENT --rpc-url $RPC
```
→ `0xb387a238` `InvalidPayee`

```bash
cast call $LIMITER "pause()" --from $AGENT --rpc-url $RPC
```
→ `0xef6d0f02` `NotGuardian`

```bash
cast call $LIMITER "pause()" --from $GUARDIAN --rpc-url $RPC
```
→ no error: the guardian could pause. Not sent, so nothing is paused.

## 7. One real purchase (A)

0.30 USDC from the Trezor address to the agent's address, without the Trezor:

```bash
cast send $LIMITER "spend(address,uint256,bytes32)" $AGENT 300000 $(cast keccak t4-1) --account sign402-agent --rpc-url $RPC
cast call $USDC "balanceOf(address)(uint256)" $AGENT --rpc-url $RPC
```
→ `300000`

Then, free again:

```bash
cast call $LIMITER "spend(address,uint256,bytes32)" $AGENT 300000 $(cast keccak t4-2) --from $AGENT --rpc-url $RPC
```
→ `0xcc70389d` `DailyCapExceeded` — 0.30 + 0.30 is over 0.50

```bash
cast call $LIMITER "spend(address,uint256,bytes32)" $AGENT 100000 $(cast keccak t4-1) --from $AGENT --rpc-url $RPC
```
→ `0xac0292f0` `RefUsed` — the paid reference cannot pay again

```bash
cast call $LIMITER "spend(address,uint256,bytes32)" $AGENT 200000 $(cast keccak t4-2) --from $AGENT --rpc-url $RPC
```
→ no error: 0.20 fits the day and the allowance, so it would go through

## 8. Revoke (B), then prove it (A)

```bash
.venv/bin/python -m trezor_sidecar.allowance revoke 0x<LIMITER>
```

On the Trezor: an approve of 0 to the limiter. Then in A, the purchase that would
have gone through a moment ago:

```bash
cast call $LIMITER "spend(address,uint256,bytes32)" $AGENT 200000 $(cast keccak t4-2) --from $AGENT --rpc-url $RPC
```
→ `execution reverted: ERC20: transfer amount exceeds allowance`. The limiter's
checks pass; USDC itself refuses. That is the property the design rests on.

## 9. Return the 0.30 (A, optional)

```bash
cast send $USDC "transfer(address,uint256)" $OWNER 300000 --account sign402-agent --rpc-url $RPC
```

## What to record

For [trezor-allowance-checks.md](trezor-allowance-checks.md): the limiter address, the four transaction hashes, each `cast call`
result, the `status` output before and after, and the photographs of the device
screens. Addresses and hashes are public. Nothing else from this run is.
