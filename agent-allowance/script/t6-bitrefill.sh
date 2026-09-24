#!/usr/bin/env bash
# T6: does Bitrefill credit an invoice paid through the limiter?
#
# A Base USDC invoice is normally paid by a direct `transfer` from the payer.
# Here the payment is `spend` on AgentAllowance, which moves the owner's USDC
# with `transferFrom`: the same Transfer event on chain, sent by a contract.
# Design: docs/trezor-allowance-v1.md, check T6.
#
#   ./script/t6-bitrefill.sh setup <cap-usdc>     new limiter sized to the product, grant from the Trezor
#   ./script/t6-bitrefill.sh pay <address> <amount-usdc> <invoice-id>
#   ./script/t6-bitrefill.sh revoke
#   ./script/t6-bitrefill.sh status
#
# The invoice itself is created through the Bitrefill MCP server, after the
# owner confirms the product and price (AGENTS.md). This script only pays it.
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
TRANSFER_TOPIC=0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef

mkdir -p "$STATE_DIR"; touch "$STATE" "$LOG"; chmod 600 "$STATE" "$LOG"
# shellcheck disable=SC1090
source "$STATE_DIR/t4.env"   # AGENT, GUARDIAN
# shellcheck disable=SC1090
source "$STATE"

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

cmd_pay() {
  require_limiter
  local to=${1:?usage: pay <address> <amount-usdc> <invoice-id>} amount_text=${2:?} invoice=${3:?} amount ref out tx receipt
  [[ "$to" =~ ^0x[0-9a-fA-F]{40}$ ]] || { echo "Not an address: $to"; exit 1; }
  amount=$(atomic "$amount_text")
  ref=$(cast keccak "bitrefill:$invoice")
  local left today used
  left=$(word "$LIMITER" 'allowanceLeft()(uint256)'); today=$(word "$LIMITER" 'remainingToday()(uint256)')
  used=$(rcast call "$LIMITER" 'usedRef(bytes32)(bool)' "$ref")
  [[ "$used" == false ]] || { echo "This invoice was already paid through the limiter."; exit 1; }
  (( amount <= CAP )) || { echo "Amount $amount exceeds the limiter cap $CAP."; exit 1; }
  (( amount <= left && amount <= today )) || { echo "Not enough allowance ($left) or daily budget ($today)."; exit 1; }
  echo "Pay Bitrefill invoice $invoice"
  echo "    $amount_text USDC on Base, from the Trezor address through the limiter, to"
  echo "    $to"
  ask "Send it?" || { echo "Not sent."; exit 0; }
  out=$(cast send "$LIMITER" "spend(address,uint256,bytes32)" "$to" "$amount" "$ref" \
    "${AGENT_SIGNER[@]}" --rpc-url "$RPC" --json)
  tx=$(python3 -c 'import json,sys;print(json.load(sys.stdin)["transactionHash"])' <<< "$out")
  save PAY_TX "$tx"; save INVOICE "$invoice"
  receipt=$(rcast receipt "$tx" --json)
  python3 - "$receipt" "$TRANSFER_TOPIC" "$OWNER" "$to" "$amount" <<'PY'
import json, sys
rc, topic, owner, to, amount = json.loads(sys.argv[1]), *sys.argv[2:]
hits = [l for l in rc["logs"] if l["topics"][0] == topic
        and l["topics"][1][-40:].lower() == owner[2:].lower()
        and l["topics"][2][-40:].lower() == to[2:].lower()
        and int(l["data"], 16) == int(amount)]
if int(rc["status"], 16) != 1 or len(hits) != 1:
    sys.exit("The transaction did not produce exactly the expected USDC transfer.")
print(f"On chain: USDC Transfer {owner} -> {to} {amount}, block {int(rc['blockNumber'], 16)}")
PY
  log "## T6 payment $(date -u '+%Y-%m-%d %H:%M UTC')"
  log "- invoice $invoice, $amount atomic USDC to $to, tx $tx"
  echo "Paid on chain. Whether Bitrefill credits it is the question T6 answers."
}

cmd_revoke() {
  require_limiter
  if [[ -n "${REVOKE_CMD:-}" ]]; then eval "$REVOKE_CMD"; else sidecar revoke "$LIMITER"; fi
  wait_for "allowanceLeft()" 0 word "$LIMITER" 'allowanceLeft()(uint256)'
  log "- revoked: allowanceLeft $(word "$LIMITER" 'allowanceLeft()(uint256)')"
}

cmd_status() {
  require_limiter
  echo "limiter $LIMITER cap ${CAP:-?}"
  echo "allowanceLeft $(word "$LIMITER" 'allowanceLeft()(uint256)') remainingToday $(word "$LIMITER" 'remainingToday()(uint256)')"
  echo "owner USDC $(word "$USDC" 'balanceOf(address)(uint256)' "$OWNER")"
  [[ -n "${PAY_TX:-}" ]] && echo "paid invoice ${INVOICE:-?} in $PAY_TX"
  return 0
}

case "${1:-}" in
  setup) shift; cmd_setup "$@" ;;
  pay) shift; cmd_pay "$@" ;;
  revoke) cmd_revoke ;;
  status) cmd_status ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
