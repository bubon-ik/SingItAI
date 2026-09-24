# Operations scripts

For service setup and tests, see [operations](../docs/operations.md).
The original local demo launcher and dashboard have been retired.

## Backup Sign402 State

Before opening the Telegram bot to public beta testers or changing production
env files, take a local VPS backup:

```bash
cd ~/apps/sign402
./scripts/backup-sign402-state.sh
```

The backup is written to `~/sign402-backups/<timestamp>/` and may contain
wallet databases, API tokens, and the wallet master key. Keep it private.

## Sign402 Operator Diagnostics

Use the local operator CLI on the VPS when a public tester gets stuck. It reads
local state without printing private keys, encrypted values, or API tokens.

```bash
cd ~/apps/sign402
python3 scripts/sign402-operator.py users
python3 scripts/sign402-operator.py users --search alice
python3 scripts/sign402-operator.py user --telegram-id 8538252718
python3 scripts/sign402-operator.py find-imessage +420736255120
python3 scripts/sign402-operator.py pending --telegram-id 8538252718
python3 scripts/sign402-operator.py last-purchase --telegram-id 8538252718
```

Telegram does not expose a user's phone number to the bot. Use `users` or ask
the user for the Support ID shown by `/start`.

To remove a broken iMessage link, use the authenticated localhost gateway
endpoint through the CLI:

```bash
python3 scripts/sign402-operator.py unlink-imessage --telegram-id 8538252718
# or
python3 scripts/sign402-operator.py unlink-imessage --phone +420736255120
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
cd ~/apps/sign402
./scripts/install-hermes-wallet-plugin.sh
hermes gateway restart
```

See `hermes-plugins/sign402-wallet/README.md` for the secure configuration,
allowlist-first test flow, and diagnostics.
