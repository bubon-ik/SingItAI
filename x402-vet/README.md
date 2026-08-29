# x402-vet

Two USDC-paid endpoints that answer the question an agent faces before it pays a
stranger: **is this endpoint worth paying?**

These are Bankr x402 Cloud services.

```text
GET|POST https://x402.bankr.bot/<your-wallet>/vet-shortlist   $0.02  USDC
GET|POST https://x402.bankr.bot/<your-wallet>/vet-service     $0.005 USDC
```

`vet-service` resolves a URL against the traction directory first and the
facilitator catalogs second, so it answers for endpoints no rating exists for.

Both verbs are accepted so the endpoint can be tried before any code is written.
Successful responses and errors are readable Markdown rather than raw JSON:

```bash
bankr x402 call https://x402.bankr.bot/<your-wallet>/vet-service?slug=venice-ai
```

```text
Venice AI
Verdict: Recommended
Price per call: $0.007
Live payment check: alive
Payment recipient: unchanged from the catalog
```

Paid in USDC on Base rather than SINGIT, deliberately: a caller who must first
acquire a custom token and sign a one-time Permit2 approval is a caller who
never arrives. USDC is what every x402 agent already holds, and the Bankr agent
caps any single automatic payment at $10 — well above both prices here.

## Two sources, because one is 1.4% of the ecosystem

| source | knows | endpoints |
|---|---|---|
| x402-list.com | what services **earn** — buyers, volume, uptime, compliance | 575 |
| facilitator Bazaars (PayAI, Coinbase, thirdweb) | the **resource URLs** themselves, with prices and receivers | ~42,000 across ~2,000 hosts |

Neither is a census — a seller registers nowhere, answering 402 on its own
domain is the whole requirement — so this is a union and says so in every
response. The two halves are complementary: the traction directory can rank but
barely covers; the facilitator catalogs cover but measure nothing.

An agent shown only the 575 is being told the ecosystem is 1.4% of its real
size, and `vet-service` would answer "not found" to roughly forty thousand real
endpoints.

## What is actually being sold

The directory facts are free and public. What is sold is the filter, and the
freshness:

- **volume net of the single largest buyer**, instead of gross. One buyer is one
  relationship, not a market: a service booking $160k with a 99% top-buyer share
  carries about $1.6k of distributed demand, and ranking it first would be a lie
  of omission.
- **revenue per buyer**, which separates real use from a swept list.
- **clusters of services reporting identical buyer/volume pairs** — rows that
  carry no independent information about demand.
- **a live unpaid 402 handshake at request time**, because catalogs never evict.
  A catalog entry means "this took a payment once", not "this works".

## Verdicts

| verdict | meaning |
|---|---|
| `ok` | live, payment-ready, uptime above the requested floor, demand spread across more than one buyer, no identical-pair cluster |
| `thin` | live and answering, but the demand behind it is one relationship or too small to read. Not an accusation — good new services look exactly like this for a month |
| `check` | something specific is off: not answering, live receiver differs from the recorded one, or its buyer/volume pair matches unrelated services to the cent |
| `unrated` | listed by a facilitator, but no traction data exists for it anywhere. Unmeasured, which is not the same as bad — and it can never be confused with `ok` |

**No verdict ever implies fraud.** `why` carries observations, not motives:
"one buyer accounts for 99% of volume", never "farmed". That distinction is what
keeps this defensible, and it is enforced by a test.

## The live probe, and what it refuses to conclude

A probe can only ever downgrade a verdict, never upgrade one — and only a clear
wrong answer to a *correctly aimed* request counts against a service.

Aiming it correctly turned out to be most of the work:

- the list route publishes only `base_url`, a host root that answers 200 or 404
  to an unpaid GET. The real paid path lives on the detail route.
- `new URL("/chat/completions", "https://api.venice.ai/api/v1")` silently drops
  the `/api/v1`. Paths are concatenated, not resolved.
- sellers declare their method; a POST-only route answers 405 to a GET.
- a path like `/v2/actors/:actorId/run` cannot be aimed at all.

Each of those, unhandled, marks a healthy service as broken. So `400`, `404`,
`405`, a templated path and a missing path all report `liveProbe: "unknown"` and
leave the verdict alone — a seller that validates its request body before
reaching its payment gate is not a broken seller, and we cannot tell the two
apart.

Probing is opt-in on the shortlist (`probe: true`) and always on for a single
lookup. The cap is 50 rows, set from measurement rather than caution: probing the
entire default result set — 27 services, 54 outbound requests — takes 1.7s in
parallel, and a full shortlist call with every row probed live returns in 3.7s.

## What one call actually covers

| | per `vet-shortlist` call |
|---|---|
| analysed | all 575 rated services, plus the ~42,000-endpoint index when `includeUnrated` is set |
| returned | up to `limit`, capped at 100 |
| probed live | up to 50 — more than the 27 the default filters yield |

