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
