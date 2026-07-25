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
  **This check lives in Hermes, not in this repository.** Only the outbound
  template client (`whatsapp_cloud.py`) is here; inbound decisions reach the
  gateway through `/agent/imessage/decision` with `channel=whatsapp`, carrying
  the Photon bearer token. So the gateway cannot itself tell a signed Meta
  webhook from a forged one — that guarantee is entirely Hermes's, and it must
  be re-verified whenever the adapter is redeployed. Last confirmed by external
  probe on 2026-07-14 (unsigned POST → `401`, bad verify token → `403`); see
  `docs/security-audit/2026-07-14-risk-register.md`.

### What hardens this beyond the shared tokens

- **Per-user access tokens.** On wallet creation the gateway mints a per-user
  token (only its SHA-256 hash is stored). Wallet reads and purchases send it as
  `X-Sign402-User-Token`; the gateway derives the user id from the token and
  rejects a body `telegramUserId` that disagrees (`_authenticated_user_id`).
  This blocks a single per-user token from acting as a *different* user.
  **It does not by itself contain a leaked shared token:** because the trusted
  plugin must be able to re-mint a token for a returning user after a restart,
  `/agent/create-wallet` will issue a per-user token for any `telegramUserId`
  presented with the shared token. Treat `SIGN402_WALLET_API_TOKEN` as a fully
  privileged server-side secret — a holder can read any user's balances/limits
  (funds still require an on-device approval, and Bitrefill redemption codes are
  revealable only once; see below).
- **Redemption codes are single-reveal.** A Bitrefill redemption code is fetched
  with a stored reveal token. The first `/agent/last-purchase` that reveals the
  code clears that token (`UserPurchaseStore.clear_fulfillment_token`), so the
  code cannot be re-fetched afterward by any later per-user token.
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
  WhatsApp always requires it (the id rides in the button payload). For
  iMessage it is optional by default, because a sidecar that does not send one
  would otherwise be unable to approve anything. Without it a "YES" resolves to
  the oldest pending approval — normally the very one the user saw, since a
  second approval cannot be created while one is pending, but a reply arriving
  after the first expired could land on a newer commitment.
  **To close that:** confirm the sidecar echoes `approvalId`, then set
  `SIGN402_REQUIRE_IMESSAGE_APPROVAL_ID=true`. Verify by approving one purchase
  with the flag on in a staging run: if the sidecar omits the id, the decision
  is refused with "No pending approval." and nothing is spent.
- **Approval text cannot be forged by provider data.** Approval bodies are
  newline-joined, and some lines carry third-party content (a Bitrefill product
  name). `_sanitize_context_lines` collapses whitespace and strips control
  characters — including U+2028/U+2029 and NEL — before any line is hashed,
  stored, displayed, or sent, so smuggled text cannot present itself as an extra
  `Total:` line above the real one. The Firefly path was already safe:
  `format_payment_context_command` drops everything outside printable ASCII and
  the `|` field separator.
- **Payments are bound to approved terms.** The x402 signer refuses to pay an
  amount/receiver/asset other than what was approved (`payment-guard`), and
  per-user spend limits cap per-transaction and daily USD value (including
  Bitrefill).
- **Spend limits hold budget across the approval wait.** The daily cap is
  checked before the approval prompt but the spend is only recorded after the
  payment settles. `UserSpendLimitStore.reserve_within_limits` closes that
  window: the check and the hold happen under one lock, and an unexpired hold
  counts against the cap exactly like a settled record, so a second purchase
  started while the first awaits approval cannot measure itself against a stale
  total. Holds are released on rejection, timeout, or error, and expire on their
  own after `SPEND_RESERVATION_TTL_SECONDS`. The Bankr LLM path is instead
  bounded by a partial unique index allowing one active purchase per user.
- **Wallet creation is rate limited.** `/agent/create-wallet` runs on the shared
  token alone and mints a per-user token for whatever id it is given, so it is
  capped both per user and globally
  (`SIGN402_WALLET_CREATIONS_PER_MINUTE`, default 30/min).

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
