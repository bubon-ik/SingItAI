# We measured every x402 service. 3.7% of them have real customers.

*Data pulled 11 August 2026 from x402-list.com. Script at the bottom — run it and check us.*

---

## The headline

| | |
|---|---|
| Services listed in the directory | **508** |
| Resources in the Coinbase facilitator catalog | **14,535** |
| Services that pass a basic quality filter | **19** (3.7%) |
| Services sharing an identical traction fingerprint | **161** (32%) |
| Real distributed volume, 30 days | **~$20,700** |
| Ecosystem settlement volume, 30 days | $980,700 across 13,891,321 settlements |
| Average settlement | **$0.07** |

x402 works. Settlement is real and the tooling is good. But almost all of the
volume belongs to one relationship, and a third of the "demand" in the directory
does not look like demand at all.

Here is the whole thing, with the numbers.

---

## 1. The catalog is not 14,535 things

The Coinbase facilitator's discovery endpoint returns **14,535 resources**. We
sampled it at three offsets and found the same pattern each time:

| Provider | Listings | What they are |
|---|---|---|
| `api.onesource.io` | 14+ | one entry per Ethereum RPC method — `eth_call`, `eth_blockNumber`, `eth_estimateGas` |
| `blockrun.ai` | many | one entry per stock ticker — MSTR, COIN, each its own listing |
| `k2so.wrong.systems` | hundreds | LLM-written prose about agent payment procedures, $0.002 each, self-called twice |
| `x402.orthogonal.com` | 20+ | B2B data resale, all registered within minutes of each other on 9 Aug |

**508 distinct services** is the honest number. That is still fast growth —
**+422 in the last 30 days** — but it is not fourteen thousand.

---

## 2. Almost all the volume is one relationship

Measured across listed services over 30 days:

| | |
|---|---|
| Distinct on-chain buyers | 5,830 |
| Settlement volume | $182,654 |
| Settlements | 7,314,073 |
| Share taken by the top 10 services | **97.5%** |
| Share taken by the single largest service | **85.5%** |
| Share of *that* taken by one buyer | **98.6%** |

The largest service is **BlockRun**: $160,538 in 30 days, 1,013 buyers, and a
top-buyer share of **0.9866**. Its status in the directory is `degraded`.

That is not a market. That is two parties with an API contract that happens to
settle over HTTP 402.

---

## 3. Unique buyers are being farmed

This is the part nobody talks about. **161 of 508 services share an identical
traction fingerprint with at least two others.**

| Buyers (30d) | Volume (30d) | Services reporting exactly this | Per buyer |
|---|---|---|---|
| 442 | $6.00 | **8** | $0.014 |
| 434 | $5.00 | 2 | $0.012 |
| 251 | $6.00 | 2 | $0.024 |

Eight unrelated providers, different categories, different products, each
reporting the same 442 buyers and the same six dollars.

Identical buyer counts across unrelated providers is what one wallet swarm
sweeping a list looks like. We are not naming intent and we are not publishing
a blacklist. The narrow, checkable point: **unique buyer count is trivially
farmable, and something is farming it.** Rank on it and you rank noise.

---

## 4. The filter

Five conditions, all readable straight from the directory API:

```
online + verified
uptime 30d              >= 90%
volume net of largest buyer >= $50
volume per buyer        >= $0.10
top-buyer share         <= 90%
identical fingerprints  dropped
```

**508 → 19.**

---

## 5. The 19

Sorted by volume net of the single largest buyer, because one buyer is one
relationship, not a market.

