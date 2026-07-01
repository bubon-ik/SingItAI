# Photon iMessage Approval Design

## Goal

Add a production-oriented iMessage trust layer for managed Sign402 users.
Users onboard in Telegram, link an iMessage phone number through Photon, and
approve or deny one exact pending action by replying `YES` or `NO`.

The first release proves the complete identity and approval path with a test
commitment that cannot move funds. Base transaction signing remains disabled
until this approval path has passed an end-to-end deployment test.

## Current Context

- Hermes and Sign402 run on the VPS.
- Telegram wallet commands use trusted `MessageEvent.source` identity.
- Each Telegram user can create one encrypted managed Base wallet.
- Base balances are read through Alchemy.
- Photon Spectrum is configured as a Hermes platform on the VPS.
- Photon provides the iMessage line and a persistent gRPC connection; no Mac,
  public webhook, domain, or tunnel is required.
- Managed-wallet spending is disabled.

## Scope

This slice includes:

- Telegram command `/connect_imessage`.
- Telegram command `/test_approval`.
- One-time pairing codes.
- A one-to-one mapping between a Telegram user and a Photon E.164 sender.
- A generic, persistent approval queue.
- Trusted Photon notifications through `hermes send`.
- Pre-LLM interception of pairing codes and pending `YES` / `NO` replies.
- Atomic approval decisions with expiry and replay protection.
- Audit records that do not contain plaintext phone numbers or secrets.
- A no-funds test approval flow.

This slice excludes:

- Signing or broadcasting Base transactions.
- Sending ETH, USDC, SINGIT, or arbitrary ERC-20 tokens.
- Wallet import or private-key export.
- Email onboarding or email approvals.
- Multiple iMessage identities per wallet.
- Replacing an existing iMessage identity without an explicit future recovery
  flow.
- Automatic onboarding for users who do not have a Telegram identity.

## Architecture

```text
Telegram user
    |
    | /connect_imessage, /test_approval
    v
Hermes sign402-wallet plugin
    |
    | authenticated localhost HTTP
    v
Sign402 Gateway
    |
    | encrypted identity mapping + generic approval state
    | exact canonical commitment hash
    |
    | hermes send --to photon:<verified E.164>
    v
Hermes Photon platform -> Photon Spectrum -> iMessage user
    |
    | pairing code or YES / NO
    v
Hermes pre_gateway_dispatch hook
    |
    | authenticated localhost HTTP using trusted Photon source identity
    v
Sign402 Gateway
```

Sign402 Gateway owns identity linking and approval state. Hermes owns message
transport. Photon owns iMessage delivery. None of these layers may infer an
approval from LLM output.

## Identity Model

The trusted user key remains the Telegram numeric user ID captured from the
Telegram `MessageEvent.source`. The Photon identity is the normalized E.164
sender captured from the Photon `MessageEvent.source.user_id`.

The database stores:

- `telegram_user_id`
- a deterministic HMAC-SHA256 lookup digest of the normalized Photon number
- a Fernet-encrypted normalized Photon number for outbound delivery
- link creation and update timestamps

The plaintext phone number must not be written to the database, logs, audit
metadata, API errors, or test snapshots.

Mappings are one-to-one:

- one Telegram ID can have one active Photon identity
- one Photon identity can belong to one Telegram ID

An existing mapping is not silently replaced. Recovery and number replacement
will be a separate explicit flow.

## Pairing Flow

1. The user sends `/connect_imessage` in Telegram.
2. The Hermes plugin captures the trusted Telegram source identity.
3. The plugin calls Sign402 Gateway over localhost using the Photon API token.
4. Gateway requires that the Telegram user already has a managed wallet.
5. Gateway expires any previous unused pairing request for that user.
6. Gateway generates an eight-character cryptographically random code from an
   unambiguous uppercase alphabet.
