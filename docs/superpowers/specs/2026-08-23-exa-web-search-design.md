# Exa Web Search Design

**Goal:** Give the chat access to the present. Today it answers from model memory
and either refuses or invents when asked about anything current — the most
visible defect in the feature we just shipped.

**Why Exa:** $0.007 per call, paid per request with no prepaid balance, unlike
Venice's flat $5 top-up. In our own ecosystem survey it had the cleanest profile
measured: 100% uptime over 30 days, 14/14 protocol compliance, and a
top-buyer share of 0.162 — the most evenly distributed demand of any service on
Base. Backed by Lightspeed and Nvidia's fund; used by Cursor.

**Scope:** search inside the existing chat. Not a menu item, not a user-facing
product, not a standalone command.

---

## Task 0 — confirm the endpoint before writing code

Our liveness sweep recorded `https://api.exa.ai/search` as **404, not 402**. That
is a false negative from our own method: the sweep probes with GET only, and
Exa's search route is POST. Confirm with a real POST:

```bash
curl -i -X POST https://api.exa.ai/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"x402 protocol","numResults":3}'
```

Expected: `402` with payment terms in the body or the `payment-required` header.

Record `payTo`, `asset`, `network`, `amount`, `scheme`. The catalog says
`0x6d6E695b09861467c7d462f5AAF31cF3540B9192` at $0.007 on Base — **verify
rather than trust it.** 59 live services in our survey pay to an address the
catalogs no longer have.

If it does not answer 402, stop. Exa may sell x402 access on a different route
than the one catalogued.

### Result — confirmed 2026-08-23

`POST https://api.exa.ai/search` answers **HTTP 402**, `x402Version: 2`, with the
terms in the JSON body and base64 in the `payment-required` header. The 404 in
our sweep was the GET-only probe, as suspected.

The Base leg, verified against the live challenge rather than the catalog:

| field | value |
|---|---|
| `scheme` | `exact` |
| `network` | `eip155:8453` (Base) |
| `amount` | `7000` atomic = $0.007 |
| `payTo` | `0x6d6E695b09861467c7d462f5AAF31cF3540B9192` |
| `asset` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` (Base USDC) |
| `extra` | `{"name":"USD Coin","version":"2"}`, `acceptId: legacy` |
| `maxTimeoutSeconds` | 60 |

**The catalog address matches the live challenge.** Bind to it; it is not one of
the 59 drifted services.

Also in the response, and out of scope for this feature:

- a second `accepts` entry for Solana USDC at the same price — ignore it, we
  settle on Base;
- a `www-authenticate: Payment ... method="tempo"` header advertising a
  different rail (chainId 4217, a different recipient). We answer the x402
  `accepts` array, not this one;
- an `agentkit` extension offering a **free trial of 100 uses** in exchange for
  an EIP-191 signature on World Chain attesting the agent is human-backed.
  Tempting, but it is a second signing path on a third chain for a 70-cent
  saving. Not now.

Body contract, from the `bazaar` extension: `{"query": str, "numResults": <=10
for x402, "type": "auto"|"keyword"|"neural"|..., "contents": {...}}`; the
response is `{"results": [{"url", "title", "score", ...}]}`.

Settlement fit: `_settle_chat_prefund` in `sign402-gateway/sign402_gateway/server.py`
already reads both v1 and v2 field names and already passes a `request_body`
through the buyer's own 402→pay→retry loop. The search client needs that same
call with the real query body and without any of the prefund path around it.

---

## Global Constraints

- Search is invisible to the user as a product. Nobody selects it; the bot
  decides. The words Exa, x402 and facilitator never appear in the interface.
- Bind payment to the `payTo` confirmed in Task 0. A challenge with any other
  recipient fails closed and does not pay.
- A search costs roughly ten times a chat message. It must be metered
  separately and shown, or a user with a $5 daily cap will burn it on news and
  not understand where the money went.
- A failed search must never fail the message. Answer from the model and say
  the web was unavailable.
- Never log or persist query text or retrieved page content beyond the turn.
- `SIGN402_PURCHASES_PAUSED=1` stops paid search; the chat keeps working
  without it.
- Ships behind `SIGN402_AI_SEARCH_ENABLED`, default off.

---

## When to search

The decision has to be cheaper than the thing it decides. **Do not ask a model
whether to search** — an LLM call to make that judgement costs more than the
$0.007 search.

Use a deterministic pre-filter, in this order:

1. **Never search** if the message is short and conversational, or is a
   follow-up to the previous turn with no new subject.
2. **Always search** when the message contains a present-tense marker: today,
   now, current, latest, right now, this week, price of, who is, what happened,
   a year at or after the model's cutoff, or a bare ticker or domain.
3. **Otherwise ask the model once**, in the same completion that answers, to
   emit a `NEED_WEB: <query>` token when it lacks current facts. If it does,
   run the search and re-ask with results. One extra completion, not a
   dedicated classifier call.

Rule 3 is the interesting one: the model decides while answering, so the
judgement is free.

---

## Flow

```
message
  → pre-filter says search, or the model emits NEED_WEB
  → policy check: search enabled, per-search cap, daily budget left
  → 402 → verify payTo → pay $0.007 → results
  → results injected into the prompt → answer
  → footer: "searched the web · $0.007 · $4.83 left today"
