# Sign402 Gateway

Unified local Mac gateway for Hermes Sign402.

It combines:

- Firefly policy approval;
- Firefly payment approval;
- local Algorand TestNet payment execution.

Hermes gets one public tunnel URL and never receives private keys.

Implementation note: `/approve-policy` uses the Firefly `PAYMENT=<policyHash>` approval path. The older `POLICY=<policyHash>` firmware command can leave the device silent after approval on the current test unit, while `PAYMENT=<hash>` returns to the approval flow reliably.

Payment approval uses the Firefly `PAYMENT-CONTEXT=<line1>|<line2>|<line3>` pre-command when context is available. The current GoPlausible demo shows:

```text
x402 WEATHER
0.01 USDC
GoPlausible API
Hash ....a1ef
OK / CANCEL
```

## Endpoints

```text
GET  /health
POST /approve-policy
POST /approve-payment
POST /execute-payment
GET  /events/latest
POST /events/latest
POST /agent/buy-probe
GET  /agent/tools
POST /agent/inspect-tool
POST /agent/buy-tool
POST /agent/inspect-x402
POST /agent/buy-x402
POST /agent/inspect-llm-credits-topup
POST /agent/top-up-llm-credits
```

`/agent/inspect-x402` and `/agent/buy-x402` now support two official x402 lanes:

- Algorand TestNet through the existing `x402-avm` payment signature builder.
- Base Mainnet through `../cdp-x402-service`, CDP API key wallets, and CDP facilitator.

## Bankr LLM Credits Funded by SINGIT

The gateway can also let Hermes fund Bankr LLM Gateway credits with a project token such as SINGIT. This is not an x402 Cloud payment. It is a separate top-up flow:

```text
SINGIT spending policy -> Firefly top-up approval -> bankr llm credits add <usd> --token <SINGIT> --yes
```

Approve a SINGIT budget policy first:

```json
{
  "policy": {
    "version": "1",
    "agentId": "hermes-demo",
    "policyId": "policy-singit-llm-001",
    "allowedPurpose": "bankr_llm_credits_topup",
    "asset": "0xc2c1e0b7C401e6217193732272444D928646eba3",
    "maxBudgetAtomic": "10000000000000000000000",
    "maxPerPaymentAtomic": "5000000000000000000000",
    "nonce": "singit-llm-credits-001"
  }
}
```

Inspect a top-up before executing:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/inspect-llm-credits-topup \
  -H "Content-Type: application/json" \
  -d '{
    "creditAmountUsd": "5",
    "fundingTokenAddress": "0xc2c1e0b7C401e6217193732272444D928646eba3",
    "fundingTokenSymbol": "SINGIT",
    "maxFundingTokenAmountAtomic": "5000000000000000000000"
  }'
```

Then execute the top-up with the returned `topUpIntent`:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/top-up-llm-credits \
  -H "Content-Type: application/json" \
  -d '{
    "creditAmountUsd": "5",
    "fundingTokenAddress": "0xc2c1e0b7C401e6217193732272444D928646eba3",
    "fundingTokenSymbol": "SINGIT",
    "maxFundingTokenAmountAtomic": "5000000000000000000000",
    "topUpIntent": "bankr-llm-..."
  }'
```

The gateway checks the stored policy against the SINGIT token address and `bankr_llm_credits_topup` purpose, asks Firefly to approve the exact top-up commitment, then invokes the Bankr CLI. Set `SIGN402_BANKR_CLI` if `bankr` is not on `PATH`. The default SINGIT token address is `0xc2c1e0b7C401e6217193732272444D928646eba3`; override it with `SIGN402_SINGIT_TOKEN_ADDRESS` if needed.

## Main Demo Flow

For the normal hackathon demo, start all local services from the repository root:

```bash
cd "/Users/mp/Documents/Berlin Hack"
./scripts/start-local-demo.sh
```

Then expose only the gateway:

```bash
cloudflared tunnel --url http://127.0.0.1:8099
```

Give Hermes the resulting base URL:

```text
SIGN402_GATEWAY_URL=https://<tunnel>.trycloudflare.com
```

Hermes uses two product endpoints:

```text
POST /approve-policy
POST /agent/buy-tool
```

