# Hermes Sign402 - Project Spec

## Summary

**Hermes Sign402** is a Telegram-based AI commerce agent with a Firefly-backed approval layer for x402 payments on Algorand.

It lets a user delegate limited spending authority to an AI agent without giving the agent unlimited wallet access. The user defines the permission in Telegram, Hermes creates a deterministic spending policy, Firefly approves the policy hash as a hardware-in-the-loop commitment, and strict mode requires Firefly approval again for the exact payment commitment before the local Mac gateway sends the Algorand payment.

The key idea:

> x402 explains how agents pay. Hermes Sign402 explains how humans safely authorize agents to pay.

## One-liner

**Hermes Sign402 is a Telegram AI agent that pays x402 resources on Algorand, but only after Firefly approves the policy and, in strict mode, the exact payment.**

## Problem

x402 enables a web-native payment flow:

```text
client requests protected resource
  -> resource server returns HTTP 402 Payment Required
  -> client submits payment proof
  -> facilitator verifies settlement
  -> resource server grants access
```

This is ideal for AI agents, pay-per-use APIs, machine-to-machine payments, premium data, on-demand compute, and automated commerce.

But it creates a missing trust layer:

**How does a human safely decide what an AI agent is allowed to spend?**

Bad options:

- give the agent unrestricted wallet access;
- manually approve every payment and destroy automation;
- rely only on a backend spending limit;
- use voice confirmation alone, which can be misheard;
- fall back to API keys and subscriptions, which misses the point of x402.

Agentic commerce needs a permission model between human intent and autonomous payment execution.

## Solution

Hermes Sign402 adds two Firefly-backed controls before x402 payments are executed:

- a **Firefly-approved policy commitment**, which defines what the agent may do;
- a **Firefly-approved payment commitment**, which makes the exact payment Ledger/Trezor-like in strict mode.

The policy answers:

```text
Can this agent make this x402 payment?
```

Example policy:

```json
{
  "version": "1",
  "agentId": "hermes-demo",
  "policyId": "policy-berlin-001",
  "allowedMerchant": "demo-merchant.local",
  "allowedPurpose": "x402_api_access",
  "asset": "ALGO_TEST",
  "maxBudgetAtomic": "1000000",
  "maxPerPaymentAtomic": "50000",
  "validFrom": "2026-06-06T09:00:00Z",
  "expiresAt": "2026-06-07T21:00:00Z",
  "nonce": "berlin-demo-001"
}
```

For the MVP, Firefly runs modified `pixie-provision` firmware with serial approval commands:

```text
PAYMENT=<64 hex chars>
```

For policy approval, the 64 hex characters are the full 32-byte SHA-256 hash of the canonical policy JSON. The gateway intentionally uses the `PAYMENT=<policyHash>` approval path for policy approval too, because the older `POLICY=<policyHash>` command can leave the current test Firefly silent after approval.

This demonstrates a **hardware-in-the-loop policy approval**, not a full cryptographic policy signature. Strict mode adds physical approval for each payment hash. Full Firefly-side transaction signing or DS attestation is a future extension.

## Core User Story

A user wants their Telegram AI agent to access paid APIs during a task without giving it unlimited wallet control.

They send a Telegram text message:

```text
Hermes, you can spend up to 1 test ALGO on x402 APIs during this demo.
```

Hermes builds a spending policy, canonicalizes it, hashes it, sends the policy hash to the Sign402 Gateway, and Firefly returns an approval event for that exact hash.

After that, Hermes can evaluate x402 requests that fit the policy:

- only the allowed merchant or purpose;
- only the allowed asset;
- no more than the per-payment limit;
- no more than the total budget;
- only before expiration;
- only with fresh nonces/payment intents.

In strict mode, a matching policy is not enough to send money. Hermes must build a payment commitment, get Firefly approval for that payment hash, and then ask the local Sign402 Gateway to execute the Algorand payment. Hermes never receives the Algorand private key.

## x402 Role Mapping

### Client / Agent

**Hermes** is the Telegram bot and x402 client. It receives user messages through Telegram, requests protected resources, receives `402 Payment Required`, evaluates payment requirements, coordinates Firefly approval and local gateway payment execution, and retries with proof.

### Merchant

The merchant is the economic actor selling a service, API, data, compute, or access.

### Resource Server

The resource server protects the HTTP endpoint. It returns `402 Payment Required` when payment is required and returns the resource after verification.

### Facilitator

The facilitator verifies settlement, pricing, access rules, replay protection, and double-spend prevention.

In the MVP, the facilitator can be:

- a managed/reference x402 facilitator, if integration is quick;
- a simple local verification service that mimics facilitator behavior;
- a combined merchant/facilitator for demo simplicity.

### Firefly / Sign402 Approval Layer

Firefly is not the resource server, merchant, or settlement layer.

Firefly is the **hardware policy and payment approval layer** in the MVP:

```text
human intent in Telegram -> canonical policy -> SHA-256 hash -> Firefly POLICY approval -> bounded agent permission
x402 requirement -> canonical payment commitment -> SHA-256 hash -> Firefly PAYMENT approval -> payment execution
```

In the full product version, Firefly becomes the cryptographic human authorization layer:

```text
policy/payment shown on Firefly -> physical approval -> signed policy/payment or signed Algorand transaction
```

The policy can be enforced by:

- the Hermes client before making a payment;
- a custom facilitator before accepting payment proof;
- the merchant/resource server as an extra verification step.

