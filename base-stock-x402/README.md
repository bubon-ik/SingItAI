# Sign402 x402 stock checkout on Base

Pay USDC over x402, receive Coinbase tokenized equities at the address that
signed the payment. Self-facilitated: no dependency on a third-party
facilitator's uptime.

| Endpoint | What it does |
|---|---|
| `GET /quote/:ticker?usd=100` | Free. What the live pool would give, and the floor below which the order refunds. |
| `POST /paid/buy/:ticker?usd=100` | Pay, and the shares are swapped straight to you. |

Markets: **NVDA, AAPL, GOOGL, META** — Coinbase's B20 predeploys on Base.

## Why the shares never touch our books

`exactInputSingle` takes a recipient. So the swap sends the equity directly to
the payer, whose address is recovered from the payment signature rather than
read out of the request body. There is no second transfer that could fail, no
window where we custody somebody's stock, and nothing to seize here if a
jurisdiction ever asks.

That is also why this works at all. B20 puts no allowlist on secondary
transfers — KYC gates minting and redeeming against the underlying shares, not
holding them — so an arbitrary payer address is a valid destination.

## The rule this node inverts, and what it costs

The Robinhood Chain node runs work **before** settlement, so a handler that
throws never leaves a buyer charged for nothing. Here the work is spending our
own USDC on shares, so settlement has to come first.

The price of that inversion is a refund path, and it is the load-bearing part
of `fulfil.mjs`. Once the money is ours there are exactly two acceptable
endings:

- the shares arrive, or
- the full amount paid — our fee included — goes back to the payer.

Anything else sets `needsOperator: true` and says so in the response rather
than resolving into a generic error. Every refusal *before* settlement reports
`charged: false`, because the authorization was never consumed.

## When something is owed

Every order writes an `intent` line to `~/.base-stock-x402/orders.jsonl` —
flushed to disk — **before** the settlement is broadcast, and a line for each
step after it. A crash between taking the money and delivering the shares
therefore leaves a record instead of a silence.

The line carries the payer and the authorization nonce, which is all it takes
to ask the chain what actually happened:

```
USDC.authorizationState(payer, nonce) == true  → they paid, we owe them
                                       == false → nothing was taken
```

`GET /health` publishes `unresolvedOrders` as a **count only** — the route is
public and the entries carry payer addresses. For the details:

```bash
npm run orders
```

It prints every order whose last step is not an ending, plus anything marked
`stranded` — an order where the delivery failed *and* the refund failed, which
is the one state that means a human owes somebody money. The server also warns
loudly at startup if the file is not clean.

## The challenge is sent in both forms

x402 v2 allows the terms in the JSON body or in a base64 `payment-required`
header. Sellers pick one: Massive publishes only the header, this repository's
own Robinhood node only the body. A buyer that reads the other one sees an
endpoint with no payable leg and refuses to pay a working service.

So this node sends both, and the test asserts they agree.

The challenge also carries a `bazaar` extension with the input schema, the size
limits and an example response — the shape Massive uses, and the reason an
agent can call this correctly the first time without finding documentation.

## The chain facts, and where they came from

Every constant in `src/chain.mjs` was read off Base, and `test/chain.test.mjs`
re-reads them.

- USDC `0x8335…2913`, "USD Coin", **6 decimals**, implements EIP-3009, so a
  payer signs one message and needs no ETH.
- Its EIP-712 domain is `{"USD Coin", "2", 8453, 0x8335…}`, which reproduces
  the on-chain `DOMAIN_SEPARATOR`
  `0x02fa7265e7c5d81118673727957699e4d68f74cd74b7db77da710fe8a2c7834f`. The
  server publishes it in `extra` on every 402 so buyers never guess.
- The four equities are predeploys at `0xb2…`, **8 decimals** — not 18, not 6.
- **USDC is `token0` in every pool** and the equity is `token1`, so the raw
  pool ratio is shares-per-dollar and has to be inverted. Getting that backwards
  prices a $300 share at half a cent, which is why the test asserts the ordering
  instead of trusting it.
- Pools are Aerodrome Slipstream, tickSpacing **10**, fee **0.05%**. The router
  is `0x698c…a92f`, taken from Bankr's published `aero-stock-lp` skill.
- `exactInputSingle` is `0xa026383e` over an **eight-word static tuple carrying
  tickSpacing where Uniswap carries fee**. The selector and the layout travel
  together; Uniswap's tuple against this selector encodes a different trade that
  still decodes.

## Run

