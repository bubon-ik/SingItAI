# P1a Exact Payment Approval Binding Design

Date: 2026-07-25

## Goal

Make every payment covered by this package provably identical to the payment
approved by Firefly, and make each approval usable for at most one signing
attempt.

This package closes two related findings:

1. the legacy `/approve-payment` and `/execute-payment` pair does not bind the
   supplied `paymentApprovalHash` to the requirements passed to the executor;
2. legacy CDP and Bankr x402 clients fetch a second `402 Payment Required`
   challenge after approval without proving that its signing terms match the
   first challenge.

The result must fail closed before signing whenever the approved terms cannot
be enforced.

## Constraints

- Build on commit `b91128d680d851c1ce19fe69332a951c707acda7`
  (`codex/p0-containment`).
- Keep the user's existing dirty `x402Bnkr` checkout untouched.
- Keep all legacy payment routes disabled by default and protected by the
  existing operator token when explicitly enabled.
- Do not read or modify live `.env` files, wallet keys, current SQLite data,
  ignored JSON state, or other production state.
- Tests must not call Bankr, CDP, a blockchain RPC, Firefly hardware, Telegram,
  WhatsApp, Photon, Bitrefill, or any paid resource.
- Do not add an unsafe compatibility flag that can bypass exact approval
  binding.
- Preserve the existing user-wallet Bitrefill flow. It does not use the
  autonomous Bankr x402 signer addressed by this package.

## Scope

### Included

1. A versioned, server-generated payment commitment that includes every field
   used to choose or sign an x402 payment.
2. A private durable payment-approval store with one-time execution semantics.
3. Server-side binding for `/approve-payment` and `/execute-payment`.
4. Exact pre-sign verification of the second CDP x402 challenge.
5. Fail-closed handling of autonomous Bankr x402 payment paths that cannot
   enforce the approved receiver, asset, and complete challenge.
6. Regression tests, security documentation, and API examples for the changed
   legacy contract.

### Excluded

- Atomic daily-cap or policy-budget reservations. Those are P1b.
- Settlement or on-chain reconciliation of an ambiguous signing attempt.
- Bulk migration of existing plaintext state.
- Stale iMessage decisions, Firefly bridge authentication, paid resource replay,
  risk-check validation, private-key CLI arguments, and response-size caps.
- Redesign of Bitrefill quotes, managed-wallet funding, Bankr swaps, or Bankr
  LLM-credit purchases that do not use the autonomous Bankr x402 client.
- Re-enabling any legacy route by default.

## Threat Model

The attacker may control a paid resource and may return different x402
requirements on consecutive requests. The attacker may also possess the
legacy operator API token, send concurrent execute requests, replay requests
after restart, or substitute syntactically valid payment requirements and a
random 64-character hash.

The attacker must not be able to:

- make the signer pay a different amount, receiver, asset, network, resource,
  method, body, timeout, scheme, or signing extension than Firefly approved;
- execute without a known, unexpired, approved server-side authorization;
- use one authorization for more than one signer invocation;
- cause an automatic retry after the signer may already have submitted a
  transaction.

Compromise of a wallet private key, Firefly firmware, the host operating
system, or the configured operator token remains outside this package.

## Considered Approaches

### 1. Extend the existing amount caps

Pass `maxAtomic`, `expectedReceiver`, and `expectedAsset` to every payment
client.

This is the smallest patch, but it remains unsafe. It does not bind the
resource, request body, network, scheme, timeout, signing extensions, or the
low-level `/execute-payment` request. A maximum also permits a different lower
amount when the approved scheme is exact.

### 2. Durable approval plus exact pre-sign verification

Generate the commitment on the server, persist an immutable approval, claim it
atomically before the signer call, and require the signer boundary to compare
the live challenge with the approved fingerprint.

This is the selected approach. It preserves the two-step debug API while
closing substitution, replay, concurrency, and second-challenge TOCTOU gaps.

### 3. Remove the two-step API

Combine Firefly approval and payment execution into one internal synchronous
operation and delete `/approve-payment` and `/execute-payment`.

This gives a smaller public surface, but it breaks the documented Hermes/debug
workflow and still requires exact verification in payment clients that refetch
the resource. It is not necessary for this remediation.

## Architecture