For the hackathon MVP, enforce it in the Sign402 Gateway and show facilitator-style verification in the dashboard.

## End-to-End Flow

1. User creates permission in Telegram:

```text
Hermes, allow yourself to spend up to 1 test ALGO on x402 APIs.
```

2. Hermes receives the Telegram message.

3. Hermes turns the command into a structured spending policy.

4. Hermes sends a Telegram confirmation:

```text
Prepared policy:
Budget: 1.00 test ALGO
Max/payment: 0.05 test ALGO
Purpose: x402 APIs

Approving this policy with Firefly.
```

5. Hermes canonicalizes the policy and computes:

```text
policyHash = SHA-256(canonicalPolicyJson)
```

6. The Sign402 Gateway sends the full hash over USB Serial:

```text
PAYMENT=<64 hex chars>
```

7. Firefly displays the policy commitment and returns an approval event:

```text
<policy.approved=buffer:<policyHash> (32 bytes)
<device.model=number:262
<device.serial=number:1056
<OK
```

8. Hermes sends a Telegram message:

```text
Policy approved by Firefly. Hermes can now pay x402 resources within this budget.
```

9. User asks Hermes to buy a paid resource:

```text
buy x402 probe for algorand.co
```

10. Hermes calls the short-mode Sign402 Gateway endpoint:

```text
POST /agent/buy-probe
{"target":"algorand.co"}
```

11. The gateway requests the protected resource locally.

12. Resource server responds:

```http
402 Payment Required
```

Example payment requirements:

```json
{
  "amount": "0.05",
  "asset": "ALGO_TEST",
  "network": "algorand-testnet",
  "receiver": "MERCHANT_ALGO_ADDRESS",
  "resource": "/probe?target=algorand.co",
  "paymentIntent": "payment-123"
}
```

13. The gateway checks the payment requirements against the Firefly-approved policy:

```text
amount <= maxPerPayment
spent + amount <= maxBudget
asset matches
merchant/purpose matches
policy not expired
paymentIntent not reused
policyHash matches Firefly-approved hash
```

14. If allowed, the gateway builds a canonical payment commitment and computes:

```text
paymentApprovalHash = SHA-256(canonicalPaymentCommitmentJson)
```

15. The gateway sends the payment hash to Firefly:

```text
PAYMENT=<paymentApprovalHash>
```

16. Firefly displays the payment hash and waits for physical approve/cancel.

17. If Firefly approves and the returned hash matches, the gateway executes the local payment path internally.

```text
internal execute-payment
```

18. The gateway sends the Algorand TestNet payment locally. The Algorand private key stays on the Mac and is never returned to Hermes.

19. The gateway retries the resource request with x402 payment proof and Sign402 policy/payment proof:

```http
GET /probe?target=algorand.co
X-Payment: <x402-compatible payment proof>
X-Sign402-Policy: <canonical policy payload>
X-Sign402-Policy-Hash: <sha256 policy hash>
X-Sign402-Firefly-Approval: <approval event>
X-Sign402-Payment-Approval-Hash: <sha256 payment approval hash>
X-Sign402-Device-Model: 262
X-Sign402-Device-Serial: 1056
```

20. Facilitator or local verifier checks:

- Algorand transaction exists;
- receiver matches merchant;
- amount and asset match the x402 requirement;
- payment intent/nonce matches;
- settlement is final;
- submitted policy hashes to `X-Sign402-Policy-Hash`;
- policy hash equals the Firefly-approved hash;
- payment approval hash equals the Firefly-approved payment hash;
- policy limits were respected;
- payment intent has not already been accepted.

21. Resource server grants access.

22. The gateway writes the latest run event for the dashboard.

23. Hermes replies in Telegram:

```text
Paid 0.05 test ALGO via Algorand TestNet.

Result:
algorand.co is reachable
HTTP 200
Latency: 42 ms

Remaining budget: 0.95 test ALGO
Tx: TESTNET_TX_ID
```

## What Makes It Different

This is not a voice wallet.

This is not a generic x402 paywall.

Hermes Sign402 is a **policy and consent layer for autonomous x402 payments**.

The project separates:

- **intent**: user describes what the agent may do in Telegram;
- **commitment**: Firefly approves a hash of the canonical policy;
- **policy enforcement**: Hermes/facilitator checks payment requirements against the approved policy;
- **settlement**: Algorand handles the payment;
- **access**: x402 resource server returns the protected resource.

Telegram is the primary input interface. The dashboard is the primary demo surface. Voice is an optional UX layer inside Telegram. Firefly is the hardware approval layer. x402 is the commerce protocol. Algorand is the settlement layer.

## Hackathon Track Fit

### x402 Payments

x402 is the center of the project. Hermes buys access to protected HTTP resources using `402 Payment Required`, payment proof, and resource retry.

### Agentic Commerce

Hermes is an AI agent that can autonomously purchase services after the user delegates bounded authority.

### Cross-Border Payments

User, agent, merchant, and resource server can be in different countries. Algorand provides low-cost global settlement without card networks or bank rails.

### Tokenization

The Firefly-approved policy commitment acts like a programmable capability:

```text
This agent may spend up to X, for Y purpose, until Z time.
```

Future versions can turn policies into on-chain access passes, prepaid credits, or delegated capability tokens.

### DeFi

The MVP does not need DeFi. DeFi can be a future extension:

- agent swaps into the required payment asset;
- agent routes payments through liquidity;
- Firefly signs bounded DeFi strategies;
- policy limits control slippage, protocols, assets, and max loss.

### RWA

The merchant can later be a physical resource:

- charging station;
- vending machine;
- locker;
- sensor node;
- compute node;
- local network probe.

For the MVP, use a paid API endpoint and position physical services as the next step.

## MVP Scope

The MVP should be narrow enough for 36 hours.

### Must Have

- Telegram bot or server-side Hermes integration reachable from Telegram;
- Telegram text commands for policy creation and paid resource requests;
- policy builder that creates deterministic canonical JSON;
- local Sign402 Gateway on the Mac connected to Firefly and the Algorand payment executor;
- Firefly visible over USB Serial;
- modified `pixie-provision` or equivalent serial firmware on Firefly;
- Firefly `PAYMENT=<64 hex chars>` approval command;
- Firefly approve/cancel button handling for payment commitments;
- Firefly approval event containing approved policy hash and device identity;
- Firefly screen showing a policy approval/commitment state;
- x402-compatible protected resource server;
- `402 Payment Required` response with structured requirements;
- x402-compatible `X-Payment` proof submission where possible;
- policy check before payment;
- Algorand TestNet payment execution;
- local `POST /agent/buy-probe` gateway endpoint so Hermes can use one short product command;
- local `POST /execute-payment` debug endpoint so low-level tests can run without giving Hermes private keys;
- payment proof submission;
- facilitator-style verification step;
- dashboard showing the complete flow.

### Nice To Have

- managed/reference x402 facilitator integration;
- Firefly display of full policy details;
- full Firefly-side canonical policy signing;
- Firefly-side Algorand transaction signing;
- DS-based `ATTEST` path if valid `pubkey-n`, `cipherdata`, and `attest` material are provisioned;
- Telegram voice messages with speech-to-text;
- TTS reply;
- remaining budget tracker;
- multiple paid resources;
- policy history;
- signed receipts per payment.

### Out Of Scope For MVP

- mainnet payments;
- real user funds;
- Ethereum/Base;
- production-grade custody;
- full Algorand wallet implementation inside Firefly;
- full on-chain policy enforcement;
- complex merchant marketplace;
- full DeFi swaps.

## Recommended Demo Resource

Use a simple paid API so the product focus stays on x402 authorization.

Recommended:

**Paid Network Probe**

Hermes pays an x402-protected endpoint to check whether a target website is reachable from Berlin.

Example result:

```json
{
  "target": "algorand.co",
  "location": "Berlin",
  "httpStatus": 200,
  "latencyMs": 42,
  "paymentTx": "TESTNET_TX_ID",
  "policyNonce": "berlin-demo-001"
}
```

Why this works:

- no extra hardware required;
- easy to explain;
- realistic paid API use case;
- fits pay-per-API access and agent-to-service payments;
- can be replaced later by compute, data, or physical services.

## Architecture

```text
User in Telegram
   |
   v
Hermes Telegram Bot
   |
   v
Text Parser
   |
   v
Policy Builder
   |
   v
Canonical JSON + SHA-256
   |
   v
Sign402 Gateway
   |
   v
Firefly over USB Serial
   |
   v
Policy Approval Event
   |
   v
Hermes x402 Client
   |
   v
Resource Server returns 402
   |
   v
Sign402 Policy Check
   |
   v
Sign402 Gateway executes payment
   |
   v
Algorand TestNet Payment
   |
   v
Facilitator / Local Verifier
   |
   v
Resource Server grants access
```

## Components

### Hermes Telegram Bot

Responsible for:

- receiving Telegram text messages;
- receiving Telegram voice messages if enabled;
- parsing user intent;
- creating spending policies;
- storing policy state;
- tracking budget used in the demo;
- acting as x402 client;
- checking policies before payments;
- calling the Sign402 Gateway for Firefly approval and payment execution;
- retrying resource requests with proof.

For the current demo, Hermes can run on a server and reach the Mac through a Cloudflare tunnel to the Sign402 Gateway. For a fully local fallback, the bot can run on the laptop with Telegram long polling.

### Speech-to-Text Module

Responsible for converting Telegram voice messages into text.

Voice is nice-to-have. The text bot flow must work first.

### Sign402 Gateway

Responsible for:

- connecting the local bot backend to Firefly over USB;
- detecting the Firefly serial port;
- canonicalizing the policy payload;
- hashing the policy payload;
- sending the full 32-byte policy hash as 64 hex chars with `PAYMENT=<policyHash>`;
- sending payment commitment hashes as `PAYMENT=<hash>`;
- receiving the Firefly approval event;
- executing Algorand TestNet payments through the local payment executor;
- keeping Algorand private keys on the Mac;
- returning the result to Hermes.

Recommended hackathon setup:

```text
Hermes server -> Cloudflare tunnel -> Sign402 Gateway on Mac -> Firefly over USB + local Algorand payment executor
```

This keeps Firefly and private key material on the Mac. Hermes gets only approval events, payment metadata, and transaction ids. If Firefly is disconnected, rejects, times out, or returns a mismatched hash, the gateway must not execute the payment.

Local demo launcher:

```bash
cd "/Users/mp/Documents/Berlin Hack"
bash scripts/start-local-demo.sh
```

The launcher starts:

- x402 resource server on `127.0.0.1:8090`;
- Sign402 Gateway on `127.0.0.1:8099`;
- dashboard server on `127.0.0.1:8100`.

If macOS blocks scripts or the virtualenv inside `Documents` with `Operation not permitted`, the launcher is not required. The manual fallback is:

```text
1. Ensure resource server is healthy on 127.0.0.1:8090.
2. Start Sign402 Gateway on 127.0.0.1:8099 with the current Firefly serial port.
3. Start a Cloudflare tunnel to 127.0.0.1:8099.
```

The known working manual gateway command uses the system Python plus explicit import paths, avoiding the blocked virtualenv executable:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"

env FIREFLY_PORT=/dev/cu.usbmodem11301 SIGN402_GATEWAY_PORT=8099 \
/opt/homebrew/opt/python@3.14/bin/python3.14 -c 'import sys; sys.path[:0]=["/Users/mp/Documents/Berlin Hack/payment-executor/.venv/lib/python3.14/site-packages","/Users/mp/Documents/Berlin Hack/sign402-gateway","/Users/mp/Documents/Berlin Hack/sign402-bridge","/Users/mp/Documents/Berlin Hack/live-demo","/Users/mp/Documents/Berlin Hack/payment-executor","/Users/mp/Documents/Berlin Hack/demo-resource-server"]; from sign402_gateway.server import main; main()'
```

For server-side Hermes short mode, expose only the Sign402 Gateway with a Cloudflare tunnel. The resource server stays local because the gateway calls it directly. A separate resource tunnel is optional for low-level protocol debugging.

Gateway endpoints:

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
```

`POST /agent/buy-probe` is the product/demo orchestration endpoint. Hermes sends only:

```json
{
  "target": "algorand.co"
}
```

The gateway then performs the full Sign402 flow locally:

```text
request x402 resource -> receive 402 -> enforce stored Firefly-approved policy -> ask Firefly to approve payment hash -> execute Algorand payment -> retry with X-Payment -> write dashboard event -> return result
```

This removes the need for long per-run Hermes instructions.

`GET /agent/tools`, `POST /agent/inspect-tool`, and `POST /agent/buy-tool` are the agent-facing paid-tool layer. They make the demo tool-oriented instead of URL-oriented:

```text
agent lists/inspects paid tool -> reads price, asset, receiver, and payment hash -> Firefly approval -> official x402 payment -> paid tool result
```

The first built-in paid tool is:

```json
{
  "id": "goplausible.weather",
  "name": "GoPlausible Weather",
  "mcpStyleName": "get_weather",
  "resourceUrl": "https://x402.goplausible.xyz/examples/weather"
}
```

Hermes can inspect it with:

```json
{
  "tool": "goplausible.weather"
}
```

Then execute it with the same body at `POST /agent/buy-tool`. Internally this uses the same official GoPlausible/x402-v2 purchase path as `/agent/buy-x402`, but it presents the experience as a paid tool call rather than a hardcoded URL purchase. This is the bridge toward MCP/Bazaar-style discovery.

`POST /agent/inspect-x402` is the GoPlausible/x402-v2 compatibility checkpoint. Hermes or a developer can send:

```json
{
  "url": "https://example.x402.goplausible.xyz/protected"
}
```

The gateway requests the external resource without payment, expects `402 Payment Required`, and parses official Algorand x402 fields:

```json
{
  "scheme": "exact",
  "network": "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
  "amount": "10000",
  "asset": "10458941",
  "payTo": "ALGORAND_RECEIVER",
  "maxTimeoutSeconds": 60,
  "extra": {}
}
```

The gateway normalizes this into the Sign402 payment commitment shape:

```json
{
  "network": "algorand-testnet",
  "x402Network": "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
  "amountAtomic": "10000",
  "asset": "10458941",
  "receiver": "ALGORAND_RECEIVER",
  "purpose": "x402_api_access"
}
```

`POST /agent/buy-x402` executes the official GoPlausible/x402-v2 path. It:

1. fetches the external resource and reads the `Payment-Required` header;
2. normalizes the Algorand TestNet accept option;
3. checks the stored Firefly-approved policy;
4. asks Firefly to approve the canonical Sign402 payment hash;
5. builds an official `x402-avm` `PAYMENT-SIGNATURE` payment group;
6. retries the GoPlausible resource request;
7. stores the gateway event with the facilitator payment response.

This is now the strongest hackathon path:

```text
Hermes Telegram -> Sign402 Gateway -> GoPlausible 402 -> Firefly approval -> x402-avm paymentGroup -> PAYMENT-SIGNATURE -> GoPlausible facilitator -> protected API response
```

If the external x402 resource does not include a nonce, payment intent, or equivalent replay identifier, the gateway creates a fresh local Sign402 intent for each purchase. This avoids treating stable GoPlausible payment requirements as replay while preserving replay protection for explicit resource-provided intents.

### Firefly Approval Firmware

Responsible for:

- running modified `pixie-provision` or equivalent serial firmware;
- exposing a USB Serial REPL;
- accepting `PAYMENT=<64 hex chars>`;
- displaying the policy commitment on the device;
- returning the approved policy hash and device identity.

The current tested response format is:

```text
<policy.approved=buffer:<hash> (32 bytes)
<device.model=number:262
<device.serial=number:1056
<OK
```

Firefly does not execute Algorand payments in the MVP.

### Firefly Consent App

This is the ideal product version, but not required for the first MVP.

