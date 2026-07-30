# Bitrefill Guest Checkout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Buy Bitrefill products as an anonymous guest so purchases attribute to affiliate code `nrVGauph`, and deliver each code to the buyer's own email as a second path alongside chat.

**Why:** Bitrefill confirmed on 2026-07-30 that a purchase made from the account holding the affiliate code is never attributed — the commission cannot be earned on the current architecture at all. Their proposed fix is an unauthenticated MCP session plus `email` on `buy-products`.

**Architecture:** `McpBitrefillClient` gains a checkout mode. In `guest` mode it opens the MCP session with no `Authorization` header, sends the buyer's email with the cart, and keeps the returned `invoice_access_token` for every later `get-invoice-by-id`. The token is bearer value, so it is persisted only through the commerce store's existing reserved-encrypted-field mechanism. Payment, approval, funding, and refunds are untouched.

**Tech Stack:** Python 3.11, `unittest`, SQLite, MCP client, Fernet via `SensitiveStateCipher`, Hermes plugin (Telegram).

## Global Constraints

- The account path stays fully working; `SIGN402_BITREFILL_CHECKOUT_MODE` selects between `account` (default) and `guest`.
- `invoice_access_token` and `access_link` are bearer values: never logged, never returned in an API response, never stored in plaintext.
- A guest purchase must not start without a stored, validated buyer email.
- Losing the invoice access token must fail loudly at write time, not silently at delivery time — the buyer's email is the backstop, not an excuse to drop it quietly.
- Chat stays the primary delivery path; email is the receipt trail.
- Guest mode cannot use `payment_method` `balance` or `cashback`; selecting them together is a configuration error.
- No change to funding, swap, approval, or the token-return path.
- Production verification must not create or pay a live invoice.

---

## File Map

- `sign402-gateway/sign402_gateway/bitrefill_mcp.py`: checkout mode, guest session, `email` on the cart, invoice-access-token capture and reuse.
- `sign402-gateway/sign402_gateway/commerce_store.py`: `encryptedInvoiceAccess` reserved field, mirroring `encryptedRecipient`.
- `sign402-gateway/sign402_gateway/user_emails.py`: new — encrypted per-user buyer email store.
- `sign402-gateway/sign402_gateway/server.py`: checkout-mode wiring, email routes, config validation.
- `sign402-gateway/sign402_gateway/diagnostics.py`: keep invoice-access fields out of logs.
- `hermes-plugins/sign402-wallet/__init__.py`: ask for, show, change, and forget the buyer email.
- Tests alongside each: `tests/test_bitrefill_mcp.py`, `tests/test_commerce_store.py`, `tests/test_user_emails.py`, `tests/test_gateway_server.py`, `tests/test_diagnostics.py`, `hermes-plugins/sign402-wallet/tests/test_plugin.py`.

---

### Task 1: Guest MCP Session

**Files:** Modify `bitrefill_mcp.py`; test `tests/test_bitrefill_mcp.py`.

**Interfaces:**
- `McpBitrefillClient(..., checkout_mode: str = "account")`, accepting only `account` or `guest`.
- In `guest` mode `McpToolCaller` is built with `api_key=""`, so no `Authorization` header is sent.
- The affiliate ref stays on the URL query in both modes.

**Steps:**
- [x] Test: guest mode builds both callers without an API key and keeps `?ref=`.
- [x] Test: account mode is unchanged and still sends the bearer header.
- [x] Test: an unknown checkout mode is rejected at construction.
- [x] Test: `guest` with `payment_method: balance` is rejected — a guest has no account balance.
- [x] Implement.

### Task 2: Buyer Email On The Cart

**Files:** Modify `bitrefill_mcp.py`; test `tests/test_bitrefill_mcp.py`.

**Interfaces:**
- `prepare_purchase(..., buyer_email: str = "")`.
- Guest mode sends `email` alongside `cart_items` on `buy-products`; account mode never sends it.

**Steps:**
- [x] Test: guest mode refuses to prepare a purchase without a buyer email.
- [x] Test: guest mode passes `email` through to `buy-products`.
- [x] Test: account mode omits `email` even when one is supplied.
- [x] Test: the email never appears in a raised error message.
- [x] Implement, including the protocol and deterministic client.