### Payment terms version 2

Add one canonical builder used by the gateway and the Node CDP guard. Its
logical output is:

```json
{
  "type": "sign402-payment",
  "version": 2,
  "paymentKind": "x402",
  "policyHash": "<64 lowercase hex>",
  "x402Version": 2,
  "scheme": "exact",
  "network": "eip155:8453",
  "signerBackend": "cdp-managed",
  "payer": "0x3333333333333333333333333333333333333333",
  "asset": "0x1111111111111111111111111111111111111111",
  "amountMode": "exact",
  "amountAtomic": "1000",
  "receiver": "0x2222222222222222222222222222222222222222",
  "resource": "https://merchant.example/paid?item=1",
  "httpMethod": "GET",
  "requestBodySha256": "<64 lowercase hex>",
  "maxTimeoutSeconds": 60,
  "extra": {},
  "paymentIntent": "x402:<64 lowercase hex>",
  "purpose": "x402_api_access"
}
```

The commitment hash is SHA-256 over UTF-8 canonical JSON with sorted object
keys and compact separators.

Canonicalization rules are part of the security contract:

- `policyHash` is lowercase and must be exactly 32 bytes of hex.
- `paymentKind` is either `direct` or `x402`; there is no heuristic conversion
  between the two after approval.
- For `x402`, `x402Version` must be the integer `2`, `scheme` must be `exact`,
  and `network` is the canonical CAIP-2 network from `x402Network`. The
  gateway's internal aliases such as `base-mainnet` are not signed.
- For the preserved direct Algorand debug executor, `paymentKind` is `direct`,
  `x402Version` is `null`, and `scheme` is `direct`.
  `algorand-testnet` maps only to the repository's existing Algorand TestNet
  CAIP-2 identifier. No other implicit direct-network mapping is accepted.
- `signerBackend` and `payer` identify the exact configured signer. The direct
  Algorand and AVM factories expose their configured sender. Legacy CDP
  approval requires `CDP_EVM_ACCOUNT_ADDRESS`, and Node verifies that the
  resolved CDP account equals it. A managed user-wallet approval uses the
  wallet's stored public address and Node verifies the private key derives that
  same address. A changed or unavailable signer identity fails before approval
  or execution.
- EVM addresses and EVM token contract addresses are lowercase. Case-sensitive
  non-EVM identifiers retain their validated form.
- `amountAtomic` is a positive base-10 integer string without a sign or leading
  zeroes. `amountMode` is `exact`; a lower amount is not an equivalent payment.
- For `x402`, `resource` is the actual non-redirected request URL. It has a
  lowercase scheme and IDNA ASCII host, no credentials or fragment, no dot
  segments, no default port, an explicit `/` for an empty path, uppercase
  percent escapes, and otherwise preserves its ASCII path and query bytes.
  HTTPS is required except for explicitly supported loopback test URLs.
  A candidate-declared `resource`, when present, must canonicalize to this same
  URL. It is never replaced by the approved URL before comparison.
- For `direct`, `resource` is the existing validated logical resource/path,
  `httpMethod` is `DIRECT`, and no transport request is made.
- Covered x402 payment execution is GET-only in P1a. `httpMethod` is `GET` and
  `requestBodySha256` is the fixed SHA-256 digest of an empty byte sequence.
  Any POST/body is rejected before approval. Supporting a body-bearing payment
  requires a separate design that carries and re-hashes the exact transmitted
  bytes.
- `maxTimeoutSeconds` is a positive integer or `null`.
- `extra` is fully bound but uses a deliberately restricted canonical JSON
  profile: object keys and string values are printable ASCII, keys match
  `[A-Za-z0-9_.:-]+`, numbers are integers within JavaScript's safe integer
  range, and floats are rejected. Lists, objects, booleans, and null are
  allowed recursively. Duplicate keys are rejected while parsing. This makes
  Python and Node sorting and serialization byte-identical without relying on
  implementation-specific Unicode or number behavior.
- For direct payments, `paymentIntent` is an explicit non-empty validated
  requirement field. For x402, it is
  `x402:` plus SHA-256 of the canonical live signing fields and effective
  request URL; both Python and Node derive it. An untrusted arbitrary local
  intent is not copied into the live comparison.
