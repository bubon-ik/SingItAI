# Bankr LLM Credits and API Key Purchase Design

Date: 2026-07-05

## Goal

Let an authenticated Telegram user buy Bankr LLM Gateway credits with SINGIT
from their Sign402 Base wallet and receive a real Bankr `bk_...` API key.

The first release covers only:

- Bankr email OTP authentication;
- Bankr wallet and LLM-enabled API key creation;
- SINGIT-funded LLM credit purchase after iMessage approval;
- delivery of the API key to the same authenticated Telegram user;
- Bankr credit balance lookup.

Proxying model requests through Sign402 is outside this release. The user calls
`https://llm.bankr.bot` directly with the issued key.

## User Experience

### Start purchase

```text
/llm_buy 10 user@example.com
```

The bot validates that the requested amount is between Bankr's supported
minimum and maximum of 1 and 1000 USD. On the first Bankr purchase, it shows the
Bankr terms link and requires explicit confirmation:

```text
/llm_terms accept
```

After confirmation, Sign402 asks Bankr/Privy to send a one-time code to the
email address.

### Verify email

```text
/llm_code 123456
```

Sign402 verifies the OTP, creates or resolves the user's Bankr EVM wallet, and
creates an LLM-enabled Bankr API key. It then calculates the maximum SINGIT
required for the selected USD amount and sends an approval request to the
user's linked iMessage identity.

The iMessage approval contains:

- action: buy Bankr LLM credits;
- credit amount in USD;
- maximum SINGIT spend;
- source Sign402 wallet;
- destination Bankr wallet;
- key fingerprint, never the full key;
- expiration time and commitment hash.

After `YES`, Sign402 transfers the approved SINGIT amount from the user's
Sign402 wallet to that user's Bankr wallet and invokes Bankr's LLM credit top-up
endpoint. Telegram receives the resulting credit balance and the `bk_...` API
key.

The API key is displayed in full only in the successful purchase response.

### Check credits

```text
/llm_credits
```

The bot returns the user's Bankr LLM credit balance and the stored key
fingerprint. It never prints the full key again.

## Architecture

### `BankrIdentityClient`

A new client implements the Bankr CLI authentication protocol without spawning
an interactive CLI process:

1. fetch Bankr CLI/Privy configuration;
2. send the email OTP;
3. verify the OTP and receive the Privy identity token;
4. create or resolve the Bankr wallet;
5. accept Bankr terms only after the Telegram user explicitly confirms them;
6. create an API key through `POST /api-keys`.

The generated key has the minimum capabilities required for this flow:

```json
{
  "walletApiEnabled": true,
  "agentApiEnabled": false,
  "readOnly": false,
  "tokenLaunchApiEnabled": false,
  "llmGatewayEnabled": true,
  "allowedIps": [],
  "allowedRecipients": []
}
```

### `BankrLlmPurchaseService`

The service owns the purchase state machine:

```text
AWAITING_TERMS
  -> AWAITING_OTP
  -> AWAITING_IMESSAGE_APPROVAL
  -> AWAITING_TRANSFER
  -> TRANSFERRING_SINGIT
  -> TOPPING_UP_BANKR
  -> COMPLETE
```

Terminal failure states are:

```text
REJECTED
EXPIRED
FAILED_BEFORE_TRANSFER
RECONCILIATION_REQUIRED
```

The service composes existing Sign402 components rather than duplicating them:

- managed Telegram wallet lookup;
- real-rate SINGIT pricing;
- per-user spending limits;
- Photon/iMessage approval;
- Base ERC-20 transfer from the user's wallet;
- Bankr LLM credit top-up.

### `BankrLlmStore`

A dedicated SQLite store is keyed by the trusted Telegram user ID. It stores:

- pending purchase ID, USD amount, email, state, and expiration;
- explicit terms acceptance timestamp and terms version/URL;
- encrypted Bankr API key;
- API key fingerprint;
- encrypted Bankr wallet identity needed by the integration;
- Bankr wallet address;
- approval and commitment identifiers;
- SINGIT quote and transfer transaction;
- Bankr top-up response and resulting balance;
- reconciliation status and last error.

Secrets are encrypted with the existing Sign402 Fernet master key. Hashing the
API key alone is insufficient because the gateway needs the real key for top-up
and balance requests.

### HTTP endpoints

The Sign402 gateway adds authenticated endpoints:

```text
POST /agent/llm-key/start
POST /agent/llm-key/accept-terms
POST /agent/llm-key/verify
POST /agent/llm-credits
```

Every endpoint requires the existing per-user Sign402 access token. The
Telegram user ID comes from the authenticated Telegram event through the
Sign402 Hermes plugin; it is not accepted as an untrusted free-form identity.

### Hermes plugin

The Sign402 wallet plugin adds:

```text
/llm_buy <usd> <email>
/llm_terms accept
/llm_code <otp>
/llm_credits
```

Handlers call the gateway directly and do not send OTPs, email addresses,
identity tokens, or API keys through the LLM.

