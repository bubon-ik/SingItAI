# SingIt Ask checks — 2026-10-02

Current Ask status (October 3): deployed actual-usage billing and one verified
Base payment of 0.001055 USDC. Solana recipient readiness is verified; a funded
Solana metered payment remains unverified. Dated entries below preserve the
state at each stage, including earlier deployment blockers that were resolved.


## Verified

- Surplus accepted the configured buyer key and a real `deepseek-v4.1-flash`
  completion returned HTTP 200 with `OK`. The provider reported 50 total tokens
  and `buyer_cost_micro: 1` (USD 0.000001).
- Fifteen tests pass locally and on the VPS, including payment challenges, both
  payment networks with a fake facilitator, no settlement for a failed model,
  request validation, free endpoints, and the HTTPS resource URL behind a local
  proxy. These tests do not prove real settlement.
- The public MetaMask test page loads with the requested payer and fixed
  0.003 USDC/Base terms. Added offline checks cover EIP-712 compatibility with
  the official SDK, signature recovery, changed terms, account/network changes,
  signature cancellation, no automatic retry and isolation of local settings.
  No real wallet signature was requested during these checks.
- A dedicated `hermes` user service is enabled and running on the existing VPS.
  It listens on `127.0.0.1:8140`; user lingering is enabled for reboot startup.
- The VPS responds HTTP 200 on `/health` and `/v1/models`.
- An unpaid POST on the VPS returns HTTP 402 with the configured recipient
  addresses and amount 3000 (0.003 USDC) on both Solana and Base.
- With the expected tunnel headers, the payment resource URL is
  `https://ask.singitai.app/v1/chat/completions`.
- After the owner configured the Cloudflare hostname, an external check of
  `https://ask.singitai.app/health` returned HTTP 200 with the correct model.
  An external unpaid POST to `/v1/chat/completions` returned HTTP 402 and the
  public HTTPS resource URL, both expected USDC assets, recipient addresses,
  and the expected amounts. No model generation or payment occurred.
- Runtime settings are held separately at
  `/home/hermes/.config/singit-ask/service.env`, mode 0600, in a 0700 directory.
  Credentials are not part of the uploaded source or this record.

## Uniform price update — 2026-10-02

At the owner's request, both networks now charge a fixed 0.003 USDC per successful
answer. Defaults, private runtime settings, challenge test expectations and the
README were updated. All 15 offline tests passed locally and on the VPS. After
a private rollback backup and service restart, public health returned 200 and
an unsigned request returned 402 with amount `3000` on both networks. No real
payment or model generation was made in this verification.

## Pending before public readiness

- The configured Solana recipient has no native USDC token account, according
  to a repeated mainnet `getTokenAccountsByOwner` check after publication. The
  endpoint currently advertises Solana, but that rail is not ready for payment.
  Create its associated token
  account and verify it before accepting Solana payments.
- Base agent-paid settlement and answer delivery are now verified below.
  Real Solana settlement and the separate manual MetaMask test remain unverified.
- Bazaar registration remains unverified. A one-request MetaMask test page is
  published; a conversational browser chat interface is not implemented.

## Isolation and rollback

Only the new `singit-ask` directory, its private settings and its user unit were
installed. Existing gateway services and wallet databases were not modified.
Disable this service with `systemctl --user disable --now singit-ask` and remove
its public hostname from the tunnel if a rollback is needed.

## First real Base web-agent payment — 2026-10-02

After the owner activated the staged integration and asked a question, read-only
checks verified the five deployed source hashes and both active system services
(started at 19:33:13 CEST). The account selected SingIt Ask; its chat usage row
recorded 553 prompt tokens, 100 completion tokens and a charge of 3000 atomic
USDC. Its settlement row binds the same charge to the Ask endpoint.

