#!/usr/bin/env bash
# T4: the Trezor allowance on Base mainnet, end to end, for cents.
#
# Procedure and reasoning: docs/trezor-allowance-t4-runbook.md.
#
# The script does everything that is not the owner's to do. The owner:
#   - chooses the two key passwords and types them when asked,
#   - sends 0.0002 ETH on Base to the agent address from Trezor Suite,
#   - confirms two approves on the Trezor (grant 1.00, revoke),
#   - answers y before the one real 0.30 USDC purchase.
#
# Resumable: addresses and hashes are kept in ~/.sign402-trezor-poc/t4.env, so
# rerunning skips what is done. Results go to ~/.sign402-trezor-poc/t4-log.md.
# Neither file ever holds a password or a key.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SIDECAR="$HERE/../trezor-sidecar"
SIDECAR_ENV="${SIDECAR_ENV:-$HOME/.config/sign402-trezor-poc/sidecar.env}"
STATE_DIR="${STATE_DIR:-$HOME/.sign402-trezor-poc}"
STATE="$STATE_DIR/t4.env"
LOG="$STATE_DIR/t4-log.md"
KEYSTORES="${KEYSTORES:-$HOME/.foundry/keystores}"
RPC="${RPC:-https://mainnet.base.org}"
USDC=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
OWNER="${OWNER:-0xB80b5Ca13583fB7E0236db4bD8834B9035654558}"
read -r -a AGENT_SIGNER <<< "${AGENT_SIGNER:---account sign402-agent}"
ASSUME_YES="${ASSUME_YES:-0}"   # rehearsal only
SKIP_VERIFY="${SKIP_VERIFY:-0}" # rehearsal only

GRANT_USDC=1.00
DAILY_CAP=500000      # 0.50 USDC
PER_PURCHASE=300000   # 0.30 USDC
SPEND=300000          # the one real purchase
MIN_GAS_WEI=100000000000000   # 0.0001 ETH
BASE_CHAIN_ID=8453

mkdir -p "$STATE_DIR"
touch "$STATE" "$LOG"
chmod 600 "$STATE" "$LOG"
# shellcheck disable=SC1090
source "$STATE"
FAILS=0

save() {
  grep -v "^$1=" "$STATE" > "$STATE.tmp" || true
  echo "$1=$2" >> "$STATE.tmp"
  mv "$STATE.tmp" "$STATE"
  printf -v "$1" '%s' "$2"
}
log() { echo "$*" | tee -a "$LOG"; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; echo "" >> "$LOG"; echo "## $*" >> "$LOG"; }
ask() {
  [[ "$ASSUME_YES" == 1 ]] && return 0
  local a; read -r -p "$1 [y/N] " a; [[ "$a" == y || "$a" == Y ]]
}
pause() { [[ "$ASSUME_YES" == 1 ]] || read -r -p "$1 — press Enter to continue " _; }
usdc() { cast call "$USDC" "balanceOf(address)(uint256)" "$1" --rpc-url "$RPC" | awk '{print $1}'; }
limiter_word() { cast call "$LIMITER" "$1" --rpc-url "$RPC" | awk '{print $1}'; }

sidecar() {
  ( cd "$SIDECAR"
    set -a; source "$SIDECAR_ENV"; set +a
    .venv/bin/python -m trezor_sidecar.allowance "$@" )
}
grant()  { if [[ -n "${GRANT_CMD:-}" ]];  then eval "$GRANT_CMD";  else sidecar grant "$LIMITER" "$GRANT_USDC"; fi; }
revoke() { if [[ -n "${REVOKE_CMD:-}" ]]; then eval "$REVOKE_CMD"; else sidecar revoke "$LIMITER"; fi; }

# A refusal is checked with eth_call: the transaction runs against the live
# chain and nothing is sent.
expect_revert() {
  local name=$1 want=$2; shift 2
  local out got
  if out=$(cast call "$@" --rpc-url "$RPC" 2>&1); then
    log "| $name | \`$want\` | no revert | **FAIL** |"; FAILS=$((FAILS + 1)); return
  fi
  if grep -q -- "$want" <<< "$out"; then
    log "| $name | \`$want\` | \`$want\` | pass |"
  else
    got=$(grep -oE 'custom error 0x[0-9a-f]{8}|execution reverted: [^"]*' <<< "$out" | head -1)
    log "| $name | \`$want\` | ${got:-unreadable} | **FAIL** |"; FAILS=$((FAILS + 1))
  fi
}
expect_ok() {
  local name=$1; shift
  if cast call "$@" --rpc-url "$RPC" > /dev/null 2>&1; then
    log "| $name | no revert | no revert | pass |"
  else
    log "| $name | no revert | reverted | **FAIL** |"; FAILS=$((FAILS + 1))
  fi
}
spend_sig="spend(address,uint256,bytes32)"
ref() { cast keccak "t4-$1"; }

