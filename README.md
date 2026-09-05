# SingIt

An AI agent that buys things for you, where every payment needs a human to
approve it on a second device.

You talk to a Telegram bot. It holds a Base wallet for you, finds gift cards
and top-ups on Bitrefill, and quotes an exact price. Nothing is paid until you
approve that exact purchase from a separate channel — iMessage or WhatsApp —
so a compromised agent, a bad prompt, or a wrong number cannot spend your money
on its own.

Live at [singitai.app](https://singitai.app).

## How a purchase works

```text
Telegram          you pick a product and see an exact quote
   |
Gateway           checks your spending limits, builds the payment
   |
iMessage /        you approve this exact purchase on your phone
WhatsApp
   |
Base              USDC payment settles on Base Mainnet
   |
Bitrefill         the code is delivered
```

The approval step is bound to one purchase: product, amount and recipient are
committed before you are asked, and the gateway refuses anything that does not
match what you approved.

## Using the bot

| Command | What it does |
| --- | --- |
| `/start` | Open the menu |
| `/wallet` | Create or show your managed Base wallet |
| `/balance` | Wallet balance |
| `/bitrefill` | Browse the catalog and buy |
| `/limits` | View and change spending limits |
| `/withdraw` | Move funds out to your own address |
| `/last_purchase` | Receipt for the most recent order |
| `/connect_imessage`, `/connect_whatsapp` | Set up the approval channel |
| `/llm_buy` | Top up LLM credits through Bankr |

Buying runs through the menu: **Buy Bitrefill → Browse Catalog** or **Search
Products**, then a quote, then approval on your phone.

## What protects your money

- **A separate approval channel.** The agent proposes; you approve somewhere
  else. Telegram alone cannot spend.
- **Spending limits** enforced by the gateway, not by the agent.
- **Exact-purchase binding.** The approval covers one product at one price;
  a changed quote invalidates it.
- **The agent never sees a private key.** Wallet keys are encrypted at rest and
  used only by the gateway.

Honest limits: the wallet is custodial — keys live on the server, encrypted, so
a server compromise is a real risk. Hardware self-custody, where the key never
leaves a Trezor and you approve each payment on the device itself, is in
development on the `codex/trezor-local-sidecar` branch and is not part of the
service today.

## Components

| Path | Purpose |
| --- | --- |
| `sign402-gateway/` | The service. Wallets, limits, approvals, Bitrefill orders, payment execution |
| `hermes-plugins/sign402-wallet/` | Telegram command surface, loaded into Hermes |
| `cdp-x402-service/` | Base Mainnet payments and swaps via CDP and x402 |
| `website/` | The public site at singitai.app |
| `singit-risk-check/` | SINGIT-paid x402 endpoint for payment-requirement risk analysis |
| `demo-dashboard/` | Live trace view used for demos |

Kept for reference, not used in production: `sign402-bridge` and
`payment-executor` (Firefly hardware bridge and the Algorand lane from the
original hackathon build), `demo-resource-server`, `live-demo`.

## Development

Running the service, deploying, and the test commands are in
[docs/operations.md](docs/operations.md).

---

# ETHOnline 2026 — Continuity submission

This is a live custodial payment system that has existed for months, entered in
a Continuity track. So the first thing this section does is draw the line
between what was already here and what was built during the event, because the
tracks are judged on the second only.

## What was built during the event

Everything below is on the `ethonline` branch, dated 5 September 2026 or later.
The diff that contains all of it, and nothing else, is
[`x402Bnkr..ethonline`](https://github.com/bubon-ik/SingItAI/compare/x402Bnkr...ethonline).

| Track | What it does | Where |
| --- | --- | --- |
| Ledger | The wallet master key is decrypted through the Ledger Key Ring at start-up instead of sitting in plaintext in `/etc/sign402-gateway.env`, and the gateway refuses to boot if the ring cannot produce it | [`keyring.py`](https://github.com/bubon-ik/SingItAI/blob/2506927ec23512d646bab54ee1bd8ad8ffb4599e/sign402-gateway/sign402_gateway/keyring.py) · [commit](https://github.com/bubon-ik/SingItAI/commit/2506927ec23512d646bab54ee1bd8ad8ffb4599e) |
| Ledger | Required DX feedback, kept from the first command of phase 0 rather than written from memory afterwards | [`docs/ledger-dx-notes.md`](https://github.com/bubon-ik/SingItAI/blob/244a98fd3087d3e5a4138ad57b1c605e44cd98bf/docs/ledger-dx-notes.md) |
| Bazantic | `POST /v1/decide` and `GET /v1/journal`: a read-only HTTP surface over the spending policy, so an agent can ask whether a payment should happen without being able to make one happen | [`decide.py`](https://github.com/bubon-ik/SingItAI/blob/df9d39bae8540b1a22a925fbebf5a51149f6a3ed/sign402-gateway/sign402_gateway/decide.py) · [OpenAPI](https://github.com/bubon-ik/SingItAI/blob/b20cab05ba8021455ec5fed6f803b2a1c6f7fc68/sign402-gateway/docs/decide-openapi.json) |
| The Graph | An agent pays The Graph's x402 gateway per subgraph query out of a budget: the daily cap applies to a cent, the first payment escalates like any unknown merchant, a moved payout address blocks and warns the whole fleet, and a question already bought inside the cache window is answered by **reading the journal** instead of paying again | [`thegraph.py`](https://github.com/bubon-ik/spending-memory/blob/cbc0739b2842e92f7d7c698580d48284a7063960/spending_memory/adapters/thegraph.py) · [live demo](https://github.com/bubon-ik/spending-memory/blob/e4a79d3eda55a4fa6043108fc909248516415b36/demo/graph_queries.py) |
| The Graph | A real query, really bought: $0.01 USDC settled on Base mainnet to the address the live 402 named | [tx `0x57ddeebd…`](https://basescan.org/tx/0x57ddeebd74b89f8834c8627d7e1ad6878e44a651744da49fae677491ac2d7958) |
| The Graph · Bazantic | `SKILL.md`: what an agent must **do** about each verdict — the part no schema can carry, and the independent variable of the Bazantic experiment | [`SKILL.md`](https://github.com/bubon-ik/spending-memory/blob/cb0cdb3a791dbbba3e6d9ef1ad04c96d165c0633/skills/paying-for-data/SKILL.md) · [experiment](https://github.com/bubon-ik/SingItAI/blob/0cd35796610900182a634b481de87c634239e45c/docs/bazantic-experiment.md) |
| All | Phase 0 findings, including the two checks that failed and changed the plan | [`docs/checks.md`](https://github.com/bubon-ik/SingItAI/blob/244a98fd3087d3e5a4138ad57b1c605e44cd98bf/docs/checks.md) |

The links are pinned to commit hashes, not to the branch, so they keep pointing
at the reviewed code after the branch moves.

The Graph work lives in the **`spending-memory`** repository rather than this
one, and deliberately so: the track asks for reusable infrastructure rather than
an application, and a paid-query client that only works inside this gateway
would be the second thing. It is a `pip install`, MIT, with no dependency on
anything here — this gateway is one caller of it.

## What was already here

None of this is offered for judging. It is listed so that a judge reading the
branch can tell at a glance which parts of it are not new.

- **The gateway itself** — Telegram bot, custodial Base wallets, spending
  limits, Bitrefill orders, second-device approval. Months old, in production,
  handling real USDC.
- **The x402 client** and Base Mainnet settlement. Months old.
- **Spending Memory** (`spending-memory` v0.5.1) and its integration at the
  spend chokepoint — built days earlier for the Sibyl Labs hackathon, on the
  `spending-memory` branch, commits dated 3–4 September. The work in this
  submission sits on top of it: `/decide` is a read-only surface over that
  policy and adds no second decision point to the payment path.
- **Hardware approval through Trezor**, with video. Prior work, and deliberately
  not shown in the Ledger material: it is evidence that the architecture wanted
  a hardware root of trust before Ledger was in the picture, which is what makes
  the difference legible — same flow, different root of trust.

## Use of AI tools

Development was spec-driven and AI-assisted throughout, with Claude (Opus) as
the assistant.

The spec — [`docs/ethonline-spec.md`](docs/ethonline-spec.md) — was written
first and is committed unedited, including the parts the implementation later
departed from. It fixed the phase 0 checks, the acceptance criteria and the cut
lines before any code existed. `docs/checks.md` records what those checks
actually returned, two of them failures that changed the plan: `WALLET_CLI_MOCK`
does not give CI a device-free path, so the tests use a stand-in binary instead.

AI assistance covered implementation and test drafting under that spec.
Direction, the phase 0 findings, the threat model in `keyring.py` and every
decision about what not to build were the author's. The commit messages carry
the reasoning behind each choice and are the best record of it.

## Running the new work

```bash
cd sign402-gateway
pytest tests/test_ledger_keyring.py tests/test_decide_endpoint.py
```

The Ledger key ring is off by default (`SIGN402_LEDGER_KEYRING_ENABLED`), so an
unprovisioned checkout behaves exactly as it did before. The tests need no
device.
