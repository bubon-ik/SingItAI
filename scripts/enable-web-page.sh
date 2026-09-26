#!/usr/bin/env bash
# Turn on the agent allowance page: https://app.singitai.app/app/ with its API
# at /web/v1 on the same origin (docs/allowance-web-v1.md, "Running it").
#
# The web API serves the page and the API on 127.0.0.1:8130. Nothing opens on
# this host: the page goes out through the existing Cloudflare Tunnel, like
# decide.singitai.app (docs/decide-public-endpoint.md). One hostname is added
# in the Cloudflare dashboard; this script says exactly which.
#
# Run as hermes, with a terminal for sudo, after deploy-trezor-allowance.sh:
#
#   scp scripts/enable-web-page.sh hermes@164.68.104.44:
#   ssh -t hermes@164.68.104.44 'bash ~/enable-web-page.sh 0xYourAddress[,0xAnother]'
#
# The argument is the beta allowlist: the only wallets that may sign in ("*"
# opens it to everyone). Safe to run again.
set -euo pipefail
umask 077

ALLOWED="${1:?usage: enable-web-page.sh <allowed addresses, comma-separated, or *>}"
HOST="app.singitai.app"
ORIGIN="https://$HOST"
ENV_FILE=/etc/sign402-gateway.env
APP="$HOME/apps/sign402"
GW="$APP/sign402-gateway"
WEB_UNIT=/etc/systemd/system/sign402-web-api.service

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

[ -f "$APP/website/app/index.html" ] || fail "no page at $APP/website/app; deploy a commit that has it first"

step "1. Settings (sudo)"
sudo -v
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo cp -p "$ENV_FILE" "$ENV_FILE.bak-$stamp"
set_env() {  # name value: replace the line or add it
  if sudo grep -q "^$1=" "$ENV_FILE"; then
    sudo sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" | sudo tee -a "$ENV_FILE" >/dev/null
  fi
}
set_env SIGN402_WEB_ENABLED 1
set_env SIGN402_WEB_DOMAIN "$HOST"
set_env SIGN402_WEB_URI "$ORIGIN"
set_env SIGN402_WEB_CORS_ORIGIN "$ORIGIN"
set_env SIGN402_WEB_ALLOWED_ADDRESSES "$ALLOWED"
set_env SIGN402_WEB_STATIC_DIR "$APP/website"
# The chat agent uses the bot's own keys: Jev (TypeSafe) to understand requests,
# the OpenRouter model to talk. Copied from Hermes' env, never printed.
for name in TYPESAFE_API_KEY OPENROUTER_API_KEY; do
  if ! sudo grep -q "^$name=." "$ENV_FILE"; then
    value=$(grep -E "^$name=" "$HOME/.hermes/.env" | head -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')
    if [ -n "$value" ]; then set_env "$name" "$value"; else echo "note: no $name in ~/.hermes/.env; the chat falls back to keywords"; fi
    unset value
  fi
done
if ! sudo grep -q '^SIGN402_WEB_INTERNAL_TOKEN=.\{32,\}' "$ENV_FILE"; then
  set_env SIGN402_WEB_INTERNAL_TOKEN "$("$GW/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi
echo "web settings written (the previous env is kept as $ENV_FILE.bak-$stamp)"

step "2. The web API and the gateway"
sudo tee "$WEB_UNIT" >/dev/null <<UNIT
[Unit]
Description=SingIt web API and page (127.0.0.1:8130; public through the Cloudflare Tunnel)
After=network-online.target sign402-gateway.service

[Service]
User=hermes
WorkingDirectory=$GW
EnvironmentFile=$ENV_FILE
ExecStart=$GW/.venv/bin/python -m sign402_gateway.web_api
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl restart sign402-gateway   # reads the internal token; links Telegram chats to web accounts
for _ in $(seq 40); do curl -fsS http://127.0.0.1:8099/health >/dev/null 2>&1 && break; sleep 0.5; done
sudo systemctl enable -q sign402-web-api
sudo systemctl restart sign402-web-api
sudo systemctl restart sign402-allowance-watcher  # notices for linked web accounts
for _ in $(seq 20); do ss -ltn | grep -q "127.0.0.1:8130 " && break; sleep 0.5; done
ss -ltn | grep -q "127.0.0.1:8130 " || fail "web API: sudo journalctl -u sign402-web-api -n 30"
page=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8130/app/)
api=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8130/web/v1/session)
[ "$page" = 200 ] && [ "$api" = 401 ] || fail "on this host the page answered $page and the API $api (expected 200 and 401)"
echo "page and API answer on 127.0.0.1:8130"

step "3. Public: $ORIGIN"
outside=$(curl -s -o /dev/null -w '%{http_code}' "$ORIGIN/web/v1/session" || true)
if [ "$outside" = 401 ]; then
  echo "$ORIGIN answers from outside. Open $ORIGIN/app/ and sign in with one of: $ALLOWED"
else
  cat <<TEXT
$ORIGIN is not published yet (answered '$outside'). In Cloudflare:
  Zero Trust -> Networks -> Tunnels -> the running tunnel -> Public Hostnames -> Add
    Subdomain: app    Domain: singitai.app    Path: (empty)
    Service:   HTTP   URL: 127.0.0.1:8130
Only the web API listens on 8130, and it serves only /app/, /assets/ and /web/v1.
Then open $ORIGIN/app/ and sign in with one of: $ALLOWED
TEXT
fi
cat <<TEXT

Turn it off: set SIGN402_WEB_ENABLED=0 in $ENV_FILE, then
  sudo systemctl disable --now sign402-web-api && sudo systemctl restart sign402-gateway
and remove the app hostname from the tunnel.
TEXT
