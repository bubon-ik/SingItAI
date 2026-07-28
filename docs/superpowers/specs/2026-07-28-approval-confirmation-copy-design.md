# Approval Confirmation Copy

## Goal

Replace the internal-looking result `Sign402 test approval approved.` with
clear, action-appropriate confirmation copy across every linked approval
channel.

## Root Cause

The gateway stores test channel checks with the internal action type
`sign402_test`, but real flows also use `sign402_bitrefill`,
`sign402_bankr_llm`, and `sign402_withdrawal`. After a decision,
`_decision_text()` treats only `sign402_purchase` as a payment and converts
every other action type into `Sign402 test approval <status>.`. Hermes
forwards the gateway's `imessageText` unchanged, so the technical wording
appears in both WhatsApp and iMessage, including after real Bitrefill
purchases.

## Copy Contract

For `sign402_test`, the decision text will be:

- approved: `✅ Approval confirmed. You're ready to approve payments.`
- denied: `Approval declined. No changes were made.`

For purchase action types `sign402_purchase`, `sign402_bitrefill`, and
`sign402_bankr_llm`, the decision text will be:

- approved: `✅ Payment approved. Your purchase is being processed.`
- denied: `Payment declined. No funds were moved.`

For `sign402_withdrawal`, the decision text will be:

- approved: `✅ Withdrawal approved. Your transfer is being processed.`
- denied: `Withdrawal declined. No funds were moved.`

## Architecture and Data Flow

The copy will be changed at its source in
`sign402_gateway.imessage_approvals._decision_text`. The decision service will
continue returning the text in `imessageText`; Hermes will continue forwarding
that value without channel-specific rewriting. This keeps WhatsApp and
iMessage consistent and avoids duplicating product copy in adapters.

No persistence schema, approval state, decision status, audit event, API
payload shape, or payment behavior changes.

## Error Handling

Unknown action types use neutral copy instead of being mislabeled as a test,
purchase, or withdrawal:

- approved: `✅ Approval confirmed. Your request is being processed.`
- denied: `Approval declined. No changes were made.`

Unexpected decision statuses retain a conservative
`Sign402 approval <status>.` fallback and are never described as approved.

## Tests

1. A real Bitrefill decision returns the exact approved purchase copy.
2. A denied Bitrefill decision returns the exact declined payment copy.
3. A test-channel decision returns the exact approved and declined test copy.
4. A withdrawal decision uses transfer-specific copy.
5. An unknown action type uses neutral request copy.
6. Hermes integration fixtures use the new approved Bitrefill text and prove
   it is forwarded unchanged to the linked channel.

The focused gateway and Hermes test modules will run before the complete
relevant test suites.
