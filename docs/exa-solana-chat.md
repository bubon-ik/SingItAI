# Exa search in Solana AI chat

Exa supplies web results to the selected Venice model. Search uses the user's
managed Solana wallet and a separate, opt-in budget. It does not consume prepaid
Venice credit. Base chat retains its existing search implementation.

## User flow

1. Open **Chat → AI settings → Solana**. Create a Solana wallet if needed, choose
   a model and approve the Venice budget. Each Venice top-up still needs approval
   of its exact quote. Search requires usable Venice credit before it can spend.
2. Open **Web search → Review search budget**. The review identifies the wallet,
   Exa recipient, network, native USDC mint, endpoint, current price and limits.
3. Choose **Request search approval** and confirm through the linked phone
   channel. Connect a phone in Settings first if it is not paired.
4. Send a freshness or explicit search request, such as “latest Solana news” or
   “найди документацию Solana”. The bot can make one Exa search for that message,
   then pass its excerpts to Venice. Ordinary conversation does not search.
5. The answer shows numbered source links, the separate Exa charge, a Solana
   transaction link, and the remaining Venice credit.

The standing approval permits **at most 0.02 USDC per search, 0.20 USDC and 20
searches per UTC day, for 30 days**. There is no further phone prompt for each
search within that allowance. Approval itself moves no money. Revocation is
available under **Web search → Turn search off**. Reapproving does not reset
spending already recorded that day.

The initial implementation uses bounded English/Russian freshness and explicit
search detection; it does not ask an LLM to decide or repeatedly refine searches.
Users can explicitly ask to search when the detector misses a query. A search
question is limited to 2,000 characters, with three results and up to 1,200
characters of text per result. No search query or result is persisted by the
new search journals; the providers necessarily receive their request content.

## Payment and recovery

- Fixed endpoint: `POST https://api.exa.ai/search`, `type: auto`, three results,
  `contents.text.maxCharacters: 1200`. No Exa API key is attached.
- Read the fresh `PAYMENT-REQUIRED` x402 v2 challenge. Only Solana mainnet/native
  USDC, an approved merchant and a sponsored network fee are accepted.
- Reserve cost and one call atomically in SQLite, rechecking the policy, amount,
  ownership, expiry and daily limits. Reservations cannot cross UTC midnight.
- Sign with the authenticated user's decrypted managed key over private stdin.
  Before the only submission, persist the signed message's hash. Never persist
  a secret key, signed payload, authentication header, query or source excerpt.
- Submit `PAYMENT-SIGNATURE` once. Verify the `PAYMENT-RESPONSE` transaction
  through Solana RPC against that exact signed message. Provider success alone
  is not proof of a confirmed payment.
- An unresolved payment blocks new searches, including after restart or midnight.
  **Search payment status** checks the journal and chain without sending money.
  For a missing receipt, `/chat_search_payment QUOTE_ID TRANSACTION_SIGNATURE`
  can reconcile an externally obtained signature; unrelated transactions fail.
- If the payment response or search results are lost, results cannot be recovered
  from this journal. A confirmed charge remains recorded. There is no automatic
  repurchase. If Venice fails after a successful search, show the search's sources
  and receipt. Empty results do not trigger a paid Venice completion.
- After a process restart, a Python reservation without a Node attempt remains
  held: an earlier child may still be running. An operator must establish that
  it exited without submission before releasing that hold.

Source excerpts are untrusted model input. They cannot change a payment policy,
select a merchant or trigger another payment. They are sent with a fixed system
instruction to ignore embedded commands and cite only supplied sources. This
instruction is not a guarantee of model answer accuracy.

## Operations

`SIGN402_AI_SEARCH_ENABLED=1` and `SIGN402_SOLANA_SEARCH_ENABLED` (defaults to `1`)
control availability. Individual users still start with search **off**. Turning
search availability off preserves status, revocation and payment recovery routes.
The global payment pause also blocks budget activation and paid searches.

Budget/proposal/receipt tables are in the existing private
`SIGN402_SOLANA_CHAT_STATE_DIR/chat.sqlite3`. Node submission journals use
`operations/<hashed-user-and-wallet>/exa/operations.sqlite3`, separate from
Venice top-ups. Back up the whole state directory, both managed-wallet tables
and configuration before deployment. Check `exa_payments` for `paying` or
`uncertain` rows before restarting a production gateway.

Authenticated gateway routes: `/agent/chat/search`, `search-prepare`,
`search-approve`, `search-disable`, and `search-payment` under the same prefix.
Explicit `chain: solana` is required by the plugin; a stale network selection
is rejected, never silently executed on Base.

## Verification — September 22, 2026

An **unpaid** probe from the VPS with the exact search body above returned HTTP
402, Solana mainnet/native USDC, `amount: 7000` (0.007 USDC), a 60-second payment
lifetime and a sponsored fee payer. This verifies offered terms, not settlement.

Offline tests cover standing consent, linked-phone hash binding, wallet
isolation, concurrent reservations, money/count limits, expiry, revocation,
restart/midnight recovery, separate receipts and source handling. The Node flow
uses the real x402 SVM builder and real Ed25519 signatures with local RPC and
provider fixtures. It verifies the signed transaction and passes results to the
selected Venice model fixture. Tests use generated unfunded wallets only.

Local verification passed: 1,309 gateway tests, 422 Telegram plugin tests and
49 Solana Node tests, plus syntax and whitespace checks.

A real funded Exa search and a paid Venice response remain live acceptance steps
for the user after reviewing and approving their budgets. No mainnet Exa payment
was sent during implementation.

Primary protocol reference: [Exa x402 quickstart](https://exa.ai/docs/integrations/payments/x402/quickstart).
