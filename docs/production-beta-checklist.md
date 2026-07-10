# Sign402 Public Beta Checklist

This checklist is for opening the Telegram bot to external testers while keeping
the current single-VPS SQLite deployment.

## Deployment Mode

For public beta, expose only the Sign402 plugin. Keep the underlying Hermes
gateway private to the operator: the Sign402 pre-dispatch hook handles public
wallet messages before Hermes reaches its general agent and tool dispatcher.

Edit `~/.hermes/.env`:

```env
SIGN402_TELEGRAM_SIGN402_ONLY=1
SIGN402_TELEGRAM_ALLOWED_USERS=*
TELEGRAM_ALLOWED_USERS=<operator Telegram ID>
```

In this mode, unknown Telegram text is handled by the Sign402 plugin and receives
the wallet menu instead of falling through to the general Hermes LLM chat. This
prevents public users from spending the operator's LLM credits through ordinary
chat messages. Do not set `TELEGRAM_ALLOW_ALL_USERS=true` or
`GATEWAY_ALLOW_ALL_USERS=true` for public beta.

The Sign402 Telegram policy is deny-by-default: set
`SIGN402_TELEGRAM_ALLOWED_USERS` explicitly. When it is unset, the plugin only
uses the private `TELEGRAM_ALLOWED_USERS` setting; if both settings are absent,
it drops Telegram wallet traffic. A wildcard policy also forces Sign402-only
handling if the explicit mode flag is accidentally omitted.

## Server Update

```bash
ssh hermes@164.68.104.44

cd ~/apps/sign402
git pull

rsync -a --delete ~/apps/sign402/hermes-plugins/sign402-wallet/ ~/.hermes/plugins/sign402-wallet/
```

## iMessage Onboarding

Edit `~/.hermes/.env`:

```env
SIGN402_PHOTON_AUTO_REGISTER_USERS=1
```

With automatic Photon registration enabled, each new user receives their
assigned Photon line after entering the phone number used for iMessage. Only
when automatic registration is disabled should you set
`SIGN402_IMESSAGE_PUBLIC_LINE=<public Sign402 iMessage number or contact>`.

Restart Hermes gateway:

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user restart hermes-gateway
systemctl --user status hermes-gateway --no-pager
```

## Backup Before Opening

Run a local state backup before opening access:

```bash
cd ~/apps/sign402
./scripts/backup-sign402-state.sh
```

The backup is written to:

```text
~/sign402-backups/<timestamp>/
```

Treat the backup as secret material. It can include encrypted wallet databases,
API tokens, and the wallet master key if the env files are readable.

## Required Health Checks

```bash
curl -sS http://127.0.0.1:8099/health
systemctl is-active sign402-gateway
systemctl --user is-active hermes-gateway
```

Expected:

- Sign402 gateway returns `ok: true`.
- `sign402-gateway` is `active`.
- `hermes-gateway` is `active`.

## Fresh User Flow

Run this with a Telegram account that was never allowlisted and never used the
bot before:

1. Send `/start`.
2. Confirm wallet is created and Base address is shown.
3. Send `Balance`.
4. Send `Connect iMessage`.
5. If automatic registration is enabled, enter the phone number used for
   iMessage.
6. Confirm Telegram shows the assigned iMessage line and pairing code, then
   send the code to that assigned line.
7. Send `Limits` and set a low beta limit.
8. Fund the wallet with a small amount of ETH for gas and SINGIT/USDC for tests.
9. Buy a low-value Bitrefill product.
10. Approve in iMessage.
11. Use `Last Purchase` to reveal the code.
12. Use `Withdraw` to return remaining ETH or ERC-20 funds. Leave a small ETH
    reserve for the Base network gas fee.

## Public Beta Guardrails

- Keep default spending limits low.
- Keep `SIGN402_TELEGRAM_SIGN402_ONLY=1` enabled.
- Use `SIGN402_TELEGRAM_ALLOWED_USERS=*` only for an intentional public beta.
  Keep `TELEGRAM_ALLOWED_USERS` restricted to the operator as a separate
  Hermes safety boundary.
- Never set `TELEGRAM_ALLOW_ALL_USERS=true` or `GATEWAY_ALLOW_ALL_USERS=true`
  on this VPS.
- With automatic Photon registration disabled, keep
  `SIGN402_IMESSAGE_PUBLIC_LINE` set before accepting new users.
- Automatic Photon provisioning is rate-limited to protect the shared number
  pool; it applies only when a user submits a valid iMessage phone number.
- Treat the Photon/iMessage line as approval-only. The Sign402 plugin drops
  ordinary iMessage chat so it cannot reach the general Hermes agent.
- Do not expose the Sign402 gateway port publicly.
- Keep `SIGN402_GATEWAY_URL=http://127.0.0.1:8099` in the Hermes plugin env.
- Keep `SIGN402_WALLET_MASTER_KEY` out of git, chat, prompts, and screenshots.
- Leave `SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR` unset. It is for isolated
  local Firefly/demo work and requires a separate operator token when enabled.
- Leave `SIGN402_ENABLE_TEST_ENDPOINTS` unset. It exposes a non-product
  iMessage approval probe and is only for short-lived local diagnostics.
- Leave `SIGN402_ENABLE_CORS` unset. The localhost gateway has no browser
  client in public beta and should not accept cross-origin requests.
- Take a backup before changing env files or restarting services during beta.

## Rollback

To close Sign402 public access again, replace the Sign402 public allowlist with
the operator's Telegram ID:

```env
SIGN402_TELEGRAM_ALLOWED_USERS=<operator Telegram ID>
TELEGRAM_ALLOWED_USERS=<operator Telegram ID>
```

Then restart:

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user restart hermes-gateway
```
