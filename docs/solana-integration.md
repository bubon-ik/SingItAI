# Solana integration — implementation plan

Baseline: SingItAI/main `f39959059922b693f14c2a3e9bec97c87881e07b`.

## Completed: repository preparation

- Imported the committed main branch, preserving Git history.
- Added the existing Venice/Solana client as `solana-x402-service/`.
- Kept original project modifications, credentials and runtime state out of the import.
- Removed the clone's remote: no push target or production deployment is configured.
- Added Node 24 CI for the Solana module. No funded payment has been made.

## 1. Managed wallets and read-only Telegram flow

Primary files: `sign402-gateway/sign402_gateway/user_wallets.py`,
`sign402-gateway/sign402_gateway/server.py`,
`hermes-plugins/sign402-wallet/client.py`,
`hermes-plugins/sign402-wallet/__init__.py`.

The current `user_wallets` table uses `telegram_user_id` as its primary key.
Add a backward-compatible representation for a user's Base and Solana wallets;
make network selection explicit in lookup, creation, signing and balance paths.
Preserve existing Base callers and tokens. Generate per-user Ed25519 keys and
store them encrypted using the gateway's wallet encryption boundary. Never
return a private key to the agent or reuse the CLI prototype wallet for users.

Initial Telegram acceptance scenario: `/wallet solana` creates or shows the
same user's Solana wallet; `/balance solana` returns SOL and native USDC.
Existing Base commands retain their current behavior. Unsupported operations
on Solana return a clear response without falling back to another chain.

Tests: existing-wallet migration, independent wallets per user/network,
idempotent and concurrent creation, user isolation, encryption, correct
address casing and native-USDC mint, authorization and Base regression checks.

## 2. Venice adapter and approved payment

Primary integration points: `sign402-gateway/sign402_gateway/venice_chat.py`,
`sign402-gateway/sign402_gateway/server.py`, `solana-x402-service/src/index.mjs`.

Bridge the Python gateway to the Node client with a private authenticated
interface or subprocess protocol. Keys and signed payloads must never appear
in arguments, agent context, logs or responses. Bind each request and payment
to its authenticated user, network, wallet, exact quote and operation ID.

Connect quote presentation and user approval before submission. Keep daily
spending accounting separate by asset/network and include Venice credit
consumption in its explicit chat policy. Preserve durable uncertain-payment
handling across restarts. Do not automatically top up or retry an uncertain
payment. Confirm chain payment and Venice credit separately.

Tests: denied/expired/changed approval, wrong user/network, budget rejection,
lost response and restart, duplicate submission, provider errors, confirmed
payment with delayed credit, Base Venice regression checks.

## 3. Isolated Telegram run and one mainnet payment

Use a separate bot configuration and databases. First exercise wallet and
balance without funds. Obtain a fresh Venice quote, fund the correct managed
wallet, and get explicit approval before one real payment. Verify transaction,
Venice credit and a selected model's paid response through the agent.

The old CLI wallet remains a prototype wallet; funding it does not fund a
newly created per-user managed wallet.

## 4. Bitrefill

Verify live Solana payment support in the actual project MCP purchase route
before implementing it. Respect the repository's Bitrefill skill and exact
purchase-confirmation rules. Provider support is a prerequisite, not an
assumption. A working Venice payment does not establish Bitrefill support.

## Later

Own x402 endpoint for tokenized-stock execution, Jupiter and merchant-side
facilitator selection follow the working agent payment flow.
