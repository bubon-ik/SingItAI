#!/usr/bin/env bash
# T6: a Bitrefill purchase through x402, paid by the agent, funded by the limiter.
#
# The owner chose x402 over Bitrefill's MCP route for this (an exception to
# AGENTS.md they authorised); confirmation before every order still applies.
# Design: docs/trezor-allowance-v1.md, check T6.
#
#   ./script/t6-bitrefill.sh grant <usdc>                 grant the limiter from the Trezor
#   ./script/t6-bitrefill.sh buy <slug> "<package>" [--dry-run]
#   ./script/t6-bitrefill.sh revoke                       revoke it from the Trezor
#   ./script/t6-bitrefill.sh status
#   ./script/t6-bitrefill.sh setup <cap-usdc>             a new limiter, for products above the T4 caps
#
# The limiter is the one `setup` deployed if any, else the T4 limiter.
# Reuses the agent and guardian keys from T4 (~/.sign402-trezor-poc/t4.env).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SIDECAR="$HERE/../trezor-sidecar"
SIDECAR_ENV="${SIDECAR_ENV:-$HOME/.config/sign402-trezor-poc/sidecar.env}"
STATE_DIR="${STATE_DIR:-$HOME/.sign402-trezor-poc}"
STATE="$STATE_DIR/t6.env"
LOG="$STATE_DIR/t6-log.md"
RPC="${RPC:-https://mainnet.base.org}"
USDC=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
OWNER="${OWNER:-0xB80b5Ca13583fB7E0236db4bD8834B9035654558}"
read -r -a AGENT_SIGNER <<< "${AGENT_SIGNER:---account sign402-agent}"
ASSUME_YES="${ASSUME_YES:-0}"
SKIP_VERIFY="${SKIP_VERIFY:-0}"

mkdir -p "$STATE_DIR"; touch "$STATE" "$LOG"; chmod 600 "$STATE" "$LOG"
# shellcheck disable=SC1090
source "$STATE_DIR/t4.env"   # AGENT, GUARDIAN, LIMITER (T4's)
T4_LIMITER="${LIMITER:-}"
LIMITER=""
# shellcheck disable=SC1090
source "$STATE"
LIMITER="${LIMITER:-$T4_LIMITER}"

save() {
  grep -v "^$1=" "$STATE" > "$STATE.tmp" || true
  echo "$1=$2" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; printf -v "$1" '%s' "$2"
}
log() { echo "$*" | tee -a "$LOG"; }
ask() { [[ "$ASSUME_YES" == 1 ]] && return 0; local a; read -r -p "$1 [y/N] " a; [[ "$a" == y || "$a" == Y ]]; }
rcast() {
  local out rc attempt
  for attempt in 1 2 3 4 5 6; do
    out=$(cast "$@" --rpc-url "$RPC" 2>&1) && rc=0 || rc=$?
    if (( rc == 0 )); then printf '%s\n' "$out"; return 0; fi
    if grep -qiE '429|rate.?limit|too many requests' <<< "$out"; then sleep $((attempt * 2)); continue; fi
    printf '%s\n' "$out"; return $rc
  done
  printf '%s\n' "$out"; return 1
}
word() { rcast call "$@" | awk '{print $1}'; }
wait_for() {
  local what=$1 want=$2 got i; shift 2
  for i in $(seq 1 20); do got=$("$@") && [[ "$got" == "$want" ]] && return 0; sleep 2; done
  log "- WARNING: $what still reads ${got:-nothing}, expected $want (RPC node behind the chain)"
}
atomic() {  # "1.25" -> 1250000, exact or refused
  python3 - "$1" <<'PY'
import sys, re
from decimal import Decimal
t = sys.argv[1]
if not re.fullmatch(r"[0-9]+(\.[0-9]{1,6})?", t) or Decimal(t) <= 0:
    sys.exit(f"not a USDC amount: {t!r}")
print(int(Decimal(t) * 1_000_000))
PY
}
sidecar() { ( cd "$SIDECAR"; set -a; source "$SIDECAR_ENV"; set +a; .venv/bin/python -m trezor_sidecar.allowance "$@" ); }
require_limiter() { [[ "${LIMITER:-}" =~ ^0x[0-9a-fA-F]{40}$ ]] || { echo "Run setup first."; exit 1; }; }

