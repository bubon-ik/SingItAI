# Bitrefill MCP Runtime Migration Design

## Goal

Route every live Bitrefill catalog and purchase operation initiated by a project user through the Bitrefill eCommerce MCP server instead of the Bitrefill v2 REST API. Preserve the existing Telegram/Hermes user experience, Sign402 approval, wallet ownership checks, spending limits, quote persistence, funding, and protected redemption delivery.

The required live path is:

```text
user -> Hermes/Telegram agent -> Sign402 Gateway -> Bitrefill MCP -> user-funded payment -> protected delivery
```

There is no REST fallback in live mode. If MCP is unavailable or rejects a call, the purchase fails closed.

## Scope

This migration includes:

- product search and catalog browsing through `search-products`;
- product details and authoritative denominations through `get-product-details`;
- invoice creation through `buy-products`;
- invoice status, orders, and redemption retrieval through `get-invoice-by-id`;
- service authentication with the existing `BITREFILL_API_KEY` for headless gateway operation;
- the existing `balance` and `usdc_base` payment choices;
- deterministic test mode without network access or real spending;
- tests proving that live mode calls MCP tools and never calls `/v2` REST endpoints.

Multi-step bill-payment/prepayment forms are not added in this migration. A product that requires `submit-prepayment-step` is rejected before purchase with a safe unsupported-product message. That flow needs a separate conversational-state design.

## Current State and Root Cause

`sign402_gateway.bitrefill.LiveBitrefillClient` currently sends HTTP requests directly to `https://api.bitrefill.com/v2`. In particular, `buy_product()` creates an invoice with `POST /invoices`, then reads `/invoices/{id}` and `/orders/{id}`. The repository-level Codex MCP configuration does not affect this application runtime.

The rest of the application already depends on the transport-independent `BitrefillClient` protocol. That boundary is the correct migration point: replace the live implementation while leaving the agent, gateway routes, approval runners, commerce store, and wallet funding flow intact.

## Chosen Architecture

### MCP client adapter

Add `McpBitrefillClient`, implementing the existing `BitrefillClient` protocol. It translates the gateway's normalized operations into Bitrefill MCP tool calls and translates MCP results back into the existing normalized product, quote, invoice, order, and redemption shapes.

The adapter uses the official Python MCP client SDK with Streamable HTTP. The production transport is hidden behind an injectable synchronous `call_tool(name, arguments)` boundary so unit tests do not require network access or credentials. Because the gateway uses `ThreadingHTTPServer`, each application operation owns its MCP client session; no async session or event loop is shared across request threads.

The MCP URL is derived from:

- `SIGN402_BITREFILL_MCP_URL`, defaulting to `https://api.bitrefill.com/mcp`;
- `BITREFILL_API_KEY`, appended using Bitrefill's documented headless authentication form.

The key is never logged, persisted in the commerce store, returned in errors, or committed to the repository.

### Runtime selection

`build_bitrefill_client_from_env()` retains the safe explicit mode gate:

- unset or `test` -> `TestBitrefillClient`;
- `live` -> `McpBitrefillClient`, requiring `BITREFILL_API_KEY`;
- any other value -> startup failure.

`SIGN402_BITREFILL_BASE_URL`, which points at the v2 REST API, is removed from the live path. There is no configuration switch that restores REST fulfillment.

### Compatibility boundary

The public gateway endpoints remain unchanged:

- `POST /agent/search-bitrefill`;
- `POST /agent/list-bitrefill-products`;
- `POST /agent/get-bitrefill-product`;
- `POST /agent/quote-bitrefill`;
- `POST /agent/buy-wallet-bitrefill`;
- `POST /agent/get-bitrefill-order`.

The Telegram/Hermes plugin therefore does not need to know about MCP and does not receive Bitrefill credentials. It continues to call the local Sign402 Gateway.

## Tool Mapping

### Search and catalog

`search_products()` and `list_products()` call `search-products` with the applicable query, country, category, product type, test-product flag, and result limit. Pagination remains a gateway concern: the adapter requests enough results, normalizes them, and applies the existing `start`/`limit` slice.

### Details and quote

`get_product_details()` calls `get-product-details` with the selected product ID and display currency. The adapter normalizes:

- product ID, name, country, currency, stock status, and product type;
- fixed packages and variable ranges;
- `package_value` as the value passed to `buy-products`;
- recipient requirements;
- a safe USD price used by the existing spend-cap and Sign402 quote logic.

`quote_product()` continues to validate the selected package, recipient fields, and `SIGN402_BITREFILL_LIVE_MAX_USD` before any approval or payment.

