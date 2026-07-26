# Discard Legacy Fulfillment Tokens Design

**Date:** 2026-07-26  
**Status:** Approved in conversation and reviewed by the user

## Goal

Remove the three obsolete plaintext Bitrefill fulfillment tokens from the
production `user-purchases.json` store so the gateway's fail-closed write
preflight no longer blocks new purchases. Preserve all non-secret purchase
history.

## Context

The P0 sensitive-state hardening intentionally permits legacy plaintext
fulfillment tokens to be read, but refuses every update while any such token
remains in the store. Production currently has three purchase records with a
top-level `fulfillmentToken` and no `encryptedFulfillmentToken`. The gateway
therefore stops before approval, token transfer, or Bitrefill order creation
and reports:

```text
legacy plaintext fulfillment tokens must be migrated before updating user purchase state
```

The user confirmed that all three old purchases have already been redeemed and
that access to their old fulfillment data is no longer required.

## Considered Approaches

### 1. Discard only the obsolete token fields — selected

Remove `fulfillmentToken` and `encryptedFulfillmentToken` from every purchase
record while retaining the records and all other fields. This minimizes the
data changed, preserves non-secret history, and removes the material causing
the preflight failure.

### 2. Encrypt the legacy tokens

Migrate each plaintext token into `encryptedFulfillmentToken` using the
configured master key. This preserves old redemption access, but retains
bearer-value data that the user no longer needs and creates unnecessary key
and recovery obligations.

### 3. Delete the complete purchase store

Delete `user-purchases.json`. This also unblocks writes, but unnecessarily
removes non-secret purchase history and broadens the destructive scope.

## Architecture

Add a small operator-only Python command under the gateway package. The command
accepts the exact purchase-store path and supports two modes:

- Default dry run: parse and validate the complete document, report record and
  token-field counts, and make no filesystem changes.
- Explicit `--apply`: parse and validate the complete document, remove both
  fulfillment-token field formats from every record in memory, then persist
  the cleaned document with the existing `atomic_write_private_json` helper.

The command-line interface is:

```text
python -m sign402_gateway.discard_legacy_fulfillment_tokens \
  --path /absolute/path/to/user-purchases.json [--apply]
```

On success it emits one JSON object containing only `mode`, `records`,
`plaintext_token_records`, `encrypted_token_records`, `token_fields_removed`,
and `changed`. This makes the dry-run and post-apply checks machine-verifiable
without exposing purchase contents.

The command must never print token values, complete purchase records, or the
master key. It does not require `SIGN402_WALLET_MASTER_KEY` because the selected
operation discards tokens rather than decrypting or encrypting them.

The production runbook invokes the command only while `sign402-gateway` is
stopped. This prevents a concurrent purchase-state write from racing with the
one-time rewrite.

## Input Validation and Failure Handling

The operator command fails without modifying the file when:

- the target path does not exist or is a symlink;
- the document is not valid JSON;
- the top-level value is not an object;
- any purchase-record value is not an object;
- the atomic replacement cannot complete. In that case the original JSON
  bytes remain unchanged; the existing private-state helper may still tighten
  filesystem permissions before the failed replacement.

An empty store and a valid store with no token fields are safe no-ops, including
in apply mode; their bytes and timestamps remain unchanged. The command reports
counts only:

- total purchase records;
- records containing `fulfillmentToken`;
- records containing `encryptedFulfillmentToken`;
- total token fields that would be or were removed.

On success, the state directory remains mode `0700` and the state file remains
mode `0600`. No temporary plaintext copy is created by the command.

## Production Procedure

1. Verify the server checkout is at the exact reviewed commit.
2. Run the command in dry-run mode and require the expected production shape:
   three records with three plaintext token fields and zero encrypted token
   fields.
3. Stop `sign402-gateway` and confirm it is inactive.
4. Create a root-only temporary backup in `/var/backups/sign402` with directory
   mode `0700` and file mode `0600`. Do not display or log its contents.
5. Run the command with `--apply`.
6. Run the command again in dry-run mode and require zero plaintext and zero
   encrypted token fields.
7. Verify `user-purchases.json` is a regular file with mode `0600`.
8. Start `sign402-gateway`; require `active/running`, zero restart loops, a
   listening socket on `127.0.0.1:8099`, and an `ok` health response.
9. Delete the temporary backup after the cleaned state and healthy service are
   both verified. The backup deletion is intentional because the user
   explicitly chose to discard the obsolete bearer tokens.

If validation or the rewrite fails, leave the gateway stopped and preserve the
temporary backup. If the gateway cannot start after a successful rewrite,
restore the backup only as an explicit rollback, then keep purchases stopped
until the startup failure is diagnosed.

## Testing

Use standard-library `unittest` and real temporary files. Tests must prove:

- dry run reports exact counts and leaves bytes and timestamps unchanged;
- apply mode removes both token-field formats from all records while preserving
  unrelated nested data;
- apply mode writes a private file through an atomic replacement;
- malformed JSON, a non-object root, a non-object record, a missing file, and
  a symlink all fail without mutation;
- a clean or empty valid store is an idempotent no-op;
- command output and exceptions never contain marker token values.

The focused tests must be observed failing before implementation, then passing.
The complete gateway suite must pass before integration.

## Scope

Included:

- the operator command and automated tests;
- the production cleanup and service health verification;
- removal of the temporary root-only backup after success.

Excluded:

- initiating or approving a live Bitrefill purchase;
- changing the gateway's fail-closed policy;
- changing user spending limits;
- changing the Telegram message that currently says a purchase has started
  before backend preflight completes;
- deleting non-secret purchase history.

After deployment, the user can retry the purchase normally. That retry remains
subject to approval, balance, pricing, spending-limit, and Bitrefill checks.
