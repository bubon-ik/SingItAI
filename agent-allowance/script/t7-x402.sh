#!/usr/bin/env bash
# T7: x402 on Base mainnet, paid by the agent key and funded by the limiter.
#
# Design: docs/trezor-allowance-v1.md, "x402: the agent pays, the limiter
# funds it". Reuses the T4 limiter and keys (~/.sign402-trezor-poc/t4.env).
#
# The owner: confirms the grant on the Trezor, types the sign402-agent password
# once, answers y before each purchase, and confirms the revoke on the Trezor.
# Results: ~/.sign402-trezor-poc/t7-log.md. No file holds a password or key.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SIDECAR="$HERE/../trezor-sidecar"
PY="$SIDECAR/.venv/bin/python"
SIDECAR_ENV="${SIDECAR_ENV:-$HOME/.config/sign402-trezor-poc/sidecar.env}"
CDP_X402_DIR="${CDP_X402_DIR:-$HOME/Documents/Berlin Hack/cdp-x402-service}"
STATE_DIR="${STATE_DIR:-$HOME/.sign402-trezor-poc}"
KEYSTORES="${KEYSTORES:-$HOME/.foundry/keystores}"
LOG="$STATE_DIR/t7-log.md"
RPC="${RPC:-https://mainnet.base.org}"
USDC=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
OWNER="${OWNER:-0xB80b5Ca13583fB7E0236db4bD8834B9035654558}"
ASSUME_YES="${ASSUME_YES:-0}"   # rehearsal only
TRANSFER_TOPIC=0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef

GRANT_USDC=1.00
FLOAT_TARGET=50000   # 0.05 USDC
LOW_WATER=10000      # 0.01
THRESHOLD=10000      # payments above this are funded exactly
BANKR=https://x402.bankr.bot/0x3b3e349e6cfee692b69d2c63ce86f7d444667d98
TARGETS=(
  "$BANKR/vet-service?slug=treza"
  "$BANKR/vet-service?slug=venice"
  "$BANKR/vet-shortlist?limit=3"
)

mkdir -p "$STATE_DIR"; touch "$LOG"; chmod 600 "$LOG"
# shellcheck disable=SC1090
source "$STATE_DIR/t4.env"   # AGENT, GUARDIAN, LIMITER
FAILS=0

log() { echo "$*" | tee -a "$LOG"; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; echo "" >> "$LOG"; echo "## $*" >> "$LOG"; }
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
usdc() { word "$USDC" "balanceOf(address)(uint256)" "$1"; }
wait_at_least() {  # until the agent's USDC is visible at >= want
  local want=$1 got i
  for i in $(seq 1 20); do got=$(usdc "$AGENT") && (( got >= want )) && return 0; sleep 2; done
  log "- WARNING: agent USDC still reads ${got:-nothing}, needed $want"; FAILS=$((FAILS + 1)); return 1
}
wait_for() {
  local what=$1 want=$2 got i; shift 2
  for i in $(seq 1 20); do got=$("$@") && [[ "$got" == "$want" ]] && return 0; sleep 2; done
  log "- WARNING: $what still reads ${got:-nothing}, expected $want"; FAILS=$((FAILS + 1))
}
sidecar() { ( cd "$SIDECAR"; set -a; source "$SIDECAR_ENV"; set +a; .venv/bin/python -m trezor_sidecar.allowance "$@" ); }

