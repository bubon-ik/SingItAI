# Bitrefill Exact Search Design

## Goal

Prevent Telegram search from displaying unrelated Bitrefill products. A search for a company should return only products whose names match that company. If the MCP response contains no relevant product, the agent should say that no exact matches were found instead of presenting unrelated items.

## Scope

This change applies only to the Telegram **Search Products** flow after the existing Bitrefill MCP search returns. It does not change catalog browsing, product details, pricing, purchase creation, or payment.

## User-visible behavior

- `Amazon` returns only products with Amazon in the product name.
- `Amazon gift card` behaves like `Amazon`; generic commerce words do not need to appear in the product name.
- `Biterfill gift card` returns no exact matches when the MCP response contains only `Bitrefill` products. Search does not silently correct company-name typos.
- `eSIM` returns only eSIM products.
- `eSIM Europe` returns eSIM products whose names also match Europe.
- If nothing remains after relevance filtering, the agent says that no exact Bitrefill products were found and asks the user to try another company name.

## Relevance rules

Add one deterministic helper in the Telegram plugin that receives the original query and the normalized MCP product list.

1. Normalize query and product text case-insensitively and ignore punctuation and separator differences.
2. Remove generic commerce terms such as `gift`, `card`, `cards`, `giftcard`, `giftcards`, `voucher`, and `vouchers` from the company-name portion of the query.
3. If the query contains `eSIM`, require the product type/category or product name to identify the product as an eSIM.
4. Require every remaining meaningful query term to occur in the normalized product name. Compact comparison also allows punctuation or spacing differences such as `Bol.com` and `bolcom`, or `Play Station` and `PlayStation`.
5. Preserve the order returned by MCP.
6. If the query contains neither a company term nor the eSIM intent, treat it as insufficiently specific and return no exact matches.

The filter runs locally after the existing MCP response. It performs no retry and no additional network request, so it does not add user-visible latency.

## Data flow

1. Telegram accepts the search text and immediately starts the existing background operation.
2. The gateway performs the existing read-only Bitrefill MCP search.
3. The Telegram plugin normalizes the returned products.
4. The new relevance helper filters those products using the original query.
5. The plugin either stores and displays the relevant products or restores the `awaiting-search` state and sends the no-match response.

## Error handling

MCP and gateway failures continue through the existing background-operation recovery path. A successful MCP response containing only irrelevant products is not an error; it produces the normal no-match response and lets the user search again.

## Tests

Add focused plugin tests proving that:

- an Amazon query removes a non-Amazon product while retaining Amazon;
- `Biterfill gift card` does not display Bitrefill eSIM results and sends the no-match response;
- `eSIM` retains eSIM results and removes non-eSIM results;
- the filtered product list, rather than the unfiltered MCP response, is stored for numeric selection;
- existing Bitrefill search, background execution, catalog, and purchase tests remain green.

## Non-goals

- Fuzzy matching or automatic typo correction.
- AI-based relevance ranking.
- A second MCP request.
- Enabling or disabling any Bitrefill product type.
- Changing the MCP-only purchase route or purchase-confirmation safeguards.
