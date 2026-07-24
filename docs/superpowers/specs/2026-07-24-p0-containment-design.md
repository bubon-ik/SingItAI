# P0 Containment Design

Date: 2026-07-24

## Goal

Contain the highest-risk security findings without changing live credentials,
the existing Bitrefill SQLite database, or other production state during
development.

This package must:

- make newly written gateway state private by construction;
- prevent plaintext Bitrefill bearer values and redemption material from being
  persisted;
- make the incident kill switch cover every transaction-oriented gateway
  route from one authoritative location;
- preserve the existing user-facing API and test mode;
- add regression tests that perform no network calls or real payments.

This is the first of several remediation packages. Durable payment
idempotency, settlement reconciliation, exact approval binding, backup/restore,
CI, and deployment hardening remain separate follow-up designs.

## Constraints

- Do not read, rewrite, chmod, migrate, rotate, or otherwise modify the real
  ignored `.env` files, current `demo-dashboard/bitrefill-orders.sqlite3`,
  wallet keys, or live state as part of implementation or tests.
- Preserve all pre-existing uncommitted user changes in the working tree.
- Reuse `SIGN402_WALLET_MASTER_KEY`, which is already required for managed
  wallets, iMessage identities, and Bankr API keys. Do not introduce a second
  encryption key.
- Never fall back to plaintext persistence when the master key is absent or
  invalid.
- Keep legacy payment routes disabled by default.
- No automated test may call Bitrefill, Bankr, CDP, a blockchain RPC, Firefly
  hardware, Telegram, WhatsApp, or Photon.

## Scope

### Included

1. Private directory, file, and SQLite permissions for gateway state.
2. Encrypted persistence of new `fulfillmentToken` values.
3. Encrypted persistence of new Bitrefill recipient data.
4. Allowlisted, non-secret Bitrefill provider snapshots that exclude
   redemption and activation material.
5. Protected on-demand redemption refresh that keeps redemption in memory
   only.
6. A centralized transaction-route kill switch.
7. Unit and gateway regression tests for all of the above.
8. Documentation of the compatibility and deployment boundary.

### Excluded

- Bulk migration or deletion of existing plaintext rows and JSON values.
- Changes to real file permissions or credential rotation.
- Payment-intent reservations, transaction idempotency, invoice recovery, or
  on-chain reconciliation.
- Daily-cap accounting changes.
- Legacy approval/policy redesign.
- Backup contents, offsite storage, or restore automation.
- Firefly bridge, iMessage approval, paid risk-check, packaging, and CI fixes.

## Architecture

### Private state I/O

Add a focused `sign402_gateway.secure_state` module. It owns filesystem safety
instead of duplicating `mkdir`, temporary-file, and `replace` sequences across
stores.

The module exposes:

```python
def ensure_private_directory(path: Path) -> None: ...
def ensure_private_file(path: Path) -> None: ...
def atomic_write_private_json(path: Path, payload: Mapping[str, Any]) -> None: ...
```

Required behavior:

- parent directories are created with mode `0700`;
- a pre-existing parent used for sensitive state is repaired to `0700`;
- temporary files are created with mode `0600`, independent of process umask;
- the final file is mode `0600` after `os.replace`;
- a pre-existing sensitive file is repaired to `0600`;
- JSON output is UTF-8, deterministic enough for existing tests, and ends with
  a newline;
- permission or replacement failures propagate; callers must not continue
  after an unsafe write;
- writes stay atomic and never expose a partially written JSON document.

`UserPurchaseStore` and `UserSpendLimitStore` use this helper for every write.
`BitrefillCommerceStore` creates its parent privately and enforces `0600` on
the SQLite file after initialization and after opening a newly created
database. Existing transaction and locking behavior remains unchanged.

The implementation tests these rules only with temporary paths. Merely
developing and running the test suite must not touch the repository's real
state files.

### Sensitive state cipher

Add a small `SensitiveStateCipher` in the same module, backed by
`cryptography.fernet.Fernet` and the existing
`SIGN402_WALLET_MASTER_KEY`.

It exposes explicit string and JSON operations:

```python
class SensitiveStateCipher:
    def encrypt_text(self, value: str) -> str: ...
    def decrypt_text(self, value: str) -> str: ...
    def encrypt_json(self, value: Mapping[str, Any]) -> str: ...
    def decrypt_json(self, value: str) -> dict[str, Any]: ...
```

Construction with an invalid key fails with a configuration error that names
the environment variable but never includes the supplied key. Decryption
failure returns no partial value and raises a redacted state error.

### User purchase persistence

