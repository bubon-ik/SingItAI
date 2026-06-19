# Hermes Sign402 Demo Script

## Goal

Show that Hermes can buy x402 paid tools on Algorand and Base from Telegram, while Firefly remains mandatory for policy approval and exact payment approval.

## Before The Pitch

Connect Firefly over USB-C and check the port:

```bash
ls /dev/cu.usb*
```

Start the local stack:

```bash
cd "/Users/mp/Documents/Berlin Hack"
bash scripts/start-local-demo.sh
```

Open the dashboard:

```text
http://127.0.0.1:8100
```

Expose the Sign402 Gateway:

```bash
cloudflared tunnel --url http://127.0.0.1:8099
```

Give Hermes the new gateway URL:

```text
Use Sign402 Gateway:
https://<gateway-tunnel>.trycloudflare.com

For policy approval, call POST /approve-policy.
For listing paid tools, call GET /agent/tools.
Before buying GoPlausible weather, call POST /agent/inspect-tool with {"tool":"goplausible.weather"}.
For buying GoPlausible weather, call POST /agent/buy-tool with {"tool":"goplausible.weather"}.
Before buying the Base Sign402 report, call POST /agent/inspect-tool with {"tool":"base.sign402.report"}.
For buying the Base Sign402 report, call POST /agent/buy-tool with {"tool":"base.sign402.report"}.
When I say "buy base sign402 report", "buy base report", or "buy sign402 report", use the Base Sign402 report tool.
Before buying an X/Twitter profile lookup, call POST /agent/inspect-tool with {"tool":"x402.twitter.profile","username":"<username>"}.
For buying an X/Twitter profile lookup, call POST /agent/buy-tool with {"tool":"x402.twitter.profile","username":"<username>"}.
When I say "buy x profile <username>", "buy twitter profile <username>", or "buy x402 twitter <username>", use the X/Twitter Profile tool and strip a leading @ from the username.
When I say "buy crypto news", use {"tool":"otto.crypto_news"}.
When I say "buy hyperliquid <asset>", use {"tool":"otto.hyperliquid_market","asset":"<asset>"}.
When I say "buy funding <symbol>", use {"tool":"otto.funding_rates","symbol":"<symbol>"}.
When I say "buy ens <name>", use {"tool":"onesource.ens","input":"<name>"}.
When I say "buy token price <symbol>", use {"tool":"anchor.token_price","symbol":"<symbol>"}.
When I say "buy singit risk check", "buy singit risk", or "buy risk check", call POST /agent/buy-tool with {"tool":"singit-risk-check"}.
When I say "buy x402 <url>", first call POST /agent/inspect-x402 with {"url":"<url>"}.
If the quote is acceptable and it is Base Mainnet USDC, call POST /agent/buy-x402 with {"url":"<url>"}.
After buying SINGIT Risk Check, reply using only telegramText if present.
After buying a raw x402 URL, reply using only telegramText if present.
For one Telegram user command, call the matching buy endpoint only once. If a response contains duplicateSuppressed=true, reply with its telegramText and do not retry.
Do not build the x402 payment yourself. Do not ask for private keys. Only call the Sign402 Gateway.
```

## Stage Flow

### 1. Explain The Problem

Say:

```text
x402 lets agents pay for web resources. Sign402 adds the missing consent layer: the agent can act autonomously, but payments are bounded by a hardware-approved policy and each live payment needs physical Firefly approval.
```

### 2. Approve Policy

In Telegram:

```text
approve sign402 policy
```

Use the official USDC policy:

```json
{
  "version": "1",
  "agentId": "hermes-demo",
  "policyId": "policy-goplausible-usdc-demo",
  "allowedPurpose": "x402_api_access",
  "asset": "10458941",
  "maxBudgetAtomic": "100000",
  "maxPerPaymentAtomic": "10000",
  "nonce": "goplausible-weather-demo"
}
```

For the Base Mainnet demo, use this USDC policy instead:

```json
{
  "version": "1",
  "agentId": "hermes-demo",
  "policyId": "policy-base-usdc-001",
  "allowedPurpose": "x402_api_access",
  "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
  "maxBudgetAtomic": "100000",
  "maxPerPaymentAtomic": "10000",
  "nonce": "base-mainnet-usdc-001"
}
```

Expected:

- Firefly shows the policy hash.
- User presses approve.
- Hermes replies that policy is approved.
- Dashboard shows policy hash and Firefly device identity.

### 3. Buy Resource

In Telegram:

```text
buy goplausible weather
```

Expected:

- Hermes treats GoPlausible Weather as a paid tool, not a hardcoded URL.
- Gateway requests `https://x402.goplausible.xyz/examples/weather`.
- GoPlausible returns `402 Payment Required`.
- Gateway checks the stored policy.
- Firefly shows `x402 WEATHER`, `0.01 USDC`, `GoPlausible API`, and the short payment approval hash.
- User presses approve.
- Gateway creates official `x402-avm` `PAYMENT-SIGNATURE`.
- GoPlausible facilitator settles the Algorand TestNet USDC payment.
- GoPlausible returns protected weather JSON.
- Hermes replies with decision, tx id, policy hash, payment hash, weather result, and remaining budget.
- Dashboard updates automatically.

For the Base Mainnet path, use this Telegram command:

```text
buy base sign402 report
```

Expected:

- Hermes treats Base Sign402 Report as a paid tool, not a hardcoded URL.
- Gateway requests `http://127.0.0.1:4021/paid/sign402-report`.
- The local CDP x402 seller returns `402 Payment Required`.
- Gateway checks the stored Base USDC policy.
- Firefly shows the exact `0.01 USDC` payment approval hash.
- User presses approve.
- Gateway invokes the CDP x402 buyer.
- Coinbase facilitator settles USDC on Base Mainnet.
- The protected Sign402 report JSON is returned to Hermes.
- Hermes replies with decision, Base tx hash, policy hash, payment hash, and remaining budget.

For a generic Base Mainnet USDC x402 endpoint, use this Telegram command:

```text
buy x402 https://merchant.example/paid-resource
```

Expected:

- Hermes calls `/agent/inspect-x402` first and reads `quoteText`.
- Gateway rejects anything that is not Base Mainnet USDC.
- If the quote is acceptable, Hermes calls `/agent/buy-x402`.
- Firefly shows `BASE x402 PAYMENT`, the USDC amount, and `Base Mainnet`.
- Gateway invokes the CDP x402 buyer.
- Hermes replies with `telegramText`, including a clickable Basescan transaction link.

### 4. Point To The Audit Trail

Show:

- Telegram command;
- Firefly hash on device;
- dashboard payment approval hash;
- Algorand tx id;
- official GoPlausible `Payment-Response`;
- protected weather JSON.

Say:

```text
The agent did not get the private key. It could only trigger a payment after the gateway checked the USDC policy and Firefly approved the exact payment hash. The final API response came from a real GoPlausible x402 resource.
```

## Failure Demo

Optional if time allows:

- Disconnect Firefly or press cancel.
- Run `buy goplausible weather` again.
- Expected result: payment rejected, no Algorand transaction and no protected API response.

Say:

```text
Without Firefly approval, Hermes cannot execute the payment.
```

## Backup

If Cloudflare quick tunnel changes, run it again and give Hermes only the new gateway URL.

If a port is busy:

```bash
lsof -nP -iTCP:8099 -sTCP:LISTEN
lsof -nP -iTCP:8100 -sTCP:LISTEN
```

Stop the old process or restart the terminal session, then run:

```bash
cd "/Users/mp/Documents/Berlin Hack"
bash scripts/start-local-demo.sh
```