7. Gateway stores only an HMAC digest of the code.
8. The pairing request expires after ten minutes and is single-use.
9. Telegram tells the user to send the code to the assigned Hermes iMessage
   line.
10. A Photon pre-dispatch hook recognizes the pairing-code format before
    Hermes authorization or LLM dispatch.
11. The hook submits the code plus the trusted Photon source ID to Gateway
    with a short, bounded localhost request.
12. Gateway atomically consumes the code and creates the identity mapping.
13. On success, the hook grants the Photon source through Hermes'
    `PairingStore` public pairing operations.
14. The hook schedules a fixed confirmation through the active Photon adapter
    and returns `{"action": "skip"}`.

Invalid, expired, consumed, or conflicting codes return fixed safe errors.
They never expose whether a Telegram ID or phone number already exists. A
code-shaped message is consumed by the hook even when invalid, so it cannot
fall through to the LLM or trigger a second, unrelated Hermes pairing flow.

## Approval Model

Approvals are generic so the same interface can later protect payments, API
calls, authentication, and other agent actions.

Each approval stores:

- opaque random approval ID
- Telegram user ID
- action type
- SHA-256 commitment hash
- up to three sanitized human-readable context lines
- status
- creation and expiry timestamps
- decision timestamp
- optional future executor reference

The commitment hash is computed from canonical JSON owned by Sign402 Gateway.
Hermes and the LLM cannot choose or alter the approved hash.

Initial statuses are:

- `pending`
- `approved`
- `denied`
- `expired`
- `delivery_failed`

Only one non-expired pending approval is allowed per Telegram user. A new
request is rejected while another is pending.

The test commitment contains:

- schema version
- action type `sign402_test`
- wallet address
- random nonce
- creation time
- expiry time

It explicitly contains no executable transaction and cannot be passed to a
payment executor.

## Approval Notification

Gateway sends a fixed canonical message:

```text
Sign402 approval request

Action: TEST APPROVAL
Wallet: 0x1234...abcd
Funds: No funds will move
Expires: 2 minutes
Hash: 1a2b3c4d

Reply YES or NO.
```

The notification is sent without an LLM:

```text
/home/hermes/.local/bin/hermes send --to photon:<verified-number> <message>
```

The subprocess uses an argument array, never a shell. It has a short timeout,
bounded output capture, a fixed executable path from configuration, and a
minimal environment rooted at `/home/hermes`.

If delivery fails, the approval becomes `delivery_failed` and cannot later be
approved.

## YES / NO Interception

The Hermes plugin inspects Photon messages in `pre_gateway_dispatch`.

- Only exact case-insensitive `YES` or `NO`, after trimming whitespace, can be
  decisions.
- The hook first asks Gateway whether the trusted Photon sender has a live
  pending approval.
- If no approval is pending, the message is left unchanged and continues to
  normal Hermes conversation.
- If an approval is pending, the hook submits the trusted Photon sender and
  decision to Gateway with a short, bounded localhost request.
- Gateway atomically checks identity, status, expiry, and sender ownership.
- A successful decision permanently transitions the approval.
- The hook schedules a fixed result through the active Photon adapter and
  returns `{"action": "skip"}`.

The original `YES` or `NO` is never sent to the LLM when it resolves an
approval. LLM text, Telegram text, API request bodies, and command arguments
cannot override the trusted Photon identity.

Late, duplicate, or replayed decisions return a fixed "no pending approval"
response and have no side effects.

## Gateway API

All endpoints bind through the existing localhost-only Sign402 Gateway and
require:

```text
Authorization: Bearer SIGN402_PHOTON_API_TOKEN
```

Endpoints:

```text
POST /agent/imessage/pairing
POST /agent/imessage/link
POST /agent/imessage/pending
POST /agent/imessage/decision
POST /agent/test-imessage-approval
```

Requests from Telegram commands include the trusted Telegram ID captured by
the plugin. Requests from Photon commands include the trusted Photon source ID
captured by the plugin.

