# Local Trezor Sidecar for Base USDC Bitrefill Purchases

## Summary

Build an isolated local proof of concept in which a user's Base wallet is a
physical Trezor, private keys never leave the device, and a Bitrefill purchase
requires hardware approval. The proof runs beside the existing Sign402 stack
and does not change the production managed-wallet, iMessage, WhatsApp, Hermes,
gateway, systemd, environment, or database paths.

The proof has two new processes:

```text
trezor-poc-runner
    -> obtains and validates a Bitrefill quote and invoice
    -> requests a purchase-intent signature and payment from the sidecar

trezor-sidecar (127.0.0.1:8111)
    -> connects to Trezor Suite MCP (127.0.0.1:21340/mcp)
    -> binds one Base address to the local test user
    -> obtains physical approval and a signature from the Trezor
    -> verifies the signed transaction before broadcasting it
```

This proof is limited to one local test user, Base Mainnet, and native USDC.
It is not deployed to production and is not reachable from Telegram, Hermes,
or the existing public gateway.

## Goals

- Keep the Base private key exclusively on the user's Trezor.
- Bind the proof to one Base address verified on the device screen.
- Require physical Trezor approval for the purchase intent before creating a
  Bitrefill invoice.
- Require physical Trezor approval for the exact Base USDC payment.
- Validate the signed transaction independently before broadcast.
- Reuse the existing Bitrefill MCP integration where practical without
  changing its production callers or configuration.
- Provide automated coverage without network access, a real purchase, or a
  connected hardware wallet.
- Provide a separate, manual, low-value live smoke-test path.

## Non-goals

- Replacing or migrating existing managed Base wallets.
- Changing iMessage or WhatsApp approval behavior.
- Adding Trezor commands to the Hermes Telegram plugin.
- Deploying the sidecar on the production server.
- Supporting Bitcoin, other EVM networks, arbitrary ERC-20 contracts, swaps,
  SINGIT, or arbitrary contract calls in the first proof.
- Designing the remote per-user companion protocol. That is a separate phase
  after the local proof succeeds.
- Automatically retrying invoice creation, signing, or transaction broadcast.

## Current Production Boundary

The production application currently creates an encrypted Base private key per
Telegram user in `ManagedBaseWalletService`. The Sign402 Gateway uses that key
for user-wallet transfers, while iMessage or WhatsApp provides the purchase
approval channel. These paths are serving users and remain authoritative.

The repository already has a Streamable HTTP MCP client in
`sign402_gateway.bitrefill_mcp`. The local proof may reuse its public Bitrefill
adapter behavior, but no existing production module is changed to import,
construct, or route to the Trezor proof.

## Chosen Architecture

### `trezor-sidecar`

The sidecar is the only component allowed to hold the Trezor MCP bearer token.
It binds to `127.0.0.1:8111`, rejects non-loopback requests, and requires a
separate local bearer token for every non-health request.

It exposes a narrow application API rather than a generic MCP proxy:

- `GET /health`
- `POST /v1/pair`
- `POST /v1/purchase-intents/approve`
- `POST /v1/payments`
- `GET /v1/payments/{paymentId}`

The sidecar internally allows only these Trezor MCP tools:

- `trezor_get_address`
- `trezor_sign_typed_data`
- `trezor_send_transaction`
- `trezor_push_transaction`

Callers cannot supply an MCP tool name, an arbitrary chain, token contract,
transaction object, or calldata.

### `trezor-poc-runner`

The runner owns the test-only Bitrefill workflow. It obtains product details
and a quote, displays the required purchase summary, asks the sidecar to approve
the purchase intent, creates one Bitrefill invoice, validates its payment
requirements, and asks the sidecar to pay it.

The runner does not receive the Trezor MCP token, derivation secrets, a private
key, or an unsigned generic signing capability. The sidecar does not receive
the Bitrefill API key or redemption data.

### Future per-user topology

Trezor Suite MCP accepts localhost clients only. A future multi-user product
therefore requires a companion process on every user's computer. That
companion will make an authenticated outbound connection to the central
gateway and receive tightly scoped signing requests. The Trezor MCP token will
remain local. The remote companion protocol, enrollment, revocation, updates,
and recovery are outside this proof.

