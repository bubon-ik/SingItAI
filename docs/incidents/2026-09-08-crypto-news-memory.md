# Crypto News repeat purchase blocked, 8 September 2026

At 08:55:34 UTC, Sibyl recorded `BLOCK / repeated_escalations` for
`x402.ottoai.services`, five seconds after a successful settlement. Three
`unknown_merchant` decisions during earlier cold starts were counted as
evidence against the seller. Once the seller became known, the journal rule
ran and blocked the repeat purchase. The Telegram client discarded the
gateway's `telegramText` for `buy-tool`, hiding the reason behind
`Wallet request failed`.

The deployed Spending Memory revision was
`2bc8ec6efbac063029caf812db99f17108655baf`. It already supported `claim_scope`;
the older local 0.5.0 installation did not. That local dependency mismatch
was a separate failure, not the cause of the production incident.

## Correction

- Pin Spending Memory to `443743ea2d69ef765528bd70b28a7e2d8c80e564`.
  The journal rule counts `price_spike` and `previously_rejected`, excluding
  initial introductions and an owner's exhausted daily budget. Existing
  merchant, payout-address, budget, and duplicate-payment checks still run.
- Display the gateway's buyer-facing text for memory blocks and approval
  refusals. Unexpected upstream errors remain hidden.
- Release the wallet budget reservation if converting or authorising a payment
  fails before the caller receives its reservation ID.

No memory records need deleting or rewriting. Old journal entries are filtered
when read, and merchant history remains available after restart.

## Verification

The regression test records three initial approval requests, settles one
purchase, and attempts a new request. The old dependency returns the exact
production block; the corrected dependency allows it. A companion test keeps
repeated price spikes blocked. The dependency tests also reopen the Sibyl
database to verify the result survives a fresh memory client.

Gateway and plugin tests cover reservation cleanup, distinct request IDs,
retries, readable policy refusals, and hiding unexpected error details.
All payment execution in these tests is mocked.

## Deployment

Install the pinned dependency in the interpreter used by the systemd gateway
unit, not only in a developer environment. A git pull alone does not update
installed Python dependencies. Deploy the gateway and plugin changes together,
then restart `sign402-gateway` and the user's `hermes-gateway` during a quiet
period. Verify `/health` and both service states. Live payment verification
requires a separate user purchase.