cmd_setup() {
  local cap_text=${1:?usage: setup <cap-usdc>} cap
  cap=$(atomic "$cap_text")
  if [[ -z "${LIMITER:-}" ]]; then
    echo "Deploying a T6 limiter: per purchase = daily cap = $cap_text USDC, expires in 1 day."
    echo "Type the sign402-agent password."
    local out
    out=$(cd "$HERE" && forge create src/AgentAllowance.sol:AgentAllowance --rpc-url "$RPC" \
      "${AGENT_SIGNER[@]}" --broadcast --constructor-args "$USDC" "$OWNER" "$AGENT" "$GUARDIAN" \
      "$cap" "$cap" "$(( $(date +%s) + 86400 ))")
    save LIMITER "$(awk '/Deployed to:/{print $3}' <<< "$out")"
    save CAP "$cap"
    log "## T6 setup $(date -u '+%Y-%m-%d %H:%M UTC')"
    log "- limiter $LIMITER, cap $cap, deploy tx $(awk '/Transaction hash:/{print $3}' <<< "$out")"
    if [[ "$SKIP_VERIFY" != 1 ]]; then
      (cd "$HERE" && forge verify-contract --chain 8453 --watch "$LIMITER" src/AgentAllowance.sol:AgentAllowance) \
        || echo "Verification did not complete; check the link before granting."
    fi
  fi
  require_limiter
  echo; echo "On your PHONE: https://base.blockscout.com/address/$LIMITER?tab=contract"
  echo "owner $OWNER, agent $AGENT, guardian $GUARDIAN, caps $CAP."
  [[ "$ASSUME_YES" == 1 ]] || read -r -p "Checked — press Enter to grant $cap_text USDC from the Trezor " _
  if [[ -n "${GRANT_CMD:-}" ]]; then eval "$GRANT_CMD"; else sidecar grant "$LIMITER" "$cap_text"; fi
  wait_for "allowanceLeft()" "$cap" word "$LIMITER" 'allowanceLeft()(uint256)'
  log "- granted: allowanceLeft $(word "$LIMITER" 'allowanceLeft()(uint256)')"
}

cmd_grant() {
  require_limiter
  local amount_text=${1:?usage: grant <usdc>} amount
  amount=$(atomic "$amount_text")
  if [[ -n "${GRANT_CMD:-}" ]]; then eval "$GRANT_CMD"; else sidecar grant "$LIMITER" "$amount_text"; fi
  wait_for "allowanceLeft()" "$amount" word "$LIMITER" 'allowanceLeft()(uint256)'
  log "- granted $amount to $LIMITER"
}

cmd_buy() {
  require_limiter
  LIMITER="$LIMITER" "$SIDECAR/.venv/bin/python" "$HERE/script/bitrefill_x402.py" "$@"
}

cmd_revoke() {
  require_limiter
  if [[ -n "${REVOKE_CMD:-}" ]]; then eval "$REVOKE_CMD"; else sidecar revoke "$LIMITER"; fi
  wait_for "allowanceLeft()" 0 word "$LIMITER" 'allowanceLeft()(uint256)'
  log "- revoked: allowanceLeft $(word "$LIMITER" 'allowanceLeft()(uint256)')"
}

cmd_status() {
  require_limiter
  echo "limiter $LIMITER per purchase $(word "$LIMITER" 'perPurchaseCap()(uint256)')"
  echo "allowanceLeft $(word "$LIMITER" 'allowanceLeft()(uint256)') remainingToday $(word "$LIMITER" 'remainingToday()(uint256)')"
  echo "owner USDC $(word "$USDC" 'balanceOf(address)(uint256)' "$OWNER")"
  return 0
}

case "${1:-}" in
  setup) shift; cmd_setup "$@" ;;
  grant) shift; cmd_grant "$@" ;;
  buy) shift; cmd_buy "$@" ;;
  revoke) cmd_revoke ;;
  status) cmd_status ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