`UserPurchaseStore` receives an optional `SensitiveStateCipher`. A
catalog/test-only server may start without a master key, but any write
containing a fulfillment token and any read of an encrypted token fails
closed when the cipher is absent. A non-empty invalid key still fails during
gateway construction.

For new writes:

- remove top-level `fulfillmentToken` from the persisted event;
- store its Fernet envelope as `encryptedFulfillmentToken`;
- return the original in-memory event to the caller so the HTTP contract does
  not change;
- never serialize the plaintext token, including in temporary files.

For reads:

- decrypt `encryptedFulfillmentToken` into an in-memory
  `fulfillmentToken`;
- do not expose the encrypted field to callers;
- accept an existing legacy plaintext `fulfillmentToken` for compatibility,
  but do not rewrite the file automatically;
- do not accept a malformed encrypted envelope as if the token were absent.

`clear_fulfillment_token()` removes both the encrypted and legacy field. It
uses the private atomic writer, so clearing a token cannot reset the file to
world-readable permissions.

Because `user-purchases.json` contains all users in one JSON object, a normal
write would reserialize every legacy plaintext token through the temporary
file. Therefore every write scans the complete post-update document and fails
closed if any top-level legacy `fulfillmentToken` remains. Clearing a legacy
token is allowed only when the resulting document contains no other legacy
plaintext tokens. This keeps legacy reads available without silently migrating
or re-persisting bearer values.

This package intentionally does not bulk-migrate the existing
`user-purchases.json`. A later controlled operational step will migrate it
after a private backup and validation.

### Bitrefill commerce persistence

The commerce store remains the durable order state machine, but it stops being
a storage location for provider secrets.

Provider results are converted at the persistence boundary into a non-secret
snapshot. The snapshot is an allowlist containing only values needed for
status and refresh:

- invoice ID;
- order ID;
- normalized status;
- product and package identifiers already present in the quote;
- payment method;
- non-secret treasury transaction hash, network, asset, and amount;
- bounded timestamps and provider state needed for polling.

The snapshot must exclude:

- `redemption`;
- gift-card codes and PINs;
- activation or claim URLs;
- eSIM activation data;
- payment links;
- provider credential material;
- arbitrary unrecognized provider fields.

The sanitizer applies both to the initial successful purchase result and every
refreshed result before `advance_state()` writes metadata. Tests inspect the
raw SQLite JSON and temporary files, not only public return values.

New recipient dictionaries are encrypted with `SensitiveStateCipher` and
stored as `encryptedRecipient`. Store reads decrypt them into the current
in-memory `recipient` shape. Existing legacy plaintext `recipient` metadata is
read for compatibility but is not rewritten automatically. Any state update
for a row that still contains a legacy plaintext recipient fails closed before
SQLite writes or journals that metadata again. The controlled migration must
encrypt such rows before normal state transitions resume.

`fulfillmentTokenHash` remains a one-way SHA-256 value and does not need
encryption.

### Protected redemption refresh

`lookup_bitrefill_order()` continues to require either the exact stored
recipient or a valid fulfillment token before revealing redemption.

When `include_redemption=True` and authorization succeeds:

1. Use the persisted non-secret invoice snapshot to call
   `refresh_purchase()`.
2. Read redemption from the provider response in memory.
3. Persist only the sanitized provider snapshot.
4. Return redemption to the authorized caller.
5. Have the authenticated gateway handler clear the encrypted fulfillment
   token only after a non-empty redemption value was returned.

An unauthenticated status lookup never asks the provider for redemption.

If provider refresh fails or returns no redemption:

- return a non-secret processing/unavailable result;
- keep the encrypted fulfillment token so the user can retry;
- do not claim that delivery succeeded;
- do not persist exception text containing provider data.

This design deliberately favors recoverable on-demand retrieval over storing
gift-card value locally.

### Centralized kill switch

Replace scattered handler guards with one authoritative transaction-route
classification in `Sign402GatewayHandler.do_POST`.

The paused route set is:

```text
/approve-payment
/execute-payment
/agent/buy-probe
/agent/buy-tool
/agent/buy-x402
/agent/top-up-llm-credits
/agent/buy-bitrefill
/agent/buy-wallet-bitrefill
/agent/withdraw
/agent/llm-key/start
/agent/llm-key/verify
/agent/llm-key/reconcile
/internal/fulfill-bitrefill
```

After validating `Content-Length` and resolving the request path, but before
reading the body, acquiring the Firefly lock, prompting for approval,
decrypting a wallet key, or invoking any payment client, `do_POST` returns:

```json
{
  "ok": false,
  "paused": true,
  "telegramText": "⏸️ Purchases are temporarily paused for maintenance. Please try again later."
}
```

