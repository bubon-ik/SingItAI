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
POST /agent/wallet
POST /agent/create-wallet
POST /agent/wallet-balance
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

## Managed Base Wallet MVP

The hosted Telegram bot can create one managed Base agent wallet per Telegram user.
This wallet is custodial and intended for small agent budgets only. Spending remains
disabled until the iMessage approval provider and per-user spend limits are implemented.

Generate required server secrets:

```bash
python3 - <<'PY'
from cryptography.fernet import Fernet
import secrets

print("SIGN402_WALLET_MASTER_KEY=" + Fernet.generate_key().decode())
print("SIGN402_WALLET_API_TOKEN=" + secrets.token_urlsafe(32))
PY
```

Set them in the gateway service environment:

```env
SIGN402_WALLET_MASTER_KEY=...
SIGN402_WALLET_API_TOKEN=...
SIGN402_USER_WALLET_STORE_PATH=<STATE_DIR>/user-wallets.db
```

The wallet API token is required even on localhost. Hermes or any trusted local
adapter must call wallet endpoints with:

```text
Authorization: Bearer <SIGN402_WALLET_API_TOKEN>
```

Agent-facing endpoints:

```text
POST /agent/wallet
POST /agent/create-wallet
POST /agent/wallet-balance
```

Example:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/create-wallet \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SIGN402_WALLET_API_TOKEN" \
  -d '{"telegramUserId":"<telegram-id>","telegramUsername":"<telegram-username>"}'
```

The response never includes private key material. The encrypted wallet database
should stay on the VPS filesystem with restrictive permissions; the gateway
creates the wallet directory as `0700` and the SQLite database as `0600`.

### Base wallet balances

Hosted wallet balances require an explicit private Base Mainnet JSON-RPC
endpoint. Any standard Base Mainnet RPC works; the recommended one is a CDP
Node RPC endpoint from the Coinbase Developer Platform:

```env
SIGN402_BASE_RPC_URL=https://api.developer.coinbase.com/rpc/v1/base/<client-api-key>
```

An Alchemy Base Mainnet endpoint also works and additionally enables automatic
discovery of other ERC-20 balances via the Alchemy Token API; this is optional.

Treat the full RPC URL as a secret because it contains your API key. Do not
put it in Hermes prompts, Telegram, screenshots, repository files, or shared
logs. The hosted balance provider does not silently fall back to the public,
rate-limited Base endpoint.

`/agent/wallet-balance` always reads Base Mainnet ETH, canonical USDC, and
SINGIT. When the endpoint is Alchemy, it also discovers up to 10 additional
non-zero ERC-20 balances automatically via the Alchemy Token API. On any other
RPC, set `SIGN402_BASE_BALANCE_TOKENS` to list specific ERC-20 tokens to read
instead:

```env
SIGN402_BASE_BALANCE_TOKENS=SYMBOL:0xaddress:decimals,SYMBOL2:0xaddress2:decimals2
```

Comma-separated, up to 10 entries. These extra tokens are labeled as
unverified, include their contract address, and remain display-only:
listing them never grants the agent permission to spend them.

The CDP Node RPC URL above also works for `SIGN402_ALLOWANCE_RPC_URL`, which
needs `eth_getLogs`; the public `https://mainnet.base.org` endpoint refuses
that method.

## Running the gateway

Use [the operations guide](../docs/operations.md) for installation, startup and
tests. The original local demo server, dashboard and launcher have been retired.
For the maintained Graph/Ledger demonstration, use
[its runbook](../docs/telegram-graph-ledger-demo.md).

## Base Mainnet CDP Flow

Set up the CDP helper once:

```bash
cd cdp-x402-service  # from the repo root
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
../payment-executor/.venv/bin/python -m pip install pyserial
```

Check the Firefly port:

```bash
ls /dev/cu.usb*
```

Start the gateway:

```bash
cd sign402-gateway  # from the repo root
FIREFLY_PORT=/dev/cu.usbmodemXXXX ../payment-executor/.venv/bin/python -m sign402_gateway
```

Default URL:

```text
http://127.0.0.1:8099
```

## Wallet-native Bitrefill checkout

This is the production checkout direction for Telegram/consumer purchases:

```text
Hermes/Telegram -> Sign402 Gateway -> Bitrefill eCommerce MCP -> Base USDC payment -> protected redemption
```

