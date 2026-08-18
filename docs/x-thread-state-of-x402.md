# X thread — "We measured x402"

Draft for @SingItAgent. Data as of 2026-08-11.
Post the thread first; run the product video a few days later on the warmed audience.

Tag on posts 7 and 12: @x402 @base @VeniceAI @x402list (verify handles before posting).

---

**1/**

Everyone quotes x402 by catalog size.

We pulled all 508 listed services and checked who actually gets paid.

19 survive a basic quality filter.

The whole real, distributed x402 economy is about $20,700 a month.

Here is the data. 🧵

---

**2/**

Method, so you can argue with it:

— source: x402-list.com, an independently monitored directory
— every service: uptime, compliance grade, risk level, 30d on-chain volume, unique buyers, top-buyer share
— script is open, run it yourself

No estimates. All of it is measured on-chain.

---

**3/**

First, catalog size is not a metric.

Coinbase's facilitator discovery endpoint returns 14,535 resources.

Sample it and you find providers registering one listing per URL parameter. One provider had a separate entry for every single ticker symbol.

508 distinct services is the honest number.

---

**4/**

Second, the volume is one relationship.

Across all measured facilitators: $980.7k in 30 days.

But one service, BlockRun, books $160,538 of it — and 98.7% of that comes from a single buyer.

That is not a market. That is two parties with an API contract.

---

**5/**

Third, and this is the one nobody mentions.

We found 161 of 508 services sharing an identical traction fingerprint.

Eight unrelated services. Each reporting exactly 442 unique buyers. Each reporting exactly $6.00 in 30 days.

That is 1.4 cents per buyer.

---

**6/**

Identical buyer counts across unrelated providers is what one wallet swarm sweeping a list looks like.

We are not naming intent. We are saying the number "unique buyers" is trivially farmable, and it is being farmed.

If you rank on buyer count, you are ranking noise.

---

**7/**

So we filtered on things that are harder to fake:

— online + verified
— 30d uptime ≥ 90%
— volume excluding the single largest buyer
— dollars per buyer ≥ $0.10
— drop identical-fingerprint clusters

508 → 19. That is 3.7%.

---

**8/**

The 19 that survive, by volume net of their largest buyer:

Clash of Coins Checkout — $9,698
AnySpend — $7,146
StableEnrich — $1,324
Bitrefill — $538
StableSocial — $337
Venice AI — $244
Apify — $209 + $206
Nansen — $97
Exa — small, but the cleanest profile in the set

---

**9/**

What surprised us: the top two are checkout, not data.

The catalog is 221 Data, 79 AI, 71 Finance, 9 Compute — it looks like a data marketplace.

But the actual dollars are in commerce. People buying things, not agents buying rows.

---

**10/**

The honest read:

x402 works. Settlement is real, the tooling is good, growth is fast (+422 services in 30 days).

Demand is the missing half. 500 sellers, almost no buyers. Everyone built a shop, nobody built a wallet with permission to spend.

---

**11/**

Disclosure, because we are not neutral:

We run SingIt, a wallet that lets agents pay inside a signed spending limit. We are on that list of 19 through the services we use.

We measured the market we are in. Read it with that in mind.

---

**12/**

Script that produced all of this — run it, change the thresholds, disagree with us:

[link to gist / repo]

Data: x402-list.com (CC BY 4.0)

If you are building on x402 and your numbers differ, post them. This ecosystem needs fewer claims and more measurements.

---

## Notes before posting

- Verify every handle. Wrong tags kill reach.
- Post 8: link each name to its x402-list page so the claim is checkable in one tap.
- Post 12: publish the script as a public gist or repo first, or the whole thread loses its credibility.
- Do not post this and the product video the same day. Research earns the audience, the product converts it.
- Expect pushback from the providers named in post 5 if you ever name them. Current draft does not name them — keep it that way.
- If asked "which 8 services", answer with the method (identical fingerprint) and let people run the script. Do not hand out a blacklist.
