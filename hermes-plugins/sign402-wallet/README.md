# Sign402 Wallet Hermes Plugin

This Hermes plugin exposes deterministic managed-wallet commands in
Telegram:

```text
/wallet
/create_wallet
/balance
```

The commands do not call the configured LLM. The plugin binds each request
to `MessageEvent.source.user_id`, lets Hermes apply its normal Telegram
authorization, and then calls the protected Sign402 Gateway on localhost.
Raw command arguments cannot select another Telegram user.

## Server Configuration

The Hermes gateway process needs these values in `~/.hermes/.env`:

```env
SIGN402_GATEWAY_URL=http://127.0.0.1:8099
SIGN402_WALLET_API_TOKEN=<same value configured for sign402-gateway>
```

Do not put the wallet API token in `SOUL.md`, a skill, a prompt, Telegram,
or repository files. Keep `~/.hermes/.env` readable only by the `hermes`
user:

```bash
chmod 600 ~/.hermes/.env
```

The plugin rejects non-loopback gateway URLs so a configuration mistake
cannot send the bearer token to a remote host.

## Install

From the server checkout as the `hermes` user:

```bash
cd ~/apps/sign402
./scripts/install-hermes-wallet-plugin.sh
hermes plugins list
hermes gateway restart
hermes gateway status
```

The installer creates:

```text
~/.hermes/plugins/sign402-wallet -> ~/apps/sign402/hermes-plugins/sign402-wallet
```

It refuses to overwrite an unrelated existing plugin path and never reads
or writes secrets.

## First Test

Keep the existing Telegram allowlist restricted to the operator account.
In Telegram, run:

```text
/wallet
/create_wallet
/balance
```

Expected behavior:

- `/wallet` returns the existing Base address or offers wallet creation.
- `/create_wallet` creates one wallet or returns the existing address.
- `/balance` returns balances or the safe balance-unavailable response.

No public domain, reverse proxy, or tunnel is required. Hermes and the
Sign402 Gateway communicate over `127.0.0.1:8099`.

## Diagnostics

Check plugin and gateway status:

```bash
hermes plugins list
hermes gateway status
journalctl --user -u hermes-gateway -n 100 --no-pager
```

The plugin returns fixed user-safe errors. It does not log bearer tokens,
private keys, request payloads, or upstream response bodies.