## Isolation Contract

The first implementation must satisfy all of the following:

- Add new sidecar, runner, test, and documentation files only.
- Do not modify `sign402_gateway/server.py`.
- Do not modify the Hermes wallet plugin.
- Do not modify production service definitions or deployment scripts.
- Do not modify production environment files or examples used by deployment.
- Do not open or migrate the production wallet, commerce, approval, or user
  databases.
- Use a separate state directory, defaulting to
  `~/.sign402-trezor-poc/`, with files restricted to the current OS user.
- Bind all proof HTTP listeners to loopback only.
- Keep live mode disabled unless an explicit proof-only enable flag is set.
- Never fall back from the Trezor path to a managed private key or production
  approval channel.
- Never route an existing user to the proof based on missing or ambiguous
  configuration.

Failure to initialize the proof must not affect production because the proof
is not imported or started by any production process.

## Configuration and Secret Handling

The proof uses two local environment files outside the repository, both with
mode `0600`. Splitting the process environments enforces the rule that the
sidecar never receives the Bitrefill credential and the runner never receives
the Trezor MCP credential.

The sidecar environment contains:

```text
SIGN402_TREZOR_POC_ENABLED=1
SIGN402_TREZOR_MCP_TOKEN=<local Trezor Suite MCP token>
SIGN402_TREZOR_SIDECAR_TOKEN=<independent random local API token>
SIGN402_TREZOR_POC_MAX_USD=<required positive live limit>
SIGN402_TREZOR_BASE_RPC_URL=<secure Base Mainnet JSON-RPC URL>
```

The runner environment contains:

```text
SIGN402_TREZOR_POC_ENABLED=1
SIGN402_TREZOR_SIDECAR_TOKEN=<same local API token>
SIGN402_TREZOR_POC_MAX_USD=<required positive live limit>
BITREFILL_API_KEY=<test operator's Bitrefill credential>
```

There is no live spending default. Startup fails if live mode is enabled and
the maximum is absent, malformed, or non-positive. The runner enforces its cap
before requesting approval, and the sidecar independently enforces its cap;
the lower effective limit wins if they differ.

The Trezor MCP URL is fixed to `http://127.0.0.1:21340/mcp`. The token is sent
as an `Authorization: Bearer` header so it does not enter URLs, access logs, or
exception messages. The sidecar's custom representations and diagnostics
redact both local bearer tokens.

The proof must not commit or log API keys, Trezor tokens, local sidecar tokens,
RPC credentials, raw signed transactions, signatures, recipient values,
payment links, or redemption data.

## Pairing and Wallet Identity

Pairing uses the fixed EVM derivation path `m/44'/60'/0'/0/0` for the first
proof. The sidecar calls `trezor_get_address` with:

```json
{
  "coin": "base",
  "path": "m/44'/60'/0'/0/0",
  "showOnTrezor": true
}
```

The operator verifies the address on the device. The sidecar stores only the
normalized Base address, derivation path, a locally generated pairing ID, and
timestamps. A newly observed address or derivation path mismatch invalidates
the operation and requires explicit re-pairing; it never silently replaces the
paired wallet.

ETH and token balances are read through the existing Base JSON-RPC balance
logic using the public address. Balance reads do not need the Trezor MCP token
or a device confirmation.

## Purchase Intent Approval

Before `buy-products` is called, the runner must display the exact:

- product and package or denomination;
- quoted total and maximum authorized payment;
- Base Mainnet network and USDC payment asset;
- payment method;
- recipient details required by the product;
- expiration time.

The runner canonicalizes the recipient fields, hashes them, and sends a typed
purchase intent to the sidecar. Raw recipient values remain in runner memory
only and are not persisted by the sidecar.

The EIP-712 domain is:

```text
name: SingIt Trezor Purchase
version: 1
chainId: 8453
```

The `PurchaseIntent` contains:

- `bytes32 intentId`
- `string productSlug`
- `string packageId`
- `string denomination`
- `uint256 quotedTotalUsdMicros`
- `uint256 maxPaymentUsdcAtomic`
- `string paymentAsset` fixed to `USDC`
- `string paymentNetwork` fixed to `Base Mainnet`
- `bytes32 recipientHash`
- `uint64 expiresAt`