## Requests

### vet-shortlist

```json
{ "category": "AI", "maxPriceUsd": 0.05, "minBuyers": 5, "limit": 20, "probe": true }
```

Every field optional. Returns surviving services ranked by net volume, with
`checked`, `returned`, `probed` and the filter that was applied.

### vet-service

```json
{ "slug": "venice-ai" }
```

Or `{ "url": "https://api.venice.ai/api/v1/chat/completions" }` — when the
caller passes a URL, that URL is probed, because they know the paid path and the
directory may not.

## Honest limits, stated in every response

- Most of the raw data is free and public. What is sold is the filter and the
  freshness, not the facts.
- A verdict is a measurement, not a guarantee. `ok` does not mean the service
  will deliver — the directory has confirmed delivery for one service out of 527.
- Absence of traction is not evidence of a bad service.

## Attribution

Directory facts come from x402-list.com under CC BY 4.0. The attribution ships
in every response, including error responses. That is the licence, not politeness.

## Failure states

| Situation | Charged | Response |
|---|---|---|
| Filter matches nothing | yes | 200, empty list, `checked` populated |
| Directory unavailable and snapshot older than 6h | no | 503 |
| Unknown slug or URL | no | 404 |
| Live probe times out | yes | record returned with `liveProbe: "timeout"` |

An empty result is a real answer and stays charged. A stale snapshot is our
failure and is not.

This is not our own convention — it is the platform's: *"Payments are only
collected if your endpoint returns successfully"*, and a handler that exceeds the
30-second timeout returns 503 with nothing settled. Returning the right status
code is therefore the whole of the billing logic; there is no payment code in
these handlers at all.

## Building the index across requests

The facilitator index cannot be built inside one request: PayAI alone is 27
pages of 1.5 MB against a 30-second limit. So each cold request spends a bounded
budget extending the index from where the last one stopped, and persists the
progress.

Measured against the live catalogs: 70% indexed after the first request, 97%
after the second, complete on the third — about 25 seconds of work spread over
three calls, then 9.5 MB cached for 24 hours.

**Every response says how complete the index is.** An agent told "not found" by
a 12%-built index has been misled; one told "not found, 12% indexed" has not.

Two things that had to be handled, both found by running it:

- **Facilitators clamp page size silently.** thirdweb returns 200 rows when
  asked for 1,000. Treating a short page as exhaustion stopped at a quarter of
  its catalog, so a source ends only on an empty page or a declared total.
- **A dead facilitator must not block coverage forever.** dexter answers HTTP
  502. After three failures a source is retired and reported as `unavailable`,
  rather than being retried on every request while `complete` stays false
  permanently.

## Runtime, and what it forced

x402 Cloud runs each request in an isolated serverless invocation: 256 MB,
30-second timeout, outbound `fetch` allowed, and — the constraint that shaped
this code — **no module-level state between requests**.

So the hourly directory snapshot is cached in a file through `ctx.files`, not in
a variable. A variable would be cold on every single request and re-pull all six
pages of the catalog each time. Measured here: 3.4s cold, 2ms warm, 234 KB of
cache.

The handler takes `ctx` as its second argument and degrades cleanly without it —
a store that is missing, unreadable or read-only costs speed, not correctness,
and there is a test for each of those.

`paymentScheme` is set to `exact`: the price is fixed and known before the
handler runs. `upto` advertises a ceiling and settles the real cost, which is
for metered work these endpoints do not do.

## Local test

```bash
npm test
```

The test suite runs without network access: the directory, the detail route, the
facilitator catalogs, the persistent store and every probe are stubbed.

## Deploy

From this directory:

```bash
bankr login
```

```bash
bankr x402 deploy
```

Deploys both services; `bankr x402 deploy vet-shortlist` does one. Bankr reads
`bankr.x402.json`, bundles `x402/<service>/index.ts`, and wraps the handler with
x402 payment enforcement.

Each handler is self-contained — the shared analysis is duplicated between the
two rather than imported, so neither depends on the bundler resolving local
modules.

Afterwards:

```bash
bankr x402 logs vet-shortlist
```

**One thing to watch on the first deploy.** The docs show the file-access
capability declared two ways — `"files": { enabled, roots, read, write }` on the
service (the counter example) and `"fileAccess": { ... }` (the scheduler
example). This manifest uses `"files"`. If deploy rejects it, rename the key;
nothing else changes, and the endpoint still answers correctly without the store,
just at 3.4s per request instead of 2ms.

## Fees

First 1,000 settled requests each month are free (0%). Above that, Pro is 5% and
Enterprise 3%. At $0.02 a call, the fee is a fifth of a cent.

## Making the shortlist free

One value: set `price` to `"0"` for `vet-shortlist` in `bankr.x402.json`. Worth
considering — a permanently fresh free shortlist would make this the default
entry point to x402, which is plausibly worth more than the pennies it earns.