For the official GoPlausible weather demo, Hermes can inspect and buy the paid tool:

```bash
curl -sS http://127.0.0.1:8099/agent/tools

curl -sS -X POST http://127.0.0.1:8099/agent/inspect-tool \
  -H "Content-Type: application/json" \
  -d '{"tool":"goplausible.weather"}'

curl -sS -X POST http://127.0.0.1:8099/agent/buy-tool \
  -H "Content-Type: application/json" \
  -d '{"tool":"goplausible.weather"}'
```

The paid-tool endpoints wrap the official `/agent/buy-x402` path so the agent workflow is tool-oriented rather than URL-oriented. `/agent/buy-probe` remains available for the local probe demo.

## Base Mainnet CDP Flow

Set up the CDP helper once:

```bash
cd "/Users/mp/Documents/Berlin Hack/cdp-x402-service"
npm install
cp .env.example .env
npm run account
```

Fill `.env` with local CDP secrets before running `npm run account`. Fund the printed buyer EVM address with Base Mainnet USDC before live purchases.

Run the local Base x402 seller:

```bash
npm run serve
```

Default protected resource:

```text
http://127.0.0.1:4021/paid/sign402-report
```

Approve a Base policy through `/approve-policy`:

```json
{
  "policy": {
    "version": "1",
    "agentId": "hermes-demo",
    "policyId": "policy-base-usdc-001",
    "allowedPurpose": "x402_api_access",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
    "maxBudgetAtomic": "100000",
    "maxPerPaymentAtomic": "10000",
    "nonce": "base-mainnet-usdc-001"
  }
}
```

Then inspect and buy the Base paid tool through the same agent-facing flow:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/inspect-tool \
  -H "Content-Type: application/json" \
  -d '{"tool":"base.sign402.report"}'

curl -sS -X POST http://127.0.0.1:8099/agent/buy-tool \
  -H "Content-Type: application/json" \
  -d '{"tool":"base.sign402.report"}'
```

Aliases accepted by the gateway: `base-report`, `sign402-report`, and `get_sign402_report`.

The gateway still checks the stored policy and requires Firefly approval before invoking the CDP buyer. The raw URL endpoint remains available for debugging:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/buy-x402 \
  -H "Content-Type: application/json" \
  -d '{"url":"http://127.0.0.1:4021/paid/sign402-report"}'
```

## Manual Run

Install the only extra dependency into the payment executor venv:

```bash
"/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python" -m pip install pyserial
```

Check the Firefly port:

```bash
ls /dev/cu.usb*
```

Start the gateway:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
FIREFLY_PORT=/dev/cu.usbmodem11301 ../payment-executor/.venv/bin/python -m sign402_gateway
```

Default URL:

```text
http://127.0.0.1:8099
```

## Wallet-native Bitrefill checkout

This is the production checkout direction for Telegram/consumer purchases:

```text
user/embedded wallet SINGIT -> CDP wallet swap to USDC -> Bitrefill USDC/Base invoice
```

Create the local live env file:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
cp .env.wallet-bitrefill.example .env.wallet-bitrefill
```

Fill:

```text
BITREFILL_API_KEY=...
SIGN402_BITREFILL_REFUND_ADDRESS=0x...
FIREFLY_PORT=/dev/cu.usbmodem11301
```

If `SIGN402_BITREFILL_REFUND_ADDRESS` is omitted, the run script uses `CDP_EVM_ACCOUNT_ADDRESS` from `../cdp-x402-service/.env`.

Start the gateway in wallet-native mode:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
./scripts/run-wallet-bitrefill.sh
```

If your Bitrefill key already lives in another local env file, reuse it instead of copying secrets:

```bash
SIGN402_ENV_FILE="/path/to/your/existing.env" ./scripts/run-wallet-bitrefill.sh
```

The script sets the important checkout mode:

```text
SIGN402_BITREFILL_MODE=live
SIGN402_BITREFILL_PAYMENT_METHOD=usdc_base
SIGN402_BITREFILL_USDC_TREASURY_MODE=cdp_wallet
SIGN402_BITREFILL_PRICING_SOURCE=cdp_wallet
SIGN402_BITREFILL_FUNDING_MODE=cdp_wallet_swap
```

Quote a Bitrefill product:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/quote-bitrefill \
  -H "Content-Type: application/json" \
  -d '{"productId":"bitrefill-giftcard-usd","packageId":"0.1","country":"US"}'
```