The user/embedded wallet funds the purchase as SINGIT, and the configured CDP
wallet swaps it to USDC before the gateway pays the MCP-issued Base invoice.

Create the local live env file:

```bash
cd sign402-gateway  # from the repo root
cp .env.wallet-bitrefill.example .env.wallet-bitrefill
```

Fill:

```text
BITREFILL_API_KEY=...
FIREFLY_PORT=/dev/cu.usbmodemXXXX
```

Live catalog, quote, purchase, and invoice-status requests use Bitrefill MCP at
`https://api.bitrefill.com/mcp/<BITREFILL_API_KEY>`. The key-bearing URL is
constructed only inside the MCP client and is redacted from its representation.
There is no Bitrefill REST fallback. To use another compatible HTTPS endpoint,
set `SIGN402_BITREFILL_MCP_URL` to its base URL without the API-key suffix.

Catalog browsing uses a durable 10-minute stale-while-revalidate cache. One
country snapshot serves all category filters and pages locally. A fresh or
stale snapshot returns immediately; a stale snapshot triggers one background
refresh, and refresh failures keep the last successful public catalog. Product
details, package prices, availability checks, invoices, purchases, payment,
fulfillment, and redemption data always bypass this cache and remain live MCP
requests.

The non-secret catalog settings are:

```text
SIGN402_BITREFILL_CATALOG_CACHE_TTL_SECONDS=600
SIGN402_BITREFILL_CATALOG_TIMEOUT_SECONDS=8
SIGN402_BITREFILL_CATALOG_CACHE_PATH=~/.sign402/bitrefill-catalog-cache.json
```

The persistent file contains only normalized public product-list metadata. It
is size-bounded, written atomically with user-only permissions, and never
contains the Bitrefill key, recipient data, invoices, purchase responses,
product-detail prices, or redemption values.

Start the gateway in wallet-native mode:

```bash
cd sign402-gateway  # from the repo root
./scripts/run-wallet-bitrefill.sh
```

If your Bitrefill key already lives in another local env file, reuse it instead of copying secrets:

```bash
SIGN402_ENV_FILE="/path/to/your/existing.env" ./scripts/run-wallet-bitrefill.sh
```

The script sets the important checkout mode:

```text
SIGN402_BITREFILL_MODE=live
SIGN402_BITREFILL_MCP_URL=https://api.bitrefill.com/mcp
SIGN402_BITREFILL_PAYMENT_METHOD=usdc_base
SIGN402_BITREFILL_USDC_TREASURY_MODE=cdp_wallet
SIGN402_BITREFILL_PRICING_SOURCE=cdp_wallet
SIGN402_BITREFILL_FUNDING_MODE=cdp_wallet_swap
```

With `SIGN402_BITREFILL_PAYMENT_METHOD=usdc_base`, the MCP invoice payment
requirements are validated as USDC on Base Mainnet before the configured CDP or
Bankr treasury transfers the exact approved amount. With `balance`, Bitrefill
uses the balance associated with `BITREFILL_API_KEY` and no treasury transfer is
made. Existing Firefly approval, quote expiry, purchase caps, replay protection,
and order persistence still wrap the MCP purchase.

Automated tests use injected MCP responses and never purchase anything. Run a
real low-value smoke purchase only manually, with explicit user confirmation,
after checking the live cap and treasury balance.

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

The legacy `/agent/buy-bitrefill` route is disabled by default. It is only for
isolated Bankr x402 experiments and requires both
`SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR=1` and a separate
`SIGN402_LEGACY_OPERATOR_API_TOKEN`. Public Telegram checkout uses
`/agent/buy-wallet-bitrefill`.

The following tunnel example is for an isolated local demo only. Do not expose
the production gateway: the Hermes wallet plugin requires
`SIGN402_GATEWAY_URL=http://127.0.0.1:8099` on the VPS.

Expose one local-demo tunnel:

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

## Latest run event

The gateway retains the latest safe run event for diagnostics:

```text
GET /events/latest
```

In the main short-mode demo, `/agent/buy-probe` writes this event automatically.

For low-level debugging, a client can record an event after a completed flow:

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

The default event store retains its historical location for compatibility.
Removing the dashboard HTML does not move or delete existing runtime data:

```text
<repo>/demo-dashboard/latest-run.json
```

## MCP server (remote, for Claude Code, Cursor, Codex)

