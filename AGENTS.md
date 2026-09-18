# Project agent instructions

## Purchases through Bitrefill

- For every request to browse, price, order, or buy a product, invoke the repository skill `$bitrefill` first.
- Use the project `bitrefill` MCP server as the exclusive purchase-creation route for every item supported by the Bitrefill catalog, including gift cards, prepaid products, mobile top-ups, eSIMs, and bill payments.
- Do not bypass Bitrefill with a browser checkout, direct merchant API, CLI purchase, or another commerce provider when Bitrefill supports the requested item.
- If Bitrefill does not support the requested purchase, stop and tell the user. Do not buy through another route unless the user explicitly authorizes that exception.
- Before calling `buy-products`, show the exact product, denomination, total price, network/payment method, and any recipient details, then wait for explicit user confirmation. Never auto-approve `buy-products`.
- Do not commit API keys, wallet secrets, payment links, redemption codes, eSIM activation data, or other bearer-value data. Keep redemption data out of files and logs.
- Log only the non-secret purchase record required by the Bitrefill skill: invoice ID, product slug, amount, payment method, and timestamp.

## Solana development repository

- This repository is independent of `/Users/mp/Documents/Berlin Hack`; do not edit that original project.
- The imported baseline is SingItAI/main at f39959059922b693f14c2a3e9bec97c87881e07b.
- Solana client code lives in solana-x402-service; read its AGENTS.md before editing it.
- Use isolated bot credentials, encryption keys, databases, ports and runtime paths for parallel deployments. Replacing an existing deployment requires the user's explicit instruction, a private state/configuration backup and verification that existing wallet records remain intact. On September 18, 2026, the user explicitly chose to replace the existing VPS bot with this repository while retaining its state.
- Preserve existing Base behavior when adding Solana. Do not route unsupported Solana operations silently through Base.
- A shared CLI prototype wallet is not a per-user managed wallet. Do not use it as the default wallet for Telegram users.
- Real Venice payments require explicit approval of the exact fresh quote; tests must never send payments.

## Documentation language

- Write all README files and future README updates in English.

## Hackathon development history

- Keep HACKATHON.md current when completing a feature: include its commit or PR, verification and limitations.
- Distinguish imported SingIt functionality, new Solana work, and planned features. Preserve the published history and singit-base-baseline tag; do not rewrite dates.
- The imported baseline is dated September 17, 2026, not the competition start. Never present it as a September 14 snapshot.
