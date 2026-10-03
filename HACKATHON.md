# Crypto World's Fair 2026 — development record

Current Ask status (October 3): deployed actual-usage billing and one verified
Base payment of 0.001055 USDC. Solana recipient readiness is verified; a funded
Solana metered payment remains unverified. Dated entries below preserve the
state at each stage, including earlier deployment blockers that were resolved.


## Review links

- [Original SingIt project](https://github.com/bubon-ik/SingItAI)
- [Imported baseline](https://github.com/bubon-ik/singit-solana/tree/f39959059922b693f14c2a3e9bec97c87881e07b)
- [Changes since the imported baseline](https://github.com/bubon-ik/singit-solana/compare/singit-base-baseline...main)
- [Commit history](https://github.com/bubon-ik/singit-solana/commits/main/)
- [Standalone Solana client checks](solana-x402-service/CHECKS.md)
- [Managed wallet and Telegram command checks](docs/solana-wallet-checks.md)
- [First real Bitrefill Solana purchase](docs/bitrefill-solana-checks.md)
- [Native Telegram UI and purchase history checks](docs/telegram-ui-checks.md)
- [Conversation and Telegram Menu checks](docs/conversation-ui-checks.md)
- [Venice/Solana agent integration](docs/venice-solana-agent.md)
- [Integration plan](docs/solana-integration.md)

## Competition period and disclosure

The [official rules](https://colosseum.com/legal/Crypto%20World%27s%20Fair%20Hackathon%20Rules.pdf), section 5, specify September 14, 2026 at 6:00 AM PT through October 12, 2026 at 11:59 PM PT. The [Colosseum FAQ](https://colosseum.com/hackathon) permits existing code, requires disclosure of relevant previous development, and says products are judged on work completed during the competition period.

SingIt existed before this Solana effort. This repository preserves its Git history and builds on its existing agent, Base wallet, payment and approval infrastructure. Creating this repository does not make the imported code new hackathon work.

The imported baseline is `f39959059922b693f14c2a3e9bec97c87881e07b`, dated September 17, 2026. The tag `singit-base-baseline` marks the source version used for this integration. **It is not a snapshot from the competition's September 14 start.** The comparison above isolates changes in this Solana repository; it is not an exhaustive list of all work across other repositories during the competition. Imported upstream work is excluded from the Solana contribution described here.

## Existing foundation

The imported source already contains the Telegram/Hermes agent, managed Base wallets, gateway authentication, payment policies and approval channels, Bitrefill purchase flows, Base x402 tooling, and a Venice integration using Ethereum authentication. These are reused components, not new Solana capabilities. Their availability in a running deployment depends on configuration.

## Work recorded in this repository

### October 2: SingIt Ask deployment and manual payment test

The `trezor-local-sidecar` working tree contains the new, currently uncommitted
[`singit-ask`](singit-ask/README.md) service. It exposes an OpenAI-compatible
endpoint priced per answer through x402, using DeepSeek V4.1 Flash from Surplus.
It is deployed as a separate VPS user service at `ask.singitai.app`.
Fifteen local and VPS tests pass; public health and unpaid payment challenges
are verified, and one real upstream model response succeeded. A MetaMask page
lets the operator explicitly sign a single Base test payment. Customer payment
settlement has not yet been verified, and the Solana recipient still needs its
native USDC token account. [Evidence and remaining limits](singit-ask/CHECKS.md).
This entry records working-tree/deployment state, not a published commit or PR.

### October 2: actual-usage Ask billing (Base)

The same uncommitted worktree now implements direct per-request `upto` settlement:
actual Surplus token cost + 30% markup + 0.001 USDC settlement fee, within a 0.003
USDC authorization ceiling. It uses CDP for Base and retains the legacy PayAI
fixed-price route. This is Base infrastructure, not a new Solana capability.
The new merchant endpoint is deployed and its public unpaid quote is verified.
Gateway/UI integration and partial-spend accounting are staged, awaiting the
operator's sudo activation; the running web agent still uses the fixed-price path.
23 Node tests, 278 isolated VPS web tests and 11 reservation tests pass. No real
metered payment has been made. Solana metered payments remain unavailable.
[Implementation and exact limits](singit-ask/CHECKS.md).

### October 2: Solana actual-usage Ask billing

The uncommitted worktree extends the metered Ask service to Solana `upto`, using
CDP and the canonical Solana payment-channels program. A single-question escrow
reserves at most 0.003 USDC; actual token cost + 30% + 0.002 USDC is settled, with
the unused portion returned to the same user's agent. The existing Solana SPL
grant and agent funding sponsor are reused, with durable budget holds and no
automatic retry of uncertain funding or payments. This extends the previously
implemented Base path and preserves legacy exact endpoints.

The Solana merchant route is deployed and its public unpaid quote is verified.
31 Node tests, 287 isolated VPS web tests and 21 allowance/reservation tests pass.
The gateway/UI update remains staged. The receiver's native USDC account is absent,
protected Solana configuration needs sudo validation, and no real metered Solana
payment has been made. No published commit or PR is claimed.
[Evidence and activation prerequisites](singit-ask/CHECKS.md).

| Date | Commit | Contribution | Evidence and limits |
| --- | --- | --- | --- |
| 2026-09-17 | [2f5c1b0](https://github.com/bubon-ik/singit-solana/commit/2f5c1b0) | Added the standalone Solana/Venice x402 client, tests, integration plan and CI workflow to the imported agent repository. | 34 local tests passed. Live Solana authentication and an unpaid Venice quote were checked. No real payment or paid model response was completed. The client was developed separately earlier in this work session and first committed here as a module; this is not a record of each individual implementation step. |
| 2026-09-17 | [57ef9c3](https://github.com/bubon-ik/singit-solana/commit/57ef9c3) | Documented the separate public repository. | Documentation only. |
| 2026-09-17 | [f0eaa0b](https://github.com/bubon-ik/singit-solana/commit/f0eaa0b) | Converted the root and Solana module README files to English. | Documentation only. |
| 2026-09-17 | [eb10f40](https://github.com/bubon-ik/singit-solana/commit/eb10f40) | Added per-user encrypted Solana wallets, mainnet SOL/native-USDC balances, explicit network routing, and `/wallet solana` / `/balance solana` in the Telegram plugin. Preserved Base wallets and blocked Solana requests from entering legacy Base spending routes. | 1,234 gateway tests and 284 plugin tests passed. A temporary empty wallet was accepted by the Solana SDK; live mainnet RPC returned zero SOL and USDC. No production deployment, real Telegram transport run or payment. |
| 2026-09-18 | [19ae280](https://github.com/bubon-ik/singit-solana/commit/19ae280) | Refreshed native Telegram navigation, added named inline shopping controls and order review, editable operation cards, a Base/Solana wallet selector, and private purchase history with selected-order code reveal. | 1,245 gateway tests and 302 plugin tests passed, including actual PTB handler/markup construction with mocked transport. Existing Base approvals and spending policy remain in place. No Mini App, live bot deployment or Solana payment integration. [Details](docs/telegram-ui-checks.md). |
| 2026-09-19 | [37f7863](https://github.com/bubon-ik/singit-solana/commit/37f7863) | Replaced persistent Telegram navigation with native Menu and inline controls; added free-text AI entry, model/budget review and approval before the saved first question, payment-aware settings, safe cancellation and catalog search shortcuts. | 1,246 gateway and 332 plugin tests passed locally and on the VPS. Deployed to the existing bot at `a2ac835`; native Telegram Menu verified via API. Payments and transport tests were mocked; agent spending remains on Base. [Details](docs/conversation-ui-checks.md). |
| 2026-09-20 | [858a11e](https://github.com/bubon-ik/singit-solana/commit/858a11e) | Added Settings beside Wallet on Home, explained phone linking before payment, and put WhatsApp/iMessage connection first in Settings. | 332 plugin tests passed locally and on the VPS. Deployed to the existing bot; 95 wallet records and purchase history preserved. Existing phone verification and payment controls are unchanged. |
| 2026-09-20 | [PR #3](https://github.com/bubon-ik/singit-solana/pull/3) · [715d919](https://github.com/bubon-ik/singit-solana/commit/715d919), [337e777](https://github.com/bubon-ik/singit-solana/commit/337e777) | Integrated Venice into managed Solana wallets, added an AI network selector, separate per-network models/budgets, exact-quote phone approval, persistent payment holds and read-only recovery. | 1,289 gateway, 343 Telegram and 39 Node tests passed locally and on the VPS; all seven CI checks passed. Deployed at `337e777`, preserving 95 Base and 1 Solana wallet. Live managed-wallet SIWX balance and an unpaid quote were verified; no live Venice payment or paid Solana answer has been completed. [Flow and limits](docs/venice-solana-agent.md). |

The Venice client implements Solana SIWX authentication, mainnet USDC quote validation, explicit quote approval, SDK transaction construction, durable payment attempts, duplicate prevention and read-only reconciliation. Its Venice payment flow has only been exercised with mocked network responses.

### September 18: first real Bitrefill purchase

An operator-assisted Alza CZ 200 CZK purchase completed through the project Bitrefill MCP client and the Solana SDK payment wrapper. The final charge was 9.44 USDC, the exact transaction was confirmed on mainnet, and the actual gift-card code was retrieved using the paying wallet. [Evidence and limits](docs/bitrefill-solana-checks.md) · [Record history](https://github.com/bubon-ik/singit-solana/commits/main/docs/bitrefill-solana-checks.md).

Temporary helpers coordinated this live check; it does not constitute a reusable Bitrefill Solana adapter or a deployed Telegram purchase flow. No redemption data, buyer email or payment credentials are published.

### September 18: existing Telegram bot updated

At the owner's explicit request, release
[`6b3c2f5`](https://github.com/bubon-ik/singit-solana/commit/6b3c2f5) replaced the
existing VPS bot after private backups. It retained the existing bot identity,
Base wallets, configuration and purchase history. Before switching, 1,245
gateway tests, 302 plugin tests with the server's installed Telegram library,
and 46 CDP helper tests passed. Both services started, gateway health and
Telegram authentication succeeded, and unauthenticated purchase-history access
was rejected. Manual Telegram navigation and a purchase through the refreshed
UI remain unverified. This deployment does not enable Solana spending through
the bot. [Deployment checks](docs/telegram-ui-checks.md#existing-vps-bot-replacement).

### September 19: conversation and native Menu deployed

Release [`a2ac835`](https://github.com/bubon-ik/singit-solana/commit/a2ac835)
updated the existing VPS bot after private backups and server-runtime checks.
All 95 wallet records, configuration and purchase history were verified intact.
Both services are healthy; Telegram API confirms the six-command native Menu.
No live inference or purchase was performed for verification.
[Deployment details](docs/conversation-ui-checks.md#existing-vps-bot-updated).

### September 20: optional natural-language routing (local verification)

[619ec8c](https://github.com/bubon-ik/singit-solana/commit/619ec8c) adds a
TypeSafe-based entry point to the imported Telegram plugin. Ordinary messages
can select existing catalog and read-only wallet workflows without first
opening Venice chat. Direct delivery, physical-goods and booking requests
offer gift cards only as a separately accepted alternative. This is application
routing work, not a new Solana payment integration.

All 313 plugin tests passed locally (284 existing plus 29 new), using mocked
provider responses. No live TypeSafe request, bot deployment or purchase was
performed. The feature is disabled by default. Semantic accuracy, product
matching beyond catalog categories, and reconciliation with the deployed bot
remain unverified. See [setup and limitations](docs/natural-language-assistant.md).

### September 21: TypeSafe validation against the deployed code

The natural-language routing work was integrated with `venice-solana` at
`337e777` plus its documentation follow-up `8a5de52`. All 375 plugin tests
passed using the VPS bot's actual runtime, including native Telegram checks.
Seven synthetic live TypeSafe requests returned the expected decisions in
0.60–0.71 seconds. This verifies the configured API key and a small sample of
intents; it is not a production accuracy benchmark. No purchase or wallet
payment was performed. Pending chat setup and genuine Venice conversation
remain supported. See [verification details](docs/natural-language-assistant.md).

Release [`adca498`](https://github.com/bubon-ik/singit-solana/commit/adca498),
[PR #4](https://github.com/bubon-ik/singit-solana/pull/4), is now installed on the
existing bot. All six applicable CI checks passed. Private backups and state
comparison confirmed preservation of 95 Base wallets, 1 Solana wallet, bot
configuration values and purchase history. Only the Telegram service restarted;
the payment gateway remained running. An initial formatting-only `.env`
difference triggered a code rollback; a second attempt verified parsed values
and completed successfully. Real Telegram conversations remain a manual check.

### September 21: conversation routing corrections

[PR #4](https://github.com/bubon-ik/singit-solana/pull/4) also fixes two failures
reported in Telegram screenshots: a borderline eSIM classification now prompts
a focused confirmation, and catalog browsing no longer traps new balance
questions. Checkout fields retain their existing input handling. All 382 plugin
tests passed in the server runtime. A live TypeSafe harness replayed the exact
internet, Czech food, gift-card and repeated Base-balance messages with fake
catalog/wallet handlers. Actual Telegram delivery and catalog availability after
this fix remain manual checks; no purchases or wallet payments were made.

Fix [`0647997`](https://github.com/bubon-ik/singit-solana/commit/0647997) was then
deployed to the existing bot after all six GitHub checks passed and a fresh
private backup was created. Only Telegram restarted; both services are active.
All 95 Base wallets, the Solana wallet, configuration values and purchase history
were verified preserved.

### September 21: pending-task context correction

[PR #4](https://github.com/bubon-ik/singit-solana/pull/4) additionally fixes the
reported US-food conversation: free-form gift-card follow-ups retain the pending
country/category, and a newly recognized task can interrupt a country question.
Expired context is not reused; explicit new fields replace previous fields.
Provider failure preserves pending context and local cancellation rejects late
classification results. All 391 plugin tests passed in the VPS runtime. Live
TypeSafe checks with fake catalog/wallet handlers passed the exact reported
conversation, balance interruption during country clarification, and a country
name completing an eSIM request. Post-fix Telegram delivery remains a manual
check; no wallet payment or purchase was made.

Context fix [`b371841`](https://github.com/bubon-ik/singit-solana/commit/b371841)
was deployed after all six GitHub checks passed. A fresh private backup and
post-restart comparison verified all 95 Base wallets, the Solana wallet,
configuration and purchase history intact. Only Telegram restarted; both
services are active and the payment gateway process stayed unchanged.

### September 21: conversation regression audit

[PR #4](https://github.com/bubon-ik/singit-solana/pull/4) expands the checks from
reported phrases to browsing/pending-state transitions, cancellation, duplicate
and delayed messages, expiry and provider failures. Fixes add bounded enum
context to classification, natural follow-up replies, network clarification,
short wallet-task interruption of search, explicit all-category changes and
recovery of cancelled loading screens. Private checkout input stays local.

A repeatable opt-in live classifier harness covers 18 synthetic conversations
with fake Telegram/catalog/wallet handlers. The first run failed five cases;
all 18 passed after correction. This is regression coverage, not a guarantee of
arbitrary-language accuracy or verification of real Telegram delivery. No
purchase or wallet payment was made. All 414 plugin tests passed in the VPS
runtime. See the [verification instructions](docs/natural-language-assistant.md#conversation-regression-audit).

Release [`2015199`](https://github.com/bubon-ik/singit-solana/commit/2015199)
was deployed after all six GitHub checks passed. A fresh private backup and
post-restart comparison verified preservation of 95 Base wallets, the Solana
wallet, configuration and purchase history. Only Telegram restarted; both
services are active and the payment gateway process stayed unchanged.

### September 22: Exa search for Solana Venice chat

[PR #5](https://github.com/bubon-ik/singit-solana/pull/5) extends the current conversation release with a fixed
Exa x402 v2 search adapter, per-user Solana signing, a separately approved search
budget, durable payment holds/recovery, source links and a separate cost/receipt
in the selected Venice model's answer. Base search behavior is retained.

The exact unpaid request returned HTTP 402 with a 0.007-USDC Solana option and
sponsored fees. Offline tests cover consent, limits, concurrency and recovery;
the full SVM fixture uses real transaction signatures against local providers.
Local checks passed: 1,309 gateway tests, 422 plugin tests and 49 Solana Node
tests. No real Exa payment or paid Venice answer was sent. See the
[feature record](docs/exa-solana-chat.md) for limits and verification.

Release [`12d8dc0`](https://github.com/bubon-ik/singit-solana/commit/12d8dc004235352262bb5c22fbbb124e8ad72e9b) was deployed to the existing bot after all seven
GitHub checks and the full VPS test suites passed. A private backup and post-restart
checks preserved 95 Base wallets, the Solana wallet, configuration and history.
Authenticated Exa review and signed Venice balance checks passed; search stayed
off and no real payment was made. [Deployment evidence](docs/exa-solana-chat.md#existing-vps-deployment).

### September 22: model-selected Solana web search

[PR #5](https://github.com/bubon-ik/singit-solana/pull/5) adds semantic search
selection by the user's chosen Venice model. Its first completion either answers
or requests one Exa query; the final completion receives the original question
and retrieved sources. Search no longer depends on freshness keywords. The
separate standing budget, payer/merchant binding, durable holds and one-search
limit remain enforced by the gateway. Both model completions use Venice credit.

Offline coverage includes implicit research questions, translations containing
search keywords, strict control replies, missing consent, expiry/pause between
stages, original-question/model preservation, receipt retention and refusing a
second search. Real funded model/search quality evaluation remains pending;
scripted provider fixtures are not evidence of every model's semantic accuracy.
Local checks passed: 1,319 gateway tests, 423 plugin tests and 52 Solana Node tests.

### September 29: web search and live data from a Solana wallet on the web page

On app.singitai.app a Solana wallet approves an SPL allowance to its agent once; the agent then pays straight
from the owner's USDC account as delegate, within limits the server enforces, with the seller's fee payer
sponsoring the fee.

[`dfa47b7`](https://github.com/bubon-ik/SingItAI/commit/dfa47b7) adds Exa web search to the web chat: a
question about now is searched once and Venice answers from the pages, with the price and links under the
answer. The Exa payment on Solana is built by `buildDelegated` from the owner's account. Verified on mainnet
after deployment: a real 0.007-USDC search, transaction
[`4Lr4rE…Sesd6`](https://solscan.io/tx/4Lr4rECE7GzLjuyf6TZcZdgnXLgyjykQnj52tEHp9knrH6kNzaBdgMv8LMEwDS82SkoySdSRYDC9ajKDeu9Sesd6),
signed by the agent as delegate, paid to Exa's Solana address, fee sponsored; the allowance and the day's limit
fell by the same amount.

[`6334ca4`](https://github.com/bubon-ik/SingItAI/commit/6334ca4) adds live data per question, picked from
Coinbase's Agentic Market and PayAI's Bazaar: weather, exchange rates, token and stock prices, Polymarket, crypto
news and funding rates (Otto AI), reading a link (Exa), a flight's status (FlightAware) and flight prices (Google
Flights) via stabletravel.dev, and Tripadvisor places. The bridge's new `data-pay` pays only listed hosts, only
the address bound for each seller, never above its ceiling, one attempt per request. Unpaid 402 checks confirmed
every source's Base and Solana address and price. Local checks passed: 1,581 gateway tests and 74 Solana Node
tests. No real live-data payment has been made yet.

### September 29: correct phone-call refusals on the web page

[`b8058ca`](https://github.com/bubon-ik/SingItAI/commit/b8058ca) rejects numbers outside StablePhone's
published `+1` format before showing a call card or attempting payment, including drafts saved before
this change. The page shows a call as started only after the provider returns its call ID; refusals and
lost responses no longer show a success checkmark or automatically repeat the request. This follows
[`a1640bc`](https://github.com/bubon-ik/SingItAI/commit/a1640bc), which keeps settlement queries within
the Base RPC's ten-block limit so they no longer hide the provider's refusal.

Verification: 211 web gateway tests and five frontend call-state regression tests passed. The observed
production attempt returned HTTP 400, `Validation failed`. Czech `+420` calls are not supported by this
integration; a successful real call has not been verified. These fixes do not add a new calling provider.

## Pending work — not claimed as completed

- Manually verify the deployed wallet commands and refreshed navigation in Telegram.
- A real mainnet Venice payment and paid response through the agent.
- A real Exa search paid through the Solana agent and its sourced Venice response.
- Integration of the verified Bitrefill Solana route into the per-user agent, approvals and durable recovery.
- A custom x402 stock-purchase endpoint.

## Evidence to maintain during development

For each completed feature, add the date, commit or pull-request link, user-visible behavior, verification results and remaining limitations. Keep feature commits focused and push completed milestones regularly. Preserve published history and the baseline tag. Do not change timestamps or describe planned behavior as implemented.

Record mainnet transaction links only after actual execution and add short demo recordings for completed agent flows. Do not publish private keys, auth tokens, payment payloads or redemption data. At submission, link a fixed final commit or release and its comparison with the baseline, and copy the prior-work disclosure into the submission form. Git history, public progress updates, tests and demos provide complementary evidence; commit dates alone do not establish when every line was developed.

### October 2: uniform SingIt Ask price (uncommitted working tree)

The owner chose a fixed 0.003 USDC per successful answer on both Base and Solana;
usage-based billing and prepaid customer balances were not implemented. Updated
service defaults, private local/VPS settings, docs and existing challenge tests.
All 15 offline tests passed locally and on the VPS. After a private backup and
service restart, public health returned 200 and an unsigned request returned
402 with amount `3000` on both networks. No real customer payment was made.
Solana recipient token-account readiness and real settlement remain unverified
as recorded in `singit-ask/CHECKS.md`. This change has no commit or PR yet.

### October 2: automatic Ask payments from the web agent (staged, not active)

Added an opt-in SingIt Ask model for Base web accounts. The existing agent pays
0.003 USDC through x402 from its approved allowance, using bound merchant terms
and existing spending/settlement checks. The UI shows a per-answer price rather
than token pricing or chat-credit top-ups. Existing Venice selections and linked
Telegram models are preserved. Solana is explicitly unavailable for this new
choice pending receiver readiness and delegated-payment verification.

Verification: eight new adapter tests, two dispatch tests and existing web
regressions passed (272 web tests in total across the isolated runs); frontend
syntax and diff checks passed. The operator activation script checks reviewed
file hashes before deployment and creates private backups. Production has not
been activated: restarting its system services requires sudo authentication.
No live customer payment was attempted. Changes remain uncommitted; no PR yet.

### October 2: first real Ask payment through the Base web agent

The owner activated the prepared integration and asked a question. All five
production file hashes match the reviewed integration. The answer's usage row
records SingIt Ask, 553 input tokens, 100 output tokens and 0.003 USDC; the
account's settlement row points at the Ask endpoint. Independent Base RPC
verification confirms the agent-to-merchant USDC transfer in
[0x2b56b609…745eac](https://basescan.org/tx/0x2b56b609a130264de8434dcaa844e6d9cf210f503ead0266bd79f0bfe2745eac),
block 52086557, status 1, at 17:34:21 UTC. Verification itself was read-only.
The Base web integration is now active and has one verified real payment;
Solana remains unavailable for this model. Changes remain uncommitted.

### October 3: web chat location follow-ups and Ask failure reporting

The owner reported a Prague coffee lookup returning other cities, followed by an
unrelated eSIM catalog on a location correction. Live-data planning now carries
the latest explicit user location across turns, excludes assistant/search text
as a source of location, and keeps a location correction attached to the
immediately preceding data lookup. Context is scoped to the conversation; a new
explicit request keeps its own route. Only the extracted location is shared with
the auxiliary planner, not the full private conversation.

Ask errors retain their provider and settlement-review reason instead of being
masked by the old Venice fallback message. A pending Ask payment blocks additional
paid search data. Base buyer diagnostics record only controlled stage labels and
HTTP status, never keys, signed payment payloads or provider response bodies.

Verification: 295 isolated web tests and 32 Node tests passed without real payments.
`activate-context.py --check-only` verified the five reviewed files and both unpaid
quotes. Deployment requires the operator's sudo authentication; the prepared
command creates private code/config/state backups and verifies wallet identities.
One prior Base Ask authorization remains under read-only reconciliation; this is
not evidence of a completed metered payment. Changes remain uncommitted; no PR.

The owner subsequently ran the context activation successfully. All five live
hashes match the reviewed patch; gateway and web API are active and all three
health/page checks returned HTTP 200. The operator's private rollback snapshot is
`20261003T092247Z-before-ask-context-fix`; wallet identity verification passed.
This confirms deployment, not a successful new metered payment.

The prior Base Ask attempt was subsequently reconciled as expired and unused at
finalized block 52114913. Its non-secret checkpoint and journal were privately
archived with the chain proof, and the affected account's Ask block was removed.
No payment was retried. A verify-only CDP probe on an unfunded fixture reached the
expected insufficient-funds check; a successful funded metered answer still
requires the owner's next chat test.

### October 3: Base metered buyer extension echo fix

Reproduced the owner's repeated HTTP 402 failure using the real buyer, SDK signer
and Express middleware in one offline test: the buyer discarded the merchant's
extension description, causing `extension_echo_mismatch` before CDP verification.
The client now preserves advertised extension metadata while adding its bounded
signed permit. All 33 Node tests passed locally and on the VPS. Installed the
single-file fix with a private code/config/state backup and wallet identity
verification; the per-request buyer process needs no service restart.
The second failed authorization was reconciled as expired and unused at finalized
Base block 52115703 and unblocked. No live payment was sent by the repair; the
next funded end-to-end test belongs to the owner. Uncommitted; no PR yet.


### October 3: first verified actual-usage Base Ask payment

The owner's new chat request completed on SingIt Ask. Independent Base RPC
verification confirms transaction
[0xe9b5af13…618e6ad](https://basescan.org/tx/0xe9b5af13f5070e882635db2ef85540a00515eea968fd93b2a513d5cf0618e6ad)
succeeded in block 52116473 at 2026-10-03T10:11:33+00:00, with 77 confirmations at inspection.
Its single native-USDC Transfer moved 1055 atomic units (0.001055 USDC) from the
user's existing Base agent to their configured merchant receiver. Transaction
gas was paid by a different facilitator address, not the user's agent.

The returned usage records 498 prompt and 335 completion tokens. Billing is
42 atomic units of provider cost + 13 of rounded 30% markup + 1000 of the
published settlement fee = 1055. The chat usage and allowance settlement ledger
agree with the chain. The 3000-unit cap was not charged. No unresolved Ask
journal remains for this account. This is a verified live Base metered payment;
Solana's actual-usage path still has no verified funded end-to-end example here.
Verification was read-only; the owner initiated the request. Changes remain
uncommitted; no PR.

## October 3: committed Ask integration

Implementation: [0e5762b](https://github.com/bubon-ik/SingItAI/commit/0e5762bc328f6a94e0f24c17a6e2a8c40dcf7887)
on `trezor-local-sidecar`. Includes Base and Solana actual-usage adapters, English
operator test page, conversation-location continuity, provider-specific errors
and the Base extension-echo fix. Verification: 33 Node tests locally and on the
VPS, 295 isolated web tests, and 21 allowance/reservation tests passed. The owner's
Base payment settled 0.001055 USDC and matches the receipt and ledger. Solana's
receiver account is ready; a real funded metered Solana payment is still untested.
The final model-picker price label correction is committed locally; this commit
step did not redeploy that static asset. Earlier entries describe historical
working-tree states; no PR was created for this integration.

## October 3: user-funded Solana network fees

Implementation: [91e9a07](https://github.com/bubon-ik/SingItAI/commit/91e9a07bf2c1461fc8c436454e4828074c04c092).

The owner rejected operator-funded gas. Ask now checks the user's agent SOL
balance against live network-fee and token-account-rent quotes before funding,
and signs the funding transaction only with that agent. Insufficient SOL sends
nothing and releases the Ask hold. Wallet approvals/revokes have no operator
sponsor fallback. Allowance now exposes agent SOL and an explicit, wallet-signed
SOL transfer to the account's own agent; amount and destination are validated.
Unknown SOL transfers are not resubmitted, and new funding is blocked for review.

Verification: 77 Solana Node tests, 297 isolated web tests and 13 allowance tests
passed on the VPS without real payments. A pre-existing archive test assumed
ordering of equal timestamps; it now selects the intended chat by ID. Six-file
source-hash and unpaid-quote activation preflight passed. The update is staged,
not active: the operator must run `activate-user-gas.py` with sudo. No new wallet
or payment was created. Real user-funded Solana Ask settlement remains untested.

Operator activation completed at 2026-10-03 11:25:35 UTC, with private rollback
snapshot `20261003T112535Z-before-user-funded-solana` and preserved wallet
identities. All six deployed source hashes match, gateway/web API are active,
and three health/page checks return 200. The live JavaScript includes the
Agent network fees control. This confirms deployment, not a funded Solana
end-to-end payment; no payment was sent during verification.


## October 3: direct delegated Solana Ask payments

The replacement web Ask path generates a measured invoice privately, then
settles one CDP x402 `exact` payment directly from the user's existing USDC
account using the existing agent's SPL delegate grant. CDP provides the network
fee payer. No agent SOL funding, intermediate USDC transfer or agent token
account is required. Pricing is actual model cost + rounded 30% markup +
0.001 USDC, capped at 0.003. The legacy public Solana `upto` endpoint remains
unchanged at 0.002 USDC; Base's working payment adapter is preserved.

Preparation uses a server-only token and request-bound, expiring quotes. Answers
remain private until settlement; a durable SQLite claim prevents repeated
settlement after concurrent requests or restarts. Unknown results remain blocked.
Both client and gateway verify the owner debit, receiver credit, delegated signer,
request memo and facilitator gas payer against confirmed chain data. Existing
wallets, approvals and unresolved holds are preserved. The merchant bears model
cost if a prepared answer is abandoned before payment.

Verification: 43 Node tests and 294 isolated web tests passed on the VPS without
payments. Live CDP verify accepted the existing agent's delegated exact transfer;
settle was never called. Nine-file deployment preflight passed. This update is
staged, pending sudo activation and a real user-initiated Solana chat payment.
Implementation: [d405ca0](https://github.com/bubon-ik/SingItAI/commit/d405ca0ccc4ff2291277ef5477517ff722fe346f).


Operator activation completed at 2026-10-03 12:08:02 UTC with private rollback
snapshot `20261003T120802Z-before-direct-solana`. All nine deployed hashes match;
gateway, web API and merchant services are active. Public health and JavaScript
checks with the actual Node client return 200 and advertise the new 0.001-USDC
fee and no-agent-SOL flow. The preparation route requires authentication. Wallet
identities were preserved, there are no pending Solana Ask journals and the
new quote ledger has no paid records. This confirms deployment/readiness;
a real user-initiated direct Solana settlement remains to be verified.


## October 3: first verified direct Solana Ask payment

The owner's `hello` settled successfully in finalized Solana slot 452930408 at
2026-10-03T12:14:46Z. Transaction:
[WDX88g28…T7nmd](https://solscan.io/tx/WDX88g28scLasw4BJVQj98KLhm4gn9wBJv5rRcSogy5F9kukaDZUAaf7NUJpvCE7M1a76MbRtepwxJBzo9T7nmd).

Independent RPC inspection confirms one native-USDC transfer of 1069 atomic
units directly from owner `BTXXtaRQfzd7BF6zADrMtqDeDhdiiP3t2WYXHz3hCCSK` to
receiver `4an2sqamWWhny9mjLsMtGXCDXakeNtg6vSLq4QvhdmQu`, authorized by the existing
agent `qSgKeem2rCAEqHRQZvMEmrpiQQNEQaCZCLtcUA8VgMc`. The owner's USDC balance
changed from 4.875000 to 4.873931; the receiver gained exactly 0.001069 USDC.
The advertised CDP fee payer paid 10001 lamports; neither owner nor agent paid
that transaction's network fee.

Usage: 546 input + 535 output = 1081 tokens. Provider cost 53 micro-USDC + rounded
30% markup 16 + settlement service fee 1000 = **1069 micro-USDC (0.001069 USDC)**.
The merchant receipt, independently validated transfer and allowance spend ledger
agree. No metered hold or pending Solana Ask journal remained. Verification was
read-only; the owner sent the paid question. The 0.003 ceiling was not charged.

The answer incorrectly asked for agent USDC/SOL funding despite this successful
payment. The model had received the internal zero float and an outdated
Base-oriented system prompt without the new direct-payment rules. The follow-up
fix supplies owner balance instead of internal float, identifies the actual
network, distinguishes spending permission from balance and attaches provider-
specific funding rules that override obsolete advice in earlier chat messages.
Greetings should not trigger unsolicited wallet checklists. All 297 isolated web
tests pass; the two-file source-hash preflight passes. This context correction is
staged pending operator activation; it does not change the payment implementation.

Context correction: [1cd8e64](https://github.com/bubon-ik/SingItAI/commit/1cd8e646d59e7d358371eeebadfa232ccc250e60).
