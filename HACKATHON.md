# Crypto World's Fair 2026 — development record

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

## Pending work — not claimed as completed

- Manually verify the deployed wallet commands and refreshed navigation in Telegram.
- A real mainnet Venice payment and paid response through the agent.
- A real Exa search paid through the Solana agent and its sourced Venice response.
- Integration of the verified Bitrefill Solana route into the per-user agent, approvals and durable recovery.
- A custom x402 stock-purchase endpoint.

## Evidence to maintain during development

For each completed feature, add the date, commit or pull-request link, user-visible behavior, verification results and remaining limitations. Keep feature commits focused and push completed milestones regularly. Preserve published history and the baseline tag. Do not change timestamps or describe planned behavior as implemented.

Record mainnet transaction links only after actual execution and add short demo recordings for completed agent flows. Do not publish private keys, auth tokens, payment payloads or redemption data. At submission, link a fixed final commit or release and its comparison with the baseline, and copy the prior-work disclosure into the submission form. Git history, public progress updates, tests and demos provide complementary evidence; commit dates alone do not establish when every line was developed.
