# Natural-language entry to SingIt

This optional Telegram entry point routes ordinary messages to the existing
wallet and Bitrefill workflows before the public bot's menu fallback. It uses
TypeSafe Jev for typed classification. It does not require a user's Venice
model or Venice credit balance.

## Implemented behavior

- Mobile-internet requests start an eSIM catalog search for the requested
  country. Mobile top-ups and gift cards open existing catalog categories.
- A missing country triggers a follow-up. Country names are classified by Jev;
  ISO country-code buttons work without another model call.
- Follow-ups retain the pending task's country, category and network when the
  user continues it in their own words. Explicit replacements take precedence.
  A newly recognized task can interrupt a country or intent clarification.
- Food delivery, physical goods and travel booking requests explain that direct
  fulfillment is unavailable. They offer gift cards as an explicitly different
  alternative and wait for the user to accept before browsing.
- Balance, last-purchase and limit-view requests use existing authenticated
  handlers. Explicit Solana balance requests retain the Solana network.
- Existing commands, buttons, private checkout/withdrawal forms and pending Venice
  setup/model selection retain priority. Catalog browsing accepts a new task,
  such as a balance question, while numeric selections and navigation stay local.
  Genuine conversation requests enter
  the existing Venice policy and setup flow. Unknown actions do not reach Hermes's general
  tool-using agent. Free text never becomes an approval or payment instruction.
- Uncertain classifications, provider errors and requests for unsupported
  actions return clarification or the working menu. The router has no payment,
  withdrawal, swap, limit-change or credential-management action.

Example: `я хочу закать еду в Чехии` can be classified as food in CZ. The
assistant explains that it cannot order a meal, offers to check food-related
gift cards, and opens the real CZ catalog only after the user chooses that
alternative. Catalog availability is not assumed.

## Configuration

Configure the isolated **Hermes plugin process**, not only the gateway:

```dotenv
SIGN402_INTENT_ROUTER_ENABLED=1
TYPESAFE_API_KEY=<server-side TypeSafe key>
SIGN402_TYPESAFE_MODEL=jev-latest
```

The service pays for classification; it does not top up or charge a user's
Venice balance. Keep the key in the deployment's private environment file.
Leave the enable flag unset or set it to `0` to retain the previous behavior.
The public bot's existing access policies still apply.

The HTTP endpoint is `https://api.typesafe.ai/v1/systemone`. The current message
and, during a follow-up, validated enum context (stage, action, country, category,
network) are sent. No wallet keys, receipts, account history, user identifier,
product records, checkout fields or gateway access token is included by the
adapter. Message text and provider responses are not written to application logs.
As with any external classifier, the user's message itself may contain
personal information. Reflect this data flow in deployment privacy notices.

Requests have a five-second timeout and bounded input/output size. In-memory
limits allow at most 12 classifications per user and 120 per process per
minute. Pending clarification state is bounded and expires after 15 minutes.
These are per-process limits, not a durable account-wide spending budget.
The automatic routing confidence cutoff is 0.8. Supported action candidates
with confidence from 0.5 to below 0.8 receive a focused yes/no clarification,
retaining the detected country and network. Language selection uses 0.5.
These cutoffs still require evaluation against labeled examples.

## Verification and rollout

```sh
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests
```

Local tests use fake TypeSafe and catalog responses. They verify routing,
country follow-ups, alternative consent, network preservation, cancellation,
authorization, provider failure, response validation and the off switch.
They do **not** establish production classification accuracy, catalog coverage
or end-to-end Telegram behavior.

On September 21, all 375 plugin tests passed with the existing VPS bot's Python
and Telegram library, including the eight native Telegram checks skipped by
the local environment. Seven live TypeSafe requests using synthetic Russian
and English messages returned the expected intent/country/network decisions:
German mobile data, a German eSIM, a misspelled Czech food request, Solana
balance, an explanatory question, a Czech food gift card, and an unsupported
transfer. Calls took 0.60–0.71 seconds. These seven examples are a smoke check,
not a measured accuracy benchmark. No wallet payment was performed.

Before enabling publicly, test an isolated bot with real TypeSafe credentials
on Russian and English shopping requests, typos, questions versus purchase
requests, ambiguous countries, multiple tasks and unsupported networks.
Measure misrouting and unnecessary clarifications. No real purchases are
required for this validation. Pin a tested model version for a controlled
rollout if the provider makes that version available.

## Scope and remaining work

This is a routing foundation, not a universal autonomous shopping agent.
It opens existing catalogs; it does not extract arbitrary merchant names,
compare packages by duration/data/budget, verify eSIM device compatibility,
rank products or perform direct food delivery and bookings. Catalog menus allow
task switching, including package selection; private checkout forms retain their
existing input rules. The classifier distinguishes merchant search terms from
short wallet questions in the search-input step. Assistant replies are Russian
or English; downstream catalog screens retain their existing language.

Country-scoped eSIM search reduces unrelated results, but country placement in
a catalog is not proof of package coverage. Regional/global packages may be
omitted. A later product-selection layer must inspect authoritative product
details and validate requirements before recommending a package.

Solana purchasing integration remains pending. Requests explicitly selecting
Solana or another unsupported payment network do not enter the Base purchase
wizard. This patch adds no payment capabilities or approval exceptions.