- `purpose` is server-owned route/tool policy context, not a challenge field.
  The approved purpose is carried unchanged into the live fingerprint and is
  revalidated against policy; a caller cannot select it freely.
- Duplicate transport copies such as `originalPaymentRequirements`,
  `sourceFormat`, and the gateway's internal network alias are not hashed.

All other strings in the commitment must be printable ASCII or a separately
validated address/identifier with the same byte representation in both
languages. Canonical objects use lexicographically sorted ASCII keys, compact
separators, lowercase JSON literals, and UTF-8 encoding.

Python and Node use fixtures containing the same canonical JSON and expected
hash, including adversarial unsafe integers, floats, Unicode, URL encodings,
duplicate keys, and nested `extra` values. Cross-language fixture equality is
required before the CDP guard can be considered safe.

### Durable approval store

Add `sign402_gateway.payment_approvals.PaymentApprovalStore`, backed by a
dedicated SQLite file. The runtime default is
`demo-dashboard/payment-approvals.sqlite3`, configurable through the existing
gateway construction pattern for tests and deployment.

The implementation adds this exact runtime path to `.gitignore` before any
runtime can create it. Tests verify that the default file is ignored.

The parent directory is private, the database is mode `0600`, and SQLite
sidecar files are created only inside the same private directory. Store
initialization or write failure prevents approval and execution.

Each row contains:

- opaque random `approval_id`;
- commitment version, commitment hash, policy hash, canonical JSON, and the
  normalized payment requirements needed by the executor;
- signer backend and payer identity;
- for AVM only, the validated selected raw x402 requirement needed by the
  official signing library, capped at 64 KiB and stored as canonical JSON
  rather than as the arbitrary provider response;
- `pending`, `approved`, `denied`, `executing`, `completed`, `expired`,
  `cancelled_before_sign`, or `outcome_unknown` status;
- created, expiry, decision, claim, and completion timestamps as applicable;
- the allowlisted Firefly decision metadata;
- a random execution-attempt ID once claimed;
- an allowlisted payment receipt after completion;
- a non-sensitive failure code for an unknown outcome.

The store never persists private keys, mnemonics, fulfillment tokens,
redemption data, request authorization headers, arbitrary provider response
bodies, or raw subprocess output.

The approval lifetime is 120 seconds. Expired approvals cannot be revived.
Creating a new approval for identical terms creates a new `approvalId`; a hash
alone is never an execution credential.

### Approval state machine

All transitions use SQLite transactions and compare-and-swap predicates:

```text
pending -> approved | denied
approved -> executing
executing -> completed | cancelled_before_sign | outcome_unknown
pending | approved -> expired
```

Rules:

- `approved -> executing` is one SQL compare-and-swap that requires
  `status = 'approved'` and `expires_at > transaction_now`. Expiry is not a
  separate read/check.
- Exactly one concurrent caller can claim an approval.
- A duplicate request for a `completed` approval returns the stored allowlisted
  receipt with `replayed: true`; it does not call the signer.
- `executing` and `outcome_unknown` return a conflict and never call the signer.
- Any exception after control is handed to the signer becomes
  `outcome_unknown`, even when the error text suggests a pre-sign failure.
  Conservative ambiguity is preferable to a duplicate payment.
- All validation that can be completed without the signer occurs before the
  claim. A validation failure leaves the approval unclaimed until it expires.
- An execution lease is five minutes, longer than every covered client timeout
  plus a safety margin. On startup and before a claim/read decision,
  `executing` rows older than the lease are atomically changed to
  `outcome_unknown`. A younger execution is never reclassified by another
  process. P1a does not automatically reconcile or retry stale attempts.
- Claim creates a random `executionAttemptId`. Completion, cancellation, and
  unknown-outcome updates require both `status = 'executing'` and the same
  attempt ID. A late process cannot overwrite a row recovered by another
  process.
- `cancelled_before_sign` is allowed only in gateway code that has not yet
  invoked an executor/payment-client function. It is terminal and requires a
  new approval.
- No administrative force-retry endpoint is added in P1a.

### Approval service

Add a small `PaymentApprovalService` boundary. It owns commitment creation,
Firefly/iMessage approval persistence, claim/finalize operations, the final
pause check, and receipt allowlisting. HTTP handlers and the covered internal
x402 buyers call this service instead of coordinating the store directly.

