# Telegram → Ledger → The Graph demo

The private `/graph_demo` command connects the existing Telegram bot to a local
Mac service. A fresh WETH/USDC query receives The Graph's x402 quote, asks the
connected Ledger for readable EIP-191 consent, verifies the signature, pays
0.01 USDC using the operator gateway wallet, and returns the price, indexed
block and BaseScan transaction link to the same Telegram chat.

The command is restricted to the configured owner's private chat. Other users,
groups and existing wallet/chat commands keep their current behavior. The
production payment gateway and existing Trezor configuration are not replaced.
Only the private command helper and a small dispatch hook are installed in the
running Hermes bot.

## Recording

1. Unlock the configured Ledger and open its Ethereum application.
2. Send `/graph_demo` to the existing bot from the owner's private chat.
3. Review `The Graph - WETH price`, `0.01 USDC on Base` and the recipient on
   the physical device, then approve the matching purchase.
4. The bot returns WETH price data, its indexed block, payment verification
   status and a BaseScan link. A temporary receipt RPC failure is explicitly
   reported as pending rather than inventing a successful verification.
5. Repeat `/graph_demo` within the default 300-second cache lifetime. It returns
   the journal's answer at zero cost, without another hardware prompt.
6. `/graph_demo status` reads the saved operation without starting a query or
   payment. It can show a historical reading after the cache has expired.

The Ledger signs off-chain consent. The existing gateway wallet submits the
x402 payment. This is not hardware custody of the spending wallet, ERC-7730
Clear Signing, or a production rollout of Ledger approval to all purchases.

## Local service and private connection

Use the configured project Python environment, installed Ledger Node SDKs,
existing CDP configuration and wallet encryption key. Start the Mac service:

```bash
payment-executor/.venv/bin/python sign402-gateway/scripts/graph-telegram-demo.py \
  --owner <Telegram-user-id> --approver <Ledger-public-address>
```

The service binds only `127.0.0.1:8117`. An SSH reverse forward connects the
bot host's `127.0.0.1:8107` to it. All operations require an independent bridge
token stored in ignored, private state. No wallet secrets or Ledger signatures
are transferred to the bot host. Keep the Mac service, SSH connection and
device available during recording.

`python3 scripts/install-graph-demo-plugin.py --owner <Telegram-user-id>` is the
project-specific installer for the configured bot host and explicit owner.
It backs up the plugin and Hermes environment,
patches only the private command hook, installs the helper and restarts Hermes.
It transfers the bridge token through SSH stdin, never shell arguments. It does
not deploy the production gateway. Review its host/owner before reuse elsewhere.

## Payment and retry guarantees

The Graph adapter retains its real spending policy, query fingerprint, payment
claim and persistent answer cache. The additional hardware gate commits the
exact GraphQL request hash, resource, recipient, amount, owner, policy decision
and expiry. The signed message is reconstructed and verified with the existing
Ledger verifier; the quote and policy are rechecked after device interaction.

Operations are encrypted in `.graph-live/telegram-demo/operations.sqlite3`.
A durable transition precedes payment submission. An exclusive, permanent
`payment-attempt.json` allows only one paid query in this recording state,
including across process restarts and Telegram message retries. An ambiguous
payment blocks another operation; neither its marker nor state should be reset
to retry. The daily query budget is additionally capped at 0.01 USDC.

The first merchant trust entry is imported from a previously verified real
Graph settlement; it is not fabricated. The new hardware gate is still required
for the fresh payment. A cached answer needs neither a payment nor a signature.
The purchase record contains only the transaction ID, product, amount, payment
method and timestamp.

Validation before rollout: 71 focused gateway/data/signature checks, including
10 combined-flow checks, and 310 Hermes plugin checks. The combined-flow checks
use real policy/storage and cryptographic signature verification with test
network, signer and payer callbacks. They do not substitute for the physical
Telegram/hardware/mainnet demonstration.

The physical demonstration subsequently completed on 13 September: one
Ledger-approved 0.01 USDC query and a second Telegram request served free from
the cache. The transaction, returned data and verification scope are recorded
in [G7 of the verification report](checks.md#g7--telegram--ledger--the-graph-13-september).