The sidecar validates the fixed fields, configured live cap, expiration,
paired address, and single-use intent ID before calling
`trezor_sign_typed_data`. It verifies the resulting signature against the
paired address. A rejected, invalid, expired, or mismatched signature prevents
invoice creation.

This signature is an authorization commitment for the local proof. It is not
an on-chain permit and grants no generic spending authority.

## Invoice Creation and Validation

After a valid purchase-intent signature, the runner calls Bitrefill
`buy-products` exactly once for the approved item and `usdc_base` payment
method. It binds the returned invoice ID to the intent ID before any payment
attempt.

The runner rejects the invoice unless all of these hold:

- the bound product and package are the approved values;
- the payment network is Base Mainnet;
- the payment asset is the canonical Base USDC contract;
- the invoice amount is positive and no greater than
  `maxPaymentUsdcAtomic`;
- the invoice amount is within the configured proof live cap;
- the destination is a valid non-zero EVM address;
- the invoice has enough remaining lifetime for signing and broadcast;
- the invoice ID has not been used by another intent or payment.

An invoice failure never triggers a second `buy-products` call automatically.
The existing invoice is reconciled or expires.

## Payment Signing and Broadcast

The runner sends the sidecar only this high-level payment request:

```json
{
  "intentId": "...",
  "invoiceId": "...",
  "payTo": "0x...",
  "amountAtomic": "...",
  "expiresAt": "..."
}
```

The sidecar checks the request against its approved purchase intent. It fixes:

- coin to `base`;
- chain ID to `8453`;
- token contract to canonical Base USDC;
- native transaction value to zero;
- ERC-20 selector and encoding to an exact `transfer(payTo, amountAtomic)`.

The sidecar independently reads the public address's USDC and ETH balances
through its configured Base Mainnet RPC before opening a device prompt. It
verifies the RPC chain ID is `8453`. Insufficient token balance or an
insufficient conservative gas reserve fails before signing.

The sidecar calls `trezor_send_transaction` with `broadcast=false`. After the
user physically confirms the transaction, the sidecar decodes the returned
signed transaction and independently verifies:

- signer equals the paired address;
- chain ID equals `8453`;
- destination equals the canonical Base USDC contract;
- native value equals zero;
- calldata is exactly one ERC-20 transfer;
- transfer recipient and amount equal the bound invoice;
- nonce and fee fields are well-formed;
- the invoice and purchase intent have not expired.

Only a full match permits `trezor_push_transaction`. The signed transaction
stays inside the sidecar and is not returned through the local API or written
to disk.

## State and Idempotency

The proof uses a separate SQLite database at
`~/.sign402-trezor-poc/state.db`. The parent directory and database are made
readable and writable only by the current OS user where the platform allows.

The state machine is:

```text
QUOTED
  -> DEVICE_APPROVED
  -> INVOICE_CREATED
  -> TX_SIGNED
  -> TX_BROADCAST
  -> COMPLETE
```

Terminal or recovery states are:

- `CANCELLED`: the user rejected the intent or transaction on Trezor;
- `FAILED`: a known pre-broadcast validation or provider failure;
- `RECONCILIATION_REQUIRED`: broadcast or provider completion is ambiguous.

Unique constraints bind one intent to one invoice and one payment attempt.
Only one Trezor approval job may be active at a time. Repeating a request with
the same idempotency key returns its recorded state rather than creating a new
invoice, signature prompt, or broadcast.

After `TX_SIGNED`, errors never cause an automatic second signature. After a
broadcast attempt, the runner checks the transaction hash and invoice state
before any operator-guided recovery. It never creates or pays a replacement
invoice automatically.

## Local API Behavior

All mutating requests require:

- the sidecar bearer token;
- a caller-generated idempotency key;
- a bounded JSON body;
- an unexpired request timestamp.

The sidecar returns fixed error codes and safe messages. Provider error bodies
and MCP content are not reflected to callers. Long-running device prompts are
represented as payment jobs so the runner can poll status without repeating
the operation.

