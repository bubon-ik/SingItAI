# Managed Base Wallet MVP Design

## Project

Hermes Sign402

## Goal

Add a first multi-user wallet layer for the hosted Telegram Hermes bot. Each Telegram user can create one managed Base/EVM agent wallet, view its address, and check basic status. Real spending remains disabled until the iMessage approval provider and per-user limits are implemented.

This turns the current single-operator gateway into the beginning of a user-facing trust layer:

```text
Telegram user -> Hermes -> Sign402 Gateway -> user profile -> managed Base wallet
```

The wallet is an agent wallet, not the user's main wallet. Users should fund it with small amounts only.

## Non-Goals

- Importing an existing private key or seed phrase.
- Exporting private keys from Telegram.
- Signing or sending live payments from the managed wallet.
- Replacing external wallets such as MetaMask or Coinbase Wallet.
- Building a Telegram Mini App.
- Building the iMessage approval provider in this slice.

Import can be added later behind a strong warning and only after the encryption, audit, approval, and recovery stories are clear.

## User Experience

The first Telegram-facing wallet commands are:

```text
/wallet
/create_wallet
/balance
```

`/wallet` shows the user's current wallet status. If the user has no wallet yet, Hermes should offer to create one.

`/create_wallet` creates a new managed Base wallet for the Telegram user. If a wallet already exists, the gateway returns the existing address instead of creating a second wallet.

`/balance` returns the Base wallet address and, when RPC is configured, selected balances such as ETH, USDC, and SINGIT. If balance lookup is not configured, it returns the address and a clear "balance unavailable" message instead of failing the flow.

Example creation response:

```text
Your Base agent wallet is ready:
0x...

Fund this wallet with a small amount only.
Spending is disabled until iMessage approval is configured.
```

## Architecture

Add a user-wallet layer inside `sign402-gateway` rather than inside Hermes prompts. Hermes can ask the gateway to create or read wallet state, but it never sees private keys.

New gateway responsibilities:

- Map Telegram user IDs to local user records.
- Create exactly one managed Base wallet per user.
- Store encrypted private key material outside LLM-visible state.
- Return safe wallet metadata to Hermes.
- Record audit events for wallet creation and status checks.

Proposed module boundaries:

- `user_wallets.py`: wallet model, EVM key generation, encryption/decryption boundary, and wallet service.
- `user_store.py` or an extension to an existing store: durable user-to-wallet mapping.
- `server.py`: HTTP endpoints and response shaping for Hermes.
- tests in `sign402-gateway/tests/test_user_wallets.py` and focused server tests.

The existing `commerce_store.py` can remain commerce-specific. Wallet profile storage should be separate so wallet ownership does not become tangled with Bitrefill order state.

## Storage

Use SQLite for the MVP, consistent with the current gateway's local durable state style. A future Postgres migration is acceptable when the hosted multi-user deployment grows.

Minimum user wallet record:

```text
telegram_user_id
wallet_address
encrypted_private_key
created_at
updated_at
status
```

`telegram_user_id` is unique. `wallet_address` is unique. The encrypted private key is never returned by an API endpoint.

## Key Generation And Encryption

Generate a standard Base/EVM wallet using a well-known Ethereum account library. The address is public metadata. The private key is encrypted before being written to disk.

The gateway uses a server-side master key supplied through environment:

```env
SIGN402_WALLET_MASTER_KEY=...
```

Requirements:

- Startup fails for wallet creation if the master key is missing.
- Existing metadata reads can still report that wallet operations are unavailable if encryption is not configured.
- The master key is not committed to git.
- The encrypted private key is not logged.

For the MVP, symmetric authenticated encryption is enough. The implementation should choose a standard primitive from a maintained library rather than inventing crypto.

## API Shape

Add internal agent-facing endpoints on the gateway:

```text
POST /agent/wallet
POST /agent/create-wallet
POST /agent/wallet-balance
```

Payloads include the Telegram user identity from Hermes:

```json
{
  "telegramUserId": "1045618308",
  "telegramUsername": "optional"
}
```

Responses contain only safe metadata:

```json
{
  "ok": true,
  "wallet": {
    "chain": "base",
    "address": "0x...",
    "status": "created",
    "spendingEnabled": false
  },
  "telegramText": "Your Base agent wallet is ready: 0x...\n\nFund this wallet with a small amount only. Spending is disabled until iMessage approval is configured."
}
```

When a wallet already exists, `/agent/create-wallet` is idempotent and returns the existing wallet with `created: false`.

## Telegram And Hermes Behavior

Hermes should treat the gateway as the source of truth. The LLM may explain the flow, but it should not fabricate wallet addresses, balances, or spend state.

For MVP operation, Hermes can be instructed with a concise system/project note:

- For `/wallet`, call `/agent/wallet`.
- For `/create_wallet`, call `/agent/create-wallet`.
- For `/balance`, call `/agent/wallet-balance`.
- Reply using `telegramText` when present.
- Never ask the user for a seed phrase or private key.
- Never claim spending is enabled.

If Hermes cannot call the gateway, it should say that wallet service is unavailable rather than improvising.

## Security Rules

- One wallet per Telegram user ID in the MVP.
- No private key import.
- No private key export.
- No signing endpoint uses managed wallets in this slice.
- No LLM-visible logs contain private keys or encrypted private key blobs.
- Audit events record wallet creation and balance/status reads.
- The bot remains allowlisted while the product is in dev mode.

The managed wallet is custodial. Product copy must describe it as a managed agent wallet and avoid implying that the user controls keys directly through Telegram.

## Error Handling

- Missing `telegramUserId`: return HTTP 400 with a clear error.
- Existing wallet on create: return HTTP 200 with existing wallet metadata.
- Missing encryption master key on create: return HTTP 503 and a safe setup message.
- RPC unavailable on balance: return wallet address with `balanceUnavailable: true`.
- Corrupt encrypted key record: return HTTP 500, log a redacted operator error, and do not overwrite the wallet.

## Testing

Add unit tests for:

- Creating a new Base wallet record.
- Idempotent create for an existing Telegram user.
- Private key is encrypted at rest and never returned.
- Missing master key blocks wallet creation.
- Wallet status endpoint returns safe metadata only.
- Balance endpoint degrades gracefully when RPC is unavailable.
- Server endpoints reject missing Telegram user IDs.

Run the full gateway test suite after implementation:

```bash
cd sign402-gateway
python -m unittest discover -s tests -v
```

## Rollout

1. Implement create-only wallet endpoints behind disabled spending.
2. Deploy to the VPS.
3. Set `SIGN402_WALLET_MASTER_KEY` on the server.
4. Restart `sign402-gateway`.
5. Test from Telegram with the allowlisted operator account.
6. Add tester Telegram IDs only after wallet creation and status reads are stable.

## Future Work

- iMessage approval provider with `yes` / `no` confirmation.
- Per-user spend policies and limits.
- Managed wallet signing after approval.
- Optional import flow behind strong warnings.
- Recovery/export flow outside normal Telegram chat.
- Postgres-backed user storage for larger hosted deployment.
