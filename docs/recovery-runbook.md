# Sign402 Recovery Runbook

This runbook covers the current single-VPS beta deployment that stores Sign402
state in SQLite and JSON files.

## Critical State

The deployment is recoverable only if both state files and secrets are preserved.

State files:

```text
/home/hermes/.sign402/user-wallets.db
/home/hermes/.sign402/imessage-approvals.db
/home/hermes/.sign402/bankr-llm.db
/home/hermes/.sign402/user-spend-limits.json
/home/hermes/apps/sign402/demo-dashboard/bitrefill-orders.sqlite3
/home/hermes/apps/sign402/demo-dashboard/latest-run.json
/home/hermes/apps/sign402/demo-dashboard/agent-state.json
```

Secret/config files:

```text
/home/hermes/.hermes/.env
/etc/sign402-gateway.env
```

The most important value is `SIGN402_WALLET_MASTER_KEY`. If the encrypted wallet
database exists but the master key is lost, custodial private keys cannot be
decrypted.

## Create Backup

As `hermes` on the VPS:

```bash
cd ~/apps/sign402
./scripts/backup-sign402-state.sh
```

Backups are stored in:

```text
/home/hermes/sign402-backups/<timestamp>/
```

Keep these backups private. They may contain API tokens and the wallet master
key.

## Restore Procedure

1. SSH to the VPS.

```bash
ssh hermes@164.68.104.44
```

2. Stop services.

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user stop hermes-gateway
sudo systemctl stop sign402-gateway
```

3. Take a pre-restore backup of the current state.

```bash
cd ~/apps/sign402
./scripts/backup-sign402-state.sh
```

4. Restore state files from the selected backup.

```bash
BACKUP=/home/hermes/sign402-backups/YYYYMMDDTHHMMSSZ

mkdir -p /home/hermes/.sign402
cp -p "$BACKUP/state/"* /home/hermes/.sign402/ 2>/dev/null || true
cp -p "$BACKUP/repo-state/bitrefill-orders.sqlite3" /home/hermes/apps/sign402/demo-dashboard/ 2>/dev/null || true
cp -p "$BACKUP/repo-state/latest-run.json" /home/hermes/apps/sign402/demo-dashboard/ 2>/dev/null || true
cp -p "$BACKUP/repo-state/agent-state.json" /home/hermes/apps/sign402/demo-dashboard/ 2>/dev/null || true
```

5. Restore secrets only when intentionally rolling back env values.

```bash
cp -p "$BACKUP/secrets/hermes.env" /home/hermes/.hermes/.env
sudo cp -p "$BACKUP/secrets/sign402-gateway.env" /etc/sign402-gateway.env
```

6. Fix permissions.

```bash
chmod 700 /home/hermes/.sign402
chmod 600 /home/hermes/.sign402/* 2>/dev/null || true
chmod 600 /home/hermes/.hermes/.env
sudo chmod 600 /etc/sign402-gateway.env
```

7. Start services.

```bash
sudo systemctl start sign402-gateway
systemctl --user start hermes-gateway
```

8. Verify.

```bash
curl -sS http://127.0.0.1:8099/health
systemctl is-active sign402-gateway
systemctl --user is-active hermes-gateway
```

## User-Level Verification

From Telegram:

1. Send `Wallet` and confirm the expected Base address appears.
2. Send `Balance`.
3. Send `Connect iMessage` only if pairing needs to be repaired.
4. Send `Last Purchase` if restoring Bitrefill order state.

## Emergency Close

If public beta needs to be closed quickly, restore a Telegram allowlist in
`/home/hermes/.hermes/.env` and restart Hermes gateway:

```env
TELEGRAM_ALLOWED_USERS=1045618308
```

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user restart hermes-gateway
```
