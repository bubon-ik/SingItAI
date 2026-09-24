#!/usr/bin/env bash
# Phase 6: put the Trezor allowance lane on the VPS. docs/trezor-allowance-deploy.md.
#
# Run as hermes on the VPS, with a terminal (sudo asks for the password once):
#
#   scp scripts/deploy-trezor-allowance.sh hermes@164.68.104.44:
#   ssh -t hermes@164.68.104.44 'bash ~/deploy-trezor-allowance.sh <commit>'
#
# It never prints a secret. Operator keys are created with the gateway's own
# environment and written straight into it; the broker token is taken from the
# running broker; the bot token from Hermes' env. Safe to run again: a step that
# is already done is kept, not repeated.
set -euo pipefail
umask 077

COMMIT="${1:?usage: deploy-trezor-allowance.sh <commit>}"
OWNER="1045618308:0xB80b5Ca13583fB7E0236db4bD8834B9035654558"
APP="$HOME/apps/sign402"
GW="$APP/sign402-gateway"
CONF="$HOME/.config/sign402"
ENV_FILE=/etc/sign402-gateway.env
BROKER_UNIT="$HOME/.config/systemd/user/sign402-trezor-broker.service"
WATCHER_UNIT=/etc/systemd/system/sign402-allowance-watcher.service
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
listening() { ss -ltn | grep -q "127.0.0.1:$1 "; }

