#!/bin/sh
set -eu

if [ -z "${SIGN402_TREZOR_LOCAL_AGENT_HOME:-}" ]; then
  printf '%s\n' 'SIGN402_TREZOR_LOCAL_AGENT_HOME is required.' >&2
  exit 1
fi

expected_local_agent_home="$HOME/.sign402-trezor-agent"
if [ "$SIGN402_TREZOR_LOCAL_AGENT_HOME" != "$expected_local_agent_home" ]; then
  printf 'SIGN402_TREZOR_LOCAL_AGENT_HOME must be exactly %s\n' \
    "$expected_local_agent_home" >&2
  exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
default_source="$script_dir/../hermes-local-plugin"
source_dir=${SIGN402_TREZOR_LOCAL_PLUGIN_SOURCE:-"$default_source"}

if [ ! -d "$source_dir" ] || [ ! -f "$source_dir/plugin.yaml" ]; then
  printf 'Local Trezor plugin source is invalid: %s\n' "$source_dir" >&2
  exit 1
fi

mkdir -p "$SIGN402_TREZOR_LOCAL_AGENT_HOME"
local_agent_home=$(CDPATH= cd -- "$SIGN402_TREZOR_LOCAL_AGENT_HOME" && pwd -P)

source_dir=$(CDPATH= cd -- "$source_dir" && pwd -P)
plugins_dir="$local_agent_home/.hermes/plugins"
destination="$plugins_dir/sign402-trezor-local"
mkdir -p "$plugins_dir"

if [ -L "$destination" ]; then
  current_target=$(readlink "$destination")
  if [ "$current_target" != "$source_dir" ]; then
    printf 'Refusing to replace existing plugin symlink: %s\n' "$destination" >&2
    exit 1
  fi
elif [ -e "$destination" ]; then
  printf 'Refusing to replace existing plugin path: %s\n' "$destination" >&2
  exit 1
else
  ln -s "$source_dir" "$destination"
fi

printf 'Local Trezor plugin staged for a dedicated Hermes instance at %s\n' "$destination"
printf '%s\n' 'No command was run against the working ~/.hermes instance.'