### Task 3: Invoice Access Token Storage

**Files:** Modify `commerce_store.py`; test `tests/test_commerce_store.py`.

**Interfaces:**
- Metadata key `invoiceAccess` (object) encrypts to reserved `encryptedInvoiceAccess`, decrypting back on read, exactly like `encryptedRecipient`.
- Writing `encryptedInvoiceAccess` directly raises `SensitiveStateError`.
- Persisting `invoiceAccess` without a configured cipher raises `SensitiveStateError` rather than storing plaintext.

**Steps:**
- [x] Test: round-trip encrypt/decrypt of an invoice access record.
- [x] Test: the reserved key cannot be supplied by a caller.
- [x] Test: no cipher configured is a hard failure, not a plaintext write.
- [x] Test: the raw token never appears in the stored row.
- [x] Implement, generalising the existing recipient mechanism to a field table.

### Task 4: Guest Polling

**Files:** Modify `bitrefill_mcp.py`; test `tests/test_bitrefill_mcp.py`.

**Interfaces:**
- `prepare_purchase` returns the access token inside the checkpoint for the store to encrypt.
- `complete_purchase` and status polling pass `invoice_access_token` to `get-invoice-by-id` in guest mode.

**Steps:**
- [x] Test: guest mode fails preparation when the provider returns no access token.
- [x] Test: `get-invoice-by-id` carries the token in guest mode and omits it in account mode.
- [x] Test: a redemption poll with a missing stored token raises a recoverable error naming no secret.
- [x] Implement `invoice_status` plus token capture in the invoice snapshot.

### Task 5: Buyer Email Store

**Files:** Add `user_emails.py`; modify `server.py`; tests `tests/test_user_emails.py`, `tests/test_gateway_server.py`.

**Interfaces:**
- `set_email(telegram_user_id, email)`, `get_email(telegram_user_id)`, `forget_email(telegram_user_id)`.
- Stored encrypted with `SensitiveStateCipher`; responses return only a masked form (`k***@example.com`).
- Validation reuses the address rules already applied to Bankr LLM purchases.

**Steps:**
- [x] Test: set/get round-trip returns the address, and the stored row holds no plaintext.
- [x] Test: an invalid address is rejected before storage.
- [x] Test: reading an unset user returns nothing rather than raising.
- [x] Test: forget removes the row and later reads return nothing.
- [x] Test: `mask_email` exposes only the masked form.
- [x] Implement `user_emails.py`; the gateway routes remain for Task 6's wiring.

### Task 6: Telegram Email Capture

**Files:** Modify `hermes-plugins/sign402-wallet/__init__.py`; test `tests/test_plugin.py`.

**Interfaces:**
- A guest purchase with no stored email asks for one and holds the pending quote.
- `/email` shows the masked address; `/email <address>` sets it; `/forget_email` clears it.
- The welcome screen mentions that a code also goes to the buyer's email.

**Steps:**
- [x] Gateway route `/agent/buyer-email` with get/set/forget, returning only the masked address.
- [x] Client method plus `/email`, `/email <address>` and `/forget_email` commands.
- [x] Test: the address is never echoed unmasked back into chat.
- [x] Test: buying without a stored email asks for it instead of creating an invoice.
- [x] Test: a supplied address is stored and the purchase resumes.

**Note:** the welcome screen was left alone. Guest mode is off by default, and
on the account path nothing is emailed at all, so an unconditional "your codes
also go to your email" line would be false. `/help` lists `/email` and
`/forget_email`, which is the discoverability path.

### Task 7: Diagnostics And Wiring

**Files:** Modify `diagnostics.py`, `server.py`, `scripts/run-wallet-bitrefill.sh`, `.env.wallet-bitrefill.example`; tests `tests/test_diagnostics.py`, `tests/test_gateway_server.py`.