`GET /health` reports only coarse states such as `ready`, `suite_unavailable`,
`device_unavailable`, or `disabled`. It never returns tokens, addresses,
session IDs, or upstream error bodies.

## Error Handling

- Suite unavailable or MCP authentication failure: fail without state advance.
- Different device address: fail and require explicit re-pairing.
- User rejection: mark the current operation `CANCELLED`.
- Device timeout: fail closed without automatic re-prompt.
- Expired intent or invoice: fail before signing or broadcast.
- Insufficient USDC or ETH: fail before signing.
- Typed-data signature mismatch: fail before invoice creation.
- Signed-transaction mismatch: retain no broadcastable output and fail.
- Known pre-broadcast provider failure: mark `FAILED`.
- Unknown broadcast result: mark `RECONCILIATION_REQUIRED` and stop.
- Bitrefill polling timeout after a known broadcast: retain the invoice and
  transaction identifiers for reconciliation; never create another order.

## Logging and Sensitive Data

Application logs may contain only operation IDs, safe state transitions,
coarse error codes, and timestamps. They must not contain wallet tokens,
Trezor tokens, signatures, signed transaction bytes, calldata, payment links,
recipient data, or redemption values.

The non-secret purchase record contains exactly:

- invoice ID;
- product slug;
- amount;
- payment method;
- timestamp.

Redemption data is returned only to the initiating local caller after a
completed invoice. It is not persisted by the proof or included in exceptions.

## Testing Strategy

### Unit tests

Use injected fake MCP and Bitrefill transports. Cover:

- pairing and address normalization;
- wrong-device and path mismatch rejection;
- EIP-712 intent construction and signature verification;
- live-cap, expiry, recipient-hash, and single-use checks;
- invoice network, token, destination, amount, and lifetime validation;
- deterministic ERC-20 calldata construction;
- signed-transaction decoding and every mismatch dimension;
- idempotent intent, invoice, signing, and broadcast handling;
- cancellation, timeout, insufficient funds, and ambiguous broadcast states;
- bounded responses and fixed safe errors;
- secret and recipient-data absence from logs, representations, and responses;
- rejection of generic MCP tools, arbitrary calldata, chains, and tokens.

No automated test calls a real MCP server, invokes `buy-products`, signs with a
device, or broadcasts a transaction.

### Production regression tests

Run the existing suites covering at least:

- gateway routing and startup;
- managed user wallets;
- Bitrefill quote and purchase runner;
- iMessage approvals;
- WhatsApp Cloud approvals;
- Hermes wallet plugin client and dispatch behavior.

The proof is not complete if these regressions change or fail.

### Manual live smoke test

The operator performs these steps explicitly:

1. Start the proof-only sidecar and runner with a required low live cap.
2. Pair the Trezor and verify the Base address on its screen.
3. Read ETH and USDC balances through Base RPC.
4. Sign a non-purchasing test `PurchaseIntent`.
5. Select a low-value Bitrefill product and review the exact summary.
6. Approve its `PurchaseIntent` on Trezor.
7. Create and validate exactly one invoice.
8. Approve the exact USDC transaction on Trezor.
9. Verify the transaction and invoice completion.
10. Confirm that production Hermes, managed wallets, iMessage, and WhatsApp
    behaved unchanged throughout the test.

## Completion Criteria

The local proof is complete when:

1. The paired Base address is derived from and verified on Trezor.
2. No private key exists in the runner, sidecar state, environment, logs, or
   responses.
3. A purchase intent cannot create an invoice without a verified Trezor
   EIP-712 signature.
4. A Base USDC payment cannot be broadcast without physical Trezor approval.
5. The signed transaction is independently verified before broadcast.
6. Replayed or ambiguous requests cannot create a second invoice or payment.
7. Automated proof tests and existing production regression suites pass.
8. A manually approved low-value Bitrefill purchase completes locally.
9. No production process, route, database, environment, or approval channel is
   changed or deployed.

## Follow-up Phase

After the local proof succeeds, write a separate design for a distributable
per-user companion. That design must cover outbound authenticated transport,
device enrollment, user-to-device binding, revocation, software updates,
offline behavior, request expiry, central reconciliation, and migration. It
must not make a user's localhost MCP endpoint or Trezor token remotely
accessible.
