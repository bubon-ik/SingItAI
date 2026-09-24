# SingIt Solana — Venice x402

A standalone client for paying Venice with USDC on **Solana mainnet**. This is the first step toward adding Solana to SingIt. The existing `Berlin Hack` project remains unchanged.

The client can create a dedicated wallet, authenticate with Venice using Solana SIWX/Ed25519, read balances, request a top-up quote, submit an explicitly approved payment, and request a model response. Venice credits the top-up to its internal balance and deducts model usage from that balance.

## Installation

Requires Node.js **24+**. The client uses built-in SQLite; Node may emit an experimental-module warning.

```sh
npm ci --ignore-scripts
npm test
npm start -- --help
```

SDK versions are pinned in `package.json` and `package-lock.json`. The public RPC is used by default. Set `SOLANA_RPC_URL` to your own mainnet RPC if you encounter limits.

## First run

```sh
npm start -- wallet-init
npm start -- wallet
npm start -- balance
npm start -- quote
```

`wallet-init` creates a **new** wallet at `.local/wallet.json` with `0600` permissions and never overwrites an existing file. To use an existing wallet, set `SOLANA_KEYPAIR_FILE` or provide a base58-encoded 64-byte keypair through the `SOLANA_PRIVATE_KEY` environment variable. Private keys are not passed as command-line arguments. `.env` files are not loaded automatically.

Fund the **displayed wallet address** with native USDC on Solana. USDC mint: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`. The balance check reads USDC from the associated token account used by the SDK for payment. The `quote` command does not make a payment. Venice determines the minimum top-up; it was 5 USDC when checked on September 17, 2026.

The quote shows the amount, network, mint, payer wallet, recipient, fee payer, expiry, and `approvalHash`. After **explicit approval of those terms**, run:

```sh
npm start -- pay --quote QUOTE_ID --approve FULL_QUOTE_HASH
```

Replace `QUOTE_ID` and `FULL_QUOTE_HASH` with values from the approved quote. Do not automatically pipe the hash from `quote` into `pay`: the hash binds a decision to the terms but does not itself prove human consent. The future gateway integration must provide it only after actual user approval.

Quotes expire after five minutes. Before signing, the client requests the terms from Venice again and compares them without changing address casing. A changed amount, recipient, network, mint, or fee payer requires a new quote and approval. The prototype caps each top-up at 50 USDC. Venice's specified fee payer covers the transaction fee; the client rejects requirements where the fee payer is the buyer.

After topping up:

```sh
npm start -- balance
npm start -- models
npm start -- chat --model MODEL_ID --message "Reply briefly: hello from Solana." --max-tokens 64
```

Choose `MODEL_ID` from the current model list. `chat` uses existing Venice credit, requires an explicit invocation, and **never tops up automatically**. `max-tokens` limits model output, not the monetary cost of the entire generation. Choose a model with a known price before making a real request. Daily spending limits and Telegram approval will be added during integration with SingIt.

## If the payment response is lost

The client records the attempt **before submission**. A second `pay` for the same order is prohibited, and an uncertain payment blocks further top-ups for that wallet, including after a restart.

```sh
npm start -- status --quote QUOTE_ID
npm start -- history
npm start -- reconcile --quote QUOTE_ID --transaction SOLANA_TRANSACTION_SIGNATURE
```

If Venice has already returned the signature, `--transaction` can be omitted. `reconcile` sends no payment: it reads the transaction from Solana RPC and compares the hash of the **entire transaction message** with the locally signed payment. An unrelated transaction cannot clear the block. The `confirmed` status means the payment was confirmed onchain; the current Venice balance is reported separately.

If the outcome is unknown, do not delete `.local/` or SQLite records, or create another attempt in a different directory. If the transaction cannot be found, manual reconciliation with Venice is required. The prototype does not clear uncertainty on a timeout or implement automatic refunds.

## Storage and milestone scope

- `.local/` contains the private wallet and a SQLite journal of public payment terms. Git ignores this directory. The key is stored locally in **unencrypted** Solana format with `0600` permissions. This is a prototype wallet, not storage for service users.
- Signed payment payloads and authentication headers are neither logged nor printed in errors.
- The payment URL is fixed to `api.venice.ai`; HTTP redirects are prohibited.
- This milestone does not include PayAI, a custom paid endpoint, Jupiter, stock purchases, Bitrefill, automatic top-ups, or Telegram bot changes.
- Library classes are exported from `src/index.mjs` for a future gateway adapter.

## Verification

`npm test` runs local checks without real payments. The end-to-end test uses the actual Solana SDK and verifies both transaction signatures, while RPC and Venice responses are mocked in memory.

Live check results and limitations: [CHECKS.md](CHECKS.md).

Venice documentation: [x402 Venice API](https://docs.venice.ai/guides/integrations/x402-venice-api).