Base RPC independently confirmed transaction
[0x2b56b609…745eac](https://basescan.org/tx/0x2b56b609a130264de8434dcaa844e6d9cf210f503ead0266bd79f0bfe2745eac),
status 1, block 52086557, at 17:34:21 UTC. The native USDC Transfer log shows
3000 atomic units from agent `0x6A2516085B726cBA7e74b774335C80E6A197575D` to
the configured receiver `0xC23d1Dc0f5fCe1abfFB051e06cB93f0329968B4e`.
This verification submitted no new request, signature or payment. Base is
verified; the Solana limitations above still apply.


## October 2 — actual-usage x402 payment implementation

Uncommitted work in `trezor-local-sidecar`; no published commit or PR for this change.

- Confirmed CDP support for Base `upto` using the existing authenticated facilitator;
  no new credentials, dependency installation or wallet was required.
- Merchant endpoint `/v1/chat/completions/metered` deployed with private code/config
  backups. Public HTTP 402 is accepted by the real buyer's quote validator: Base
  `upto`, 3000-atomic authorization maximum, `eip2612GasSponsoring`, actual-cost
  billing + 30% markup + 1000-atomic settlement fee. No signature/model call/payment
  was submitted by this live check. Legacy Base/Solana `exact` challenges remain 3000.
- 23 local Node tests passed, including real SDK signing against mocked chain
  reads, bounded sponsored approval, one paid submission only, actual settlement
  override (1130 from a 3000 maximum), malformed invoices and failed upstream calls.
- 278 isolated VPS web tests passed, covering existing web flows and new metered
  adapter accounting, unchanged network routing and persistent uncertainty blocks.
- 11 isolated spend-reservation tests passed, including partial settlement,
  ceiling enforcement and preserving existing exact settlement behavior.
- Model picker and Usage page disclose the formula and ceiling. Answer metadata
  shows six-decimal actual cost; its tooltip separates provider cost, markup and fee.
- Updated gateway/UI activation manifest matches reviewed production hashes and
  staged files. Activation is pending the operator's sudo password. Current live
  web-agent chat therefore still uses the fixed-price route until activation.
- No real metered payment has been tested. On first live use, verify the receipt's
  native USDC amount against `billing.totalAtomic`, the gateway ledger and chat usage.
  Solana metered payments remain unavailable. An ambiguous submission requires
  operator reconciliation; automatic payment retry is intentionally disabled.


## October 2 — Solana actual-usage integration

This section supersedes the earlier Base-only implementation status. The code is
uncommitted in `trezor-local-sidecar`; no published commit/PR is claimed.

- Existing authenticated CDP `/supported` confirms mainnet Solana `upto` with a
  sponsored `feePayer` and delegated `receiverAuthorizer`. Installed SVM SDK 2.28.0
  already implements it; no dependency installation or new keys were needed.
- The deployed `/v1/chat/completions/metered/solana` publicly returns a valid x402
  `upto` quote accepted by the real Solana buyer validator: native USDC, the
  configured receiving wallet, ceiling 3000, markup 30%, fee 2000 and 300-second
  escrow withdrawal grace. The Base quote still validates with fee 1000.
- The standard Solana flow uses two CDP on-chain transactions. The service's fixed
  0.002-USDC settlement fee reflects that standard flow, including during the
  promotional free tier. Any existing agent-funding sponsor costs are separate.
- 31 Node tests pass locally and on the VPS Node v22 runtime, covering real unfunded SDK signing, deposit before inference,
  actual charge, full unused refund, full refund on inference failure, recipient
  readiness, changed terms, and refusing repeated signed submissions.
- 287 isolated VPS web tests pass, including Base/Solana selection isolation and
  nine new Solana adapter/storage tests. 21 isolated allowance/reservation tests
  pass. Existing Base and legacy exact routes remain compatible.
- Gateway reserves are durable SQLite `metered_holds`: normal Solana purchases see
  pending holds; successful settlement atomically replaces the ceiling with actual
  spend. Restart, funding uncertainty or payment uncertainty never clears a hold.
- The agent reuses its refunded USDC and pulls only a funding shortfall through the
  user's existing SPL grant. Confirmed zero-charge refunds release the budget;
  unknown refunds/funding stop further Ask attempts for that account.
- The receiver's native USDC ATA is absent, confirmed read-only on mainnet. No funds
  were moved and no real payment was attempted. Recipient creation is required
  before activation. Protected gateway settings (Solana allowance and funding fee
  payer) require sudo validation by the activation script.
- Both metered endpoints are deployed. The eight-file gateway/UI update is staged,
  not active. The operator script backs up config/state/code, verifies source
  hashes and wallet identities, and refuses activation when prerequisites fail.


Final activation dry-run verified all eight source hashes and both unpaid quotes.
It correctly exited before any mutation because the recipient USDC ATA is absent.
The two initial isolated Node test failures were missing copies of the existing
browser-test assets in staging; copying those fixtures produced 31/31 passing tests.

## October 3 context/fallback regression

- 295 isolated web tests passed, including Prague coffee continuity, explicit new
  requests, cross-chat isolation, Ask provider-preserving errors and avoiding new
  paid searches while a previous Ask payment needs review.
- 32 Node tests passed; the new diagnostic test proves an HTTP rejection is
  reported by stage/status without retrying or exposing response content.
- Prepared `activate-context.py` with a five-file hash manifest. Read-only
  preflight passed; activation needs the operator's sudo password.
- A real user's earlier Base metered attempt left a durable unresolved checkpoint.
  At the initial inspection its Permit2 nonce was unused on the latest chain,
  but the finalized chain had not yet passed the authorization deadline. The
  block must not be cleared merely because a newer unfinalized block says unused.
- Operator activated the patch at 09:22:47 UTC; all five production hashes match.
  Gateway, web API and merchant health checks pass. Existing wallet identities
  were preserved by activation. No new paid request was made by this check.
- Bounded read-only reconciliation on October 3 ended with `FINALITY_PENDING`.
  The finalized Base timestamp had not passed the saved authorization deadline;
  no journal/checkpoint was removed and no payment was repeated. The original
  rejection reason is unavailable in the old helper's coarse error output; new
  diagnostics will identify the stage/status on any subsequent user-requested
  attempt. The context fix is active; the affected account's Ask payment remains
  blocked pending final settlement reconciliation.

## October 3: Base Ask unblocked for the owner's next test

Finalized Base block 52114913 (timestamp 1791019173) is after the prior
Permit2 deadline 1791018823; its nonce bitmap proves the authorization unused.
The private checkpoint and journal were archived with that evidence under
`~/.sign402/ask-metered/reconciled/1791020028` and removed from the active block.
No payment was sent or retried. The old attempt did not settle through Permit2.
A read-only CDP verify probe on a newly generated unfunded fixture reached
`insufficient_funds`, as expected; it never called settle or paid the merchant.
This checks verification connectivity, not a successful funded payment. The
context patch remains active; the next real end-to-end request is the owner's
manual chat test.

## October 3: reproduced and fixed Base extension echo rejection

The second owner-requested `hello` failed at the paid HTTP response with 402.
The manual buyer used the scheme SDK's signed extension directly, dropping the
merchant-declared `info.description`. x402 core 2.28 validates advertised extension
info before calling the facilitator; it returned `extension_echo_mismatch`.
The earlier buyer and merchant tests tested those components separately and
missed this integration failure.

A new test joins the real buyer, real SDK signer and real Express middleware,
with only chain/facilitator/upstream replaced by offline fixtures. It reproduced
`extension_echo_mismatch` with zero facilitator verify calls before the fix.
The buyer now merges the merchant declaration and buyer permit fields; bounded
3000-unit approval and billing validation remain enforced. The same test then
passed through one verify and one actual-amount settlement. All 33 Node tests
passed locally and on the VPS with no real payments.

Installed buyer hash:
`3dcd6af574bb88804457a81118ab29ce86432a0c182c96174efcb149fe2c30f2`.
Private rollback snapshot: `20261003T100036Z-before-extension-echo-fix`.
Existing wallet identities were verified unchanged. No restart is required:
each Ask request starts a new buyer process.

The second failed authorization was proven expired and unused at finalized Base
block 52115703 (timestamp 1791020753, after deadline 1791020538), archived under
`~/.sign402/ask-metered/reconciled/1791021675`, and its active block removed.
No payment was sent or retried. A successful funded metered answer remains for
the owner's next chat test; readiness is not claimed as payment success.


### October 3: first verified actual-usage Base Ask payment

The owner's new chat request completed on SingIt Ask. Independent Base RPC
verification confirms transaction
[0xe9b5af13…618e6ad](https://basescan.org/tx/0xe9b5af13f5070e882635db2ef85540a00515eea968fd93b2a513d5cf0618e6ad)
succeeded in block 52116473 at 2026-10-03T10:11:33+00:00, with 77 confirmations at inspection.
Its single native-USDC Transfer moved 1055 atomic units (0.001055 USDC) from the
user's existing Base agent to their configured merchant receiver. Transaction
gas was paid by a different facilitator address, not the user's agent.

The returned usage records 498 prompt and 335 completion tokens. Billing is
42 atomic units of provider cost + 13 of rounded 30% markup + 1000 of the
published settlement fee = 1055. The chat usage and allowance settlement ledger
agree with the chain. The 3000-unit cap was not charged. No unresolved Ask
journal remains for this account. This is a verified live Base metered payment;
Solana's actual-usage path still has no verified funded end-to-end example here.
Verification was read-only; the owner initiated the request. Changes remain
uncommitted; no PR.

## October 3: committed Ask integration

Implementation: [0e5762b](https://github.com/bubon-ik/SingItAI/commit/0e5762bc328f6a94e0f24c17a6e2a8c40dcf7887)
on `trezor-local-sidecar`. Includes Base and Solana actual-usage adapters, English
operator test page, conversation-location continuity, provider-specific errors
and the Base extension-echo fix. Verification: 33 Node tests locally and on the
VPS, 295 isolated web tests, and 21 allowance/reservation tests passed. The owner's
Base payment settled 0.001055 USDC and matches the receipt and ledger. Solana's
receiver account is ready; a real funded metered Solana payment is still untested.
The final model-picker price label correction is committed locally; this commit
step did not redeploy that static asset. Earlier entries describe historical
working-tree states; no PR was created for this integration.

## October 3: user-funded Solana network fees

The owner rejected operator-funded gas. Ask now checks the user's agent SOL
balance against live network-fee and token-account-rent quotes before funding,
and signs the funding transaction only with that agent. Insufficient SOL sends
nothing and releases the Ask hold. Wallet approvals/revokes have no operator
sponsor fallback. Allowance now exposes agent SOL and an explicit, wallet-signed
SOL transfer to the account's own agent; amount and destination are validated.
Unknown SOL transfers are not resubmitted, and new funding is blocked for review.

Verification: 77 Solana Node tests, 297 isolated web tests and 13 allowance tests
passed on the VPS without real payments. A pre-existing archive test assumed
ordering of equal timestamps; it now selects the intended chat by ID. Six-file
source-hash and unpaid-quote activation preflight passed. The update is staged,
not active: the operator must run `activate-user-gas.py` with sudo. No new wallet
or payment was created. Real user-funded Solana Ask settlement remains untested.
