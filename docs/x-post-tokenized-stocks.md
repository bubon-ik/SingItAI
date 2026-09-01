# Posts — tokenized equities on Base

Drafts for @SingItAgent. Nothing here is posted. Two of the three can go out
today; the third cannot, and the reason is in the publication notes.

---

## A · The quote tweet (on the Bankr double-incentives post)

> "Run the whole loop using the Bankr agent" — worth being precise about which
> agent.
>
> Coinbase for Agents can buy S&P 500 equities. Its agent toolset has no
> external withdrawal: `transfer` moves funds between portfolios.
>
> A position you cannot withdraw is a position you cannot LP.

*Why it works: it adds the one thing the original post does not say, it is
checkable in Coinbase's own docs, and it sells nothing. A quote tweet that
pitches gets ignored; a quote tweet that corrects gets quoted.*

---

## B · The finding (standalone, post a day or two later)

> I was sure tokenized equities were gated at the contract. Dinari's dShares
> are: the wallet has to clear a whitelist or the transfer reverts.
>
> Coinbase's B20 tokens are not. No transfer allowlist. KYC gates minting and
> redeeming against the real shares, not holding them.
>
> Read off Base rather than off the docs:
>
> NVDAc `0xb20000000000000000000078ee7ce2fE4908108C` — "NVIDIA Corporation",
> 8 decimals, not 18
> USDC is `token0` in all four Aerodrome pools, tickSpacing 10, fee 0.05%
> The USDC domain `{"USD Coin", "2", 8453}` reproduces the on-chain
> DOMAIN_SEPARATOR, so a payer never has to guess it
>
> Which means a wallet that has never touched Coinbase can hold a share of
> NVIDIA. And it means an x402 endpoint can deliver one.

*Why it works: it opens by being wrong about something, which is the format
that travels, and every number in it is verifiable in one RPC call. The last
line sets up post C without promising it.*

---

## C · The product (CANNOT be posted yet — see notes)

> An x402 endpoint that sells shares.
>
> Pay USDC, the shares arrive at the address that signed the payment.
> No account. No API key. No custody.
>
> The swap's recipient is the payer, recovered from the signature rather than
> read out of the request body — so the stock never touches our server. There
> is nothing here to seize.
>
> Two endings, no third: the shares arrive, or the full amount comes back,
> our fee included.
>
> NVDA, AAPL, GOOGL, META. $X per order, 1%.

*Why it works, once true: "the stock never touches our server" is the claim
nobody else can make, and it is a mechanism rather than an adjective.*

---

## The thread version of B, if the standalone lands

1. The wrong assumption (Dinari gates in-contract, so surely these do too).
2. What B20 actually does — allowlist vs blocklist, mint/redeem vs holding.
3. The four constants, read off chain.
4. The decimals trap: 8, not 18. Anyone porting Uniswap code gets this wrong.
5. The Slipstream trap: `exactInputSingle` carries tickSpacing where Uniswap
   carries fee. The wrong tuple still decodes — into a different trade.
6. What it unlocks: a wallet with USDC and nothing else is a buyer.

---

## Publication notes

**Post C is not publishable today.** The service is built and tested, but no
key is configured, nothing is deployed, and no order has ever run. Our own
longread argues that self-reported claims are the disease; shipping a post that
says "pay this endpoint" before one payment has settled would be the same
mistake with our name on it. Post C goes out after a real order settles, with
its two transaction hashes in the post.

**Two figures to re-check before posting, not to take from here.**

- Any claim of the form "no x402 endpoint sells a tokenized equity" needs a
  fresh count from `scripts/x402-collect.py`, stated with its snapshot date.
  The catalogs move daily, and the previous snapshot's totals are not this
  week's.
- Do not reuse the 40,585 union figure from the vet post next to a Base-only
  claim. That number is the deduplicated union across five catalogs; the
  Base-settling subset is a different number and mixing them is the kind of
  error we publicly criticised.

**Precision on the withdrawal claim in post A.** Coinbase's docs say the agent
toolset's `transfer` moves funds between portfolios and does not withdraw
externally. That is a statement about what an *agent* can do, not a claim that
Coinbase blocks withdrawals — a human can still withdraw in the app. Keep the
wording as drafted; "Coinbase won't let you withdraw" would be false and is
exactly the kind of overreach that costs a correction.

**Do not name a yield.** The Aerodrome APRs on Bankr's page are gross,
in-range-conditional, reset weekly, and the staked and unstaked routes are
alternatives rather than additive. Repeating a number we did not measure, in a
post of ours, makes it ours.

**Jurisdiction.** Coinbase's program is for eligible non-US users. Any post
about buying these should not read as an invitation to US readers.
