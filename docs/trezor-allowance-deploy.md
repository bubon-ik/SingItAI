# Deploying the Trezor allowance lane — phase 6

Design: [trezor-allowance-v1.md](trezor-allowance-v1.md). Checks so far:
[trezor-allowance-checks.md](trezor-allowance-checks.md). Production layout and
the general deploy rules: [operations.md](operations.md) — this document adds to
them and does not replace them.

Everything here is behind `SIGN402_ALLOWANCE_ENABLED` and the owner's Telegram ID.
With the flag off, the deployed gateway behaves exactly as `main` does. Every step
is the owner's or needs the owner's approval: it changes production, spends ETH,
or touches the device.

## The script

[`scripts/deploy-trezor-allowance.sh`](../scripts/deploy-trezor-allowance.sh) does
steps 0–6 below in order, stops at the first thing that is not as expected, and
never prints a secret. It needs the owner's terminal once, for sudo:

```bash
scp scripts/deploy-trezor-allowance.sh hermes@164.68.104.44:
ssh -t hermes@164.68.104.44 'bash ~/deploy-trezor-allowance.sh <commit>'
```

It ends with the gas funder's address to fund. The broker it installs is the one
already running since August (`~/apps/sign402-trezor`, same state and token),
moved to the new code under a user unit `sign402-trezor-broker`; the old
remote agent keeps running untouched.

## 0. Before anything changes

On the VPS (`hermes@164.68.104.44`, checkout `~/apps/sign402`):

1. Record the running commit: `git -C ~/apps/sign402 rev-parse HEAD`. On
   September 24, 2026 it was `5be0d7a` on `release/exa-auto-search-20260922`,
   the singit-solana work, with a clean tree; that branch is merged into this one.
   Anything newer on the server must be merged here first, never overwritten.
2. Back up runtime state as [recovery-runbook.md](recovery-runbook.md) says.
3. `git status --short` must be empty; if it is not, save the diff and merge it
   here before switching.

Rollback at any point: check out the recorded commit, reinstall its dependencies,
restart both units. Allowance state lives in its own database
(`~/.sign402/allowance.db`) and is not read by the old code.

## 1. Code

Deploy a reviewed commit of `trezor-local-sidecar` (it contains `main`, which
contains the production fixes). Install the gateway with the service's
interpreter, including the pinned `spending-memory@443743e` — `git pull` alone does
not update it. Then:

```bash
cd ~/apps/sign402/agent-allowance && python3 script/export_artifact.py --check
```

must say the artifacts match the source (it needs `forge`; skip on the VPS if
absent, it passed on the branch).

## 2. Operator keys

On the VPS, with the gateway's master key available:

```bash
cd ~/apps/sign402/sign402-gateway && .venv/bin/python -m sign402_gateway.agent_allowance new-operator-key
```

twice — once for the gas funder, once for the guardian. Each prints an address and
an encrypted blob; the plain key is never shown. The owner sends **0.003 ETH on
Base** to the gas funder's address. The guardian and every agent are funded from
it automatically; one deployment costs about 0.00001 ETH at today's prices.

## 3. The broker

The broker already exists in `trezor-sidecar/`. Run it on the VPS as its own
process, loopback only, with a fresh internal token of 32+ characters
(`SIGN402_TREZOR_BROKER_ENABLED=1`, `SIGN402_TREZOR_BROKER_INTERNAL_TOKEN`),
as its README describes. An existing broker database is migrated on start to
accept `usdc_approve`, keeping every row.

## 4. Gateway environment

Add to the gateway's environment (values are the owner's; none goes into git):

```text
SIGN402_ALLOWANCE_ENABLED=1
SIGN402_ALLOWANCE_OWNERS=<owner's Telegram ID>:0xB80b5Ca13583fB7E0236db4bD8834B9035654558
SIGN402_ALLOWANCE_GAS_FUNDER_KEY=<encrypted blob from step 2>
SIGN402_ALLOWANCE_GUARDIAN_KEY=<encrypted blob from step 2>
SIGN402_ALLOWANCE_BROKER_URL=http://127.0.0.1:8122
SIGN402_ALLOWANCE_BROKER_TOKEN=<the broker's internal token>
```

Optional, with defaults: `SIGN402_ALLOWANCE_RPC_URL` (a private Base RPC is
better than the public one), `SIGN402_ALLOWANCE_MAX_DAILY_USDC` (100),
`_MAX_PER_PURCHASE_USDC` (25), `_MAX_DAYS` (90), `_MAX_GRANT_USDC` (300),
`_FLOAT_TARGET_USDC` (0.20), `_FLOAT_LOW_USDC` (0.05), `_EXACT_ABOVE_USDC` (0.05).

Restart the gateway (`ssh -t` for sudo) and check `/health` lists
`/agent/allowance/*`. A misconfigured lane is logged and left off; the rest of the
gateway keeps working.

## 5. The watcher

A new user unit running, with the gateway's environment:

```bash
.venv/bin/python -m sign402_gateway.allowance_watcher
```

plus `SIGN402_ALLOWANCE_TELEGRAM_BOT_TOKEN` (the bot's token, to message the
owner directly) and optionally `SIGN402_ALLOWANCE_WATCH_INTERVAL_SECONDS` (30),
`SIGN402_ALLOWANCE_WATCH_MAX_SPENDS_PER_HOUR` (20).

## 6. The bot

Restart `hermes-gateway` (user unit) so the plugin's `/allowance_*` commands load.

## 7. The owner's computer

With the Trezor, Trezor Suite (MCP on) and the sidecar running, enrol the
companion through the broker (an SSH local forward of 8122 is enough, as in the
sidecar README), then run the companion. It needs to run only while granting or
revoking.

## 8. The live run (T8)

In Telegram, small amounts, each result read back on Base:

1. `/allowance_setup 1 0.3 7` — the limiter is deployed; open its link on the
   phone and check owner, agent, guardian and caps.
2. `/allowance_grant 1` — the Trezor shows an approve of 1 USDC to that limiter;
   confirm; `/allowance` shows it granted. **Photograph the screens** (closes T2).
3. An x402 tool from the menu (a cent or less): paid from the lane, settlement
   linked.
4. `/allowance_bitrefill hediyen TR`, `/allowance_quote hediyen-kart-all-access-turkey 1`,
   `/allowance_buy <code>`, then `/last_purchase` — the code, once.
5. `/allowance` — the watcher's reports of each spend.
6. `/allowance_revoke` — the Trezor shows an approve of 0; `/allowance` shows 0.

Record every transaction in the checks file, as for T4–T7.
