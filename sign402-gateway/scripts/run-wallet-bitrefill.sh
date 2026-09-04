#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATEWAY_DIR="$ROOT_DIR/sign402-gateway"
PYTHON_BIN="$ROOT_DIR/payment-executor/.venv/bin/python"

DEFAULT_ENV_FILE="$GATEWAY_DIR/.env.wallet-bitrefill"
ENV_FILE="${SIGN402_ENV_FILE:-$DEFAULT_ENV_FILE}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

missing=()
[[ -n "${BITREFILL_API_KEY:-}" ]] || missing+=("BITREFILL_API_KEY")
if [[ "${SIGN402_BITREFILL_CHECKOUT_MODE:-account}" == "guest" ]]; then
  [[ -n "${SIGN402_WALLET_MASTER_KEY:-}" ]] || missing+=("SIGN402_WALLET_MASTER_KEY")
fi

if (( ${#missing[@]} > 0 )); then
  echo "Missing required env: ${missing[*]}" >&2
  echo "Create $DEFAULT_ENV_FILE from .env.wallet-bitrefill.example, export them in this shell, or run with SIGN402_ENV_FILE=/path/to/existing.env." >&2
  exit 2
fi

export SIGN402_BITREFILL_MODE="${SIGN402_BITREFILL_MODE:-live}"
export SIGN402_BITREFILL_MCP_URL="${SIGN402_BITREFILL_MCP_URL:-https://api.bitrefill.com/mcp}"
export SIGN402_BITREFILL_AFFILIATE_REF="${SIGN402_BITREFILL_AFFILIATE_REF:-nrVGauph}"
export SIGN402_BITREFILL_PAYMENT_METHOD="${SIGN402_BITREFILL_PAYMENT_METHOD:-usdc_base}"
# "account" keeps the signed-in MCP session; "guest" buys anonymously so the
# affiliate ref above is actually credited. Guest checkout needs
# SIGN402_WALLET_MASTER_KEY set, or the gateway refuses to start.
export SIGN402_BITREFILL_CHECKOUT_MODE="${SIGN402_BITREFILL_CHECKOUT_MODE:-account}"
export SIGN402_BITREFILL_USDC_TREASURY_MODE="${SIGN402_BITREFILL_USDC_TREASURY_MODE:-cdp_wallet}"
export SIGN402_BITREFILL_PRICING_MODE="${SIGN402_BITREFILL_PRICING_MODE:-bankr_real_rate}"
export SIGN402_BITREFILL_PRICING_BUFFER_BPS="${SIGN402_BITREFILL_PRICING_BUFFER_BPS:-200}"
export SIGN402_BITREFILL_PRICING_SOURCE="${SIGN402_BITREFILL_PRICING_SOURCE:-cdp_wallet}"
export SIGN402_BITREFILL_FUNDING_MODE="${SIGN402_BITREFILL_FUNDING_MODE:-cdp_wallet_swap}"
export SIGN402_CDP_X402_SERVICE_DIR="${SIGN402_CDP_X402_SERVICE_DIR:-$ROOT_DIR/cdp-x402-service}"
export SIGN402_BANKR_SWAP_FROM_TOKEN="${SIGN402_BANKR_SWAP_FROM_TOKEN:-0xc2c1e0b7C401e6217193732272444D928646eba3}"
export SIGN402_BANKR_SWAP_TO_TOKEN="${SIGN402_BANKR_SWAP_TO_TOKEN:-USDC}"
export SIGN402_BANKR_SWAP_CHAIN="${SIGN402_BANKR_SWAP_CHAIN:-base}"
export SIGN402_BITREFILL_QUOTE_TTL_SECONDS="${SIGN402_BITREFILL_QUOTE_TTL_SECONDS:-120}"
export SIGN402_TREASURY_USDC_BUFFER_BPS="${SIGN402_TREASURY_USDC_BUFFER_BPS:-1000}"
export SIGN402_DISABLE_TREASURY_RESERVE_GUARD="${SIGN402_DISABLE_TREASURY_RESERVE_GUARD:-1}"
export SIGN402_DISABLE_BANKR_BITREFILL_SETTLEMENT="${SIGN402_DISABLE_BANKR_BITREFILL_SETTLEMENT:-1}"

cd "$GATEWAY_DIR"
exec "$PYTHON_BIN" -m sign402_gateway "$@"
