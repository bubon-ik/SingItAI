#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
BACKUP_ROOT=${SIGN402_BACKUP_ROOT:-"$HOME/sign402-backups"}
DEST="$BACKUP_ROOT/$TIMESTAMP"
STATE_DIR=${SIGN402_STATE_DIR:-"$HOME/.sign402"}
HERMES_ENV=${SIGN402_HERMES_ENV:-"$HOME/.hermes/.env"}
SYSTEM_ENV=${SIGN402_SYSTEM_ENV:-"/etc/sign402-gateway.env"}

umask 077
mkdir -p "$DEST/state" "$DEST/repo-state" "$DEST/secrets"

log() {
  printf '%s\n' "$*"
}

copy_file() {
  src=$1
  dest=$2
  if [ -f "$src" ] && [ -r "$src" ]; then
    mkdir -p "$(dirname -- "$dest")"
    cp -p "$src" "$dest"
    chmod 600 "$dest" 2>/dev/null || true
    log "copied: $src"
  elif [ -e "$src" ]; then
    log "warning: exists but is not readable: $src"
  fi
}

backup_sqlite() {
  src=$1
  dest=$2
  if [ ! -f "$src" ] || [ ! -r "$src" ]; then
    return 0
  fi
  mkdir -p "$(dirname -- "$dest")"
  if command -v python3 >/dev/null 2>&1; then
    if python3 - "$src" "$dest" <<'PY'
import sqlite3
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)

src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
try:
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
finally:
    src.close()
PY
    then
      chmod 600 "$dest" 2>/dev/null || true
      log "sqlite backup: $src"
      return 0
    fi
  fi
  cp -p "$src" "$dest"
  chmod 600 "$dest" 2>/dev/null || true
  log "copied sqlite without online backup: $src"
}

if [ -d "$STATE_DIR" ]; then
  find "$STATE_DIR" -maxdepth 1 -type f \( -name '*.db' -o -name '*.sqlite3' \) | while IFS= read -r file; do
    backup_sqlite "$file" "$DEST/state/$(basename -- "$file")"
  done
  find "$STATE_DIR" -maxdepth 1 -type f \( -name '*.json' -o -name '*.yaml' -o -name '*.yml' \) | while IFS= read -r file; do
    copy_file "$file" "$DEST/state/$(basename -- "$file")"
  done
else
  log "warning: state directory not found: $STATE_DIR"
fi

copy_file "$ROOT_DIR/demo-dashboard/latest-run.json" "$DEST/repo-state/latest-run.json"
copy_file "$ROOT_DIR/demo-dashboard/agent-state.json" "$DEST/repo-state/agent-state.json"
backup_sqlite "$ROOT_DIR/demo-dashboard/bitrefill-orders.sqlite3" "$DEST/repo-state/bitrefill-orders.sqlite3"

copy_file "$HERMES_ENV" "$DEST/secrets/hermes.env"
copy_file "$SYSTEM_ENV" "$DEST/secrets/sign402-gateway.env"

{
  printf 'created_at_utc=%s\n' "$TIMESTAMP"
  printf 'host=%s\n' "$(hostname 2>/dev/null || printf unknown)"
  printf 'repo=%s\n' "$ROOT_DIR"
  printf 'state_dir=%s\n' "$STATE_DIR"
  printf 'contains_secrets=yes\n'
} > "$DEST/MANIFEST.txt"
chmod 600 "$DEST/MANIFEST.txt" 2>/dev/null || true

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$DEST" && find . -type f ! -name SHA256SUMS -print | sort | xargs sha256sum > SHA256SUMS)
elif command -v shasum >/dev/null 2>&1; then
  (cd "$DEST" && find . -type f ! -name SHA256SUMS -print | sort | xargs shasum -a 256 > SHA256SUMS)
fi
chmod 600 "$DEST/SHA256SUMS" 2>/dev/null || true

log "backup ready: $DEST"
log "warning: this backup may contain wallet master keys and API tokens; keep it private."