Responsible for:

- receiving a policy payload;
- showing human-readable policy details;
- reading approve/reject button input;
- signing or attesting the policy only after approval;
- returning the signature or attestation.

For the hackathon, this is stretch work after the current serial `PAYMENT=<hash>` approval path and payment flow work.

### Resource Server

Responsible for:

- exposing protected HTTP endpoints;
- returning `402 Payment Required`;
- generating a fresh `paymentIntent` for every unpaid 402 response;
- keeping pending payment requirements until matching proof is accepted;
- serving the resource after successful verification.

### Facilitator / Local Verifier

Responsible for:

- verifying Algorand settlement;
- checking amount, asset, receiver, and payment intent;
- preventing replay in the demo;
- verifying the Firefly-approved policy commitment if implemented server-side.

### Dashboard

Responsible for showing:

- Telegram user command;
- transcript if the input was voice;
- generated policy;
- policy hash;
- Firefly approval state;
- Firefly device identity;
- x402 `402` response;
- policy decision;
- payment approval hash;
- Firefly payment approve/cancel result;
- Algorand transaction;
- verification result;
- access granted;
- remaining budget.

Current dashboard implementation:

- static file: `/Users/mp/Documents/Berlin Hack/demo-dashboard/index.html`;
- polls `http://127.0.0.1:8099/events/latest` every 2 seconds;
- falls back to the last embedded successful Telegram trace if the gateway is not running;
- event data is stored locally at `/Users/mp/Documents/Berlin Hack/demo-dashboard/latest-run.json`.

## Policy Message Design

The policy should be deterministic and easy to hash.

Canonical policy example:

```json
{
  "version": "1",
  "agentId": "hermes-demo",
  "policyId": "policy-berlin-001",
  "allowedMerchant": "demo-merchant.local",
  "allowedPurpose": "x402_api_access",
  "asset": "ALGO_TEST",
  "maxBudgetAtomic": "1000000",
  "maxPerPaymentAtomic": "50000",
  "validFrom": "2026-06-06T09:00:00Z",
  "expiresAt": "2026-06-07T21:00:00Z",
  "nonce": "berlin-demo-001"
}
```

The policy payload must be canonicalized before hashing.

For the MVP:

- use RFC 8785 / JCS canonical JSON;
- show the policy hash in the dashboard;
- include `policyId` or `nonce` in the Algorand transaction note;
- use the same canonicalization library in every language boundary. If the backend is Python-only, keep policy hashing in Python. If Node is used, use a JCS-compatible package and verify hashes with fixtures.

## Firefly Integration Plan

Primary MVP path:

```text
Firefly Pixie -> modified pixie-provision -> USB Serial -> PAYMENT=<policyHash> -> approval event
```

Strict payment path:

```text
x402 payment requirement -> canonical payment commitment -> SHA-256 -> Firefly PAYMENT=<paymentHash> -> physical approve/cancel -> Algorand payment or stop
```

The current tested device appears on macOS as:

```text
/dev/cu.usbmodem11201
```

The port may change after reconnecting the device, so the gateway should either accept a configured port or scan for `/dev/cu.usbmodem*`.

If multiple `/dev/cu.usbmodem*` devices are connected, the gateway must not guess silently. It should list candidates and require `FIREFLY_PORT=/dev/cu.usbmodemXXXX`.

### Current Firefly Test Result

The real device was flashed and tested with the modified firmware.

Working command:

```text
PAYMENT=00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
```

Observed response:

```text
<policy.approved=buffer:00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff (32 bytes)
<device.model=number:262
<device.serial=number:1056
<OK
```

This confirms the Firefly MVP loop is usable. In the current gateway, both policy hashes and payment hashes go through the same reliable `PAYMENT=<hash>` approval path:

```text
hash -> Firefly -> approval event -> Hermes
```

The real device was also tested with a payment approval command:

```text
PAYMENT=00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
```

Approve response:

```text
<payment.approved=buffer:00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff (32 bytes)
<device.model=number:262
<device.serial=number:1056
<OK
```

Cancel response:

```text
<payment.rejected=buffer:ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100 (32 bytes)
! PAYMENT rejected by user
<ERROR
```

Button mapping found by GPIO diagnostics:

```text
GPIO10 = approve
GPIO8 = cancel
GPIO3 / GPIO2 = available extra buttons
```

### Current Verified Telegram Live Result

The current short-mode Telegram flow has been tested end-to-end with Hermes calling the gateway `/agent/buy-probe` endpoint:

```text
Telegram command -> Sign402 Gateway /agent/buy-probe -> x402 402 response -> policy check -> Firefly PAYMENT approval -> Algorand TestNet tx -> X-Payment retry -> dashboard event -> access granted
```

Observed result:

```json
{
  "decision": "approved_and_executed",
  "paymentApprovalHash": "9176849009f5ab571de7eb647f3b718e7ed8c021a991afd9f3ee2c6a3bacea61",
  "txId": "HGZ2C5BLWRO6GVQPY3ORYHPAN463FFZWMJMJ5XOI52WQQFHWQR7A",
  "policyHash": "c48a9b0b21479ed2ca08dff60c265274f7c5950e7ab4728048f76a5265338490",
  "resource": "/probe?target=algorand.co",
  "paymentIntent": "intent-64dc6816b6438a0f",
  "amountAtomic": "50000",
  "remainingBudgetAtomic": "950000",
  "network": "algorand-testnet",
  "result": "reachable",
  "latencyMs": 42
}
```

