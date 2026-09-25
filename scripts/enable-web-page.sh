#!/usr/bin/env bash
# Turn on the web page's API on the VPS: https://api.singitai.app/web/v1 for
# the page at https://singitai.app/app/ (docs/allowance-web-v1.md, "Running it").
#
# Run as hermes, with a terminal for sudo, after deploy-trezor-allowance.sh:
#
#   scp scripts/enable-web-page.sh hermes@164.68.104.44:
#   ssh -t hermes@164.68.104.44 'bash ~/enable-web-page.sh 0xYourAddress[,0xAnother]'
#
# The argument is the beta allowlist: the only wallets that may sign in ("*"
# opens it to everyone). Needs a DNS record api.singitai.app -> this server
# (DNS only, not proxied, so Caddy can get its certificate). Safe to run again.
set -euo pipefail
umask 077

ALLOWED="${1:?usage: enable-web-page.sh <allowed addresses, comma-separated, or *>}"
API_HOST="api.singitai.app"
PAGE_DOMAIN="singitai.app"
PAGE_ORIGIN="https://singitai.app"
ENV_FILE=/etc/sign402-gateway.env
CADDYFILE=/etc/caddy/Caddyfile
GW="$HOME/apps/sign402/sign402-gateway"
WEB_UNIT=/etc/systemd/system/sign402-web-api.service

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

step "1. DNS"
public_ip=$(curl -fsS -4 https://api.ipify.org)
resolved=$(getent ahostsv4 "$API_HOST" | awk 'NR==1 {print $1}' || true)
[ "$resolved" = "$public_ip" ] || fail "$API_HOST resolves to '${resolved:-nothing}', not this server ($public_ip). Add an A record (DNS only) and wait a minute."
echo "$API_HOST -> $public_ip"

step "2. Settings (sudo)"
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
set_env SIGN402_WEB_DOMAIN "$PAGE_DOMAIN"
set_env SIGN402_WEB_URI "$PAGE_ORIGIN"
set_env SIGN402_WEB_CORS_ORIGIN "$PAGE_ORIGIN"
set_env SIGN402_WEB_ALLOWED_ADDRESSES "$ALLOWED"
if ! sudo grep -q '^SIGN402_WEB_INTERNAL_TOKEN=.\{32,\}' "$ENV_FILE"; then
  set_env SIGN402_WEB_INTERNAL_TOKEN "$("$GW/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi
echo "web settings written (previous env kept as $ENV_FILE.bak-$stamp)"

step "3. The web API and the gateway"
sudo tee "$WEB_UNIT" >/dev/null <<UNIT
[Unit]
Description=SingIt web API (/web/v1, loopback; exposed by Caddy)
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
sudo systemctl restart sign402-gateway   # it reads the internal token and links Telegram to web accounts
for _ in $(seq 40); do curl -fsS http://127.0.0.1:8099/health >/dev/null 2>&1 && break; sleep 0.5; done
sudo systemctl enable -q sign402-web-api
sudo systemctl restart sign402-web-api
for _ in $(seq 20); do ss -ltn | grep -q "127.0.0.1:8130 " && break; sleep 0.5; done
ss -ltn | grep -q "127.0.0.1:8130 " || fail "web API: sudo journalctl -u sign402-web-api -n 30"
sudo systemctl restart sign402-allowance-watcher  # notices for linked web accounts
echo "web API on 127.0.0.1:8130"

step "4. Caddy for https://$API_HOST"
[ -f "$CADDYFILE" ] && sudo cp -p "$CADDYFILE" "$CADDYFILE.bak-$stamp"
sudo tee "$CADDYFILE" >/dev/null <<CADDY
# SingIt web API (scripts/enable-web-page.sh). The page lives at $PAGE_ORIGIN/app/.
$API_HOST {
	handle /web/v1/* {
		reverse_proxy 127.0.0.1:8130
	}
	respond 404
}
CADDY
sudo caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 80/tcp >/dev/null
  sudo ufw allow 443/tcp >/dev/null
fi
sudo systemctl enable -q caddy
sudo systemctl restart caddy

step "5. From outside"
for _ in $(seq 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "https://$API_HOST/web/v1/session" || true)
  [ "$code" = "401" ] && break
  sleep 2
done
[ "$code" = "401" ] || fail "https://$API_HOST/web/v1/session answered '$code' (expected 401 before sign-in): sudo journalctl -u caddy -n 40"
echo "https://$API_HOST/web/v1 answers (401 until you sign in, as it should)"
cat <<TEXT

Done. Publish website/app/ with the site, open $PAGE_ORIGIN/app/ and sign in with
one of: $ALLOWED

Turn it off: set SIGN402_WEB_ENABLED=0 in $ENV_FILE, then
  sudo systemctl disable --now sign402-web-api caddy && sudo systemctl restart sign402-gateway
TEXT
