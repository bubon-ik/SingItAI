# Managed Solana wallets — verification

Date: September 17, 2026.

## Implemented behavior

- `/wallet solana` creates or returns a dedicated Ed25519 wallet for the authenticated Telegram user.
- `/balance solana` reads SOL and native-USDC holdings on Solana mainnet.
- `/wallet`, `/balance`, and explicit `base` arguments keep the existing Base behavior. Network selection applies to one command, not a persistent global setting.
- The additive `solana_user_wallets` table preserves existing Base wallet rows, encrypted keys and access tokens.
- Solana keypairs are encrypted with the gateway master key. The encrypted envelope binds the user, chain and public address; decryption checks those bindings and derives the public key again.
- Public responses expose addresses and balances, never private keys. Wallet creation uses the existing trusted-plugin authentication boundary; status and balance requests require the shared gateway token plus a matching per-user token.
- Solana payments and withdrawals remain disabled. Explicit Solana requests to legacy authenticated Base routes are rejected before execution.

## Automated checks

Run the gateway and plugin suites using the dependency revision pinned in `sign402-gateway/pyproject.toml`:

```sh
PYTHONPATH=sign402-gateway python -m unittest discover -s sign402-gateway/tests
PYTHONPATH=sign402-gateway python -m unittest discover -s hermes-plugins/sign402-wallet/tests
```

The local runs use Python 3.14.5 with the project's installed dependencies and the exact pinned Spending Memory source revision `443743ea2d69ef765528bd70b28a7e2d8c80e564`. CI uses Python 3.12. An initial run picked up a different editable Spending Memory checkout and failed an existing merchant-policy test; rerunning with the pinned revision resolved that mismatch. No dependency files were changed.

**Results:** 1,234 gateway tests and 284 Telegram plugin tests passed (1,518 total).

Coverage includes:

- Existing Base wallet regression checks and additive schema migration.
- Independent Base/Solana wallets and separate wallets for different users.
- Concurrent creation across separate store connections, persistence and idempotency.
- Encrypted key storage, ciphertext owner binding, wrong-key failures and absence of plaintext keys from responses and stored audit records.
- Real Ed25519 signatures, base58 vectors and preserved address casing.
- Authenticated gateway creation/status/balance with a real temporary wallet store; wrong-user and missing-token requests are refused.
- Telegram dispatch and client forwarding of network and trusted identity; malformed command arguments cannot change the user.
- RPC mainnet-genesis verification, exact native-USDC mint/owner/program checks, decimal precision and malformed/duplicate account rejection.
- Unavailable RPC results remain unavailable instead of showing a misleading zero balance.
- Explicit Solana requests cannot enter legacy Base withdrawal or paid-tool routes.

## Live read-only verification

Created an unfunded wallet in a temporary encrypted store, in memory generated a compatible keypair for the Node Solana SDK, and confirmed that the SDK derived the same public address. Read its balances through `https://api.mainnet-beta.solana.com`: **0.000000000 SOL and 0.000000 USDC**, after verifying the mainnet genesis hash. Temporary state was removed when the check completed. No private key was printed.

The provider uses Solana's documented [getGenesisHash](https://solana.com/docs/rpc/http/getgenesishash), [getBalance](https://solana.com/docs/rpc/http/getbalance) and [getTokenAccountsByOwner](https://solana.com/docs/rpc/http/gettokenaccountsbyowner) methods.

## Limits of this milestone

This code has not been deployed to the running Telegram bot or VPS. Telegram transport is exercised with local test fixtures. No funded transaction, withdrawal, token-account creation or paid model request was submitted.

The displayed USDC balance sums all owned native-USDC token accounts. It is a holdings balance, not a spendable-balance guarantee: the x402 SDK's source associated token account must be checked separately by the payment adapter.

The standalone CLI wallet and managed user wallets are different. The CLI's local wallet is not imported or used for Telegram users. Existing Base payment and Venice behavior remains separate from the pending Solana approval and payment integration.