The service has no wallet key and does not sign. The payment executor receives
only normalized requirements read back from the claimed approval row.

Before contacting Firefly, the service formats exactly three mandatory
hardware-context lines within the existing 31-character limit:

1. exact amount, asset symbol/address abbreviation, and network;
2. abbreviated payer to abbreviated receiver;
3. `DIRECT <resource>` or `GET <resource-host/path-prefix>`.

The Firefly screen continues to show the commitment hash separately. Every
abbreviation has deterministic first/last characters from the fully bound
value. If amount, network, payer, receiver, or resource context cannot be
represented safely, approval fails closed. Caller-provided marketing/tool
context cannot replace these security lines.

## API Data Flow

### `POST /approve-payment`

The legacy operator guard and transaction-pause guard remain mandatory.

The request must include:

```json
{
  "paymentKind": "direct",
  "policyHash": "<64 hex>",
  "paymentRequirements": {},
  "paymentHash": "<optional compatibility assertion>"
}
```

Processing order:

1. authenticate the legacy operator and validate the current policy;
2. resolve the server-configured signer identity, normalize requirements, and
   build the version 2 commitment;
3. if `paymentHash` is present, require it to equal the server-generated hash;
4. persist a `pending` row before contacting Firefly;
5. ask Firefly to approve the server-generated hash and exact human context;
6. persist `approved` only when Firefly returns `approved: true` and an
   `approvedHash` exactly equal to the server-generated commitment hash;
   denial, missing hash, stale hash, or provider error is persisted as
   non-executable `denied`;
7. return `approvalId`, `paymentApprovalHash`, canonical commitment,
   `expiresAt`, and the allowlisted Firefly result.

A caller-supplied `paymentCommitment` may be accepted only as another equality
assertion of the complete version 2 object. It is never the source of trusted
terms. Requests that provide only the old caller-generated hash/commitment and
omit `paymentRequirements` are rejected.

For compatibility, omitted `paymentKind` is treated as `direct` only when the
requirements exactly match the existing `algorand-testnet`/`ALGO_TEST` direct
executor shape and contain no x402 protocol fields. All x402 requests require
an explicit `paymentKind: "x402"`.

### `POST /execute-payment`

The request must include `approvalId`. Existing `policyHash`,
`paymentApprovalHash`, or `paymentRequirements` fields are optional
compatibility assertions; if supplied, each must exactly match the stored
authorization.

Processing order:

1. authenticate the legacy operator and apply the transaction-pause guard;
2. read the approval and reject unknown, denied, or expired IDs;
3. validate any compatibility assertions;
4. re-resolve and compare the current signer backend and payer, then revalidate
   that the stored policy still permits the stored requirements;
5. atomically claim `approved -> executing`;
6. perform the final transaction-pause check and, if paused, persist
   `cancelled_before_sign` without invoking the executor;
7. invoke the executor with only the stored requirements and policy hash;
8. allowlist the receipt and persist `completed`, or persist
   `outcome_unknown` on any exception once step 7 has been invoked.

The response preserves `policyHash`, `paymentApprovalHash`, and `payment` and
adds `approvalId`, `status`, and `replayed`.

Status mapping:

- malformed input or assertion mismatch: `400`;
- unknown approval: `404`;
- expired approval: `410`;
- denied, cancelled-before-sign, executing, or outcome-unknown approval: `409`;
- unavailable approval store: `503`;
- first successful execution or completed replay: `200`.

Pause activation fences requests that have not reached step 7. Once the
executor/payment-client function has been invoked, the payment is in flight and
cannot be cancelled by changing the environment flag; failures are handled as
an unknown outcome. Tests switch pause state after claim but before the final
check and require zero signer calls.

### Internal legacy x402 buyer

`ExternalX402Buyer` builds version 2 terms from the first challenge, obtains a
durable Firefly approval through `PaymentApprovalService`, and claims it before
entering any payment-capable client.

The AVM branch continues to sign the first selected challenge directly. Before
approval, the gateway validates and stores an immutable 64-KiB-capped raw
selected requirement plus x402 version. After claim it reconstructs the
official `PaymentRequired` envelope only from that stored selection, and
receives the claimed normalized requirements rather than a mutable caller
object.

