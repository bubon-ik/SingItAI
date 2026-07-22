# Telegram Smooth Button Dispatch Design

## Goal

Every Sign402 Telegram button must react after one tap without freezing the
conversation while wallet, chain, or Bitrefill data is loading. Repeated taps
must not start duplicate work or advance a multi-step purchase twice.

## Confirmed root causes

Hermes batches ordinary Telegram text for at least 80–180 ms so that fragments
of long messages can be joined. Two fast taps can therefore reach the plugin as
one string such as `Change Country\nChange Country`, which the current button
normalizer does not recognize.

The Sign402 pre-dispatch hook also performs network-bound gateway calls
synchronously. A production Bitrefill catalog read took about 3.4 seconds.
During such a call the shared Telegram event loop cannot dispatch unrelated
button presses, so the UI appears frozen and users tap again.

## Interaction design

Navigation-only actions such as Back, Change Country, Browse Catalog, and menu
selection remain synchronous because they only update in-memory flow state and
render a prompt. Repeated identical lines created by Telegram batching are
collapsed into one logical button press.

Any action that needs the gateway, blockchain, Photon, or Bitrefill is handled
as a background operation:

1. Atomically reserve a per-user operation slot and update the wizard to a
   loading state.
2. Immediately send a short action-specific acknowledgement, for example
   `Loading catalog…`, `Checking balance…`, or `Loading wallet…`.
3. Run the existing network call outside the Telegram event loop.
4. Publish the result and release the slot.

If the same button is received again while its operation is pending, the event
is consumed without starting another call or posting another acknowledgement.
Other navigation remains responsive. Back or a new top-level action invalidates
the old flow generation; a late background result from that generation is
discarded instead of overwriting the user's current screen.

Purchase confirmation and payment safeguards do not change. In particular, a
duplicate amount, token, or confirmation tap can never create a second order.

## Components

The wallet plugin will gain three small internal helpers:

- a button canonicalizer that recognizes batched identical lines as one input;
- a bounded, thread-safe per-user operation registry with unique generation
  identifiers;
- a background action wrapper that owns acknowledgement, error translation,
  stale-result suppression, and cleanup.

Existing handlers keep responsibility for validation and formatting. Only the
boundary around blocking calls changes. The initial scope covers all current
network-bound Telegram menu paths: wallet creation, balances, approval-channel
connection, limits, withdrawals, Bitrefill catalog/search/details/token lookup,
last purchase, and LLM-credit operations.

The production Hermes text-batch cap will be set to its supported minimum of
`0.08` seconds. Long Telegram messages still retain the separate split-message
delay, so reducing the short-message delay does not break 4096-character
message aggregation.

## Error handling

Gateway and provider errors use the existing user-safe messages. The operation
slot is released in `finally`, including on timeout or unexpected exceptions.
No secrets, redemption codes, API keys, or raw provider errors are included in
acknowledgements or logs.

If scheduling the background task itself fails, the action fails closed and the
user receives the existing temporary-unavailable message. A stale completion
is logged at debug level and not shown.

## Verification

Tests will prove the following behavior with a deliberately blocking fake
client:

- the dispatch hook returns before the fake network call completes;
- another user's navigation button is processed while the first call is still
  blocked;
- `Change Country\nChange Country` produces exactly one country prompt;
- two taps during one pending action produce exactly one gateway call;
- Back invalidates and suppresses a late background result;
- duplicate selection taps cannot skip a purchase stage or create two orders;
- existing Telegram, Photon, wallet, and Bitrefill test suites remain green.

Production verification will use read-only wallet/catalog operations, inspect
service health and logs, and avoid purchase or payment calls.

## Success criteria

- A navigation button produces its next prompt without a network dependency.
- A network-bound button produces acknowledgement immediately after dispatch
  and never blocks processing of another button.
- One user tap is sufficient; repeated taps do not duplicate work or messages.
- No existing confirmation, spending-limit, or payment safety gate is weakened.
