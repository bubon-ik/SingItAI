#!/bin/bash
# Run the owner's side of the Trezor lane in the background on macOS:
# the SSH tunnel to the broker, the local sidecar and the companion.
#
#   trezor-sidecar/macos/install-launch-agents.sh            install or update, then start
#   trezor-sidecar/macos/install-launch-agents.sh status     what is running
#   trezor-sidecar/macos/install-launch-agents.sh uninstall  stop and remove
#
# Three LaunchAgents start at login and restart on failure. The code runs from
# a copy under ~/Library/Application Support: macOS does not let background
# agents read ~/Documents, where the checkout lives. Secrets stay where they
# are (the two env files in ~/.config); the plists hold none. Run again after
# pulling new code to update the copy.
#
# Trezor Suite itself is a desktop app: keep it open (or in Login Items) with
# the device connected and unlocked.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd -P)"
HOME_DIR="$HOME/Library/Application Support/Sign402 Trezor"
VENV="$HOME_DIR/venv"
RUNNER="$HOME_DIR/run-with-env.sh"
LOGS="$HOME/Library/Logs/Sign402 Trezor"
AGENTS="$HOME/Library/LaunchAgents"
SIDECAR_ENV="${SIGN402_TREZOR_SIDECAR_ENV:-$HOME/.config/sign402-trezor-poc/sidecar.env}"
COMPANION_ENV="${SIGN402_TREZOR_COMPANION_ENV:-$HOME/.config/sign402-trezor-companion.env}"
VPS="${SIGN402_TREZOR_VPS:-hermes@164.68.104.44}"
PYTHON="${SIGN402_TREZOR_PYTHON:-$(command -v python3)}"
DOMAIN="gui/$(id -u)"
LABELS=(com.sign402.trezor-tunnel com.sign402.trezor-sidecar com.sign402.trezor-companion)

fail() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

status() {
  for label in "${LABELS[@]}"; do
    line=$(launchctl print "$DOMAIN/$label" 2>/dev/null | awk -F' = ' '/^\tstate = /{s=$2} /^\tpid = /{p=$2} END{print s" pid="p}')
    printf '%-32s %s\n' "$label" "${line:-not loaded}"
  done
  echo "logs: $LOGS"
}

unload() {
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  done
}

case "${1:-install}" in
  status) status; exit 0 ;;
  uninstall)
    unload
    for label in "${LABELS[@]}"; do rm -f "$AGENTS/$label.plist"; done
    echo "removed; the copy in $HOME_DIR and logs in $LOGS are kept"
    exit 0 ;;
  install) ;;
  *) fail "usage: $0 [install|status|uninstall]" ;;
esac

[ -f "$SIDECAR_ENV" ] || fail "no sidecar settings at $SIDECAR_ENV"
[ -f "$COMPANION_ENV" ] || fail "no companion settings at $COMPANION_ENV"
[ -x "$PYTHON" ] || fail "no python3 found; set SIGN402_TREZOR_PYTHON"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$VPS" true 2>/dev/null \
  || fail "cannot reach $VPS over SSH without a prompt; the tunnel needs a key that works in the background"

# Stop our own agents first, so the port checks below only see strangers.
unload
sleep 1
for port in 8122 8111; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "port $port is in use. Close the tunnel/sidecar/companion you started in a terminal (Ctrl+C), then run this again"
  fi
done

mkdir -p "$HOME_DIR" "$LOGS" "$AGENTS"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q "$SRC"
"$VENV/bin/python" -m pip install -q --force-reinstall --no-deps "$SRC"  # same version number, new code
"$VENV/bin/python" -c "import trezor_sidecar.limiter as l; assert l.ARTIFACT_PATH.exists(), 'artifact missing'"
echo "installed $(cd "$SRC" && git rev-parse --short HEAD 2>/dev/null || echo 'this checkout') into $HOME_DIR"

cat > "$RUNNER" <<'RUN'
#!/bin/bash
# run-with-env.sh <env file> <command...>: load private settings, then run.
set -euo pipefail
set -a
. "$1"
set +a
shift
exec "$@"
RUN
chmod 755 "$RUNNER"

plist() {  # label, then the program and its arguments
  local label="$1"; shift
  {
    cat <<HEAD
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
HEAD
    for arg in "$@"; do
      arg=${arg//&/&amp;}; arg=${arg//</&lt;}; arg=${arg//>/&gt;}
      printf '    <string>%s</string>\n' "$arg"
    done
    cat <<TAIL
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOGS/${label#com.sign402.}.log</string>
  <key>StandardErrorPath</key><string>$LOGS/${label#com.sign402.}.log</string>
</dict>
</plist>
TAIL
  } > "$AGENTS/$label.plist"
  plutil -lint -s "$AGENTS/$label.plist"
}

plist com.sign402.trezor-tunnel /usr/bin/ssh -N \
  -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:8122:127.0.0.1:8122 "$VPS"
plist com.sign402.trezor-sidecar "$RUNNER" "$SIDECAR_ENV" "$VENV/bin/sign402-trezor-sidecar"
plist com.sign402.trezor-companion "$RUNNER" "$COMPANION_ENV" "$VENV/bin/sign402-trezor-companion" run

for label in "${LABELS[@]}"; do
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
  launchctl enable "$DOMAIN/$label"
done
sleep 5
status
