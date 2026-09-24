# Ledger v1 — scope, operation and verification

Implementation and payment lifecycle verified on 9 September 2026. Approval
format v2 uses compact readable EIP-191 text. The owner confirmed that the
compact version was readable and comfortable to review. The Ledger signed it,
the HTTP gateway accepted it, and retry/accounting checks passed with the test
payer. [L8](checks.md#l8--readable-compact-approval-9-september) records this check.

This integration is on the hackathon branch only. The owner requested no
production deployment; production continues to use its existing Trezor setup.

A real purchase completed on 9 September: **0.001 USDC on Base** bought Otto news
with the earlier EIP-712 format. [L7](checks.md#l7--real-purchase-after-ledger-approval-9-september)
records that payment; it is not evidence of v2 device display.

## Device display and approval format v2

A no-payment hardware check reproduced the reported problem: the owner saw
hashes and a Blind signing warning. SDK steps reached `provideContext` and
`signTypedData`, without `signTypedDataLegacy`. Blocking the legacy fallback
alone therefore did not resolve this device's display.

The client now uses Ledger's supported [`signMessage` text API](https://developers.ledger.com/docs/device-interaction/dmk-ts/references/signers/eth#-use-cases).
It sends the text itself with EIP-191 `personal_sign`; EIP-712 input is refused.
This is readable off-chain purchase consent, not an ERC-7730 descriptor or
Ledger Transaction Checks integration. The gateway wallet still pays separately.

The compact screen content is five text lines (165 ASCII characters for the
test purchase; the firmware determines how many pages they occupy):

```text
SingIt purchase v2
Buy: Otto AI - Crypto News
Pay: 0.001 USDC on Base
To: 0x0000000000000000000000000000000000000001
Ref: <43-character order reference>
```

`Ref` is the full SHA-256 digest, encoded as base64url without padding, of the
canonical domain and all approval fields. The purchase/resource, merchant,
amount, recipient, owner, rule, journal ID and expiry remain cryptographically
bound without a separate device page for each technical field. The gateway
reconstructs this text from the frozen order. Field values escape line breaks
and Unicode controls rather than creating extra labels; no values or addresses
are truncated. The envelope carries `signingMethod`, `domain`, `message` and
`displayText`; the submitted signature must declare `personal_sign`.

Pending v1 typed-data operations must be cancelled and restarted under a new
request ID. They cannot be approved through v2. Finished v1 results still return
from the operation store without another signature or payment. These display
checks use only the test payer and require no new purchase.

## Scope

One configured owner, existing GET x402 tools, Base mainnet USDC. The owner is
the canonical Telegram user ID used by the gateway. When Spending Memory says
`ESCALATE`, a local client obtains a Ledger EIP-191 text signature and submits it to
the gateway. A familiar payment within the autonomy policy can still proceed
without a device; `BLOCK` and wallet spending limits cannot be overridden by a
signature.

The integration applies to that owner's `/agent/buy-tool` requests. It is not
a universal Ledger gate for Bitrefill, transfers, bank top-ups, The Graph's
separate query path or other gateway owners. Multi-owner enrolment, a browser
signer and routing every purchase product through Ledger are outside v1.

The payment wallet remains custodial. The Ledger signs an approval message;
the existing gateway wallet signs the actual x402 payment. Verification assumes
the configured approver address belongs to the intended Ledger. A compromised
gateway with the decrypted spending key remains outside this protection.

## What changed

The old verifier required a payment claim, although an escalated policy decision
does not acquire a claim. Retrying also generated a fresh decision ID. A valid
standalone device signature therefore did not prove the HTTP purchase could
resume.

`ledger_payments.py` now persists a frozen intent, quote, decision and ten-minute
expiry before requesting the device. The signature binds purchase name, resource URL, merchant, payout,
USDC amount, owner, rule, journal ID and expiry through the `SingIt Spending Approval`
domain reference, version 2, chain 8453.

The gateway consumes the pending operation atomically before calling the payer.
It checks the current quote, policy, pause switch and wallet limits again, then
reserves budget and acquires a scoped payment claim. Expiry and pause are also
checked immediately before submission. No budget is held while waiting for the
device. Signatures and private keys are not saved in the operation store.

A successful result is encrypted and retained. Reusing the same request ID
returns it without signing or paying again, including after reopening the
database. A different payload under that ID is refused. This is at-most-once
payer submission, not a claim of distributed exactly-once settlement.

## Enable the owner lane

Install the gateway from its `pyproject.toml`. The Spending Memory dependency
is pinned to `cbc0739b2842e92f7d7c698580d48284a7063960`, which contains the
scoped claims and The Graph adapter used by this checkout.

On the Mac connected to the Ledger, from the repository root:

```bash
npm ci --prefix tools/ledger-approve
node tools/ledger-approve/approve.cjs --address
```

Open the Ethereum app. The second command prints the public address at
`44'/60'/0'/0/0`; `--path` selects another derivation. The signer prints device
interaction prompts and SDK step names on stderr. Its stdout is machine-readable; do not log
stdout when using it to sign an approval.

Run `npm test --prefix tools/ledger-approve` for the six no-device action
checks, including refusal of a legacy fallback even if a signature follows it.
The current full gateway suite passes 1174 tests, including 72 Ledger checks;
the six JavaScript action checks pass separately. Tests cover changed purchase
names, resources and technical fields, v1 refusal, historical result retrieval,
exact text delivery to the SDK and refusal before signing invalid input.

Set these on the gateway, using the actual owner ID and public address:

```dotenv
SIGN402_LEDGER_APPROVAL_ENABLED=1
SIGN402_LEDGER_OWNER_ID=<canonical Telegram user ID>
SIGN402_LEDGER_APPROVER_ADDRESSES=<Ledger public address>
SIGN402_LEDGER_APPROVAL_CHAIN_ID=8453
SIGN402_LEDGER_OPERATIONS_DB=~/.sign402/ledger/operations.sqlite3
SIGN402_SPENDING_MEMORY_ENABLED=1
```

Restart the gateway to load the configuration. Missing owner, malformed or
absent approvers, unsupported chain, missing Spending Memory or an unusable
wallet encryption key refuses startup. Enabling this flag does not enrol users
or change their wallet keys. Both Ledger features remain off by default.

The local Python client uses only the standard library. Supply the existing
`SIGN402_WALLET_API_TOKEN`, the owner's `SIGN402_USER_ACCESS_TOKEN` and
`SIGN402_LEDGER_OWNER_ID` through its environment. Do not put tokens in command
arguments, source files or screenshots. Set `SIGN402_GATEWAY_URL` to the HTTPS
gateway origin, or use a loopback SSH tunnel. HTTP is allowed only on loopback;
redirects are refused so credentials and signatures cannot follow one.

```bash
# This command can spend real USDC on a configured live gateway.
python3 tools/ledger-approve/purchase.py buy --tool news --request-id my-ledger-purchase-001

# Read or resume this same purchase after interruption.
python3 tools/ledger-approve/purchase.py status --request-id my-ledger-purchase-001
python3 tools/ledger-approve/purchase.py approve --request-id my-ledger-purchase-001
python3 tools/ledger-approve/purchase.py cancel --request-id my-ledger-purchase-001
```

Use a unique request ID for a deliberate new purchase and keep it for retries.
The client generates and prints one if `buy` is given no ID. Hermes also creates
an ID for each tool invocation; a pending reply includes it for the local client
to resume. The result is returned to the local client and saved as the user's
last purchase; v1 does not proactively send a new Telegram message afterwards.

## HTTP contract

Every request requires gateway authentication and `X-Sign402-User-Token` for
the configured owner. The body includes `telegramUserId` and `requestId`.

| Endpoint | Additional body | Result |
| --- | --- | --- |
| `POST /agent/buy-tool` | Existing tool payload | `202 pending` with `approval`, `expiresAt` and `needs_ledger_approval`; or completed result |
| `POST /agent/ledger-status` | None | Current operation or cached result |
| `POST /agent/ledger-approve` | `ledgerApproval: {signature, expiresAt, journalId}` | Consumes this pending approval and executes, or returns existing state |
| `POST /agent/ledger-cancel` | None | Cancels a pending operation without sending funds |

Pending and succeeded responses use HTTP 202 and 200. Expired/cancelled use
410; failed/executing/uncertain and conflicting requests use 409. Authentication,
signature and unavailable-service errors use 401/403, 400 and 503 respectively.
Status and cancellation remain available while purchases are paused.

## Interruptions and reconciliation

For `pending`, reopen the client with `approve` and the same ID. Rejecting or
timing out on the device submits nothing; the pending request can be retried
until it expires. Use `cancel` to abandon it. An expired or cancelled ID cannot
be repurposed.

For `succeeded`, retrieve the stored result. Never make a fresh purchase to
recover a response that was lost in transit.

For `executing`, first read status again: another HTTP request may still be
finishing. If the gateway process died, or status becomes `uncertain`, payment
may have been submitted. New Ledger operations for this owner are blocked.
The operator must establish the outcome before allowing another purchase:

1. Pause purchases and retain the operation database, Spending Memory and wallet
   budget state together. Do not delete a row, change its request ID or disable
   Ledger to work around the block.
2. On the trusted host, inspect the encrypted operation through
   `LedgerOperationStore.get(owner, request_id)` using the existing master key.
   The record includes any saved `result`, `reservationId` and `claimId`. Keep
   decrypted result data out of logs and issue trackers.
3. Reconcile the payer's receipt and onchain status with the budget reservation,
   payment claim and remembered settlement. An absent HTTP response does not
   establish that no transfer happened. A failure after a saved result can mean
   only local accounting failed.

V1 deliberately has no automatic retry or general reconciliation command for
an ambiguous transfer. Operator repair requires evidence and depends on the
failure point; until then the operation stays blocked. Database backups and
the existing wallet encryption key must be retained together. Do not restore
an older operation database and replay newer approvals, or prune completed IDs
while clients may still retry them. Use one gateway process with its local
budget and memory stores; a multi-process deployment needs shared accounting.

## Key Ring is a separate feature

`keyring.py` obtains the wallet encryption master key through `wallet-cli ring`
at startup. On an enrolled host, decrypt does not require a connected Ledger
on each boot. The master key exists in gateway memory after decryption; this
is protection for its storage, not hardware signing of every managed wallet.

The tested wallet-cli 2.1.0 could not enrol the USB-less VPS by a supported path.
The checks do not establish that production was migrated. See
[the original evidence](checks.md) and [DX notes](ledger-dx-notes.md). Enrolment
and any production migration remain separate operator work. Encrypt the
existing master key when migrating; generating a replacement would make the
existing wallet data unreadable.

The repeatable Key Ring check uses a throwaway key, compares decrypted bytes
exactly, and exits nonzero if corrupt ciphertext is accepted. It needs the
enrolled host's `WALLET_PASS` and a `wallet-cli` executable. Neither is required
for the device-approval check below. Override `SIGN402_LEDGER_WALLET_CLI` and
`SIGN402_PYTHON` if needed:

```bash
bash sign402-gateway/scripts/ledger-keyring-rehearsal.sh
```

## Verification without spending money

```bash
cd sign402-gateway
python -m unittest tests.test_ledger_approval tests.test_ledger_payments tests.test_ledger_client tests.test_ledger_keyring -v
python -m unittest discover -s tests -q
```

From the repository root, with gateway dependencies available:

```bash
python sign402-gateway/scripts/ledger-approval-rehearsal.py --software-signer
python sign402-gateway/scripts/ledger-approval-rehearsal.py
```

Both the earlier EIP-712 version and the current compact EIP-191 version of the
second command were verified on 9 September 2026; only the latter has the owner's
confirmation of readable purchase details and acceptable review length.
It reads the public address before creating the challenge, starts the real HTTP
handler on loopback, runs the real spending policy and encrypted operation
store, and resumes through the shipped local client. The device signs the compact purchase text for
`0.001 USDC`, merchant `ledger-rehearsal.invalid`, payout `0x0000…0001`.

The HTTP gateway accepted the hardware signature, called the **test payer once**,
accounted once, reopened the operation store and returned the same result for
both approval and buy retries. No USDC was sent, no real wallet key was loaded,
and the signature was neither printed nor persisted. This replaces the old
signature-only rehearsal; it does not claim a live mainnet payment was tested.

Automated coverage also rejects changed amounts, payouts, owners and decision
IDs; another signer; cross-operation replay; changed quotes; new policy blocks;
exceeded wallet limits; late expiry/pause; unauthenticated access; concurrent
submission; and retries after ambiguous outcomes or process loss.

## Real purchase check

`sign402-gateway/scripts/ledger-live-purchase.py` uses the actual Otto endpoint
and the project's existing operator CDP account. The HTTP handler, Ledger
approval lifecycle, memory, budget and encrypted operation store are real.
The payment is sent by the existing CDP x402 client with approved amount,
recipient and token guards; quote and payer are not mocked. This is an operator
wallet verification, not deployment of the managed-customer-wallet lane.

It pins Otto news to 1000 atomic USDC (0.001 USDC), the observed receiver and
Base mainnet. Every purchase requires a Ledger signature and the separate live
check budget is 0.001 USDC per transaction and per day. The `prepare` and
`status` commands cannot call the payer. Only `approve` can pay, after a valid
signature; repeating an already completed request returns the saved result.

The script reads the existing local CDP configuration and wallet encryption
master key. Runtime state is private and ignored by Git under `.ledger-live/`;
retain it across retries. The purchase record contains only transaction/invoice
ID, product, amount, payment method and timestamp. Approval signatures and
wallet secrets are never printed or persisted there.

```bash
# Substitute the owner's public configuration and keep one ID for retries.
python sign402-gateway/scripts/ledger-live-purchase.py prepare \
  --request-id <purchase-id> --owner <Telegram-id> --approver <Ledger-address>

# This spends real USDC after the Ledger signature; obtain purchase consent first.
python sign402-gateway/scripts/ledger-live-purchase.py approve \
  --request-id <same-id> --owner <Telegram-id> --approver <Ledger-address>

# Retrieve data and independently check the existing receipt; never pays again.
python sign402-gateway/scripts/ledger-live-purchase.py status \
  --request-id <same-id> --owner <Telegram-id> --approver <Ledger-address>
```

The receipt check validates success and exactly one matching USDC `Transfer`
from the configured payer to Otto for 1000 atomic units. A diagnostics failure
after settlement must be handled by `status`, never by a replacement purchase.
