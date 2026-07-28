# Approval Confirmation Copy

## Goal

Replace the internal-looking test-approval result
`Sign402 test approval approved.` with clear user-facing confirmation copy
across every linked approval channel.

## Root Cause

The gateway stores test channel checks with the internal action type
`sign402_test`. After a decision, `_decision_text()` converts every action type
other than `sign402_purchase` into the literal phrase
`Sign402 test approval <status>.`. Hermes forwards the gateway's
`imessageText` unchanged, so the technical wording appears in both WhatsApp
and iMessage.

## Copy Contract

For `sign402_test`, the decision text will be:

- approved: `✅ Approval confirmed. You're ready to approve payments.`
- denied: `Approval declined. No changes were made.`

The existing `sign402_purchase` decision text is outside this change and
remains `Sign402 payment <status>.`

## Architecture and Data Flow

The copy will be changed at its source in
`sign402_gateway.imessage_approvals._decision_text`. The decision service will
continue returning the text in `imessageText`; Hermes will continue forwarding
that value without channel-specific rewriting. This keeps WhatsApp and
iMessage consistent and avoids duplicating product copy in adapters.

No persistence schema, approval state, decision status, audit event, API
payload shape, or payment behavior changes.

## Error Handling

Unknown non-purchase action types retain the existing test-approval fallback
instead of being silently reported as successful. Only the known
`sign402_test` plus `approved` and `denied` combinations receive the new copy.

## Tests

1. The gateway decision formatter returns the exact approved copy for
   `sign402_test`.
2. The gateway decision formatter returns the exact declined copy for
   `sign402_test`.
3. Existing purchase decision copy remains unchanged.
4. Hermes integration fixtures use the new approved text and prove it is
   forwarded unchanged to the linked channel.

The focused gateway and Hermes test modules will run before the complete
relevant test suites.
