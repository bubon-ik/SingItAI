#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
DEFAULT_SOURCE="$SCRIPT_DIR/../hermes-plugins/sign402-wallet"
SOURCE=${SIGN402_PLUGIN_SOURCE:-"$DEFAULT_SOURCE"}

if [ ! -d "$SOURCE" ] || [ ! -f "$SOURCE/plugin.yaml" ]; then
  printf 'Sign402 wallet plugin source is invalid: %s\n' "$SOURCE" >&2
  exit 1
fi

SOURCE=$(CDPATH= cd -- "$SOURCE" && pwd -P)
PLUGIN_DIR="$HOME/.hermes/plugins"
DESTINATION="$PLUGIN_DIR/sign402-wallet"

if ! command -v hermes >/dev/null 2>&1; then
  printf 'Hermes CLI was not found on PATH.\n' >&2
  exit 1
fi

mkdir -p "$PLUGIN_DIR"

if [ -L "$DESTINATION" ]; then
  CURRENT_TARGET=$(readlink "$DESTINATION")
  if [ "$CURRENT_TARGET" != "$SOURCE" ]; then
    printf 'Refusing to replace existing plugin symlink: %s\n' "$DESTINATION" >&2
    exit 1
  fi
elif [ -e "$DESTINATION" ]; then
  printf 'Refusing to replace existing plugin path: %s\n' "$DESTINATION" >&2
  exit 1
else
  ln -s "$SOURCE" "$DESTINATION"
fi

hermes plugins enable sign402-wallet
printf 'Sign402 wallet plugin enabled at %s\n' "$DESTINATION"