```bash
npm install
cp .env.example .env   # set BASE_FACILITATOR_KEY and BASE_PAY_TO to the same wallet
npm run serve
```

### On a CDP wallet, without a key here

This service holds a buyer's USDC for the seconds between settlement and the
swap, so the wallet doing the holding is the one worth protecting. Point it at
a CDP account and nothing on this machine ever holds a key — signing happens in
Coinbase's TEE and this process sees only a transaction hash:

```env
BASE_CDP_ACCOUNT_NAME=sign402-stocks-seller
```

with CDP credentials in the environment. The address is resolved at startup and
becomes `payTo`, so the two cannot disagree. `getOrCreateAccount` is idempotent
by name, so a fresh name is a fresh wallet and re-running is safe.

**Use a name of its own.** This repository already has a production CDP account
(`sign402-mainnet-buyer`) that pays for real Bitrefill orders; an experimental
endpoint that holds float and signs swaps has no business being that wallet.
Set `BASE_PAY_TO` alongside the name and startup fails if they disagree, which
turns a typo from "quietly spent from the wrong wallet" into an error.

The buyer can do the same — it only ever signs a message:

```bash
BUYER_CDP_ACCOUNT_NAME=sign402-stocks-buyer npm run buy -- --ticker NVDA --usd 1
```

The trade-off is honest: CDP is an external dependency inside the settlement
path. If it is unreachable mid-order the swap fails and the refund runs — but
the refund needs CDP too, so an outage in that window strands the order instead
of resolving it. That is what the journal is for.

`@coinbase/cdp-sdk` is deliberately **not** a dependency of this package; the
adapter takes an already-constructed account, so the CDP path costs nothing to
anyone running on a plain key. The adapter is unit-tested without credentials,
but it has never been run against the live CDP API — unlike the rest of this
service, treat that path as unproven until one order goes through it.

### On a local key

`BASE_PAY_TO` must be the facilitator's own address — settlement pays it and
the swap spends from it — and the server refuses to start if they differ rather
than discovering it one order at a time. That wallet needs ETH on Base: the
payer signs only a message, and we pay gas for the whole sequence.

- `GET /health` — what this node is, and whether it can actually be paid.
- `GET /.well-known/x402` — machine-readable catalog for agent discovery.

## Buy something

```bash
curl "http://localhost:8413/quote/NVDA?usd=100"
```

Then place the order. The buyer needs USDC on Base and nothing else — no ETH,
no account, no key with this server:

```bash
BUYER_KEY=0x… npm run buy -- --ticker NVDA --usd 10 --server http://localhost:8413
```

`src/client.mjs` reads the challenge from **both** forms (body and
`payment-required` header), signs one EIP-3009 authorization under the domain
the seller published, and always sends a buyer-side ceiling: an agent that does
not cap its own spend has not set a budget.

The signature path is proven end to end against live Base. Pointing an empty
wallet at the endpoint fails with `the payer does not hold enough USDC` rather
than `the signature does not match the stated payer` — which is what a domain
mismatch would say, and is the failure that looks like the payer's fault and is
not.

## Test

```bash
npm test                 # offline, against fakes — no key, no money
BASE_LIVE=1 npm test     # also re-reads every constant from Base
```

The fulfilment tests replace the whole chain surface, because a test that can
move real money eventually does. One of them asserts that the intent reaches
disk *before* the settlement call — which is the only property the journal
actually exists for.

## Honest limits

- **We hold float.** Between settlement and the swap the buyer's USDC is ours —
  seconds, and no working capital of our own is required, but a server
  compromise in that window is a real risk, exactly as `README.md` at the
  repository root says of the custodial wallet.
- **No order has ever run.** Everything here is tested against fakes and every
  constant is checked against live Base, but no real payment has settled. Until
  one has, treat the gas cost and the real fill as unmeasured.
- **Order size is capped at $250 by default** because a market order into a
  concentrated-liquidity band moves the price it fills at. Past the cap the
  buyer is mostly paying for their own impact, and the 1.5% floor starts
  rejecting fills that were never dishonest.
- **The quote is an estimate.** The fill is whatever the pool gives when the
  swap lands; the only promise is the floor, and below it the order refunds.
- **Jurisdiction is not enforced here.** Coinbase's program is for eligible
  non-US users and restricts that at its own application layer. This node does
  not check who you are, and whoever operates it owns that question.
- **The issuer can freeze.** B20 keeps freeze-and-seize powers at the token
  level. Delivery to the payer does not change that, and buyers should know it.
