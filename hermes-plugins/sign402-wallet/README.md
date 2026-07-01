# Sign402 Wallet Hermes Plugin

This Hermes plugin exposes deterministic managed-wallet commands in
Telegram:

```text
/wallet
/create_wallet
/balance
/connect_imessage
/test_approval
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
SIGN402_PHOTON_API_TOKEN=<same value configured for sign402-gateway>
```

The Sign402 Gateway systemd environment also needs:

```env
SIGN402_PHOTON_API_TOKEN=<independent random bearer token>
SIGN402_IMESSAGE_APPROVAL_STORE_PATH=/home/hermes/.sign402/imessage-approvals.db
SIGN402_HERMES_CLI=/home/hermes/.local/bin/hermes
SIGN402_HERMES_HOME=/home/hermes/.hermes
```

Do not put wallet or Photon API tokens in `SOUL.md`, a skill, a prompt,
Telegram, iMessage, or repository files. Keep `~/.hermes/.env` readable only
by the `hermes` user:

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
/connect_imessage
```

Expected behavior:

- `/wallet` returns the existing Base address or offers wallet creation.
- `/create_wallet` creates one wallet or returns the existing address.
- `/balance` returns balances or the safe balance-unavailable response.
- `/connect_imessage` returns a short pairing code.

Send the pairing code to the configured Photon iMessage line. After the
fixed linked confirmation, run:

```text
/test_approval
```

Expected behavior:

- iMessage receives a fixed `Sign402 approval request`.
- Replying `YES` marks the no-funds test approval as approved.
- Replying `NO` marks the no-funds test approval as denied.
- A conversational `yes` with no pending approval stays normal Hermes chat.

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