# ---------------------------------------------------------------------------
step "0. Preconditions"
[[ "$(cast chain-id --rpc-url "$RPC")" == "$BASE_CHAIN_ID" ]] || { echo "RPC is not Base"; exit 1; }
log "- run started $(date -u '+%Y-%m-%d %H:%M UTC'), RPC chain $BASE_CHAIN_ID"
log "- owner (Trezor) $OWNER: $(usdc "$OWNER") atomic USDC, $(cast balance "$OWNER" --rpc-url "$RPC" --ether) ETH"
if [[ -z "${GRANT_CMD:-}" ]]; then
  [[ -f "$SIDECAR_ENV" ]] || { echo "Missing $SIDECAR_ENV"; exit 1; }
  grep -q '^SIGN402_TREZOR_POC_ENABLED=1' "$SIDECAR_ENV" || {
    echo "Set SIGN402_TREZOR_POC_ENABLED=1 in $SIDECAR_ENV for this run."; exit 1; }
fi

# ---------------------------------------------------------------------------
step "1. Agent and guardian keys"
new_key() {
  local name=$1 var=$2 out addr
  if [[ -n "${!var:-}" ]]; then log "- $name: $(eval echo "\$$var") (already created)"; return; fi
  if [[ -f "$KEYSTORES/$name" ]]; then
    echo "$KEYSTORES/$name exists but its address was not recorded. Type its password:"
    addr=$(cast wallet address --keystore "$KEYSTORES/$name")
  else
    mkdir -p "$KEYSTORES"
    echo "Creating $name. Choose a password and keep it: it is asked for on each"
    echo "transaction this key sends. It is not stored anywhere by this script."
    out=$(cast wallet new "$KEYSTORES" "$name")
    addr=$(awk '/^Address:/{print $2}' <<< "$out")
  fi
  [[ "$addr" =~ ^0x[0-9a-fA-F]{40}$ ]] || { echo "Could not read the $name address."; exit 1; }
  save "$var" "$addr"
  log "- $name: $addr"
}
new_key sign402-agent AGENT
new_key sign402-guardian GUARDIAN

# ---------------------------------------------------------------------------
step "2. Gas for the agent"
bal=$(cast balance "$AGENT" --rpc-url "$RPC")
if (( bal < MIN_GAS_WEI )); then
  echo "From Trezor Suite, send 0.0002 ETH on Base to:"
  echo "    $AGENT"
  echo "Waiting for it to arrive (checks every 10 s, Ctrl-C to stop and rerun later)…"
  while (( $(cast balance "$AGENT" --rpc-url "$RPC") < MIN_GAS_WEI )); do sleep 10; done
fi
log "- agent gas: $(cast balance "$AGENT" --rpc-url "$RPC" --ether) ETH"

# ---------------------------------------------------------------------------
step "3. Deploy the limiter"
if [[ -z "${LIMITER:-}" ]]; then
  expiry=$(( $(date +%s) + 7 * 86400 ))
  echo "Deploying with the agent key (type the sign402-agent password)."
  out=$(cd "$HERE" && forge create src/AgentAllowance.sol:AgentAllowance \
    --rpc-url "$RPC" "${AGENT_SIGNER[@]}" --broadcast \
    --constructor-args "$USDC" "$OWNER" "$AGENT" "$GUARDIAN" "$DAILY_CAP" "$PER_PURCHASE" "$expiry")
  save LIMITER "$(awk '/Deployed to:/{print $3}' <<< "$out")"
  save DEPLOY_TX "$(awk '/Transaction hash:/{print $3}' <<< "$out")"
fi
[[ "$LIMITER" =~ ^0x[0-9a-fA-F]{40}$ ]] || { echo "No limiter address."; exit 1; }
log "- limiter: $LIMITER"
log "- deploy tx: ${DEPLOY_TX:-unknown}"
for f in owner agent guardian token; do
  got=$(cast call "$LIMITER" "$f()(address)" --rpc-url "$RPC")
  log "- $f(): $got"
done
log "- dailyCap(): $(limiter_word 'dailyCap()(uint256)'), perPurchaseCap(): $(limiter_word 'perPurchaseCap()(uint256)'), expiry(): $(limiter_word 'expiry()(uint256)')"

# ---------------------------------------------------------------------------
step "4. Publish the source"
if [[ "$SKIP_VERIFY" != 1 && -z "${VERIFIED:-}" ]]; then
  if (cd "$HERE" && forge verify-contract --chain "$BASE_CHAIN_ID" --watch "$LIMITER" src/AgentAllowance.sol:AgentAllowance); then
    save VERIFIED 1
  else
    echo "Verification did not complete; the test can continue, but check the link below."
  fi
fi
log "- source: https://base.blockscout.com/address/$LIMITER?tab=contract"
echo
echo "On your PHONE, not this Mac, open:"
echo "    https://base.blockscout.com/address/$LIMITER?tab=contract"
echo "Check: verified AgentAllowance; owner $OWNER; token USDC;"
echo "agent $AGENT; guardian $GUARDIAN; dailyCap 500000; perPurchaseCap 300000."
pause "Checked on the phone"