The CDP branch passes the complete approved terms and hash to the Node process.
The Python buyer accepts success only when the Node result reports the same
selected commitment hash.

## CDP Exact Pre-Sign Guard

Extend the Node `paymentRequirementsSelector` from three caps to an exact
fingerprint comparison.

The CDP subprocess receives approved terms over stdin, not as wallet secrets or
large JSON command-line arguments. The Node process:

1. validates the stdin envelope and recomputes the approved hash;
2. verifies that the resolved signer backend and payer equal the approved
   identity;
3. configures fetch to reject redirects during the payment attempt;
4. defines the effective resource as the actual non-redirected request URL,
   then separately requires any candidate-declared resource to canonicalize to
   that URL before building the candidate terms;
5. converts every candidate in the live second challenge to version 2 terms;
6. selects only a candidate whose canonical JSON and hash exactly equal the
   approved values;
7. wraps the signer with an attempt counter and throws before a second signing
   invocation inside the same paid fetch, including after an HTTP failure;
8. throws before payload creation/signing when no exact candidate exists;
9. returns the selected hash, signer invocation count, and allowlisted payment
   receipt.

Post-payment hash comparison in Python is defense in depth. It does not replace
the selector check before signing.

The current user-wallet CDP call also moves to the same exact guard and durable
execution state. `PaymentApprovalService` creates a pending payment row before
requesting iMessage approval. The iMessage envelope replaces its current
reduced `paymentRequirements` snapshot with the same complete version 2 terms
and continues to bind its wallet address, nonce, and expiry. It returns both
the iMessage approval ID and embedded payment-terms hash. The payment service
marks its row approved only when that terms hash matches, then atomically
claims it before `UserWalletX402Buyer` calls Node.

The user event records the channel approval ID and the separate payment
authorization ID. Completed, concurrent, stale, and unknown user-wallet
attempts follow the same durable state machine as Firefly approvals. This
change is approval binding, not the separate stale-iMessage-decision
remediation.

P1a rejects every body-bearing x402 purchase before approval. The current CDP
implementation remains GET-only until a later design can bind the exact bytes
sent by both the initial request and signer-controlled refetch.

## Bankr x402 Fail-Closed Boundary

The installed Bankr CLI accepts `--max-payment`, but the current integration
has no pre-sign callback that can enforce the approved receiver, asset,
network, and complete challenge. Post-payment inspection of `paymentMade` is
too late.

Therefore P1a intentionally makes these autonomous Bankr x402 paths
non-payment-capable:

- the SINGIT branch of `ExternalX402Buyer`;
- the operator-only `BitrefillPurchaseRunner` path wired to
  `BankrCliX402PaymentClient`.

They fail before Firefly approval and before invoking the CLI with a clear
`exact approval binding is unavailable for Bankr x402` error. The existing
legacy opt-in flag cannot override this boundary.

The managed user-wallet Bitrefill purchase/funding path, Bankr price and swap
queries, and non-x402 Bankr LLM-credit operations are not disabled by this
decision.

Bankr x402 may be restored only in a separate reviewed change that supplies a
signer-controlled pre-sign selector with the same version 2 fixture tests.

## Error Handling and Recovery

- Commitment or policy validation fails before Firefly is contacted.
- Missing or changed signer identity fails before Firefly or signer invocation.
- Approval-store failure fails closed before Firefly or signer invocation.
- Firefly rejection is durably recorded and cannot be executed.
- A changed CDP challenge is a pre-sign rejection. Because the approval is
  already claimed when the external client is entered, it is conservatively
  recorded as `outcome_unknown`; it is never reused automatically.
- A process crash after claim is recovered as `outcome_unknown` on startup.
- A late completion from the crashed/stale attempt cannot overwrite recovery
  because finalization is fenced by `executionAttemptId`.
- Executor output is validated against stored receiver, asset, amount,
  network, intent, and policy hash before a `completed` receipt is persisted.
  Missing or contradictory receipt fields produce `outcome_unknown`.
- Error responses and logs include approval IDs and non-sensitive reason codes,
  but never raw wallet material or arbitrary subprocess output.

P1a deliberately prefers a blocked approval and manual investigation over any
automatic retry with an uncertain settlement state.