After explicit user confirmation, execute the wallet-native checkout:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/buy-wallet-bitrefill \
  -H "Content-Type: application/json" \
  -d '{"quoteId":"<quote_id_from_quote>","recipient":{}}'
```

The legacy `/agent/buy-bitrefill` route remains available for Bankr x402 experiments, but the Telegram checkout should use `/agent/buy-wallet-bitrefill`.

Expose one tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8099
```

Give Hermes the resulting base URL:

```text
SIGN402_GATEWAY_URL=https://<tunnel>.trycloudflare.com
```

## Agent Buy Probe

This is the short UX endpoint for Hermes. After a policy has been approved through `/approve-policy`, Hermes can call one endpoint:

```bash
curl -X POST http://127.0.0.1:8099/agent/buy-probe \
  -H "Content-Type: application/json" \
  -d '{"target":"algorand.co"}'
```

The gateway handles the full flow:

```text
GET resource -> 402 -> policy check -> Firefly PAYMENT approval -> Algorand payment -> X-Payment retry -> dashboard event
```

Hermes receives the final public result and never receives private keys.

## Low-Level Payment Execution

This endpoint is kept for protocol debugging and tests. In the main short-mode demo, Hermes calls `/agent/buy-probe` instead, and the gateway performs payment approval plus execution internally.

If you use the low-level flow manually, call this only after Firefly approved the payment hash.

```bash
curl -X POST http://127.0.0.1:8099/execute-payment \
  -H "Content-Type: application/json" \
  -d '{
    "policyHash": "<64 hex chars>",
    "paymentApprovalHash": "<64 hex chars>",
    "paymentRequirements": {
      "network": "algorand-testnet",
      "asset": "ALGO_TEST",
      "amountAtomic": "50000",
      "receiver": "MERCHANT_ALGO_ADDRESS",
      "resource": "/probe?target=algorand.co",
      "paymentIntent": "intent-001",
      "purpose": "x402_api_access"
    }
  }'
```

Expected response:

```json
{
  "ok": true,
  "policyHash": "...",
  "paymentApprovalHash": "...",
  "payment": {
    "txId": "...",
    "network": "algorand-testnet",
    "receiver": "...",
    "amountAtomic": "50000",
    "asset": "ALGO_TEST",
    "paymentIntent": "...",
    "policyHash": "...",
    "note": "sign402:<policyHash>:<paymentIntent>"
  }
}
```

## Live Dashboard Event

The dashboard polls the gateway for the latest safe run event:

```text
GET /events/latest
```

In the main short-mode demo, `/agent/buy-probe` writes this event automatically.

For low-level debugging, a client can update the dashboard manually after a completed flow:

```bash
curl -X POST http://127.0.0.1:8099/events/latest \
  -H "Content-Type: application/json" \
  -d '{
    "event": {
      "decision": "APPROVED & EXECUTED",
      "policyHash": "<64 hex chars>",
      "paymentApprovalHash": "<64 hex chars>",
      "txId": "<algorand tx id>",
      "resource": "/probe?target=algorand.co",
      "paymentIntent": "intent-001",
      "amountAtomic": "50000",
      "asset": "ALGO_TEST",
      "network": "algorand-testnet",
      "deviceModel": 262,
      "deviceSerial": 1056,
      "remainingBudgetAtomic": "950000",
      "resourceResult": {
        "target": "algorand.co",
        "location": "Berlin",
        "httpStatus": 200,
        "latencyMs": 42,
        "result": "reachable"
      }
    }
  }'
```

The default event store is:

```text
/Users/mp/Documents/Berlin Hack/demo-dashboard/latest-run.json
```

## Safety

- The gateway reads the Algorand private key from the local `payment-executor/.env`.
- The private key is never returned over HTTP.
- Hermes receives only payment metadata and `txId`.
- If Firefly approval fails, Hermes must not call `/execute-payment`.
- Dashboard events must contain only safe metadata, never private keys or mnemonics.
