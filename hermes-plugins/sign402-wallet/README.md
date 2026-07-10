# Sign402 Wallet Hermes Plugin

This Hermes plugin exposes deterministic managed-wallet commands in
Telegram:

```text
/start
/wallet
/balance
/connect_imessage
/limits
/withdraw
/bitrefill
/last_purchase
/llm_buy
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
SIGN402_TELEGRAM_SIGN402_ONLY=1
SIGN402_TELEGRAM_ALLOWED_USERS=*
TELEGRAM_ALLOWED_USERS=<operator Telegram ID>
SIGN402_IMESSAGE_PUBLIC_LINE=<public Sign402 iMessage number or contact>
SIGN402_PHOTON_AUTO_REGISTER_USERS=1
PHOTON_PROJECT_ID=<Photon project id>
PHOTON_PROJECT_SECRET=<Photon project secret>
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

`SIGN402_TELEGRAM_SIGN402_ONLY=1` is required for public beta. It catches
ordinary Telegram text that is not a Sign402 command or wizard response and
returns the Sign402 menu instead of letting the message fall through to the
general Hermes LLM chat.

`SIGN402_TELEGRAM_ALLOWED_USERS=*` opens only the Sign402 plugin to every
Telegram user. Keep `TELEGRAM_ALLOWED_USERS` restricted to the operator and do
not set `TELEGRAM_ALLOW_ALL_USERS` or `GATEWAY_ALLOW_ALL_USERS`; the plugin
intercepts public Sign402 traffic before Hermes can dispatch it to the general
agent. An unset `SIGN402_TELEGRAM_ALLOWED_USERS` falls back to the private
`TELEGRAM_ALLOWED_USERS` setting. If neither policy is set, Telegram wallet
traffic is denied by default. A wildcard Sign402 policy also forces
Sign402-only handling as a defensive fallback if the mode flag is omitted.

`SIGN402_IMESSAGE_PUBLIC_LINE` is shown in the `/connect_imessage` response
when automatic Photon registration is disabled. It identifies the shared line
where users should send their pairing code.

`SIGN402_PHOTON_AUTO_REGISTER_USERS=1` makes `/connect_imessage` ask the
Telegram user for their iMessage phone number, add that number to Photon
Project Users through Spectrum API, then return the pairing code. Users do not
need a Photon account, but on the shared Photon number pool they must send the
pairing code from their phone-number iMessage handle, not an Apple ID email.
For this mode, Telegram shows the assigned private Photon line only after the
phone number has been registered; do not tell users to use a stale shared line.
Automatic provisioning is capped at three valid phone-number attempts per
Telegram user per hour, with a beta-wide hourly guard, to protect the shared
Photon number pool from abuse.

WhatsApp approval is intentionally not exposed yet. Photon supports WhatsApp
Business, but it needs a separately configured Meta Business provider rather
than the bundled iMessage sidecar. Do not present it as an approval channel
until that provider, its webhook, and its credentials are deployed.

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

Set either `SIGN402_TELEGRAM_ALLOWED_USERS` or the existing Telegram allowlist
to the operator account before testing.
In Telegram, run:

```text
/wallet
/balance
/connect_imessage
```

Expected behavior:

- `/wallet` creates a Base wallet when needed, then returns its address.
- `/balance` returns balances or the safe balance-unavailable response.
- `/connect_imessage` returns a short pairing code.

Send the pairing code to the assigned Photon iMessage line. A real, low-value
purchase is the user-facing approval check: iMessage receives the exact terms,
and `YES` or `NO` resolves only that pending approval. The Photon/iMessage line
is approval-only: other messages are dropped and cannot reach the general
Hermes chat. Use Telegram for wallet and agent interactions.

No public domain, reverse proxy, or tunnel is required. Hermes and the
Sign402 Gateway communicate over `127.0.0.1:8099`.

## Public Beta

Before opening the bot, set both `SIGN402_TELEGRAM_SIGN402_ONLY=1` and an
explicit `SIGN402_TELEGRAM_ALLOWED_USERS` policy in `~/.hermes/.env`. Then
follow `docs/production-beta-checklist.md` from the repository root.

## Diagnostics

Check plugin and gateway status:

```bash
hermes plugins list
hermes gateway status
journalctl --user -u hermes-gateway -n 100 --no-pager
```

The plugin returns fixed user-safe errors. It does not log bearer tokens,
private keys, request payloads, or upstream response bodies.