with HTTP `503`.

The following remain available while paused:

- health and catalog reads;
- Bitrefill search, details, and quote;
- wallet and balance reads;
- last-purchase/status reads;
- spending-limit reads and updates;
- withdrawal-token inventory;
- LLM credit balance;
- approval-channel and messaging administration;
- legacy inspect endpoints;
- authenticated internal settlement preparation (it validates and returns
  bounds but does not execute a transfer or provider purchase).

The route set is a conservative containment boundary. In particular,
`llm-key/reconcile` is blocked because its current state-dependent behavior can
resume a transfer. A later reconciliation design may split observation from
fund movement and make the read-only portion available during incidents.
The legacy internal fulfillment route is also blocked because, when explicitly
enabled, it can invoke the treasury-funded Bitrefill purchase runner.

### Configuration

`SIGN402_PURCHASES_PAUSED` keeps the existing accepted true values:

```text
1, true, yes, on
```

All other values mean unpaused. The existing environment variable name and
user message remain unchanged.

The gateway builder passes `SIGN402_WALLET_MASTER_KEY` to the sensitive stores.
Catalog-only and other non-sensitive test components do not need a cipher.
Any configured managed-wallet purchase path that could persist bearer values
fails closed before approval, wallet decryption, funding, or provider calls
when the key is missing or invalid.

## Error Handling

- Unsafe file permissions or failed atomic replacement are fatal to the
  current write.
- Invalid encryption configuration fails before a sensitive value can be
  written.
- Invalid ciphertext fails closed; it is never treated as an empty token or
  recipient.
- Provider errors are redacted before reaching logs or public responses.
- Kill-switch responses do not read or echo request bodies.
- No error path falls back to plaintext state.

## Testing Strategy

Implementation follows test-first development.

### Secure state tests

Use a temporary directory and temporarily set process umask to `022`.

Verify:

- parent directory mode is `0700`;
- temporary and final JSON files are `0600`;
- replacing an existing file preserves `0600`;
- a deliberately permissive existing file is repaired;
- a write failure leaves the previous valid document intact;
- ciphertext round-trips;
- invalid keys and ciphertext fail with redacted messages.

### Store tests

Verify:

- raw `user-purchases.json` contains ciphertext but not the fulfillment token;
- reads return the original in-memory token;
- legacy plaintext entries can be read without automatic rewrite;
- clearing removes encrypted and legacy token fields;
- raw SQLite metadata contains no redemption, code, PIN, activation data,
  payment link, or plaintext recipient;
- encrypted recipients round-trip;
- legacy plaintext recipients remain readable;
- SQLite and its parent have private modes under umask `022`;
- authenticated redemption refresh returns the code but the database remains
  free of it;
- a failed refresh preserves the reveal token.

### Kill-switch tests

Use a table-driven gateway test over every paused route. For each route, assert:

- HTTP `503`;
- `paused=true`;
- the route-specific service, wallet decryptor, payment client, Firefly
  approval client, and transfer client are not called.

Use a second table for representative allowed routes and assert their normal
status rather than `503`.

The tests must include the previously missed paths:

- Bankr LLM OTP verify followed by resume;
- LLM reconcile;
- legacy `/execute-payment`;
- legacy treasury tool/x402 purchases;
- legacy Bitrefill purchase.
- legacy internal Bitrefill fulfillment.

### Regression

Run:

- the focused secure-state, commerce, Bitrefill runner, and gateway tests;
- all gateway tests;
- all repository Python and Node test suites.

No test uses real credentials, hardware, network, or funds.

## Deployment Boundary

Completion of this code package does not authorize deployment or live-state
migration.

Deployment is a later explicit operator action:

1. Stop fund-moving traffic externally.
2. Take and verify a private backup.
3. Deploy the tested code.
4. Run a separately reviewed migration for legacy plaintext state.
5. Validate permissions and scan only for field presence, never secret values.
6. Resume traffic after smoke tests.

Credential rotation is required only if the operator determines that another
local user could have read the current `0644` files. Rotation is not automated
by this package.

## Completion Criteria

The package is complete when:

1. New sensitive JSON and SQLite state is private under umask `022`.
2. New fulfillment tokens and recipients are encrypted at rest.
3. No new Bitrefill redemption or activation value can enter SQLite or a
   temporary state file.
4. Authorized redemption still works through an on-demand provider refresh.
5. Every listed transaction route returns `503` before side effects while the
   kill switch is enabled.
6. Read-only and administrative routes remain available while paused.
7. Focused and full repository test suites pass without network or real
   payments.
8. The real ignored env files, current SQLite database, keys, and live state
   remain unchanged during development and testing.