## Compatibility

- Legacy routes remain disabled by default.
- The two-step API remains available to explicitly enabled operator tooling,
  but its request contract now requires server-verifiable requirements and an
  opaque `approvalId`.
- `paymentApprovalHash` remains in responses and payment proofs for dashboard
  compatibility; it is no longer accepted as an execution credential.
- There is no migration of historic approvals because the old code did not
  persist a trustworthy payment authorization. Every execution after upgrade
  requires a new version 2 approval.
- Existing version 1 commitment fixtures remain readable only in historical
  events and documentation. They cannot authorize a new payment.
- Documentation must state that the operator Bankr x402 paths are intentionally
  unavailable until an exact pre-sign integration exists.

## Testing

All tests use temporary files and mocked signers/providers.

### Canonical commitment

- deterministic Python vectors for every field and normalization rule;
- matching Node vectors for the same canonical JSON and SHA-256 hash;
- mutation of each bound field changes the hash;
- changed signer backend or payer changes the hash and is rejected before
  execution;
- floats, credentials in URLs, fragments, unsupported methods/schemes,
  malformed amounts, and non-JSON `extra` values are rejected;
- any request body or non-empty-body digest is rejected in P1a.

### Approval store and service

- private directory/database permissions;
- pending-to-approved and pending-to-denied persistence;
- unknown, expired, denied, cancelled-before-sign, executing, and
  outcome-unknown rejection;
- two threads using independent SQLite connections can produce exactly one
  successful claim;
- the claim SQL rejects an approval expiring at the transaction-time boundary;
- completed replay returns one stored receipt and never invokes the signer;
- executions older than the five-minute lease become outcome-unknown after
  restart or the next read, while younger executions remain executing;
- a stale attempt ID cannot complete or cancel a recovered row, including a
  hard-kill simulation after signer return but before receipt persistence;
- store or Firefly failure never invokes the executor;
- Firefly `approvedHash` mismatch is persisted non-executable;
- no forbidden secret keys or raw provider bodies are persisted.

### HTTP handlers

- `/approve-payment` recomputes the hash and rejects a mismatched caller hash or
  commitment;
- `/execute-payment` rejects a random hash, missing approval ID, changed policy,
  changed requirements, expiry, and replay in progress;
- the executor receives only stored requirements;
- successful duplicates return the cached receipt;
- a mocked post-claim exception records outcome-unknown and a retry never calls
  the executor;
- pause activation after claim but before the final handoff check produces
  `cancelled_before_sign` and zero signer calls;
- legacy guards and the global transaction pause remain effective.

### CDP and Bankr

- CDP accepts an identical second challenge;
- changes to version, scheme, network, asset, amount in either direction,
  receiver, timeout, extra, URL, method, or the fixed empty-body digest fail
  before the mocked signer;
- redirects fail before signing;
- Python rejects a missing or mismatched selected commitment hash;
- AVM signing reconstructs only the size-capped stored selected requirement,
  not a mutable provider response;
- user-wallet iMessage approval embeds the complete version 2 terms and its
  returned terms hash must match the Node guard envelope;
- user-wallet execution uses the common durable claim and replay/unknown
  semantics;
- user-wallet CDP uses the same complete guard and rejects POST before approval;
- both autonomous Bankr x402 entry points fail before Firefly and before the
  CLI mock;
- unrelated Bankr and managed-wallet Bitrefill tests remain passing.

### Verification

Run focused Python and Node suites first, then every existing repository test
suite. The final verification must also scan the P1a diff for private-key,
mnemonic, bearer-token, redemption, placeholder, and accidental live-state
material.

## Success Criteria

P1a is complete only when:

1. no covered signer can be invoked with caller-supplied or second-challenge
   terms, signer backend, or payer that differ from the approved version 2
   commitment;
2. one `approvalId` causes at most one signer invocation across concurrency and
   restart;
3. ambiguous attempts cannot be retried automatically;
4. Bankr x402 paths without an exact pre-sign guard cannot move funds;
5. legacy routes remain disabled by default;
6. focused and full offline test suites pass;
7. documentation describes the new contract and intentional Bankr boundary;
8. no live credential, wallet, payment, provider, or production-state action
   occurs during implementation or verification.
