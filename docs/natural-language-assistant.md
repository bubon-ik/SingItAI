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
- Food delivery, physical goods and travel booking requests explain that direct
  fulfillment is unavailable. They offer gift cards as an explicitly different
  alternative and wait for the user to accept before browsing.
- Balance, last-purchase and limit-view requests use existing authenticated
  handlers. Explicit Solana balance requests retain the Solana network.
- Existing commands, buttons, purchase/withdrawal wizards and explicitly opened
  Venice chat retain priority. Unknown actions do not reach Hermes's general
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

The HTTP endpoint is `https://api.typesafe.ai/v1/systemone`. Only the current
message is sent: no wallet keys, receipts, account history, user identifier or
gateway access token is included by the adapter. A country follow-up is sent
on its own. Message text and provider responses are not written to logs.
As with any external classifier, the user's message itself may contain
personal information. Reflect this data flow in deployment privacy notices.

Requests have a five-second timeout and bounded input/output size. In-memory
limits allow at most 12 classifications per user and 120 per process per
minute. Pending clarification state is bounded and expires after 15 minutes.
These are per-process limits, not a durable account-wide spending budget.
The initial confidence cutoff is 0.8 (language selection uses 0.5); it must be
evaluated against labeled examples before a public rollout.

## Verification and rollout

```sh
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests
```

Local tests use fake TypeSafe and catalog responses. They verify routing,
country follow-ups, alternative consent, network preservation, cancellation,
authorization, provider failure, response validation and the off switch.
They do **not** establish Jev's real classification accuracy, latency, cost,
catalog coverage or Telegram deployment behavior.

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
rank products or perform direct food delivery and bookings. Free-form control
inside existing purchase wizards is unchanged. Assistant replies are Russian
or English; downstream catalog screens retain their existing language.

Country-scoped eSIM search reduces unrelated results, but country placement in
a catalog is not proof of package coverage. Regional/global packages may be
omitted. A later product-selection layer must inspect authoritative product
details and validate requirements before recommending a package.

Solana purchasing integration remains pending. Requests explicitly selecting
Solana or another unsupported payment network do not enter the Base purchase
wizard. This patch adds no payment capabilities or approval exceptions.

The implementation targets this repository's `main` baseline at `b518307`.
That version falls back to the public menu for unrecognized text outside chat.
A deployment which automatically opens Venice onboarding for all free text
has additional/different routing; reconcile that deployment before rollout.
This change has not been deployed or tested against a live TypeSafe account.

Sources: [TypeSafe API](https://docs.typesafe.ai/api),
[confidence](https://docs.typesafe.ai/confidence),
[intent routing](https://docs.typesafe.ai/patterns/intent-routing).