Responses expose only fixed user-facing text and non-secret status fields.
They never return phone numbers, pairing-code digests, encrypted values,
private keys, API tokens, or full canonical commitments.

## Configuration

Sign402 Gateway:

```text
SIGN402_PHOTON_API_TOKEN=<independent random bearer token>
SIGN402_IMESSAGE_APPROVAL_STORE_PATH=/home/hermes/.sign402/imessage-approvals.db
SIGN402_HERMES_CLI=/home/hermes/.local/bin/hermes
SIGN402_HERMES_HOME=/home/hermes/.hermes
```

The existing `SIGN402_WALLET_MASTER_KEY` encrypts Photon phone numbers and
keys pairing-code HMACs. No second plaintext encryption key is introduced.

Hermes plugin:

```text
SIGN402_GATEWAY_URL=http://127.0.0.1:8099
SIGN402_PHOTON_API_TOKEN=<same independent bearer token>
```

The token is stored only in root- or owner-readable environment files. It is
not committed, printed, sent to Telegram or iMessage, or included in service
status screenshots.

## Failure Behavior

The system fails closed:

- Photon unavailable: notification fails and approval is not actionable.
- Hermes gateway unavailable: no notification and no decision.
- Sign402 Gateway unavailable: plugin returns a fixed temporary error.
- Wrong sender: no decision.
- Expired approval: no decision.
- Duplicate decision: no decision.
- Missing identity link: no approval is created.
- Database error: no approval is created or consumed.
- Notification timeout: status becomes `delivery_failed`.
- LLM failure: approval state is unchanged.

No fallback channel can silently approve an iMessage approval.

## Concurrency

SQLite writes use explicit transactions and a process-local lock. Conditional
updates include the expected current status and expiry. Two simultaneous
decisions can therefore produce at most one successful transition.

Creating a new request first expires stale pending rows, then checks for an
existing live pending approval in the same transaction.

## Auditing

Audit events include:

- pairing created
- identity linked
- pairing rejected
- approval created
- approval delivered
- approval delivery failed
- approval approved
- approval denied
- approval expired
- duplicate or late decision rejected

Audit metadata may contain approval ID, action type, commitment hash, and
status. It must not contain plaintext phone numbers, pairing codes, tokens, or
private keys.

## Testing

Unit tests cover:

- E.164 validation and normalization
- pairing-code generation, digest storage, expiry, and single use
- one-to-one identity constraints
- encrypted phone storage
- canonical test commitment hashing
- one-pending-approval constraint
- exact `YES` / `NO` parsing
- unrelated conversational "yes" passing through when nothing is pending
- trusted Photon source capture
- pre-authorization pairing handling through Hermes `PairingStore`
- direct fixed hook responses through the active Photon adapter
- atomic decisions and replay rejection
- expiry and delivery-failure behavior
- notifier argument construction without a shell
- safe errors and secret redaction
- endpoint authentication
- backward compatibility for Telegram wallet commands

The deployment test is:

1. `/connect_imessage` in Telegram.
2. Send the pairing code through Photon iMessage.
3. Receive the fixed linked confirmation.
4. `/test_approval` in Telegram.
5. Receive the canonical test request in iMessage.
6. Reply `YES`.
7. Receive a fixed approved confirmation.
8. Verify the Gateway audit record.
9. Repeat and reply `NO`.
10. Verify that no wallet balance changed and no transaction was broadcast.

## Success Criteria

- A Telegram wallet owner can link one Photon iMessage identity.
- A different Photon sender cannot link or decide for that wallet.
- A test approval is delivered without LLM-generated content.
- Exact pending `YES` / `NO` replies are consumed before LLM dispatch.
- Conversational `yes` remains normal chat when no approval is pending.
- Decisions are atomic, expiring, and non-replayable.
- No private key or token crosses the messaging boundary.
- No Base transaction can be produced by this release.
