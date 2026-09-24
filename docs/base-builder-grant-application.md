# Base Builder Grant — application draft

Form: https://forms.gle/xeXWvAfpKuCQFDLg7
Fields marked **[FILL]** need your data. Everything else is ready to paste.

---

## Full name *
**[FILL]**

## Email *
kostiantynzubarev@gmail.com

## X (Twitter) handle *
@SingItAgent

## Telegram username *
**[FILL]** — your personal handle, not the bot.

## Project name + one-line description *

> SingIt — a Telegram wallet that lets an AI agent spend your crypto on real
> goods, inside a limit you signed on a hardware wallet.

## Tell us about the founding team *

**[FILL]** — prior companies, exits, funding, domain expertise.

If the honest answer is "solo, no exits", say so plainly and lead with what you
shipped instead: a live payments product on Base mainnet with hardware-backed
approvals, a working x402 client, and integrations with Bitrefill and Coinbase
CDP. Grant reviewers discount claims and reward shipped systems. Do not pad.

## Link to your live product *

> https://singitai.app — https://t.me/SingIt0qk_bot

Make sure the Telegram CTA is deployed before you submit. Right now the live
site still shows "View on GitHub" as its only call to action; a reviewer who
clicks through and cannot find the product will score you as pre-launch.

## Link to a Product Demo (Loom) *

**[FILL]** — you have a recording on X. Re-record it as a screen capture of the
Telegram flow: request → approval → code delivered. Show the Trezor confirmation
if you can get it in frame; it is the most differentiated thing you have and no
other applicant in this track will have it.

## Contract address on Base *

> SINGIT (project token): 0xc2c1e0b7C401e6217193732272444D928646eba3
>
> The product itself settles in canonical Base USDC
> (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913) through CDP-managed wallets and
> the x402 payment lane; there is no custom application contract.

State this plainly. Pretending to have a protocol contract you do not have is
the fastest way to lose credibility with this particular reviewer.

## Which track best fits what you're building? *

**Agents / Agentic Commerce**

## Share your key usage numbers *

**[FILL]** — all-time users onboarded, current DAU, current WAU, all-time volume
processed, last-30-day volume.

Sources: the managed-wallet SQLite database for users, `commerce_store` for
purchases and volume. Pull the real figures, do not estimate.

Suggested framing once you have them:

> [N] users onboarded, [N] DAU / [N] WAU, $[N] processed all-time and $[N] in
> the last 30 days across Bitrefill gift cards, eSIMs and mobile top-ups, all
> settled in USDC on Base.
>
> For scale context: we measured every listed x402 service in the ecosystem
> (508 of them) and found the entire distributed x402 economy runs at roughly
> $20,700 a month once you exclude the single dominant buyer. We are small in
> absolute terms and material relative to the rail we are on.

Small numbers stated precisely beat vague big ones. And the second paragraph is
the strongest thing in your whole application — it shows you understand the
ecosystem better than most people applying to it.

## How does your product make money today? *

> A 1% service fee on every purchase, charged on top of the product price and
> disclosed in the quote before the user approves. On a $50 gift card we take
> $0.50.
>
> Second line, in progress: users fund purchases with any token in their wallet
> and we handle the swap to USDC. That conversion is a service we provide
> regardless of which merchant the user buys from.

## What's your GTM plan for the next 3 months? *

> **Month 1 — fix the funnel and prove the product.**
> The site now routes to the Telegram bot instead of GitHub. Ship a screen-capture
> demo of a real purchase. Move the phone-number requirement out of onboarding
> and behind the first spend, so a new user reaches value before any ask.
>
> **Month 2 — launch AI chat as the acquisition wedge.**
> Venice AI over x402: five free messages, then a standing "$5/day, Venice only"
> policy the user signs once. This targets the audience whose cards do not work
> internationally, which is the same audience already buying gift cards from us.
> It is the first flow where paying per call beats a subscription, and it needs
> no card, no account and no KYC.
>
> **Month 3 — distribution through the ecosystem.**
> Publish our x402 measurement dataset and the audit script publicly. Partner
> posts with Bitrefill and Venice AI, both of whom we send volume. Add a referral
> loop in the bot, which is the only growth mechanism that does not scale with
> our own time.
>
> What we want from Base is the third month. We can build and we can measure;
> we are weakest at reaching the people who need this.

## What's your Base Builder Code? *

**[FILL]** — if you do not have one, get it before submitting. Check base.org /
the Base builder program; the form treats it as required, so a blank or a
guessed value will read as carelessness.

## Primary challenge or bottleneck *

**User acquisition** and **Go-to-market strategy**

These are the honest answers and they match what the program says it offers.
Do not tick "smart contract security" — you have no custom contract, and the
mismatch will be noticed.

## Which credits would be most useful? *

> Alchemy — we pay for a private Base Mainnet RPC endpoint today and it is a
> real line item; the public endpoint is rate-limited past our needs.
>
> Coinbase CDP — we use CDP wallets and the CDP facilitator for the x402 lane on
> Base Mainnet.
>
> AWS or equivalent — the gateway currently runs on a single VPS with SQLite.
> Credits would let us move to something that survives a host failure.

Name specific things you actually pay for. Generic "AWS credits would help"
answers get skimmed.

---

## Before you submit

1. **Deploy the Telegram CTA.** The single most damaging thing a reviewer can
   find is a landing page with no way to try the product.
2. **Record the demo.** It is a required field and yours would be the only one
   in the track showing hardware approval.
3. **Pull the real usage numbers.** Do not submit ranges or estimates.
4. **Get the Base Builder Code.**
5. **On "exclusively building on Base":** your production consumer product is
   Base-only. The Algorand lane is hackathon-era code that no user touches. If
   asked, say exactly that — the repo is public and a reviewer may look.
6. **Do not oversell the token.** An $80k market cap with thin trading is not an
   asset in this conversation. List the address because the form asks for it and
   move on.