# The agent key signs x402 payments and sends spend/return. The password is
# read once. cast accepts it only from a regular file, so it goes into one in a
# private temporary directory (mode 700, file 600) that is removed on any exit;
# never into argv. The decrypted key goes only to the x402 client's environment.
PW_DIR=$(mktemp -d)
chmod 700 "$PW_DIR"
trap 'rm -rf "$PW_DIR"; unset AGENT_PK AGENT_PW' EXIT
unlock_agent() {
  if [[ -z "${AGENT_PW:-}" ]]; then
    read -rs -p "sign402-agent password: " AGENT_PW; echo
  fi
  ( umask 077; printf '%s' "$AGENT_PW" > "$PW_DIR/pw" )
  AGENT_PK=$(CAST_UNSAFE_PASSWORD="$AGENT_PW" cast wallet decrypt-keystore \
    --keystore-dir "$KEYSTORES" sign402-agent 2>/dev/null | awk '{print $NF}') || true
  [[ "$AGENT_PK" =~ ^0x[0-9a-fA-F]{64}$ ]] || { echo "Could not unlock sign402-agent: wrong password?"; exit 1; }
  local derived
  derived=$(AGENT_PK="$AGENT_PK" "$PY" -c 'import os; from eth_account import Account; print(Account.from_key(os.environ["AGENT_PK"]).address)')
  [[ "$(tr 'A-F' 'a-f' <<< "$derived")" == "$(tr 'A-F' 'a-f' <<< "$AGENT")" ]] \
    || { echo "That keystore is not the agent $AGENT."; exit 1; }
}
agent_send() {  # cast send as the agent; prints the transaction hash or fails loudly
  local out
  out=$(cast send "$@" --keystore "$KEYSTORES/sign402-agent" --password-file "$PW_DIR/pw" \
    --rpc-url "$RPC" --json 2>&1) || { echo "Agent transaction failed: $out" >&2; return 1; }
  python3 -c 'import json,sys;print(json.load(sys.stdin)["transactionHash"])' <<< "$out" \
    || { echo "Unexpected cast output: $out" >&2; return 1; }
}
tx_hash() { python3 -c 'import json,sys;print(json.load(sys.stdin)["transactionHash"])'; }
spend_to_agent() {  # amount ref-text -> tx hash
  agent_send "$LIMITER" "spend(address,uint256,bytes32)" "$AGENT" "$1" "$(cast keccak "$2")"
}
terms() {  # the Base USDC exact leg of a 402, as "amount payTo asset"
  curl -s --max-time 20 -D - -o /dev/null "$1" | python3 -c '
import sys, json, base64
hdr = [l for l in sys.stdin if l.lower().startswith("payment-required:")]
body = json.loads(base64.b64decode(hdr[0].split(":", 1)[1].strip())) if hdr else {}
legs = [a for a in body.get("accepts", []) if a.get("scheme") == "exact"
        and a.get("network") == "eip155:8453"
        and a.get("asset", "").lower() == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"]
if not legs: sys.exit("no Base USDC exact leg in the 402")
a = legs[0]
print(a.get("amount") or a["maxAmountRequired"], a["payTo"], a["asset"])'
}
settled() {  # tx payTo amount: exactly one USDC Transfer agent -> payTo of amount
  local receipt
  receipt=$(rcast receipt "$1" --json) || return 1
  python3 - "$receipt" "$TRANSFER_TOPIC" "$AGENT" "$2" "$3" <<'PY'
import json, sys
rc, topic, frm, to, amount = json.loads(sys.argv[1]), *sys.argv[2:]
hits = [l for l in rc["logs"] if l["topics"][0] == topic
        and l["topics"][1][-40:].lower() == frm[2:].lower()
        and l["topics"][2][-40:].lower() == to[2:].lower()
        and int(l["data"], 16) == int(amount)]
sys.exit(0 if int(rc["status"], 16) == 1 and len(hits) == 1 else 1)
PY
}
x402_pay() {  # url amount payTo asset -> JSON from the x402 client
  if [[ -n "${X402_PAY_CMD:-}" ]]; then eval "$X402_PAY_CMD"; return; fi
  ( cd "$CDP_X402_DIR" && SIGN402_EVM_PRIVATE_KEY="$AGENT_PK" node src/index.mjs buy-user \
      --url "$1" --max-atomic "$2" --expected-receiver "$3" --expected-asset "$4" 2>&1 )
}

# ---------------------------------------------------------------------------
step "0. Preconditions"
[[ "$(rcast chain-id)" == 8453 ]] || { echo "RPC is not Base"; exit 1; }
[[ "${LIMITER:-}" =~ ^0x[0-9a-fA-F]{40}$ ]] || { echo "No T4 limiter in $STATE_DIR/t4.env"; exit 1; }
[[ -n "${X402_PAY_CMD:-}" || -d "$CDP_X402_DIR/node_modules/@x402" ]] || { echo "No x402 client at $CDP_X402_DIR"; exit 1; }
log "- run started $(date -u '+%Y-%m-%d %H:%M UTC'); limiter $LIMITER; agent $AGENT"
log "- limiter caps: per purchase $(word "$LIMITER" 'perPurchaseCap()(uint256)'), left today $(word "$LIMITER" 'remainingToday()(uint256)'), paused $(rcast call "$LIMITER" 'paused()(bool)')"
log "- owner USDC $(usdc "$OWNER"); agent USDC $(usdc "$AGENT"), ETH $(rcast balance "$AGENT" --ether)"
log "- float: target $FLOAT_TARGET, low-water $LOW_WATER, exact above $THRESHOLD (atomic USDC)"

step "1. Grant $GRANT_USDC USDC from the Trezor"
if (( $(word "$LIMITER" 'allowanceLeft()(uint256)') == 0 )); then
  echo "Confirm on the Trezor only for an approve of 1 USDC to $LIMITER."
  if [[ -n "${GRANT_CMD:-}" ]]; then eval "$GRANT_CMD"; else sidecar grant "$LIMITER" "$GRANT_USDC"; fi
  wait_for "allowanceLeft()" 1000000 word "$LIMITER" 'allowanceLeft()(uint256)'
fi
log "- allowanceLeft $(word "$LIMITER" 'allowanceLeft()(uint256)')"

step "2. Unlock the agent"
unlock_agent
log "- agent key unlocked for this run"

step "3. x402 purchases"
log "| Resource | Price | Funding | Funding tx | Settlement tx | HTTP | Float after | Result |"
log "| --- | --- | --- | --- | --- | --- | --- | --- |"
n=0
for url in "${TARGETS[@]}"; do
  n=$((n + 1))
  read -r amount pay_to asset < <(terms "$url")
  float=$(usdc "$AGENT")
  echo
  echo "$url"
  echo "    price $amount atomic USDC on Base, to $pay_to"
  if ! ask "Buy it?"; then log "| ${url#"$BANKR"} | $amount | — | — | — | — | $float | skipped |"; continue; fi

  funding="float" fund_tx="—"
  if (( amount > THRESHOLD )); then
    funding="exact"
    fund_tx=$(spend_to_agent "$amount" "x402:$url:$amount:$(date +%s)")
    wait_at_least $(( float + amount )) || true
  elif (( float - amount < LOW_WATER )); then
    refill=$(( FLOAT_TARGET - float ))
    (( refill >= amount )) || refill=$amount
    funding="refill $refill"
    fund_tx=$(spend_to_agent "$refill" "float:$(date +%s):$n")
    wait_at_least $(( float + refill )) || true
  fi

  out=$(x402_pay "$url" "$amount" "$pay_to" "$asset" || true)
  if grep -q '"insufficient_funds"' <<< "$out"; then
    echo "The facilitator does not see the funds yet; retrying in 6 s."
    sleep 6
    out=$(x402_pay "$url" "$amount" "$pay_to" "$asset" || true)
  fi
  read -r ok status settle_tx < <(python3 -c '
import json, sys
raw = sys.stdin.read(); i = raw.find("{")
try: d = json.loads(raw[i:]) if i >= 0 else {}
except Exception: d = {}
print(str(bool(d.get("ok"))).lower(), d.get("status", "—"), d.get("transactionHash") or "—")' <<< "$out")
  result="**FAIL**"
  if [[ "$ok" == true && "$settle_tx" != "—" ]] && settled "$settle_tx" "$pay_to" "$amount"; then
    result=pass
  else
    FAILS=$((FAILS + 1))
    echo "$out" | tail -5
  fi
  log "| ${url#"$BANKR"} | $amount | $funding | $fund_tx | $settle_tx | $status | $(usdc "$AGENT") | $result |"
done

step "4. Close the float and revoke"
float=$(usdc "$AGENT")
if (( float > 0 )) && ask "Return the float ($float atomic USDC) from the agent to the Trezor address?"; then
  ret_tx=$(agent_send "$USDC" "transfer(address,uint256)" "$OWNER" "$float")
  log "- float returned: $float, tx $ret_tx"
fi
if ask "Revoke the allowance on the Trezor now?"; then
  if [[ -n "${REVOKE_CMD:-}" ]]; then eval "$REVOKE_CMD"; else sidecar revoke "$LIMITER"; fi
  wait_for "allowanceLeft()" 0 word "$LIMITER" 'allowanceLeft()(uint256)'
fi
log "- allowanceLeft $(word "$LIMITER" 'allowanceLeft()(uint256)'); owner USDC $(usdc "$OWNER"); agent USDC $(usdc "$AGENT")"

step "Result"
if (( FAILS == 0 )); then log "**All x402 purchases settled as expected.**"; else log "**$FAILS problem(s).**"; fi
echo "Log: $LOG"