```

The search runs before the answer, not after. Results are injected as context
with their source URLs so the model can cite them.

---

## Payment

Same lane as the existing x402 purchases: exact scheme, Base USDC, from the
user's managed wallet. Simpler than Venice — no prefund, no outstanding credit,
no local ledger. One call, one payment.

Reuse whatever the Venice client uses to settle, with the prefund logic removed.

**Who pays.** The user, from their own managed wallet, from the first search.

Exa charges for every call, so a "free" search only means *paid by someone
else* — the gateway. That machinery stays (two settle callables, one client,
chosen per call) but `SIGN402_AI_SEARCH_FREE_CALLS` defaults to 0, so the
gateway account is not touched unless an operator deliberately funds a trial.

The cost of switching it off is the case the trial covered: a new user whose
wallet holds no USDC gets "the web was unavailable" instead of a current
answer, because the payment simply fails.

---

## Budget

Search shares the user's daily chat cap but is counted separately so it can be
shown and limited:

```env
SIGN402_AI_SEARCH_ENABLED=0
SIGN402_AI_SEARCH_MERCHANT_PAYTO=0x6d6E695b09861467c7d462f5AAF31cF3540B9192
SIGN402_AI_SEARCH_FREE_CALLS=0                  # per user, lifetime; 0 = no trial
SIGN402_AI_SEARCH_MAX_PER_CALL_ATOMIC=20000     # $0.02 ceiling
SIGN402_AI_SEARCH_MAX_PER_DAY=20                # searches, not dollars
SIGN402_AI_SEARCH_RESULTS=3
SIGN402_AI_SEARCH_URL=https://api.exa.ai/search
```

A per-day *count* rather than an amount, because the failure mode users care
about is "it kept googling", not "it spent eleven cents".

At the cap: answer from the model and say the web budget for today is spent.
Do not silently degrade.

---

## Failure states

| Situation | Charged | User sees |
|---|---|---|
| Pre-filter says no search needed | no | a normal answer |
| Daily search count reached | no | answer, plus one note that web is off until tomorrow |
| `payTo` mismatch | no | answer without web; **search** paused, chat unaffected; operator alerted |
| Exa returns 5xx or times out | no | answer without web |
| Paid, but no results | yes | answer without web, cost still shown |

The last row is deliberate: a paid search that returns nothing was still a
delivered service. Hiding the charge would be dishonest; refunding it would
invite abuse.

---

## Verification

Tests inject a stub search client and a stub settlement callable. They never
call Exa and never move funds.

- pre-filter classifies a fixed corpus of messages correctly in both directions;
- `NEED_WEB` from the model triggers exactly one search, never two;
- a challenge with an unexpected `payTo` pays nothing and pauses;
- reaching the daily count stops searching but not answering;
- a provider timeout produces an answer with no charge;
- query text and page content appear in no log;
- with `SIGN402_AI_SEARCH_ENABLED` unset, behaviour is byte-identical to today.

Manual: ask something the model cannot know — a price, a headline from this
week — and confirm the answer is current, the footer shows the cost, and the
daily counter moves.

---

## What shipped

- `sign402_gateway/web_search.py` — the pre-filter, the paid client, the
  store-backed counters, the one-turn orchestrator, and the env builder.
- `chat_store.py` — `searches_this_window`, `searches_total`, `search_paused`,
  migrated onto live databases; the daily count rolls over with the chat spend.
- `venice_chat.py` — `VeniceChatClient(web_search=...)`. Unset means the old
  code path, asserted by a test that the prompt reaches Venice untouched.
- `server.py` — two settle callables (gateway account for the free trial,
  user wallet after it) and `webFooter` on the chat response.
- `cdp-x402-service` — the gateway's `buy` command learned `--method`,
  `--body-json` and the approved-terms flags, and enforces them when given.

Search and chat pause independently: they pay different merchants, so a
binding that broke for one says nothing about the other.

**Verified live on 2026-08-24.** One free-trial search settled from the gateway
account: 402 → `payTo` matched the binding → paid → three current results, the
newest from July 2026. The account balance moved 7012105 → 7005105 atomic,
exactly $0.007. First attempt failed on a 67-second clock skew on the dev Mac
(`Failed to create payment payload: JWT token has expired`) — CDP mints
short-lived JWTs from the local clock, and Exa's challenge allows only
`maxTimeoutSeconds: 60`. Nothing was charged for that failure, and the client
returned `SearchUnavailable`, which is the path that keeps the chat answering.

## Open questions

1. Show source links under the answer? Honest and useful, but it turns a chat
   reply into a search results page. Perhaps only on request.
2. Cache identical queries across users for a few minutes? Cuts cost
   noticeably at scale and leaks nothing, since queries are not user-specific
   once answered — but it does mean one user's spend subsidises another's.
3. ~~Should search be free for the first N per user?~~ **Decided: no.** The
   trial was specified as five free searches and built, then dropped before
   launch: the gateway's wallet is not the place to absorb other people's
   spending, and at scale the 3.5 cents per user is a bill nobody agreed to.
   Users pay from their own wallet from the first search. The trial remains
   available as `SIGN402_AI_SEARCH_FREE_CALLS`, off by default; when it is on,
   free searches still count against the daily limit and still show `$0.000`
   in the footer, so the meter never lies about what happened.