**Steps:**
- [x] Test: `invoice_access_token`, `access_link`, and the buyer email are filtered out of provider diagnostics.
- [x] Test: the factory reads `SIGN402_BITREFILL_CHECKOUT_MODE` and defaults to `account`.
- [x] Test: guest mode without a configured cipher refuses to start.
- [x] Implement, and document both settings.

### Task 8: End-To-End Wiring (added during execution)

Tasks 1–4 built the guest pieces but nothing joined them: the stored buyer
email never reached `prepare_purchase`, and the access token was captured into a
checkpoint whose sanitizer silently drops unknown keys, so it was lost before
the first poll. Guest mode could not have completed a purchase.

**Files:** `bitrefill_mcp.py`, `bitrefill_runner.py`, `bitrefill.py`, `server.py`;
tests `tests/test_bitrefill_mcp.py`, `tests/test_bitrefill_runner.py`.

**Steps:**
- [x] Test: `complete_purchase`, the post-payment poll, and `refresh_purchase` carry the token in guest mode and omit it in account mode.
- [x] Test: preparation stores the token under `invoiceAccess` and leaves it out of both the row and the returned checkpoint.
- [x] Test: a token that cannot be persisted fails the purchase loudly, naming no secret.
- [x] Test: the wallet purchase runner names the buyer when it prepares.
- [x] Test: order lookup refreshes a guest order with its stored token.
- [x] Implement, passing `buyer_email` and `invoice_access_token` only when set so the account path calls the provider client exactly as before.

---

## Answers From Bitrefill (2026-07-30)

1. **The affiliate code stays on the MCP URL** and survives a guest purchase; they verified it with a Hermes agent.
2. **The 25 tx/day cap no longer applies** — the purchase is not made from our account. This removes the need for the order-count guard that was previously an open item.
3. **`email` is mandatory.** Any burner address is fine. It matches their own website's guest checkout, and exists precisely so a closed chat does not lose the product.
4. **Losing the invoice access token is not fatal** — the buyer has the codes in their email. Storage is still required for polling inside the purchase flow, but it is no longer the only route to the product.
5. **Support is unchanged:** help.bitrefill.com or their support email, for guest and logged-in orders alike. They plan to surface it through MCP later.

## Verification

- [x] Full gateway suite (738 tests) and plugin suite (175 tests) green.
- [x] The account path is unchanged: the provider client is called with exactly the arguments it always was, because the guest arguments are omitted when unset. The one new call on that path is the plugin asking the gateway whether an address is required, which answers `false` and buys as before.
- [ ] One live low-value purchase in guest mode, then confirm attribution appears in the affiliate dashboard.

## How The Anonymous Session Actually Works (verified 2026-07-30)

"Unauthenticated MCP session" does not mean sending no `Authorization` header —
that is answered with an OAuth challenge (`401`, `WWW-Authenticate: Bearer`) and
kills the connection before any tool runs. Bitrefill supports **anonymous
dynamic client registration**, so a session belonging to no account is obtained
by registering a throwaway client:

1. `GET /.well-known/oauth-protected-resource` → `resource`, `authorization_servers`
2. `GET /.well-known/oauth-authorization-server/oauth/mcp` → `registration_endpoint`, `token_endpoint`
3. `POST /oauth/mcp/register` with `redirect_uris` (required even for a headless client) → `client_id`, `client_secret`
4. `POST /oauth/mcp/token` with `grant_type=client_credentials` **and `resource`** (rejected without it) → `access_token`, 6h
5. MCP session with that bearer on `…/mcp?ref=<code>` → `search-products`, `get-product-details`, `buy-products`, `get-invoice-by-id` all available

Probed end to end from the shipped code: the anonymous session returns the real
catalog and product details. `SIGN402_BITREFILL_AFFILIATE_REF` stays on the URL.

## Known Limits

- Guest checkout is wired for the managed-wallet chat flow. The agent route `/agent/buy-bitrefill` has no Telegram user and therefore no stored address, so in guest mode it fails at invoice creation instead of buying.
- `buy_product` (the unprepared path) keeps its access token only in memory for the length of the call, so a later redemption re-read of such an order falls back to `redemptionUnavailable`. The codes still reach the buyer's email.
