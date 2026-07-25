# Sign402 Gateway — Security Model & Verification

## Trust model (managed wallets, iMessage and WhatsApp approvals)

The gateway is a backend-for-frontend. It trusts two front-end components to
authenticate end users on their platforms, and authenticates *those components*
with shared bearer tokens:

- **Telegram wallet plugin** — holds `SIGN402_WALLET_API_TOKEN`. Telegram
  authenticates the user; the plugin forwards the authenticated `telegramUserId`.
- **iMessage / Photon sidecar** — holds `SIGN402_PHOTON_API_TOKEN`. iMessage
  delivers approval replies from real phone numbers; the sidecar relays them.
- **WhatsApp Cloud / Hermes adapter** — verifies Meta webhook signatures with
  `WHATSAPP_CLOUD_APP_SECRET`, then forwards the trusted `wa_id` through the
  same independent approval bearer-token boundary.

### What hardens this beyond the shared tokens

- **Per-user access tokens.** On wallet creation the gateway mints a per-user
  token (only its SHA-256 hash is stored). Wallet reads and purchases send it as
  `X-Sign402-User-Token`; the gateway derives the user id from the token and
  rejects a body `telegramUserId` that disagrees (`_authenticated_user_id`). A
  leaked shared token therefore cannot act as an arbitrary user.
- **Pairing binds phone ↔ Telegram.** An approver phone is linked only after the
  user enters a pairing code that was delivered to them on their authenticated
  Telegram account.
- **iMessage is approval-only.** Pairing codes and `YES`/`NO` decisions are
  consumed by Sign402; unrelated Photon/iMessage text is dropped before it can
  reach the general Hermes agent.
- **WhatsApp is approval-only.** Pairing codes and exact
  `sign402:(approve|reject):<approval_id>` button payloads are consumed before
  general agent dispatch. Plain text, malformed buttons, and unknown events are
  dropped.
- **Decisions are bound to a commitment.** `record_decision` accepts the
  `approval_id` the sidecar showed the user (from `/agent/imessage/pending`), so
  a stale "YES" cannot approve a different, newer commitment.
- **Payments are bound to approved terms.** The x402 signer refuses to pay an
  amount/receiver/asset other than what was approved (`payment-guard`), and
  per-user spend limits cap per-transaction and daily USD value (including
  Bitrefill).

### Residual assumption (#9): the sidecar is trusted

The iMessage decision endpoint is a single component serving all users, so the
approver identity (`photonUserId`) is asserted by the sidecar rather than proven
per-request. This is an accepted trust boundary: **treat the Photon/iMessage
sidecar as a trusted component.** Operationally:

- Keep `SIGN402_PHOTON_API_TOKEN` and `SIGN402_WALLET_API_TOKEN` secret, strong,
  and rotatable; never log them.
- Keep the Meta System User token, App Secret, and webhook Verify Token secret
  and rotatable. The outbound client returns only fixed error codes and never
  logs Meta response bodies.
- Run the sidecar and gateway on trusted infrastructure; restrict network access
  to the internal endpoints.
- Run the gateway single-process (the SQLite stores serialize with a
  `threading.Lock`; multi-process deployment would weaken the
  claim-once guarantees).

### Production endpoint mode

The legacy Firefly/demo payment executor is disabled by default. Do not set
`SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR` on the public VPS. If it is required
for an isolated local demo, it also requires a distinct
`SIGN402_LEGACY_OPERATOR_API_TOKEN`; it must never reuse a Telegram, Photon,
or managed-wallet token.

CORS is also disabled by default. Leave `SIGN402_ENABLE_CORS` unset while the
gateway is a localhost-only backend for Hermes. Enabling it also requires an
exact comma-separated `SIGN402_CORS_ALLOWED_ORIGINS` list; it should be a
deliberate web-client design decision, not a quick way to make the gateway
reachable from a browser.

The `/agent/test-imessage-approval` probe is disabled by default as well. It is
not part of the user product; enable `SIGN402_ENABLE_TEST_ENDPOINTS=true` only
for a short, operator-controlled diagnostic session, then remove it again.

## P0 containment controls

- New sensitive JSON and SQLite state uses private filesystem modes: directories
  are `0700` and files are `0600`.
- New fulfillment tokens and recipients are Fernet-encrypted with
  `SIGN402_WALLET_MASTER_KEY`.
- Provider snapshots are strict allowlists of the fields required for order
  status and reconciliation; raw provider responses are not durable state.
- Redemption is fetched only after authorization and is never persisted.
- Legacy plaintext state remains read-compatible, but updates fail closed until
  a separately controlled migration is completed.
- Setting `SIGN402_PURCHASES_PAUSED` blocks every transaction-oriented route,
  including LLM verify/reconcile and legacy routes, before its request body is
  read or a handler is dispatched.
- This code package does not migrate or rotate live state. Any such operation
  requires a separately reviewed, operator-controlled migration.

## End-to-end verification checklist

Prerequisites: `SIGN402_WALLET_MASTER_KEY`, `SIGN402_WALLET_API_TOKEN`,
`SIGN402_PHOTON_API_TOKEN`, CDP wallet credentials, `BITREFILL_API_KEY` (used
only by the live Bitrefill MCP client), a funded Base wallet, and either the
iMessage sidecar or signed Hermes WhatsApp Cloud adapter running.

1. **Create wallet** — `/start` (or `/wallet`) in Telegram. Expect a Base
   address and a per-user token issued server-side.
2. **Fund** the address with a little ETH (gas) and USDC.
3. **Balance** — `/balance`. Confirm the balance and that the request carried
   `X-Sign402-User-Token` (check gateway logs).
4. **Pair iMessage** — `/connect_imessage`, enter the pairing code from an
   iMessage on the paired number.
   Alternatively, **pair WhatsApp** — `/connect_whatsapp`, then send the code to
   the configured Meta business number. Confirm this becomes the sole active
   approval channel.
5. **Set limits** — `/limits 0.01 0.05` (per-tx / daily). Try a purchase above
   the per-tx cap and confirm it is rejected before any approval prompt.
6. **Buy (x402 tool)** — "buy crypto news". Approve in iMessage. Confirm: the
   signed on-chain amount equals the approved amount, `/agent/last-purchase`
   shows it, and `/events/latest` does **not**.
7. **Buy (Bitrefill)** — `/bitrefill <productId> <packageId>`. Approve in
   iMessage. Confirm delivery, that the spend counted toward the daily cap, and
   again that `/events/latest` does not expose it.
8. **Negative checks** — a stale/duplicate "YES" does not approve a new
   purchase; a request with another user's `telegramUserId` under a per-user
   token is rejected (401).
9. **WhatsApp negative checks** — a repeated or expired button payload, a
   payload from another `wa_id`, a wrong-channel decision, and ordinary text
   all fail closed without executing payment or reaching the general agent.
