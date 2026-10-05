# Bitrefill Solana — first mainnet purchase

Verified on September 18, 2026. This records an operator-assisted live purchase,
not a completed Telegram purchasing feature.

## Result

- Product: Alza CZ gift card, 200 CZK.
- User-approved spending ceiling: 9.45 USDC; final merchant charge: 9.44 USDC.
- Payment: native USDC on Solana mainnet through Bitrefill's x402 payment route.
- The project Bitrefill MCP client created one order using `usdc_solana`.
- The payment response was HTTP 200 with `accepted: true`.
- Mainnet RPC confirmed the exact transaction message constructed by the
  project's Solana SDK wrapper, at slot `448020361`.
- Bitrefill subsequently reported the invoice complete and the product delivered.
- The paying Solana wallet authenticated with a fresh SIWX message and retrieved
  the actual Alza 200 CZK redemption code. The code was displayed through a
  local, memory-only page with `Cache-Control: no-store`.
- The buyer's SOL balance was unchanged; Bitrefill's designated fee payer
  sponsored the transaction fee.

[Confirmed Solana transaction](https://solscan.io/tx/4VRuBGv35WgvsriGgXaD9CD272jhDvFiEA3DbnHuW3wAd8WJ8RJd3i7DoNQqgfB5kSrKavmjy3XDS9R96EVCZBtW)

## What was exercised

The run reused the repository's `GuestMcpAuthorizer`, `McpToolCaller`,
`loadWallet`, `SolanaChain.balances`, `SolanaChain.build` and
`SolanaChain.verify`. Temporary operator helpers coordinated the provider
calls, exact amount checks, mainnet genesis check, simulation and single
submission. No generic Bitrefill Solana adapter was added to the agent by
this verification run.

The payment used the `exact` scheme, x402 version 2, native Solana USDC,
the provider's advertised recipient and a separate sponsored fee payer.
The signed transaction was simulated before submission. A local exclusive
attempt marker prevented submitting this invoice again. It was not necessary
to retry a purchase or payment.

OAuth tokens, the invoice access token, signed payment payload, wallet key
and redemption data stayed out of Git and logs. The ignored local purchase
record contains only the invoice ID, product, denomination, payment method
and timestamp. Buyer email and private receipt links are excluded here.

## Remaining work

- Integrate this verified provider route into the per-user gateway and Telegram
  approval flow, spending limits and durable reconciliation.
- Implement reusable checkout and delivery handling, including recovery after
  an uncertain response or process restart. The temporary helpers are not a
  production recovery mechanism.
- Test those integration boundaries and deploy an isolated bot.
- Complete the separate Venice payment and paid model-response milestone.

The funded dedicated prototype wallet used here is not a managed Telegram
user wallet. This purchase does not enable Solana spending in the agent.

Provider reference: [Bitrefill partner integration guide](https://docs.bitrefill.com/docs/mcp-partner-integration-guide).
