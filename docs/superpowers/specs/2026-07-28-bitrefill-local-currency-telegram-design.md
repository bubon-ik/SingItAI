# Bitrefill Local-Currency Telegram Display

## Goal

Display a Bitrefill product's denomination in its catalog currency in every
Telegram purchase-completion message. A Czech gift card with a package value
of `500` and currency `CZK` must be shown as `500 CZK`, not `$500`.

## Scope

The change covers both completion-message paths in
`sign402_gateway.bitrefill_runner`:

1. the message returned immediately after a successful purchase; and
2. the delivery message returned when `/last_purchase` reveals the purchase.

The catalog amount picker already formats non-USD currencies correctly and is
not changed. USD-denominated pricing, service fees, payment-token amounts, and
the `Spent: …` line are also outside the change.

## Formatting Contract

A single private denomination formatter will receive `packageValue` and
`currency` from the persisted quote.

- If `packageValue` is empty, it returns no denomination suffix.
- If `currency`, after trimming and uppercasing, is `USD`, it returns
  ` $<packageValue>`.
- If a nonempty, non-USD currency is present, it returns
  ` <packageValue> <CURRENCY>`.
- If `currency` is absent, it preserves the existing fallback and returns
  ` $<packageValue>`.

This contract supports every catalog currency without maintaining a
currency-symbol table or adding locale-dependent formatting.

## Data Flow

Bitrefill product normalization already stores `currency` and `packageValue`
in the quote. Quote persistence retains those fields. Both Telegram formatters
will read the same quote and call the shared denomination formatter, so the
currency does not need to be inferred from the product name, country, payment
token, or USD settlement price.

## Error Handling and Compatibility

Currency input is normalized with `str(...).strip().upper()`. Missing currency
uses the legacy USD-style display for compatibility with old persisted quotes.
Unexpected but nonempty currency codes are displayed verbatim after
normalization rather than silently mislabeled as dollars.

Existing USD messages retain their current `$100` style. The payment currency
remains independently represented by messages such as
`Spent: 123.45 SINGIT`.

## Tests

Regression tests will exercise the real formatter paths and assert literal
user-visible output:

1. Immediate purchase completion formats a CZK quote as
   `Wolt Czech Republic 500 CZK is ready` and does not contain `$500`.
2. `/last_purchase` delivery formats a CZK quote as
   `Wolt Czech Republic 500 CZK is ready` and does not contain `$500`.
3. Existing USD output remains `Test Gift Card $25 is ready`.
4. A missing currency retains the legacy `$25` fallback for old quotes.

The focused test module and the complete gateway test suite will be run after
the implementation.