The initial implementation targeted `main` at `b518307`. It has now been
integrated with the existing bot's `venice-solana` code at `337e777` and the
subsequent documentation commit `8a5de52`. The classifier runs before automatic
Venice onboarding and before broad shop shortcuts; an active chat setup still
owns its replies. Group messages are not sent to TypeSafe. This does not
change the existing Venice funding/approval policy or Solana chat integration.

Sources: [TypeSafe API](https://docs.typesafe.ai/api),
[confidence](https://docs.typesafe.ai/confidence),
[intent routing](https://docs.typesafe.ai/patterns/intent-routing).

## Existing bot deployment — September 21, 2026

Initial release [`adca498`](https://github.com/bubon-ik/singit-solana/commit/adca498)
was installed in the existing VPS checkout on `release/typesafe-20260921`.
[PR #4](https://github.com/bubon-ik/singit-solana/pull/4) is stacked on
`venice-solana`; it remains a draft and has not been merged to `main`.
The operator supplied the TypeSafe key through the private Hermes environment.
All six applicable GitHub checks and all 375 server-runtime plugin tests passed.

A private state/configuration backup was made before the update. The Telegram
service was restarted; the unchanged payment gateway was not. Both services
are active and gateway health returns 200. All 95 Base wallet records, the
Solana wallet record, bot configuration values and purchase history were
verified preserved. No purchase or wallet payment was submitted by this work.

The first installation attempt automatically rolled back because Hermes
reformatted `.env` during startup. Comparing parsed settings confirmed that
no key was added, removed or changed. The successful second attempt verified
the parsed environment values instead of requiring byte-identical formatting.
Both private backups remain on the VPS. The prior `venice-solana` branch still
points to the previous runtime release for a code-only rollback.

### Follow-up: reported conversation failures

The user's Telegram screenshots exposed two failures: the exact phrase
`i need internet in Germany` produced an eSIM confidence of 0.79, below the
automatic 0.8 cutoff, and catalog menus consumed subsequent balance questions.
The fix clarifies travel-internet criteria, retains medium-confidence candidates
for focused confirmation, and lets new natural-language tasks interrupt browsing.
Private checkout fields remain excluded from classification.

All 382 plugin tests passed in the server runtime. A sequential harness using
the live TypeSafe API and fake catalog/wallet handlers replayed the exact internet,
Czech food, food-gift-card and repeated Base-balance messages successfully.
This verifies classification and dispatch, not live wallet balances, eSIM
availability or Telegram delivery after the fix. No purchase was submitted.

Fix [`0647997`](https://github.com/bubon-ik/singit-solana/commit/0647997) was
deployed on `release/typesafe-routing-fix-20260921`. All six GitHub checks passed.
A fresh private backup preceded the Telegram-only restart. Both services are
active; the payment gateway process, all 95 Base wallets, the Solana wallet,
configuration values and purchase history were verified preserved. The previous
`release/typesafe-20260921` branch remains available for code rollback.

### Follow-up context correction

A later screenshot exposed context loss after `i need somesing for food in US`
followed by `show me a giftcards`: the fresh classification omitted the already
known country and category. The resulting country question then overwrote the
intent of `what my ballance USDC in base?` with the previous shopping task.

The fix merges missing fields only for a continuation of the pending task or
acceptance of its gift-card alternative. Recognized new tasks keep their own
intent and fields. Provider failure preserves pending context until its original
expiry; explicit local answers cancel an older in-flight classification.

All 391 plugin tests passed with the VPS runtime. A live TypeSafe harness with
fake catalog/wallet handlers passed the exact US-food conversation, interruption
of a standalone country question by two Base-balance requests, and a German
country-name reply completing an eSIM request. This verifies dispatch and
context transitions, not production accuracy across arbitrary conversations,
live catalog coverage or real Telegram delivery. No purchases were submitted.

Context fix [`b371841`](https://github.com/bubon-ik/singit-solana/commit/b371841)
is now deployed on `release/typesafe-context-fix-20260921`. All six GitHub checks
passed. A new private backup preceded a Telegram-only restart. Both services
are active and gateway health returns 200. The gateway process, all 95 Base
wallets, the Solana wallet, configuration values and purchase history were
verified preserved. The previous routing-fix branch remains available for rollback.

### Conversation regression audit

The audit adds a transition matrix covering nine browsing states, four pending
question states, Base/Solana switching, explicit category changes, natural
acceptance/decline, context expiry, private forms, cancellation and provider
failure. Duplicate in-flight messages are coalesced before rate accounting;
a rate-limited newer task invalidates an older response. A failed classifier
restores cancelled catalog loading screens to usable input states.

Contextual replies are classified separately from task intent. An uncertain
reply retains the pending question and its original expiry. A recognized wallet
request can interrupt it. Network clarification accepts Base/Solana replies.
Explicit requests for all categories no longer inherit an old food filter.
Merchant names in a search step remain searches even when their inferred
business category is food. Checkout fields remain outside the classifier.

The repeatable, opt-in live check uses only synthetic messages and fake catalog,
Telegram and wallet handlers:

```sh
# Supply TYPESAFE_API_KEY through the private process environment.
python3 scripts/check-intent-conversations.py --live
```

All 414 plugin tests passed in the VPS runtime, including native Telegram tests.
All 18 live conversation cases passed after the audit fixes. The initial run
failed five cases, which informed the corrections; 18/18 is a regression result,
not a production accuracy benchmark. Live tests make paid TypeSafe requests but
never create purchases or send Telegram messages. The offline regression tests
run automatically in the existing GitHub plugin test job. Real Telegram delivery
and provider/catalog outages still require operational observation.