This confirms the current MVP claim: Hermes can initiate the purchase from Telegram, but the payment only executes after Firefly physically approves the exact payment hash.

### Policy Approval Flow

1. Hermes creates canonical policy JSON.

2. Hermes computes:

```text
policyHash = SHA-256(canonicalPolicyJson)
```

3. The Sign402 Gateway sends the full 32-byte hash as 64 hex chars:

```text
PAYMENT=<64 hex chars>
```

4. Firefly displays the approval/commitment state and returns:

```text
<policy.approved=buffer:<policyHash> (32 bytes)
<device.model=number:<model>
<device.serial=number:<serial>
<OK
```

5. Hermes stores:

```json
{
  "policy": "...canonical policy...",
  "policyHash": "...sha256 hex...",
  "fireflyApproval": {
    "approvedHash": "...sha256 hex...",
    "deviceModel": 262,
    "deviceSerial": 1056
  }
}
```

6. Hermes includes the policy hash and approval event when submitting x402 payment proof.

### Strict Payment Approval Flow

The main product/demo mode hides these steps behind `POST /agent/buy-probe`. The gateway performs the full flow below internally.

For low-level debugging, Hermes or a script can still use the explicit strict flow. The important rule is the same: the Algorand transaction must not be sent immediately after a policy check.

Instead:

1. The resource server returns `402 Payment Required` with payment requirements.
2. Hermes verifies the payment requirements against the approved policy.
3. Hermes builds a deterministic payment commitment:

```json
{
  "type": "sign402-payment",
  "policyHash": "<policyHash>",
  "network": "algorand-testnet",
  "asset": "ALGO_TEST",
  "amountAtomic": "50000",
  "receiver": "<merchant address>",
  "resource": "/probe?target=algorand.co",
  "paymentIntent": "<intent>",
  "purpose": "x402_api_access"
}
```

4. Hermes canonicalizes this payment commitment and computes:

```text
paymentApprovalHash = SHA-256(canonicalPaymentCommitmentJson)
```

5. Hermes calls the Sign402 Gateway:

```text
POST /approve-payment
```

6. The gateway sends to Firefly:

```text
PAYMENT=<paymentApprovalHash>
```

7. Firefly displays the hash and waits for physical input:

- approve button: return `payment.approved` and continue;
- cancel button: return `payment.rejected` and stop;
- timeout: stop.

8. Only after `payment.approved` and hash match does Hermes call the gateway payment executor:

```text
POST /execute-payment
```

9. The gateway sends the Algorand payment locally and returns safe payment metadata:

```json
{
  "ok": true,
  "payment": {
    "txId": "<algorand testnet tx id>",
    "network": "algorand-testnet",
    "receiver": "<merchant address>",
    "amountAtomic": "50000",
    "asset": "ALGO_TEST",
    "paymentIntent": "<intent>",
    "policyHash": "<policyHash>",
    "note": "sign402:<policyHash>:<paymentIntent>"
  }
}
```

10. Hermes retries the resource request with `X-Payment`.

11. The x402 payment proof includes `paymentApprovalHash` alongside `policyHash`, `paymentIntent`, and `txId`.

### Important Limitation

The current MVP serial approval path is not a cryptographic signature over the full policy.

It proves that the demo flow includes Firefly as a hardware-in-the-loop approval device and that Hermes only proceeds after Firefly returns approval for the submitted policy hash.

The current `PAYMENT=<hash>` path also proves physical approve/cancel gating for the exact payment hash before the gateway executes the Algorand transaction.

It does not fully prove that:

- Firefly parsed the full human-readable policy;
- Firefly cryptographically signed the policy;
- a remote verifier can independently validate the approval without trusting the local gateway.

The earlier `ATTEST` path was tested and is not currently usable as the MVP dependency because the device NVS does not contain:

```text
nvs.pubkey-n
nvs.cipherdata
nvs.attest
```

`ATTEST=<64 hex chars>` currently fails with missing `pubkey`, `cipherdata`, and `attest` material. Therefore, `ATTEST` should be treated as a stretch/future path, not the core hackathon path.

MVP verifier requirement:

```text
canonical policy -> SHA-256 -> equals Firefly-approved hash
```

Full product requirement:

```text
Firefly displays canonical policy -> user approves on-device -> Firefly signs full policy or full policy hash
Firefly displays payment summary -> user approves on-device -> Firefly signs Algorand transaction
```

## Verification Logic

Before payment, Hermes verifies:

- Firefly approval event exists;
- submitted canonical policy hashes to the stored policy hash;
- stored policy hash equals the Firefly-approved hash;
- payment amount is within `maxPerPaymentAtomic`;
- total spent remains within `maxBudgetAtomic`;
- asset matches;
- merchant or purpose matches;
- policy is active and not expired;
- payment intent has not been used;
- in strict mode, Firefly is connected and returns `payment.approved` for the exact payment commitment hash.

Hermes stores used payment intents in an in-memory set for the demo:

```text
usedPaymentIntents = Set<policyId + ":" + paymentIntent>
```

For production, this must be durable storage shared by the verifier/facilitator.

After payment, facilitator/local verifier checks:

- Algorand transaction exists;
- finality/confirmation is acceptable;
- receiver address matches requirement;
- amount and asset match requirement;
- transaction note links to policy/payment intent;
- x402 proof corresponds to the paid resource;
- x402 proof includes the payment approval hash when strict mode is enabled;
- submitted policy hashes to the claimed policy hash;
- policy hash equals the Firefly-approved hash;
- policy/payment intent has not already been accepted.

Because public Algorand indexers can lag immediately after broadcast, Hermes should retry the final resource request with a short backoff before treating transaction lookup failure as final. Demo rule:

```text
after /execute-payment -> retry X-Payment verification for 2-5 seconds if indexer returns 404/not found
```

## Security Model

### What The MVP Protects

- agent cannot pay until a Firefly approval event exists for the policy hash;
- in strict mode, agent cannot send a payment unless Firefly is connected and the user physically approves that payment hash;
- agent cannot exceed per-payment limit;
- agent cannot exceed total demo budget;
- policy expires automatically;
- user delegates the policy through the Telegram flow, with Firefly providing a hardware approval step for the canonical policy hash;
- payment requests are tied to nonce/payment intent to reduce replay risk.

### What The MVP Does Not Fully Solve

- production-grade custody;
- malicious host computer;
- cryptographic proof that Firefly signed the policy;
- Firefly parsing and displaying the full human-readable policy;
- malicious merchant collusion;
- full on-chain policy enforcement;
- mainnet security;
- formal x402 facilitator certification;
- regulatory or financial compliance.

This is acceptable for the hackathon if the limitation is stated clearly:

> MVP: Firefly-approved policy commitments. Full product: Firefly-signed policies.

## Demo Script

### Opening

> x402 lets AI agents pay for HTTP resources. But there is a missing piece: how do humans control what an agent is allowed to spend?

### Demo

1. Presenter starts the local demo stack:

```bash
cd "/Users/mp/Documents/Berlin Hack"
bash scripts/start-local-demo.sh
```

2. Presenter exposes one gateway tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8099
```

3. Hermes is configured with that gateway URL only. The resource server stays local behind the gateway.

4. User opens Telegram and sends Hermes:

```text
approve sign402 policy
```

5. Hermes creates the policy, calls `POST /approve-policy`, and the gateway sends `PAYMENT=<policyHash>` over USB Serial.

6. Firefly shows the policy hash with approve/cancel. The user physically approves it.

7. Hermes replies in Telegram:

```text
Policy approved by Firefly.
```

8. Dashboard shows the approved policy hash and Firefly device identity.

9. User asks in Telegram:

```text
buy x402 probe for algorand.co
```

10. Hermes calls the short-mode gateway endpoint:

```text
POST /agent/buy-probe
{"target":"algorand.co"}
```

11. The gateway requests the protected resource and receives `402 Payment Required`.

12. The gateway checks the payment requirements against the stored Firefly-approved policy.

13. The gateway builds the canonical payment commitment and sends `PAYMENT=<paymentApprovalHash>` to Firefly.

14. Firefly shows the payment hash with approve/cancel. The user physically approves the payment.

15. The gateway sends the Algorand TestNet payment locally. Hermes never receives the private key.

16. The gateway retries the resource request with `X-Payment`.

17. The resource server verifies settlement and grants access.

18. The gateway writes the latest run event for the dashboard.

19. Hermes replies in Telegram with decision, transaction id, policy hash, payment hash, resource result, and remaining budget.

20. Dashboard shows the full trace: Telegram command, canonical policy, policy hash, 402, policy check, payment approval hash, Firefly approve/cancel result, gateway payment execution, Algorand transaction, verification, access granted.

### Closing

> We are not just making agents pay. We are making agent payments controllable, auditable, and safe enough for humans to trust.

### Demo Surface

Telegram is the input channel.

The dashboard should be the main visual surface for judges. It should show the entire trace clearly enough that the audience can understand the system even if they cannot read the Telegram chat on stage.

Current static dashboard path:

```text
/Users/mp/Documents/Berlin Hack/demo-dashboard/index.html
```

## Implementation Strategy

### Before Hackathon

Validate Firefly basics:

- connect over USB-C;
- confirm the macOS serial port appears, for example `/dev/cu.usbmodem11201`;
- flash modified `pixie-provision` or equivalent serial firmware;
- run `NOP`, `PING`, `VERSION`, and `DUMP`;
- run `PAYMENT=<64 hex chars>`;
- confirm Firefly returns approval/rejection, `device.model`, `device.serial`, and `<OK` on approval.

Already validated:

- Firefly appears over USB Serial;
- firmware build and flash work with ESP-IDF v5.3.2;
- `PAYMENT=<64 hex chars>` is used for both policy-hash approval and payment-hash approval in the current gateway;
- `PAYMENT=<64 hex chars>` returns approve or reject based on Firefly button input;
- device identity from test device is model `262`, serial `1056`;
- Hermes Telegram short-mode flow has executed a real Algorand TestNet payment through the Sign402 Gateway;
- `ATTEST` is not required for the MVP path.

Also prepare:

- Algorand TestNet wallet;
- ALGO TestNet payment flow;
- optional USDC-like ASA flow with opt-in tested before the hackathon;
- minimal x402 resource server;
- fallback local verifier;
- Telegram bot token;
- Hermes Telegram integration prompt;
- local Sign402 Gateway;
- Cloudflare tunnel fallback for server-side Hermes.

### During Hackathon

Build in this order:

1. Telegram bot text mode or server-side Hermes Telegram integration;
2. deterministic policy format and in-memory policy store;
3. local Sign402 Gateway using `PAYMENT=<64 hex chars>` for both policy and payment approval;
4. protected resource server that returns `402 Payment Required`;
5. policy check before payment;
6. Firefly payment approval before payment;
7. Algorand TestNet payment through gateway and verification;
8. retry resource request with `X-Payment` and Sign402 headers;
9. Telegram result messages with tx id and remaining budget;
10. dashboard trace;
11. Telegram voice input;
12. optional Firefly full-policy display;
13. optional DS `ATTEST` recovery if provisioning material is available;
14. polish, pitch, and demo rehearsal.

## Backup Plan

If Firefly serial communication is unstable:

- show Firefly detected as a USB device;
- use the last captured `PAYMENT=<hash>` approval event in the dashboard;
- continue the payment flow with a clearly marked demo approval.

If Firefly approval firmware breaks:

- host app signs the policy for demo;
- explain that the current hardware approval firmware works separately and show the recorded serial response.

If voice input is unstable:

- use text input;
- present voice as optional UX.

If Telegram webhook or server setup is unstable:

- use Telegram long polling;
- run the bot backend locally on the demo laptop.

If Hermes cannot resolve a fresh `*.trycloudflare.com` hostname:

- confirm the tunnel works from the Mac with `curl https://<host>/health`;
- get Cloudflare edge IPs with `dig +short <host>`;
- tell Hermes to call the same hostname with `curl --resolve <host>:443:<ip>`;
- never call the raw IP URL directly, because TLS and host routing require the original hostname.

