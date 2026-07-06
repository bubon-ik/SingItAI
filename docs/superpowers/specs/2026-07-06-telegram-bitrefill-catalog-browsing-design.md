# Telegram Bitrefill Catalog Browsing Design

## Goal

Let a Telegram user browse the complete Bitrefill catalog available for their
selected country without knowing a product name in advance. Preserve the
existing search, product selection, iMessage approval, and SINGIT payment flow.

## Product Experience

The Bitrefill menu contains:

- `Browse Catalog`
- `Search Products`
- `Change Country`
- `Back`

`Browse Catalog` opens these user-facing categories:

- All
- Shopping
- Food
- Games
- Mobile
- Travel
- Entertainment

The selected country includes both local products and international products
tagged by Bitrefill as `XI`. Each product page contains at most eight numbered
products plus `Previous`, `Next`, and `Back` controls. Selecting a product
continues through the existing package, recipient, approval, and purchase flow.

Search remains independent and unchanged.

## Gateway API

Add `POST /agent/list-bitrefill-products` with this request:

```json
{
  "country": "CZ",
  "category": "food",
  "start": 0,
  "limit": 8,
  "includeInternational": true,
  "includeTestProducts": false
}
```

The gateway converts the country filter to `CZ,XI` when international products
are enabled and calls Bitrefill's official `GET /products` endpoint. The
response contains normalized product summaries and pagination metadata:

```json
{
  "ok": true,
  "products": [],
  "start": 0,
  "limit": 8,
  "hasPrevious": false,
  "hasNext": true
}
```

The gateway owns Bitrefill pagination details so the Telegram plugin does not
depend on provider URLs or response metadata.

## Category Mapping

Telegram category labels map to Bitrefill category filters:

- All: no category filter
- Shopping: `retail,ecommerce,gifts,giftcard,electronics,apparel`
- Food: `food,restaurants,food-delivery,groceries`
- Games: `games`
- Mobile: `refill,phone,data,bundles`
- Travel: `travel,flights,experiences`
- Entertainment: `entertainment,streaming,music`

The mapping is server-controlled and may be expanded without changing the
Telegram interaction contract.

## Telegram Session State

The existing per-user Bitrefill session stores:

- selected country;
- selected category label and filter;
- current offset;
- current page of products;
- `hasPrevious` and `hasNext`.

`Next` and `Previous` request adjacent pages from the gateway. `Back` from a
product page returns to categories; `Back` from categories returns to the
Bitrefill menu. Invalid product numbers keep the current page and keyboard.

## Errors

- An empty category page shows a stable message and returns to category
  selection.
- Provider or gateway errors use the existing Telegram-safe error handling.
- Pagination controls are shown only when the corresponding page exists.
- The server clamps `limit` to a safe maximum and rejects negative offsets.
- No catalog action creates a quote, requests approval, or moves funds.

## Testing

Gateway tests cover:

- listing selected-country and `XI` products;
- category forwarding;
- start and limit validation;
- normalized `hasPrevious` and `hasNext`;
- the new HTTP route.

Telegram plugin tests cover:

- opening category selection;
- requesting the first category page;
- next and previous navigation;
- selecting a product from a browsed page;
- empty pages and invalid choices;
- retaining the existing search and payment flows.

## Completion Criteria

A Telegram user can select a country, browse local and international Bitrefill
products by category, move between pages, select a product and denomination,
and complete the existing iMessage-approved SINGIT purchase flow.
