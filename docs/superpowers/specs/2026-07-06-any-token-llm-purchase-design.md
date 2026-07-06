# Any-token payment for Bankr LLM key purchases

Date: 2026-07-06
Status: approved by user (chat), approach A

## Goal

Let a Telegram user pay for a Bankr LLM key with any ERC-20 token they hold
on their managed Base wallet: `/llm_buy <amountUsd> <email> [token]`.
`token` is a known symbol or a contract address; omitted → SINGIT (current
behaviour, backward compatible).

## Non-goals

- Swapping on our side (Bankr swaps non-stables itself).
- Interactive token pickers in the bot.
- Multi-chain support (Base mainnet only, as today).

## Design

### 1. Data model (sign402_gateway/bankr_llm_purchase.py)

New columns on `bankr_llm_purchases` (TEXT NOT NULL DEFAULT '', migrated via
the same PRAGMA/ALTER pattern used for `baseline_credits_usd`):

- `payment_token_address` — checksum-insensitive 0x address
- `payment_token_symbol` — display symbol for approval/messages
- `payment_token_decimals` — decimal string, e.g. "18", "6"

Blank values mean legacy SINGIT purchases; all read paths fall back to the
configured SINGIT address / "SINGIT" / 18. Existing amount fields
(`singit_amount_atomic` etc.) keep their names but hold atomic amounts of
the purchase's payment token.

### 2. Token resolution (purchase start)

The bot passes optional `paymentToken` in `/agent/llm-key/start`.

- empty → SINGIT (env-configured address, 18 decimals)
- known symbol → address/decimals from a code dictionary of Base mainnet
  tokens: USDC, USDT, WETH, cbBTC, SINGIT (extensible constant)
- EVM address → new node command `token-info --token 0x...` reads
  `symbol()` and `decimals()` from the contract; failure → purchase fails
  at start with `invalid_payment_token`, nothing moved
- unknown symbol → `invalid_payment_token` error listing supported symbols
  and suggesting a contract address

Resolution happens once at `start`; the resolved triple is stored on the
purchase row.

### 3. Pricing — two lanes

- **Stables (USDC, USDT):** token amount == amountUsd exactly (6 decimals),
  no swap quote, no approval slippage buffer. Bankr accepts them directly.
- **Everything else:** `RealRateSingitPricer` generalised: `from_token`
  address and `decimals` become parameters (replacing the SINGIT_DECIMALS
  constant and the hard-coded 1e18 conversions in the purchase service).
  The CDP `swap-price` quote doubles as the liquidity pre-check: no
  route/quote → `FAILED_BEFORE_TRANSFER` before any transfer.

### 4. Transfer, balance, topup

- `UserWalletTokenTransferClient.transfer_token` passes `--decimals`
  (the node script already supports the option; default 18 today).
- Balance pre-check: new node command `token-balance --token 0x... --owner
  0x...` (ERC-20 `balanceOf`), used by `_balance_error` for tokens the
  wallet service does not index; SINGIT keeps the current path.
- Topup `sourceToken` = the purchase's payment token address. Baseline
  snapshot, rejected-retry, ambiguous balance polling and reconcile work
  unchanged.

### 5. Approval & errors

- iMessage approval context lines gain `Payment token: <SYMBOL>
  <address>`.
- The +5% approval buffer (SIGN402_BANKR_SINGIT_APPROVAL_BUFFER_PERCENT)
  applies to non-stable tokens only; stables approve the exact amount.
- Failure modes: `invalid_payment_token` (resolution), `invalid_pricing`
  (no route), `insufficient_token_balance` (generic wording replaces the
  SINGIT-specific message when the token is not SINGIT).

### 6. Bot (hermes-plugins/sign402-wallet)

`/llm_buy <amountUsd> <email> [token]` — third positional argument passed
through as `paymentToken`. Help/usage strings updated.

### 7. Tests

- resolution: symbol, address (fake token-info), junk symbol, junk address
- USDC lane: exact amount, 6 decimals, no quote call, no buffer
- generic lane: quote + buffer + fresh reprice, `--decimals` forwarded
- topup uses the purchase's token address
- DB migration adds columns; legacy rows behave as SINGIT
- bot argument parsing

## Rollout

Deploy gateway first (schema migrates on start), then the bot. Legacy
purchases and commands without a token argument behave exactly as before.