# ---------------------------------------------------------------------------
step "5. Grant 1.00 USDC from the Trezor"
allowance=$(limiter_word 'allowanceLeft()(uint256)')
if [[ -z "${GRANTED:-}" ]]; then
  echo "Trezor Suite open, MCP on, device unlocked. Confirm on the device only if it"
  echo "shows an approve, $LIMITER in full, and 1 USDC. Photograph each screen."
  grant
  save GRANTED 1
  allowance=$(limiter_word 'allowanceLeft()(uint256)')
fi
log "- allowanceLeft(): $allowance, remainingToday(): $(limiter_word 'remainingToday()(uint256)')"

# ---------------------------------------------------------------------------
step "6. Refusals before the purchase (eth_call, nothing sent)"
log "| Case | Expected | Observed | Result |"
log "| --- | --- | --- | --- |"
expect_revert "0.31 over the per-purchase cap" 0x80b5d3c8 "$LIMITER" "$spend_sig" "$AGENT" 310000 "$(ref a)" --from "$AGENT"
expect_revert "the owner calling spend" 0x0d9ab13f "$LIMITER" "$spend_sig" "$AGENT" 300000 "$(ref a)" --from "$OWNER"
expect_revert "paying the owner" 0xb387a238 "$LIMITER" "$spend_sig" "$OWNER" 100000 "$(ref a)" --from "$AGENT"
expect_revert "the agent pausing" 0xef6d0f02 "$LIMITER" "pause()" --from "$AGENT"
expect_ok "the guardian pausing (not sent)" "$LIMITER" "pause()" --from "$GUARDIAN"

# ---------------------------------------------------------------------------
step "7. One real purchase: 0.30 USDC, Trezor address -> agent address"
if [[ -z "${SPEND_TX:-}" ]]; then
  echo "This moves 0.30 USDC from the Trezor address to the agent's address"
  echo "(both yours) through the limiter, without the Trezor."
  if ask "Send it?"; then
    out=$(cast send "$LIMITER" "$spend_sig" "$AGENT" "$SPEND" "$(ref 1)" \
      "${AGENT_SIGNER[@]}" --rpc-url "$RPC" --json)
    save SPEND_TX "$(python3 -c 'import json,sys;print(json.load(sys.stdin)["transactionHash"])' <<< "$out")"
    save SPEND_DAY "$(( $(date -u +%s) / 86400 ))"
  else
    echo "Stopped before the purchase. Rerun to continue."; exit 0
  fi
fi
log "- spend tx: $SPEND_TX"
log "- agent USDC: $(usdc "$AGENT"), limiter USDC: $(usdc "$LIMITER"), owner USDC: $(usdc "$OWNER")"
log ""
log "| Case | Expected | Observed | Result |"
log "| --- | --- | --- | --- |"
if [[ "${SPEND_DAY:-}" == "$(( $(date -u +%s) / 86400 ))" && -z "${REVOKED:-}" ]]; then
  expect_revert "a second 0.30 the same day" 0xcc70389d "$LIMITER" "$spend_sig" "$AGENT" 300000 "$(ref 2)" --from "$AGENT"
  expect_ok "0.20, inside the day and the allowance (not sent)" "$LIMITER" "$spend_sig" "$AGENT" 200000 "$(ref 2)" --from "$AGENT"
else
  log "| daily-cap cases | — | skipped: new UTC day or already revoked | — |"
fi
expect_revert "replaying the paid reference" 0xac0292f0 "$LIMITER" "$spend_sig" "$AGENT" 100000 "$(ref 1)" --from "$AGENT"

# ---------------------------------------------------------------------------
step "8. Revoke from the Trezor"
if [[ -z "${REVOKED:-}" ]]; then
  echo "Confirm on the device only if it shows an approve of 0 to $LIMITER."
  revoke
  save REVOKED 1
fi
log "- allowanceLeft(): $(limiter_word 'allowanceLeft()(uint256)')"
log ""
log "| Case | Expected | Observed | Result |"
log "| --- | --- | --- | --- |"
expect_revert "0.20 after the revoke" "transfer amount exceeds allowance" "$LIMITER" "$spend_sig" "$AGENT" 200000 "$(ref 3)" --from "$AGENT"

# ---------------------------------------------------------------------------
step "9. Return the 0.30 (optional)"
agent_usdc=$(usdc "$AGENT")
if (( agent_usdc > 0 )) && ask "Send $agent_usdc atomic USDC from the agent back to the Trezor address?"; then
  cast send "$USDC" "transfer(address,uint256)" "$OWNER" "$agent_usdc" \
    "${AGENT_SIGNER[@]}" --rpc-url "$RPC" > /dev/null
  log "- returned $agent_usdc atomic USDC to the owner"
fi

step "Result"
if (( FAILS == 0 )); then log "**All checks passed.**"; else log "**$FAILS check(s) FAILED.**"; fi
echo "Log: $LOG"
