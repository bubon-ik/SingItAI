# SingIt Solana

Отдельный репозиторий для добавления Solana в Telegram-агента SingIt.
Основа: `SingItAI/main`, коммит `f39959059922b693f14c2a3e9bec97c87881e07b`.

## Текущее состояние

- Полный код агента перенесён из зафиксированной `main`.
- Клиент Venice/x402 для Solana mainnet находится в `solana-x402-service/`.
- **Telegram-агент пока работает с Base. Подключение Solana к агенту ещё впереди.**
- Реальные Solana-платежи и платные ответы Venice ещё не выполнялись.
- Репозиторий локальный; GitHub remote не настроен.

## Проверка Solana-модуля

Нужен Node.js 24+. Остальной проект сохраняет требования исходной версии ниже.

```sh
cd solana-x402-service
npm ci --ignore-scripts
npm test
npm run check
npm start -- --help
```

Инструкции по котировкам и оплате: [Solana service](solana-x402-service/README.md).
Следующие этапы и критерии готовности: [план интеграции](docs/solana-integration.md).

## Изоляция запуска

Перед запуском отдельного Telegram-агента задайте отдельные конфигурацию, токен
бота, ключ шифрования, базы кошельков и операций, порты и каталоги состояния.
Исходный gateway использует некоторые пути в домашнем каталоге по умолчанию;
они ещё не переопределены автоматически для этого репозитория. Нельзя запускать
его с настройками действующего Base-бота. Секреты и базы исходного проекта не
переносились. Локальный кошелёк Solana-прототипа хранится отдельно от gateway.

---

## Документация исходной версии SingIt

# SingIt

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