SingIt runs a remote MCP server at `https://app.singitai.app/mcp`, on the public web API process
(`sign402_gateway/web_api.py`, `sign402_gateway/web_mcp.py`). It is a minimal MCP server over
Streamable HTTP: JSON responses only, no SSE stream, no session — one POST is one answer. It is
authenticated the same way as the rest of `/v1`: a SingIt API key (`sk_singit_…`), made in the SingIt
app, as `Authorization: Bearer sk_singit_…` (or `x-api-key`, for clients that only know that header).

Tools:

| Tool | Cost | What |
|------|------|------|
| `get_price` | Free | A crypto, metal or fx price |
| `list_price_symbols` | Free | Every symbol `get_price` knows |
| `list_models` | Free | Models this key can ask for (chat, images, audio), with their SingIt prices |
| `web_search` | Paid, needs `search` scope | Search the web (Exa) |
| `read_page` | Paid, needs `search` scope | Read a web page's text (Exa) |
| `get_weather` | Paid, needs `tools` scope | A weather forecast |
| `get_sign402_report` | Paid, needs `tools` scope | The Sign402 report |
| `get_singit_risk_check` | Paid, needs `tools` scope | A SINGIT risk analysis |
| `get_x_profile` | Paid, needs `tools` scope | An X/Twitter profile by username |
| `get_crypto_news` | Paid, needs `tools` scope | Crypto market news |
| `get_hyperliquid_market` | Paid, needs `tools` scope | Hyperliquid perpetuals market data |
| `get_funding_rates` | Paid, needs `tools` scope | Cross-venue funding rates |
| `resolve_ens` | Paid, needs `tools` scope | Resolve a `.eth` name or address |
| `get_token_price` | Paid, needs `tools` scope | A token's USD price |

Every paid call is billed exactly like `/v1`: from the key's own daily and per-request caps, out of
its account's own SingIt limits (Base USDC). Nothing here can move funds, change limits, or buy goods
(no Bitrefill, no wallet/allowance tools — that stays out of MCP for now, v2 at the earliest).

Install:

```bash
# Claude Code
claude mcp add --transport http singit https://app.singitai.app/mcp --header "Authorization: Bearer $SINGIT_KEY"
```

Cursor (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "singit": {
      "url": "https://app.singitai.app/mcp",
      "headers": { "Authorization": "Bearer ${env:SINGIT_KEY}" }
    }
  }
}
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.singit]
url = "https://app.singitai.app/mcp"
bearer_token_env_var = "SINGIT_KEY"
```

To try it locally, run the gateway and the web API (`python -m sign402_gateway.web_api` with
`SIGN402_WEB_ENABLED=1`, see `docs/allowance-web-v1.md`), make a key in the app, and point at
`127.0.0.1:8130` instead:

```bash
curl -s http://127.0.0.1:8130/mcp \
  -H "Authorization: Bearer $SINGIT_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Limits:

- Keys made before this change were signed for `answers search` only; they lack the `tools` scope, so
  the paid data tools above refuse them with a message to make a new key. `get_price`,
  `list_price_symbols`, `list_models`, `web_search` and `read_page` work with any still-live key, per
  its own scopes (`web_search` and `read_page` need `search`).
- MCP has its own rate limit, separate from `/v1`'s: 60 requests a minute per key by default (set
  `SINGIT_MCP_REQUESTS_PER_MINUTE` to change it), so heavy use of one never starves the other. It is
  charged once per request that carries an id (`initialize`, `ping`, `tools/list`, `tools/call`);
  notifications are free. Over the limit gets a JSON-RPC error with a `Retry-After` header (seconds).
  `web_search` and `read_page` are billed only here, not a second time against `/v1`.
- The paid data tools are sold on Base only; a Solana account's key gets a clear refusal for them.
- A request with a browser `Origin` header is refused (403), as on `/v1`: keys belong in server code.
- Repeated failed authentication (a wrong or revoked key) from one client address gets 429s instead of
  401s after 30 failures in 10 minutes, to blunt key guessing.
- `GET /mcp` answers 405: there is no server-to-client SSE stream.

## Safety

- The gateway reads the Algorand private key from the local `payment-executor/.env`.
- The private key is never returned over HTTP.
- Hermes receives only payment metadata and `txId`.
- If Firefly approval fails, Hermes must not call `/execute-payment`.
- Dashboard events must contain only safe metadata, never private keys or mnemonics.
