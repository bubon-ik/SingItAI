# Verification — 2026-09-17

## Local

- Pinned `@x402/svm@2.14.0`, `@x402/core@2.14.0`, `@solana/kit@5.5.1` install successfully with lifecycle scripts disabled.
- Ed25519 SIWX signatures verify against the generated wallet; each authentication flow uses a fresh nonce.
- The actual SDK creates the correct USDC TransferChecked instruction and leaves only the merchant fee-payer signature empty.
- Approval binds the payer, exact requirements, resource and expiry. Changed terms and expired quotes fail before submission.
- SQLite prevents duplicate quote execution and concurrent unresolved payments for the same wallet.
- Lost responses remain unresolved across restarts. Reconciliation matches the complete transaction message, not just an amount.
- Empty Venice credit does not cause an automatic top-up.
- End-to-end local flow exercises the actual client, SDK, signatures, payment receipt, chain proof, merchant balance and chat response using mocked network responses.

## Live, no payment sent

- Generated a new dedicated, unfunded mainnet wallet. No existing project keys were accessed.
- Solana mainnet RPC returned 0 lamports and no funded USDC ATA for that wallet.
- Venice accepted the client's Solana SIWX authentication and returned `canConsume: false`, `balanceUsd: 0`, `minimumTopUpUsd: 5`.
- Venice returned an x402 v2 exact-payment option for Solana mainnet USDC: 5,000,000 atomic units, with a sponsored fee payer.
- No signed payment was submitted. No chat request consumed real credit.

## Pending before calling the real flow complete

1. Fund the dedicated wallet with the required USDC on Solana.
2. Obtain a fresh quote and explicit user approval of its exact conditions.
3. Send one payment; verify the chain receipt and Venice credit.
4. With approval for a selected model/request, obtain the first real text response.

The live payment receipt format and successful credit/chat path remain unverified until this funded run. Unexpected responses preserve an unresolved attempt rather than triggering a second charge.