## Payment Flow

1. Price the requested USD credit amount in SINGIT using the existing real-rate
   pricer and its configured buffer.
2. Enforce the user's USD-denominated per-transaction and daily limits before
   requesting approval. The limit store receives 6-decimal USD atomic values;
   the SINGIT atomic amount remains separate in the approval commitment.
3. Create a commitment containing the exact USD amount, maximum SINGIT amount,
   source wallet, Bankr destination wallet, purchase ID, and expiration.
4. Require `YES` from the linked iMessage sender.
5. Revalidate quote freshness, limits, wallet balance, and approval immediately
   before transfer.
6. Transfer SINGIT from the user's Sign402 wallet to the user's Bankr wallet.
7. Call Bankr `POST /llm/credits/topup` with the encrypted API key and SINGIT as
   the source token.
8. Store the resulting balance and return the key once in Telegram.

The Base transfer client receives a human-readable SINGIT amount and converts
it to 18-decimal token units itself. Sign402 verifies that this human amount
matches the approved SINGIT atomic amount before calling the transfer client.

The API key may be created before iMessage approval, but it is not delivered
until the top-up succeeds. A rejected or expired purchase leaves the key stored
for a later retry and never transfers funds.

## Idempotency and Recovery

Each purchase has a unique Sign402 purchase ID used for every state transition.

- Repeating `/llm_code` must not create another Bankr key.
- Repeating an iMessage decision must not transfer SINGIT twice.
- Repeating a completed request returns the completed summary without exposing
  the full API key again.
- Failures before the Base transfer are safe to retry.
- If SINGIT was transferred but Bankr top-up did not return a definitive
  success, mark the purchase `RECONCILIATION_REQUIRED`.
- A reconciliation-required purchase must never initiate another transfer.
  It can only inspect Bankr wallet/credit state and retry the idempotent top-up
  against the already funded Bankr wallet.

## Security Rules

- Never log OTPs, Privy identity tokens, Bankr API keys, or encryption keys.
- Never pass sensitive values to Hermes LLM context.
- Rate-limit OTP send and verify attempts per Telegram user and email.
- Expire pending OTP sessions and purchase approvals.
- Bind the Bankr identity, wallet, API key, and purchase to one trusted Telegram
  user ID.
- Require a linked iMessage identity before any SINGIT transfer.
- Apply both user-selected spending limits and operator emergency limits.
- Redact Bankr and Privy response bodies from user-facing errors.
- Return generic Telegram errors while retaining structured operator logs.
- The generated Bankr key must not enable agent or token-launch APIs.

## Error Handling

- Invalid amount or email: reject before sending OTP.
- Terms not accepted: return the exact `/llm_terms accept` next step.
- Invalid or expired OTP: keep the purchase in `AWAITING_OTP` while attempts
  remain.
- Bankr key or wallet creation failure: fail before any money movement.
- Missing iMessage link: stop before pricing or transfer and direct the user to
  `/connect_imessage`.
- Insufficient SINGIT or exceeded limits: reject before approval or transfer.
- iMessage `NO` or expiration: mark the purchase rejected/expired.
- Base transfer failure: mark `FAILED_BEFORE_TRANSFER`.
- Ambiguous Bankr top-up result after transfer: mark
  `RECONCILIATION_REQUIRED` and do not transfer again.

## Testing

### Unit tests

- Bankr auth request shapes and capability restrictions;
- terms acceptance gate;
- OTP expiration and attempt limits;
- encrypted secret persistence and redaction;
- purchase state transitions and invalid transitions;
- SINGIT pricing and spending-limit enforcement;
- commitment contents;
- idempotent OTP verification, approval, transfer, and top-up;
- reconciliation after an ambiguous Bankr response;
- Telegram identity extraction and command parsing.

### Integration tests

Use mocked Bankr/Privy HTTP responses and a fake Base transfer client to test:

- complete first-time purchase;
- returning user purchase with an existing Bankr identity/key;
- iMessage rejection and expiration;
- transfer success followed by top-up timeout;
- retry without duplicate transfer;
- `/llm_credits` with encrypted stored credentials.

### Manual production smoke test

Use a new email and a small 1 USD purchase:

1. run `/llm_buy 1 <email>`;
2. accept Bankr terms;
3. verify the emailed OTP;
4. approve the exact SINGIT commitment in iMessage;
5. confirm the SINGIT transfer originates from the Telegram user's wallet;
6. confirm Bankr credits increased by 1 USD;
7. call `https://llm.bankr.bot/v1/chat/completions` with the returned key;
8. confirm `/llm_credits` reports the reduced balance without revealing the
   full key.

## Out of Scope

- routing LLM prompts through the Sign402 gateway;
- choosing models or purchasing model-specific packages;
- API key rotation or multiple keys per Telegram user;
- importing an existing Bankr API key;
- non-Base funding;
- automatic recurring top-ups;
- exposing full Bankr wallet management in Telegram.
