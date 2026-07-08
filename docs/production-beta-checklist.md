# Sign402 Public Beta Checklist

This checklist is for opening the Telegram bot to external testers while keeping
the current single-VPS SQLite deployment.

## Deployment Mode

For public beta, Telegram can be opened to all users only when the Sign402 wallet
plugin runs in Sign402-only mode:

```env
SIGN402_TELEGRAM_SIGN402_ONLY=1
```

In this mode, unknown Telegram text is handled by the Sign402 plugin and receives
the wallet menu instead of falling through to the general Hermes LLM chat. This
prevents public users from spending the operator's LLM credits through ordinary
chat messages.

## Server Update

```bash
ssh hermes@164.68.104.44

cd ~/apps/sign402
git pull

rsync -a --delete ~/apps/sign402/hermes-plugins/sign402-wallet/ ~/.hermes/plugins/sign402-wallet/
```

## Open Telegram Access

Edit `~/.hermes/.env`:

```env
SIGN402_TELEGRAM_SIGN402_ONLY=1
```

Then remove or empty the Telegram allowlist entry used during private testing.
The exact key can vary by Hermes setup, so inspect first:

```bash
grep -nE 'TELEGRAM.*(ALLOW|USER|ID)|ALLOWED' ~/.hermes/.env ~/.hermes/config.yaml
```

If the file contains a line such as:

```env
TELEGRAM_ALLOWED_USERS=1045618308
```

change it to:

```env
TELEGRAM_ALLOWED_USERS=
```

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
5. Send the pairing code to the Photon iMessage line.
6. Send `Limits` and set a low beta limit.
7. Fund the wallet with a small amount of ETH for gas and SINGIT/USDC for tests.
8. Buy a low-value Bitrefill product.
9. Approve in iMessage.
10. Use `Last Purchase` to reveal the code.
11. Use `Withdraw` to return remaining ERC-20 funds.

## Public Beta Guardrails

- Keep default spending limits low.
- Keep `SIGN402_TELEGRAM_SIGN402_ONLY=1` enabled.
- Do not expose the Sign402 gateway port publicly.
- Keep `SIGN402_GATEWAY_URL=http://127.0.0.1:8099` in the Hermes plugin env.
- Keep `SIGN402_WALLET_MASTER_KEY` out of git, chat, prompts, and screenshots.
- Take a backup before changing env files or restarting services during beta.

## Rollback

To close public access again, restore the Telegram allowlist:

```env
TELEGRAM_ALLOWED_USERS=1045618308
```

Then restart:

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user restart hermes-gateway
```
