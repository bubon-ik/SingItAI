# MCP v2: x402 payments inside MCP — design proposal

Status: proposal for review (Notion #43, unblocks #66). Nothing here is built yet.

## Where v1 stands (25 October 2026)

- `https://app.singitai.app/mcp` is live, listed in the official MCP Registry as `app.singitai/singit`,
  and on Glama.
- Without a key you get the catalogue and the free tools (`get_price`, `list_price_symbols`,
  `list_models`). Paid tools bill a SingIt API key's own limits.
- An agent with a wallet but no SingIt account cannot use the paid tools at all. Coinbase's x402 Bazaar
  cannot index them either: Bazaar calls a tool with an example input and expects an x402
  `PaymentRequired` answer, then indexes it after the first payment settles through the CDP facilitator
  ([get discovered](https://docs.cdp.coinbase.com/x402/seller/get-discovered)).

## The x402 MCP transport, in one paragraph

Spec: [x402 `specs/transports-v2/mcp.md`](https://github.com/coinbase/x402/blob/main/specs/transports-v2/mcp.md).
A paid tool called without payment returns `isError: true` with the `PaymentRequired` object in
`structuredContent` and, JSON-encoded, in `content[0].text`. The client signs a payment and repeats the
call with it in `params._meta["x402/payment"]`. The server verifies it, runs the tool, settles, and returns
the result with `_meta["x402/payment-response"]` (transaction, network, payer). If settlement fails after
the tool ran, the server returns only the payment error, never the tool's content.

## Proposal: MCP as a thin adapter over SingIt Ask's x402 resources

SingIt Ask already sells, over x402 with the CDP facilitator: chat on 14 models (`/v1/chat/{model}`) and
on any model (`/v1/chat/completions`), images, speech, and Exa search, contents and answers. Each route
answers `GET`/`HEAD` with its 402 challenge (PR #59). Settlement, pricing, `upto`/`exact`, refusals and
receipts are already built and tested there.

So the MCP server does not settle anything itself:

1. **Keyless `tools/call` of an x402-capable tool, no payment** → the MCP server reads the matching Ask
   route's 402 challenge (cached for a short time) and returns it as the tool's `PaymentRequired`
   result. Nothing is billed.
2. **Same call with `_meta["x402/payment"]`** → the MCP server forwards the call to that Ask route with
   the payment as its `PAYMENT-SIGNATURE` header. Ask verifies, answers, and settles, as it does for
   any HTTP agent. The MCP server turns Ask's answer and `PAYMENT-RESPONSE` header into the tool result
   and `_meta["x402/payment-response"]`.
3. **A call with a SingIt API key** keeps working exactly as in v1 (billed to the key).

Phase 1 tools (all already sold by Ask):

| MCP tool | Ask route | Note |
| --- | --- | --- |
| `web_search` | `/v1/exa/search` | exists in v1 with a key |
| `read_page` | `/v1/exa/contents` | exists in v1 with a key |
| `answer` (new) | `/v1/exa/answer` | a cited answer to a question |
| `ask_model` (new) | `/v1/chat/{model}` | one chat answer from a named model; `exact` first |

Bazaar: each phase-1 tool declares `extensions.bazaar` with an input example that returns the 402,
then one real paid call per tool through the CDP facilitator gets it indexed (about $0.01 in total).

### Why an adapter and not a second seller

- One place that takes money (Ask), one set of prices, one settlement path to audit and monitor.
- Ask's tests already cover refusals, ceilings, `upto` settlement of actual usage, and failed answers
  that are never settled.
- The gateway stays free of facilitator credentials.

## What to verify in a short spike before building

1. **Resource binding.** Ask's middleware may check the payload's `resource.url` against its own route.
   The adapter must then hand out Ask's real `https://ask.singitai.app/...` URL as the resource, not
   `mcp://tool/...`. Check with one testnet payment.
2. **`upto` in MCP clients.** The MCP spec's examples use `exact`. Offer `exact` on Base first, as Ask's
   per-model routes already do. Add `upto` only once a real MCP client can sign it.
3. **Timeouts.** Chat answers can take tens of seconds; check what Claude Code, Cursor and the x402 MCP
   client wrappers allow per tool call.
4. **Which clients can pay at all.** List the MCP clients and wrappers that implement `x402/payment`
   today, and which of them Bazaar and AgentCore actually use.

## Out of scope for v2 (needs its own decision)

- **Paid data tools** (weather, X profile, crypto news, Hyperliquid, funding rates, ENS, token price).
  Ask does not sell them over x402 today; they would need new seller routes first.
- **Spending from an allowance through MCP** (`get_limits`, `buy`, Bitrefill). That moves the user's
  money on an agent's request. It should wait for the acceptance criteria in
  [payment-authority design](security-audit/2026-10-24-payment-authority-design.md), above all final-payment
  enforcement on chain.
- **OAuth sign-in** for the Claude and ChatGPT connector directories.

## Decisions needed (owner)

1. Phase-1 scope: `web_search`, `read_page`, `answer`, `ask_model`. Add or remove any?
2. When a call carries both a key and an x402 payment: the key wins, or the call is refused?
3. OK for one paid test call per tool (about $0.01 in total) to get indexed in Bazaar?
4. Paid data tools over x402 (new Ask routes): now, later, or never?
5. Allowance spending through MCP: confirm it stays out of v2.

## Rough size

The spike is about half a day. Phase 1 is 2–3 days with tests: the adapter in `web_mcp.py`,
Bazaar metadata, docs, and one paid test per tool after review.