| # | Service | Category | Net 30d | Gross 30d | Buyers | $/buyer |
|---|---|---|---|---|---|---|
| 1 | Clash of Coins Checkout | Other | **$9,698** | $9,903 | 185 | $53.53 |
| 2 | AnySpend | Content | **$7,146** | $7,200 | 260 | $27.69 |
| 3 | StableEnrich | Data | $1,324 | $1,462 | 551 | $2.65 |
| 4 | Bitrefill | Finance | $538 | $786 | 98 | $8.02 |
| 5 | StableSocial | Data | $337 | $384 | 133 | $2.89 |
| 6 | Venice AI | AI | $244 | $441 | 17 | $25.94 |
| 7 | Apify Agentic Endpoint | Data | $209 | $292 | 38 | $7.67 |
| 8 | Apify Actors API | Data | $206 | $288 | 37 | $7.77 |
| 9 | Loyal Spark | Blockchain | $165 | $167 | 84 | $1.99 |
| 10 | Deepline GTM API | Data | $151 | $169 | 103 | $1.64 |
| 11 | agentutility | AI | $121 | $679 | 244 | $2.78 |
| 12 | Deepnets API | Blockchain | $106 | $272 | 16 | $16.98 |
| 13 | Nansen | Data | $97 | $110 | 181 | $0.61 |
| 14 | OnchainPulse | Data | $78 | $311 | 159 | $1.95 |
| 15 | Linked Panda | Data | $65 | $77 | 50 | $1.53 |
| 16 | Tantra Authority | Content | $59 | $265 | 2 | $132.34 |
| 17 | Agent402.tools | AI | $55 | $132 | 159 | $0.83 |
| 18 | cn402 | Data | $53 | $91 | 241 | $0.38 |
| 19 | OneSource | Blockchain | $52 | $156 | 1,453 | $0.11 |
| | **Total** | | **$20,704** | | | |

Note row 19. OneSource has **1,453 buyers** — more than any other service in the
directory — and eleven cents of revenue per buyer. It barely clears the filter.

**Exa** does not rank high on volume ($38 / 150 buyers) but has the cleanest
profile we measured: 100% uptime, compliance grade A, $0.007 per call, and a
top-buyer share of **0.162** — the most evenly distributed demand in the set.

---

## 6. The catalog looks like data. The money is in commerce.

| Category | Services | Avg uptime 24h |
|---|---|---|
| Data | 221 | 87.8% |
| AI | 79 | 89.6% |
| Finance | 71 | 82.4% |
| Verification | 51 | 74.5% |
| Blockchain | 46 | 67.0% |
| Other | 16 | 87.5% |
| Content | 15 | 89.3% |
| Compute | 9 | 77.8% |

By count, x402 is a data marketplace: Data + AI + Finance is **73%** of it.
Compute is nine services.

By revenue, the top two earners are both **checkout**. People buying things,
not agents buying rows. The agents-buying-data story is the one x402 is sold
with, and right now it is the smaller half of what x402 is used for.

---

## 7. Reliability

| | |
|---|---|
| Answering now | 428 of 508 |
| Offline | 80 (8 of them for 30+ days) |
| Avg uptime, latest daily | 84% |
| Avg uptime, 30d | 92.7% |

**One in six listed services is not answering right now.** Anyone wiring the
catalog into a product needs an uptime filter before anything else.

---

## What this means

Supply is solved. 508 services, +422 in a month, 22 mainnet networks, 33
facilitators, settlement that works and costs seven cents a call.

Demand is not. Strip the one dominant relationship and the entire distributed
x402 economy is twenty thousand dollars a month.

That is not a protocol problem — paying is solved. What is missing is the step
before paying: a person deciding how much an agent may spend, on what, for how
long, and being able to revoke it. Until that exists at consumer scale, 500
services are waiting for customers with no safe way to arrive.

---

## Disclosure

We are not neutral. We build **SingIt**, a wallet that lets an agent pay inside
a spending limit its owner signed, with approval on a hardware device or a
messenger. Row 4 of that table is a supplier we pay. We measured the market we
are standing in — read it accordingly.

## The script

Everything above came out of one file: it pages the directory, applies the
filters, flags the fingerprint clusters and writes a CSV you can sort yourself.

**[link]**

Move the thresholds. If your numbers disagree with ours, publish them. This
ecosystem needs fewer claims and more measurements.

*Data: x402-list.com (CC BY 4.0)*