step "0. Checks and backup"
cd "$APP"
[ -z "$(git status --porcelain)" ] || fail "the production checkout has local changes; save and merge them first"
PREV=$(git rev-parse HEAD)
PREV_BRANCH=$(git branch --show-current)
./scripts/backup-sign402-state.sh | tail -1
BACKUP=$(ls -1dt "$HOME"/sign402-backups/*/ | head -1)
printf '%s %s\n' "$PREV" "$PREV_BRANCH" > "$BACKUP/pre-allowance-head"
echo "running: $PREV ($PREV_BRANCH); backup: $BACKUP"

step "1. Code"
git fetch -q base-origin trezor-local-sidecar
git cat-file -e "$COMMIT^{commit}" 2>/dev/null || fail "$COMMIT is not on base-origin/trezor-local-sidecar; push it first"
git merge-base --is-ancestor "$PREV" "$COMMIT" || fail "$COMMIT does not contain the running $PREV; merge it first"
git checkout -q -B "release/trezor-allowance-$(date -u +%Y%m%d)" "$COMMIT"
"$GW/.venv/bin/python" -m pip install -q -e "$GW"
result=$(cd "$GW" && .venv/bin/python -m unittest discover -s tests 2>&1 | tail -1 || true)
case "$result" in OK*) ;; *) fail "gateway tests: $result (roll back: git checkout $PREV)" ;; esac
result=$(cd "$APP/hermes-plugins/sign402-wallet" && "$GW/.venv/bin/python" -m unittest discover -s tests 2>&1 | tail -1 || true)
case "$result" in OK*) ;; *) fail "plugin tests: $result (roll back: git checkout $PREV)" ;; esac
echo "on $(git rev-parse --short HEAD); gateway and plugin tests pass"
plugin_link=$(readlink "$HOME/.hermes/plugins/sign402-wallet" 2>/dev/null || true)
[ "$plugin_link" = "$APP/hermes-plugins/sign402-wallet" ] || echo "note: the bot plugin points at '$plugin_link', not this checkout"

step "2. Broker (new code, same state and token)"
mkdir -p "$CONF" "$(dirname "$BROKER_UNIT")"
chmod 700 "$CONF"
if [ ! -s "$CONF/trezor-broker.env" ]; then
  old=$(pgrep -f 'bin/sign402-trezor-broker$' | head -1 || true)
  [ -n "$old" ] || fail "no running broker to take its settings from"
  tr '\0' '\n' < "/proc/$old/environ" \
    | grep -E '^SIGN402_TREZOR_BROKER_(ENABLED|INTERNAL_TOKEN|STATE_PATH|PORT|HOST)=' > "$CONF/trezor-broker.env"
  grep -q '^SIGN402_TREZOR_BROKER_INTERNAL_TOKEN=.\{32,\}' "$CONF/trezor-broker.env" || fail "the running broker has no usable token"
fi
if [ ! -x "$APP/trezor-sidecar/.venv/bin/sign402-trezor-broker" ]; then
  python3.12 -m venv "$APP/trezor-sidecar/.venv"
fi
"$APP/trezor-sidecar/.venv/bin/python" -m pip install -q -e "$APP/trezor-sidecar"
cat > "$BROKER_UNIT" <<UNIT
[Unit]
Description=Sign402 Trezor broker (loopback only)

[Service]
WorkingDirectory=$APP/trezor-sidecar
EnvironmentFile=$CONF/trezor-broker.env
ExecStart=$APP/trezor-sidecar/.venv/bin/sign402-trezor-broker
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
old=$(pgrep -f 'sign402-trezor-broker$' | head -1 || true)
if [ -n "$old" ] && ! systemctl --user is-active -q sign402-trezor-broker; then
  kill "$old"
  for _ in $(seq 20); do kill -0 "$old" 2>/dev/null || break; sleep 0.5; done
fi
state=$(grep '^SIGN402_TREZOR_BROKER_STATE_PATH=' "$CONF/trezor-broker.env" | cut -d= -f2- || true)
state=${state:-$HOME/.sign402-trezor-broker/state.db}
state=${state/#\~/$HOME}
[ -f "$state" ] && cp -p "$state" "$BACKUP/trezor-broker-state.db"
systemctl --user daemon-reload
systemctl --user enable -q sign402-trezor-broker
systemctl --user restart sign402-trezor-broker
for _ in $(seq 20); do listening 8122 && break; sleep 0.5; done
listening 8122 || fail "the broker did not come up: journalctl --user -u sign402-trezor-broker -n 30"
echo "broker up on 127.0.0.1:8122 (state kept, backed up)"

step "3. Watcher settings"
if [ ! -s "$CONF/allowance-watcher.env" ]; then
  bot=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$HOME/.hermes/.env" | head -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')
  [ -n "$bot" ] || fail "no TELEGRAM_BOT_TOKEN in ~/.hermes/.env"
  printf 'SIGN402_ALLOWANCE_TELEGRAM_BOT_TOKEN=%s\n' "$bot" > "$CONF/allowance-watcher.env"
fi
echo "watcher can message the owner"

step "4. Gateway settings (sudo)"
sudo -v
sudo install -m 600 "$ENV_FILE" "$BACKUP/sign402-gateway.env"
if sudo grep -q '^SIGN402_ALLOWANCE_ENABLED=' "$ENV_FILE"; then
  echo "already configured; kept as is"
else
  new_key() {
    sudo systemd-run --quiet --uid=hermes --pipe --wait --collect \
      -p EnvironmentFile="$ENV_FILE" -p WorkingDirectory="$GW" \
      "$GW/.venv/bin/python" -m sign402_gateway.agent_allowance new-operator-key
  }
  funder=$(new_key)
  guardian=$(new_key)
  field() { awk -v k="$1" '$1==k {print $2}' <<<"$2"; }
  [ -n "$(field encrypted "$funder")" ] && [ -n "$(field encrypted "$guardian")" ] || fail "operator keys were not created"
  broker_token=$(grep '^SIGN402_TREZOR_BROKER_INTERNAL_TOKEN=' "$CONF/trezor-broker.env" | cut -d= -f2-)
  {
    printf '\n# Trezor allowance lane (docs/trezor-allowance-deploy.md), %s\n' "$(date -u +%FT%TZ)"
    printf 'SIGN402_ALLOWANCE_ENABLED=1\n'
    printf 'SIGN402_ALLOWANCE_OWNERS=%s\n' "$OWNER"
    printf 'SIGN402_ALLOWANCE_GAS_FUNDER_KEY=%s\n' "$(field encrypted "$funder")"
    printf 'SIGN402_ALLOWANCE_GUARDIAN_KEY=%s\n' "$(field encrypted "$guardian")"
    printf 'SIGN402_ALLOWANCE_BROKER_URL=http://127.0.0.1:8122\n'
    printf 'SIGN402_ALLOWANCE_BROKER_TOKEN=%s\n' "$broker_token"
  } | sudo tee -a "$ENV_FILE" >/dev/null
  printf 'gas_funder %s\nguardian %s\n' "$(field address "$funder")" "$(field address "$guardian")" > "$CONF/allowance-operators"
  unset funder guardian broker_token
fi

step "5. Watcher unit (sudo)"
sudo tee "$WATCHER_UNIT" >/dev/null <<UNIT
[Unit]
Description=Sign402 Trezor allowance watcher
After=network-online.target sign402-gateway.service

[Service]
User=hermes
WorkingDirectory=$GW
EnvironmentFile=$ENV_FILE
EnvironmentFile=$CONF/allowance-watcher.env
ExecStart=$GW/.venv/bin/python -m sign402_gateway.allowance_watcher
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload

step "6. Restart the gateway, the watcher and the bot"
sudo systemctl restart sign402-gateway
for _ in $(seq 40); do curl -fsS http://127.0.0.1:8099/health >/dev/null 2>&1 && break; sleep 0.5; done
curl -fsS http://127.0.0.1:8099/health | grep -q '/agent/allowance/status' || {
  sudo journalctl -u sign402-gateway -n 40 --no-pager | grep -i allowance || true
  fail "the gateway is up but the lane is off (see above). Roll back: see docs/trezor-allowance-deploy.md"
}
echo "gateway healthy, lane on"
sudo systemctl enable -q --now sign402-allowance-watcher
sudo systemctl restart sign402-allowance-watcher
sleep 3
systemctl is-active -q sign402-allowance-watcher || fail "watcher: sudo journalctl -u sign402-allowance-watcher -n 30"
echo "watcher running"
systemctl --user restart hermes-gateway
sleep 3
systemctl --user is-active -q hermes-gateway || fail "bot: journalctl --user -u hermes-gateway -n 30"
echo "bot restarted"

step "Done"
cat "$CONF/allowance-operators"
cat <<TEXT

Send 0.003 ETH on Base to the gas_funder address above; it pays every deployment
and tops up the guardian and your agent. Then, in Telegram: /allowance.

Rolled back by: git -C $APP checkout $PREV, reinstall, restore
$BACKUP/sign402-gateway.env to $ENV_FILE, restart sign402-gateway and hermes-gateway,
stop sign402-allowance-watcher.
TEXT
