# Sign402 Wallet Hermes Plugin

This Hermes plugin exposes deterministic managed-wallet commands in
Telegram:

```text
/start
/wallet
/balance
/connect_imessage
/connect_whatsapp
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
SIGN402_WHATSAPP_PUBLIC_LINE=<public Meta WhatsApp Business number or wa.me link>
SIGN402_PHOTON_AUTO_REGISTER_USERS=1
PHOTON_PROJECT_ID=<Photon project id>
PHOTON_PROJECT_SECRET=<Photon project secret>

# Added by `hermes whatsapp-cloud` for inbound Meta webhooks:
WHATSAPP_CLOUD_PHONE_NUMBER_ID=<Meta Phone Number ID>
WHATSAPP_CLOUD_ACCESS_TOKEN=<Meta System User token>
WHATSAPP_CLOUD_APP_SECRET=<Meta App Secret>
WHATSAPP_CLOUD_VERIFY_TOKEN=<random webhook verify token>
WHATSAPP_CLOUD_ALLOWED_USERS=<test wa_id without +>
```

The Sign402 Gateway systemd environment also needs:

```env
SIGN402_PHOTON_API_TOKEN=<independent random bearer token>
SIGN402_IMESSAGE_APPROVAL_STORE_PATH=/home/hermes/.sign402/imessage-approvals.db
SIGN402_HERMES_CLI=/home/hermes/.local/bin/hermes
SIGN402_HERMES_HOME=/home/hermes/.hermes
SIGN402_WHATSAPP_ACCESS_TOKEN=<same Meta System User token>
SIGN402_WHATSAPP_PHONE_NUMBER_ID=<Meta Phone Number ID>
SIGN402_WHATSAPP_TEMPLATE_NAME=sign402_payment_approval
SIGN402_WHATSAPP_TEMPLATE_LANGUAGE=en_US
SIGN402_WHATSAPP_GRAPH_API_VERSION=v25.0
```

Do not put wallet, Photon, or Meta API tokens in `SOUL.md`, a skill, a prompt,
Telegram, iMessage, WhatsApp, or repository files. Keep `~/.hermes/.env` readable only
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

`/connect_whatsapp` creates the same single-use pairing code without using
Photon. The user sends it to `SIGN402_WHATSAPP_PUBLIC_LINE`; Hermes receives the
signed Meta webhook through its official `whatsapp_cloud` adapter, and the
plugin links the trusted Meta `wa_id`. Pairing WhatsApp makes it the user's sole
active approval channel; pairing iMessage later switches the channel back.

Run the Hermes Cloud setup wizard before enabling WhatsApp:

```bash
hermes whatsapp-cloud
```

For a first test, expose the wizard's webhook port with the free Cloudflare
quick tunnel it prints and configure the resulting callback as
`https://<tunnel>.trycloudflare.com/whatsapp/webhook` in Meta. Subscribe the
WhatsApp app to the `messages` field. No purchased domain is required for this
test, but the quick-tunnel URL must be updated in Meta whenever it changes.

Create and obtain Meta approval for a `UTILITY` template named
`sign402_payment_approval`, language `en_US`, with three body variables and two
quick-reply buttons in this order:

```text
Sign402 payment approval
{{1}}
Approval reference: {{2}}
Expires: {{3}}

[Approve] [Reject]
```

The gateway always sends this template for WhatsApp payment approvals, so the
request works even outside Meta's 24-hour customer-service window. Hermes
currently handles the signed inbound webhook; the gateway calls Meta directly
only for this approved outbound template.

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
/connect_whatsapp
```

Expected behavior:

- `/wallet` creates a Base wallet when needed, then returns its address.
- `/balance` returns balances or the safe balance-unavailable response.
- `/connect_imessage` returns a short pairing code.
- `/connect_whatsapp` returns a short pairing code for the Meta business number.

Send the pairing code to the chosen channel. A real, low-value purchase is the
user-facing approval check: iMessage accepts `YES`/`NO`; WhatsApp displays
Approve/Reject buttons bound to the exact approval ID. Both channels are
approval-only: unrelated messages are dropped before the general Hermes chat.
Use Telegram for wallet and agent interactions.

The Sign402 Gateway remains private on `127.0.0.1:8099`. WhatsApp additionally
requires a public HTTPS tunnel only for Hermes's signed Meta webhook endpoint.

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
