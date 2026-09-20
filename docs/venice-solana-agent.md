# Venice on Solana in the Telegram agent

Implemented on `venice-solana`, continuing the conversational UI branch
`telegram-ui`. The existing Base implementation remains available. No live
Venice payment or paid Solana model response has been performed for this release.

## User flow

1. Open **Settings** and connect a phone through WhatsApp or iMessage.
2. Open **Wallet → Solana** to create/show your personal encrypted wallet.
3. Open **Chat → Solana**, choose a model and approve a daily AI top-up budget.
4. Send a question. Existing Venice credit is used without another top-up.
5. If credit is insufficient, choose **Review Solana top-up**. Review the exact
   amount, payer, Venice recipient, USDC mint, sponsored network fee and expiry.
6. Choose **Request top-up approval**, then approve the same quote on the linked
   phone. An expired or changed quote needs a new review and approval.
7. The receipt shows the Solana transaction signature and separately whether
   Venice credit is usable. Send the question again once credit is ready.

AI network choice is separate from the Wallet screen's balance selector.
Base and Solana have separate models, budgets and credit. Each daily budget
limits **new Venice top-ups**, not consumption of existing prepaid credit.
This is the chat-specific budget; it is not the shop/withdrawal limit in Settings.
No shared CLI wallet is used for Telegram payments.

## Payment and recovery boundaries

- The authenticated gateway selects the user's managed Solana wallet. A Node 24
  subprocess receives its decrypted key only over stdin, with an explicit payer
  check. No key, prompt, answer, auth header or signed payment is persisted.
- Only Venice's current x402 v2, Solana-mainnet, native-USDC exact requirements
  are accepted. Solana address and mint casing is preserved.
- Budget approval moves no money. Every top-up additionally needs phone approval
  of its exact quote hash. The whole charge is reserved before requesting approval.
  The cap is rechecked for the submission day if approval crosses midnight.
- The Node journal records the signed message hash before the only submission.
  Neither chat, a retry button nor recovery automatically submits another payment.
- Lost/uncertain results block further top-ups across restarts and UTC day rollover.
  **Payment status** reads the existing attempt and verifies its transaction against
  the signed message hash. Chain confirmation and merchant credit are separate.
- A confirmed, previously credited payment stays recorded after the credit is used.
  Checking an old receipt cannot charge the budget again or block a new top-up.
- If no signature was returned, support can find it in the wallet/provider history
  and use `/chat_payment QUOTE_ID TRANSACTION_SIGNATURE`. A signature for another
  transaction is rejected. If a crashed payment cannot be located in the journal,
  it stays blocked for operator investigation; there is no automatic retry.
- Turning off Solana support does not redirect a saved Solana choice to Base.
  Switching networks requires an explicit user action.

## Runtime and state

The feature is available when `SIGN402_AI_CHAT_ENABLED` is on. Set
`SIGN402_SOLANA_CHAT_ENABLED=0` to disable Solana requests while retaining the
network selection and journal. Existing Base requests continue to work.

Install `solana-x402-service` dependencies with `npm ci --ignore-scripts` under
Node 24+. Set `SIGN402_SOLANA_NODE_BINARY` to its Node binary, or install a
separate runtime at `~/.local/share/singit-node24/bin/node`; otherwise the bridge
uses Node on `PATH`. The existing Hermes Node installation need not change.
`SOLANA_RPC_URL` must be an HTTPS Solana mainnet RPC endpoint.

`SIGN402_SOLANA_CHAT_STATE_DIR` defaults to `~/.sign402/solana-chat`:

- `chat.sqlite3`: network preferences, wallet binding, model, budget, last balance,
  exact public quote terms and payment reservations/results.
- `operations/<hash-of-user-and-wallet>/operations.sqlite3`: Node quote and attempt
  journal. Per-user directories are private; databases have mode 0600.

Run one gateway process for this bot. Back up **both journals together** and the
existing encrypted wallet database before upgrades. Do not delete uncertain
attempts or replace the managed wallet with a CLI wallet to unblock a payment.

## Verification

Offline gateway, Telegram and Node suites cover exact approvals, declines,
expiry, changed terms, wrong users/wallets/networks, duplicates, insufficient
funds, lost responses, restart, day rollover, delayed credit, pause controls and
Base regressions. The Node fixture exercises real Ed25519 signatures and SVM
payment construction against local mocked Venice and RPC responses.

An unpaid live Venice probe on September 20, 2026 returned identical requirements
on two successive requests: **5 USDC**, Solana mainnet, native USDC, sponsored
network fee. This is a historical observation, not a standing payment approval.
The bot always obtains and validates a fresh quote.

Remaining live acceptance: approved top-up from the user's managed wallet,
confirmed chain receipt, usable Venice credit and selected-model answer. Solana
shopping, withdrawals, paid search and on-chain data tools are outside this
adapter; no implicit Base fallback is allowed for those Solana chat requests.
