# Universal Bitrefill Catalog Design

## Goal

Replace the Amazon-only dry-run path with a transport-independent Bitrefill commerce flow that can search, quote, approve, and fulfill gift cards, phone refills, and eSIMs. Development must work without a Bitrefill API key by using Bitrefill-compatible free test products. Real purchases remain impossible until an operator explicitly configures live credentials and live mode.

## Scope

The first implementation supports:

- product search by query, country, category, and product type;
- product details, fixed packages, variable ranges, and recipient requirements;
- quotes for an explicitly selected product and package;
- gift cards, phone refills, and eSIM packages;
- the official test product identifiers `test-gift-card-link`, `test-gift-card-code`, and `test-phone-refill`;
- the existing Firefly approval, Bankr SINGIT settlement, single-use fulfillment, and order-status flow;
- automatic selection of test mode when live credentials are absent;
- explicit live-mode gating when credentials are later provided.

Bill-payment form chains and prepaid-card KYC are excluded from this iteration because they require a separate multi-step user-input and compliance design.

## Architecture

### Bitrefill client boundary

`BitrefillClient` becomes a catalog-and-commerce interface with four operations:

1. `search_products(...)` returns normalized product summaries.
2. `get_product_details(...)` returns normalized packages, ranges, product type, and recipient requirements.
3. `quote_product(...)` validates a selected package and returns the authoritative reserve price.
4. `buy_product(...)` fulfills the exact normalized quote.

The rest of Sign402 must not depend on whether the client uses local test fixtures, the eCommerce MCP server, or the REST API.

### Test client

`TestBitrefillClient` exposes a deterministic catalog for the official free test product identifiers. It models the response shapes required by gift cards and phone refills, validates package selection, and returns non-secret test redemption data. It never performs network calls or spends money.

The existing `DryRunBitrefillClient` compatibility name may remain temporarily as an alias, but no code may contain Amazon-only or US-only behavior.

### Live client gate

`build_server` selects the client from `SIGN402_BITREFILL_MODE`:

- unset or `test`: use `TestBitrefillClient`;
- `live`: require `BITREFILL_API_KEY`, otherwise fail during startup;
- any other value: fail during startup.

Live mode must never be inferred merely because a credential exists. This prevents accidental purchases in development.

The initial implementation defines the live-client boundary and configuration gate. Network-backed live fulfillment is enabled only after a Bitrefill API key is available for contract testing against the official API.

## Agent API

### Search

`POST /agent/search-bitrefill`

```json
{
  "query": "phone",
  "country": "US",
  "category": "refill",
  "productType": "phone_refill",
  "includeTestProducts": true
}
```

The response contains normalized product summaries and never creates a quote or payment.

### Product details

`POST /agent/get-bitrefill-product`

```json
{
  "productId": "test-phone-refill",
  "country": "US"
}
```

The response contains fixed packages or a valid custom range plus `recipientType` and required recipient fields.

### Quote

`POST /agent/quote-bitrefill`

```json
{
  "productId": "test-phone-refill",
  "packageId": "1",
  "country": "US",
  "recipient": {
    "phone": "+12025550123"
  }
}
```

Legacy `{query, country, value}` requests are rejected with a clear migration error. Product selection must be explicit so Hermes cannot silently choose a similarly named product.

The quote stores a normalized product snapshot, package snapshot, reserve price, recipient commitment, expiration, and SINGIT maximum. The client revalidates the selected product during fulfillment.

### Buy and status

The existing `/agent/buy-bitrefill`, `/internal/fulfill-bitrefill`, and `/agent/get-bitrefill-order` endpoints remain. Their replay, expiration, redaction, and Firefly protections continue to apply.

## Approval and data binding

The Firefly purchase commitment binds:

- quote ID;
- product ID and product type;
- package ID and display value;
- reserve price and maximum SINGIT amount;
- recipient commitment;
- quote expiration.

The physical confirmation screen shows a short product label, package value, and maximum SINGIT amount. Recipient data is hashed in the approval commitment and never displayed in Bankr logs.

## Fulfillment flow

1. Hermes searches the catalog.
2. Hermes fetches product details and presents valid packages.
3. The user selects a product, package, and required recipient data.
4. Gateway creates and persists a short-lived quote.
5. Firefly approves the exact commitment.
6. Gateway creates a per-order fulfillment token and calls Bankr.
7. Bankr invokes the protected fulfillment endpoint.
8. Gateway validates mode, state, expiration, token, recipient commitment, and product snapshot.
9. The selected client performs exactly one test or live fulfillment.
10. Gateway returns a redacted order status.

## Errors and safety

- Unknown products and packages return a validation error before Firefly.
- Missing recipient fields return a product-specific validation error before quote creation.
- Test mode refuses any product outside the free test catalog.
- Live mode cannot start without `BITREFILL_API_KEY`.
- Product or price changes invalidate the quote instead of silently changing the charged amount.
- Fulfillment failures remain non-success responses and do not emit an x402 settlement amount.
- Redemption data stays in the protected gateway store and is not included in Bankr logs or public dashboard responses.

## Testing

Automated tests cover:

- search filters across gift-card and phone-refill test products;
- fixed package and variable-range validation;
- required phone recipient validation;
- quoting a non-Amazon product;
- Firefly commitment binding product type, package, and recipient;
- successful free test fulfillment;
- rejection of unknown or live products in test mode;
- startup refusal for live mode without an API key;
- regression coverage for expiration, replay protection, secret redaction, and SQLite cleanup;
- Gateway route tests for search, details, quote, buy, fulfillment, and status.

## Completion criteria

The feature is complete when a user can search the local Bitrefill-compatible test catalog, inspect packages, quote `test-phone-refill` or either test gift-card product, approve the exact purchase on Firefly, execute the existing Bankr flow, and retrieve one redacted test order without Amazon-specific logic or any possibility of spending real funds.
