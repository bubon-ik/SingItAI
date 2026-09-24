# Project agent instructions

## Purchases through Bitrefill

- For every request to browse, price, order, or buy a product, invoke the repository skill `$bitrefill` first.
- Use the project `bitrefill` MCP server as the purchase-creation route for every item supported by the Bitrefill catalog, including gift cards, prepaid products, mobile top-ups, eSIMs, and bill payments.
- One exception, decided by the owner on 24 September 2026: purchases on the Trezor allowance lane (`docs/trezor-allowance-v1.md`) go through Bitrefill's x402 API (`https://api.bitrefill.com/x402`), paid by that user's agent key and funded by their on-chain limiter. The pay route's recipient must be Bitrefill's published x402 address and the amount no more than the price the user confirmed; settlement is read from the chain. Every other rule in this section applies to that lane unchanged.
- Do not bypass Bitrefill with a browser checkout, direct merchant API, CLI purchase, or another commerce provider when Bitrefill supports the requested item.
- If Bitrefill does not support the requested purchase, stop and tell the user. Do not buy through another route unless the user explicitly authorizes that exception.
- Before calling `buy-products`, or creating an order on the x402 route, show the exact product, denomination, total price, network/payment method, and any recipient details, then wait for explicit user confirmation. Never auto-approve an order.
- Do not commit API keys, wallet secrets, payment links, redemption codes, eSIM activation data, or other bearer-value data. Keep redemption data out of files and logs.
- Log only the non-secret purchase record required by the Bitrefill skill: invoice ID, product slug, amount, payment method, and timestamp.
