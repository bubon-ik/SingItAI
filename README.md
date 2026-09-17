# SingIt Solana

A separate repository for adding Solana support to the SingIt Telegram agent.
Based on `SingItAI/main` at commit `f39959059922b693f14c2a3e9bec97c87881e07b`.

## Current status

- The full agent code has been imported from the committed `main` branch.
- The Venice/x402 client for Solana mainnet lives in `solana-x402-service/`.
- **The Telegram agent currently supports Base. Solana integration into the agent is still pending.**
- No real Solana payments or paid Venice model requests have been completed.
- Public repository: [bubon-ik/singit-solana](https://github.com/bubon-ik/singit-solana).

## Checking the Solana module

Requires Node.js 24+. The rest of the project retains the baseline requirements below.

```sh
cd solana-x402-service
npm ci --ignore-scripts
npm test
npm run check
npm start -- --help
```

Quote and payment instructions: [Solana service](solana-x402-service/README.md).
Next steps and acceptance criteria: [integration plan](docs/solana-integration.md).

## Isolated deployment

Before starting a separate Telegram agent, configure its own bot token,
encryption key, wallet and operation databases, ports, and runtime directories.
The original gateway defaults to some paths in the user's home directory;
this repository does not yet override those paths automatically. Do not start
this copy with the running Base bot's configuration. Secrets and databases from
the original project have not been imported. The prototype Solana wallet is
stored separately from the gateway.

## Existing SingIt capabilities

**Payments for AI agents, with spending limits and human approval.**

SingIt connects a Telegram assistant to managed wallets on Base. It can buy
gift cards and mobile top-ups, pay for x402 APIs, and fund LLM credits. The
payment gateway handles wallet keys, spending controls, approvals and receipts.

[Website](https://singitai.app) · [Telegram bot](https://t.me/SingIt0qk_bot) · [Documentation](docs/README.md)

[![Security gate](https://github.com/bubon-ik/SingItAI/actions/workflows/security-gate.yml/badge.svg?branch=main)](https://github.com/bubon-ik/SingItAI/actions/workflows/security-gate.yml)

## Features

- **Managed Base wallets:** create a wallet, check balances, set spending limits
  and withdraw through Telegram.
- **Bitrefill purchases:** browse products, review a quote, buy gift cards or
  mobile top-ups, and retrieve the result.
- **x402 payments:** pay for supported APIs using USDC on Base.
- **Payment policy:** use Spending Memory to allow, escalate or block supported
  purchases according to budgets and merchant history.
- **Separate approval channels:** confirm purchases through iMessage or WhatsApp
  when human approval is required.
- **LLM credits:** top up through Bankr. Optional integrations add Venice chat
  and paid onchain data queries through The Graph.

These are capabilities in the repository. Availability in a deployment depends
on its configuration and installed version; `main` is not automatically the
version running on the server. See [operations](docs/operations.md) for the
recorded deployment state.

## How payments work

For supported x402-tool and Bitrefill purchases, the gateway checks the quote
and spending limits before execution. With Spending Memory enabled, the policy
returns one of three decisions:

| Decision | Outcome |
| --- | --- |
| `PAY` | Proceed within the configured policy and limits. |
| `ESCALATE` | Ask the owner to approve the purchase. |
| `BLOCK` | Refuse the payment. |

In strict mode, covered purchases require human approval. An approval is bound
to the purchase terms; the payment must match the approved amount, asset and
recipient. LLM credit purchases, Venice chat and web search have separate
flows and are not covered by one universal approval policy.

The optional [Ledger integration](docs/ledger-v1.md) supports purchase consent
for a configured owner's GET x402 tools. The device signs the approval; the
gateway wallet signs the payment. Ledger Key Ring support for the wallet
master key is a separate, opt-in feature.

## Using the Telegram bot

Open the [bot](https://t.me/SingIt0qk_bot) and send `/start`.
Wallet commands run through the gateway without calling an LLM.

| Command | Purpose |
| --- | --- |
| `/wallet` | Create or show your Base wallet. |
| `/balance` | Check wallet balances. |
| `/limits` | View or change spending limits. |
| `/bitrefill` | Browse products and start a purchase. |
| `/last_purchase` | Check the most recent purchase. |
| `/withdraw` | Send funds to your own address. |
| `/connect_imessage` | Pair an iMessage approval channel. |
| `/connect_whatsapp` | Pair a WhatsApp approval channel. |
| `/llm_buy` | Buy LLM credits through Bankr. |

## Development setup

Use **Python 3.12**, **Node.js 22** and **Git** to match CI. The gateway package
supports Python 3.11 or later. Clone the full repository: the gateway imports
shared code from sibling directories.

```bash
git clone https://github.com/bubon-ik/SingItAI.git
cd SingItAI
python3.12 -m venv sign402-gateway/.venv
sign402-gateway/.venv/bin/python -m pip install -e ./sign402-gateway
```

Run the Python unit tests from the repository root:

```bash
(cd sign402-gateway && .venv/bin/python -m unittest discover -s tests)
(cd hermes-plugins/sign402-wallet && ../../sign402-gateway/.venv/bin/python -m unittest discover -s tests)
```

Run the Node unit tests:

```bash
(cd cdp-x402-service && npm ci --ignore-scripts && npm test)
(cd singit-risk-check && npm ci --ignore-scripts && npm test)
(cd tools/ledger-approve && npm ci --ignore-scripts && npm test)
```

The unit suites use test doubles for external services and hardware; no funded
wallet or Ledger device is needed. They cover payment limits, approval binding,
retries, authentication and provider integrations. CI also audits dependencies.

Running the connected service requires configuration beyond installing the
package: wallet encryption and API secrets, payment-provider credentials,
Hermes, and the chosen approval channel. Start with the
[environment reference](sign402-gateway/.env.example),
[Telegram plugin setup](hermes-plugins/sign402-wallet/README.md),
[CDP service setup](cdp-x402-service/README.md#setup) and
[operations runbook](docs/operations.md). For real Ledger use, follow the
[Ledger installation instructions](docs/ledger-v1.md).

## Repository structure

| Directory | Purpose |
| --- | --- |
| `sign402-gateway/` | Python gateway: wallets, payment policy, approvals, orders and APIs. |
| `hermes-plugins/sign402-wallet/` | Telegram wallet commands and purchase flows for Hermes. |
| `cdp-x402-service/` | Node.js payment and swap integration for Base through CDP and x402. |
| `tools/ledger-approve/` | Local Ledger purchase-approval client. |
| `singit-risk-check/` | SINGIT-paid x402 endpoint for payment-requirement risk analysis. |
| `website/` | Public website. |
| `docs/` | Operating instructions, integration guides and verification records. |
| `sign402-bridge/`, `payment-executor/`, `live-demo/` | Legacy integrations and shared utilities still imported by the gateway. |

[Spending Memory](https://github.com/bubon-ik/spending-memory) is maintained in a
separate repository. It provides the reusable payment-policy library and Graph
query adapter; this gateway installs a pinned revision.

## Security and custody

Managed wallets are **custodial**. Private keys are encrypted at rest and used
by the gateway, rather than supplied to the agent. Encryption and approval
checks do not remove the need to trust the server and its approval adapters.

Keep credentials and runtime databases out of Git. Back up wallet and order
state before updating a deployment. Read the
[security model](sign402-gateway/SECURITY.md) for trust boundaries and controls,
and the [recovery runbook](docs/recovery-runbook.md) for backup and recovery.

## Further reading

- [Documentation index](docs/README.md)
- [Deployment and operations](docs/operations.md)
- [Decision API](docs/decide-public-endpoint.md) and [OpenAPI schema](sign402-gateway/docs/decide-openapi.json)
- [Ledger integration and scope](docs/ledger-v1.md)
- [Telegram / Graph / Ledger demo](docs/telegram-graph-ledger-demo.md)
- [Dated verification results](docs/checks.md)
- [ETHOnline 2026 submission](docs/ethonline-submission.md) — historical event scope and evidence

## License

[MIT](LICENSE).