### Purchase

`buy_product()` calls `buy-products` with exactly one cart item constructed from the approved quote. It passes the package value expected by MCP, not the display-only full package identifier. Required refill input is taken only from the recipient data already bound into the Sign402 commitment.

For Bitrefill balance payments, the adapter requests `payment_method="balance"`. For Base USDC, it requests `payment_method="usdc_base"` and payment details suitable for the existing treasury client.

Immediately after `buy-products` returns, the adapter checkpoints the non-secret invoice ID, status, payment requirements, and order IDs through the existing `checkpoint_callback`. Redemption data is not included in diagnostic logs.

### Payment and delivery

The existing payment safety checks remain authoritative:

- the invoice amount must not exceed `SIGN402_BITREFILL_LIVE_MAX_USD`;
- the invoice amount must not exceed the approved quote by more than the configured basis-point allowance;
- the destination, asset, and Base network must match the expected USDC payment;
- the treasury transfer happens only after all checks pass.

After payment, the adapter polls `get-invoice-by-id` until the invoice is complete or the bounded retry policy expires. It reads order and redemption data from the invoice's nested orders, converts it to the existing provider-result shape, and lets `BitrefillCommerceStore` protect delivery as before.

`refresh_purchase()` also uses `get-invoice-by-id`; it never calls an order REST endpoint.

## MCP Result Handling

The transport accepts standard MCP content blocks. It extracts structured content when present and otherwise decodes the Bitrefill text/TOON response into one normalized mapping. Malformed, oversized, or contradictory responses fail closed with errors that omit credentials and redemption secrets.

At client initialization, the MCP SDK performs its normal protocol negotiation. Tool names are fixed to the four tools in this design; unexpected tool absence is reported as an integration error rather than triggering another transport.

## Concurrency and Timeouts

The gateway remains synchronous at its service boundary. Each MCP operation has a bounded connection/tool timeout and its own session, avoiding cross-thread use of an async MCP session. Invoice polling uses the existing bounded attempt count and interval. A timeout records a non-secret fulfillment error and does not retry invoice creation automatically.

The existing store transition to `FULFILLING` remains the idempotency gate. Only one caller can invoke `buy-products` for a quote. The invoice checkpoint is persisted as soon as a response exists so reconciliation can use its ID without creating another purchase.

## Errors and Safety

- Live startup fails without `BITREFILL_API_KEY`.
- MCP connection, protocol, tool, parsing, and timeout failures fail closed.
- No live code path calls `api.bitrefill.com/v2`.
- `buy-products` is never called before the existing explicit user confirmation and Sign402 approval path succeeds.
- Products requiring unsupported prepayment steps are rejected before invoice creation.
- API keys, payment links, recipient data, and redemption values are excluded from logs and public responses.
- Exact redemption data remains available only through the existing protected order-reveal flow.
- Test mode remains local and cannot spend funds.

## Testing Strategy

Implementation follows test-first development with an injected fake MCP tool transport.

Unit tests cover:

- live factory returns `McpBitrefillClient` and requires an API key;
- live factory uses the MCP URL and never the REST base URL;
- search, list, and details issue the expected MCP tool calls;
- product and package normalization preserves the existing gateway contract;
- `buy-products` receives the approved product, package value, recipient, and payment method;
- balance fulfillment returns the expected invoice/order/redemption result;
- Base USDC fulfillment validates amount, asset, network, destination, and overage before treasury transfer;
- invoice polling and refresh use only `get-invoice-by-id`;
- malformed or secret-bearing MCP errors are sanitized;
- prepayment-required products are rejected without calling `buy-products`;
- concurrent/replayed fulfillment cannot produce a second MCP purchase call.

Gateway and plugin regression tests cover the unchanged user journey: browse, select, quote, approve, pay, poll, and reveal. A test guard scans the live client implementation for the removed `/v2` default and direct REST purchase paths.

No automated test performs a real purchase. A live smoke test is a separate operator action and requires explicit approval, a low-balance account, and the configured spending cap.

## Completion Criteria

The migration is complete when:

1. A user can complete the existing Bitrefill purchase flow inside the Telegram/Hermes agent without a separate Bitrefill login.
2. Every live Bitrefill catalog, invoice, status, and redemption request is made through the eCommerce MCP server.
3. Direct Bitrefill v2 REST calls are absent from the live runtime.
4. Sign402 approval, user-wallet ownership, spend caps, funding, persistence, and protected redemption behavior remain intact.
5. Test mode and the relevant gateway/plugin suites pass without network access or real spending.
