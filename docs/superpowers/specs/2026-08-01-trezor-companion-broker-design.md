# Trezor Companion Broker

## Purpose

Extend the isolated local Trezor proof so a Sign402 agent running on a VPS can
request hardware approval from a user's computer. Trezor Suite MCP and its
bearer token remain on that computer. Existing managed wallets, Bitrefill
routes, Hermes commands, iMessage, WhatsApp, services, databases, and
environment files remain unchanged.

## Processes

```text
VPS: separate Trezor agent plugin
  -> existing Bitrefill MCP client
  -> Trezor broker client

VPS: sign402-trezor-broker (loopback behind HTTPS reverse proxy)
  -> per-user enrollment
  -> narrow durable approval/payment job queue

User computer: sign402-trezor-companion
  -> outbound HTTPS polling only
  -> local sidecar at 127.0.0.1:8111
  -> Trezor Suite MCP at 127.0.0.1:21340/mcp
```

The VPS never connects to the Suite MCP endpoint and never receives its token,
a private key, a signature, or a raw signed transaction. The local sidecar
independently validates and broadcasts the exact Base USDC transfer and returns
only its transaction hash.

## Isolation and rollout

- All new code lives under `trezor-sidecar/` and this design document.
- The broker uses a separate state database and a separate internal bearer.
- The companion makes outbound HTTPS requests; no user port is exposed.
- The broker accepts only `purchase_intent` and `usdc_payment` jobs.
- One enrolled Base address is bound to one explicitly allowlisted user.
- Live purchases require three independent flags: proof, remote agent, and
  remote purchases.
- The separate Hermes plugin registers only `/trezor_status`, `/trezor_test`,
  `/trezor_prepare`, `/trezor_confirm`, and `/trezor_cancel`.
- It never replaces `/bitrefill`, never imports the working wallet plugin, and
  never falls back to a managed wallet or an existing approval channel.
- Initial deployment uses one test user and a maximum of 1 USDC.

## Enrollment

An operator creates a one-time enrollment code on the VPS for one numeric user
ID. The user first pairs the local sidecar with the Trezor and verifies the Base
address on the device. The companion exchanges the code and that verified
address over HTTPS for a random bearer token. The broker stores only a salted
token hash, the public Base address, identifiers, status, and timestamps.

Enrollment codes expire, are single-use, and are stored only as hashes. A new
address requires explicit revocation and enrollment; it is never silently
substituted.

## Jobs

The remote client on the VPS creates an idempotent job with a short expiration.
The companion claims it and calls only the corresponding high-level local
sidecar method:

- `purchase_intent` -> approve the fixed EIP-712 `PurchaseIntent`;
- `usdc_payment` -> pay the bound Bitrefill invoice.

The companion returns the sidecar's bounded response or a fixed safe error.
Jobs are single-consumer, expire closed, and are never automatically recreated.
A leased job may be reclaimed only before a local operation has been reported
as started. Payment ambiguity remains a reconciliation condition rather than a
retry.

## Purchase flow

1. The separate agent obtains product details and an exact quote.
2. It shows product, denomination, exact maximum USDC, Base network, payment
   method, recipient details, expiration, and a one-time confirmation code.
3. Nothing is bought until the user sends that exact code.
4. The VPS queues the typed purchase intent. The Trezor user approves it.
5. Only then the existing Bitrefill MCP adapter calls `buy-products` once.
6. The returned Base USDC address and amount are strictly validated.
7. The VPS queues the exact payment. The local sidecar checks balances,
   constructs the canonical USDC transfer, obtains physical Trezor approval,
   validates the signed transaction, and broadcasts it.
8. The VPS polls the existing Bitrefill invoice and returns redemption data
   once without persisting it.

## Production safety

The working gateway and Hermes plugin are not modified by this phase. The new
broker and plugin are staged disabled. No production process is restarted or
reconfigured during implementation. Before any deployment, all sidecar tests
and the existing gateway and Hermes regression suites must pass. The first live
test requires an exact receipt and a fresh explicit user confirmation before
`buy-products` is invoked.