If Hermes LLM calls fail with OpenRouter `HTTP 429`:

- check whether the selected model is rate-limited upstream;
- avoid `openrouter/owl-alpha` for live demos if it is returning 429;
- switch Hermes to a stable OpenRouter model such as `openai/gpt-4.1-mini`;
- restart the Hermes container after config changes.

If managed x402 facilitator integration is unstable:

- implement the protocol-shaped flow manually:

```text
GET resource -> 402 response -> Firefly payment approval -> Algorand payment -> retry with proof -> access
```

- keep the request/response shape close to x402: structured payment requirements, `402 Payment Required`, `X-Payment`, and a payment proof that can be inspected in the dashboard.

Current official x402 integration status:

- implemented: GoPlausible/x402-v2 payment requirement parsing;
- implemented: conversion from official `amount/payTo/network/asset` fields into Sign402 commitment fields;
- implemented: `POST /agent/inspect-x402` for protocol compatibility checks;
- implemented: official `x402-avm` signer integration for Algorand TestNet USDC `paymentGroup`;
- implemented: official facilitator-backed settlement through `PAYMENT-SIGNATURE` against GoPlausible weather API;
- implemented: `POST /agent/buy-x402` for Firefly-approved official GoPlausible purchases.

Latest verified official x402 run through Hermes Telegram:

```text
command: buy goplausible weather
decision: approved_and_executed
mode: official_x402_avm
asset: USDC TestNet ASA 10458941
amountAtomic: 10000
amount: 0.01 USDC
txId: BTVGJ3MN42KKFBUN6BV3QRDZZDO54H2OCDX5LKHRU3PFASFYW72A
receiver: ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA
remainingBudgetAtomic: 80000
result: official_x402_resource_access_granted
```

If USDC test asset is slow to set up:

- use ALGO on TestNet for the demo;
- keep the UI denominated as demo credits or test currency;
- avoid spending hackathon time debugging ASA opt-in unless the ALGO flow is already working.

## Success Criteria

The project is successful if judges understand this in under three minutes:

- x402 lets agents buy HTTP resources;
- Hermes is a Telegram agent that wants to pay;
- the user delegates a bounded budget;
- Firefly approves the canonical policy hash over USB Serial;
- Firefly approves the exact payment hash before money is sent;
- Hermes pays through Algorand only after policy and payment approval checks;
- resource access is granted after x402-compatible verification;
- the dashboard proves the budget and policy were respected.

## Final Positioning

Hermes Sign402 is not a generic crypto wallet.

It is a **hardware-backed approval layer for autonomous x402 commerce**.

It answers:

> When AI agents can pay for things, how do humans safely give them permission?

Answer:

> With Firefly-approved policy and payment commitments today, Firefly-signed policies or transactions next, x402 payment flows, and Algorand settlement.

## Bitrefill Catalog Paid With SINGIT

The Gateway defaults to `SIGN402_BITREFILL_MODE=test`. This mode exposes Bitrefill-compatible free test products and cannot spend Bitrefill balance. Live mode is intentionally unavailable until `BITREFILL_API_KEY` exists and the official API contract has been tested.

Search the catalog:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/search-bitrefill \
  -H "Content-Type: application/json" \
  -d '{"query":"phone","country":"US","includeTestProducts":true}'
```

Inspect packages and recipient requirements:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/get-bitrefill-product \
  -H "Content-Type: application/json" \
  -d '{"productId":"test-phone-refill","country":"US"}'
```

Create an explicit quote:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/quote-bitrefill \
  -H "Content-Type: application/json" \
  -d '{"productId":"test-phone-refill","packageId":"1","country":"US","recipient":{"phone":"+12025550123"}}'
```

Use the returned `quoteId` with `/agent/buy-bitrefill`. Firefly approves the exact product type, package, price, recipient commitment, expiration, and maximum SINGIT amount before Bankr is called.
