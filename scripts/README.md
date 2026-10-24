# Operations scripts

For service setup and tests, see [operations](../docs/operations.md).
The original local demo launcher and dashboard have been retired.

## Backup Sign402 State

Before opening the Telegram bot to public beta testers or changing production
env files, take a local VPS backup:

Set `SIGN402_BACKUP_ROOT`, `SIGN402_STATE_DIR`, `SIGN402_HERMES_ENV`, and
`SIGN402_SYSTEM_ENV` from private ops notes, then:

```bash
cd <APP_CHECKOUT>
./scripts/backup-sign402-state.sh
```

The backup is written to `<BACKUP_ROOT>/<timestamp>/` and may contain
wallet databases, API tokens, and the wallet master key. Keep it private.
`<APP_CHECKOUT>` and `<BACKUP_ROOT>` are placeholders.

## Sign402 Operator Diagnostics

Use the local operator CLI on the VPS when a public tester gets stuck. It reads
local state without printing private keys, encrypted values, or API tokens.

```bash
cd <APP_CHECKOUT>
python3 scripts/sign402-operator.py users
python3 scripts/sign402-operator.py users --search alice
python3 scripts/sign402-operator.py user --telegram-id <telegram-id>
python3 scripts/sign402-operator.py find-imessage <e164-phone>
python3 scripts/sign402-operator.py pending --telegram-id <telegram-id>
python3 scripts/sign402-operator.py last-purchase --telegram-id <telegram-id>
```

Telegram does not expose a user's phone number to the bot. Use `users` or ask
the user for the Support ID shown by `/start`.

To remove a broken iMessage link, use the authenticated localhost gateway
endpoint through the CLI:

```bash
python3 scripts/sign402-operator.py unlink-imessage --telegram-id <telegram-id>
# or
python3 scripts/sign402-operator.py unlink-imessage --phone <e164-phone>
```

The unlink command needs `SIGN402_PHOTON_API_TOKEN` and
`SIGN402_GATEWAY_URL` available from the environment or standard Sign402 env
files.

## Hosted Hermes Telegram Wallet Plugin

The VPS deployment can expose the managed Base wallet endpoints through
deterministic Hermes Telegram commands without giving the LLM control of a
Telegram user ID.

Configure `SIGN402_GATEWAY_URL` and `SIGN402_WALLET_API_TOKEN` in the
Hermes gateway environment, then install the repository plugin:

```bash
cd <APP_CHECKOUT>
./scripts/install-hermes-wallet-plugin.sh
hermes gateway restart
```

See `hermes-plugins/sign402-wallet/README.md` for the secure configuration,
allowlist-first test flow, and diagnostics.

## Health Alerts

Probes the public endpoints and Base RPC, and alerts an operator (Telegram, a webhook, or stdout -
whichever is configured) when one is down. Read-only; no deploy, no channel chosen by default.

```bash
python3 scripts/singit-health-alert.py --dry-run
python3 scripts/singit-health-alert.py --rpc-url "$SIGN402_BASE_RPC_URL" --journal-unit sign402-web
```

See the script's own docstring for every check, env var, and a sample systemd user timer /
crontab line (every 5 minutes). Tests: `python3 -m unittest discover -s scripts/tests`.
