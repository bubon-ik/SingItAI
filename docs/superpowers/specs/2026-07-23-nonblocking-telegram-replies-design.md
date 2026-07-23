# Non-blocking Telegram replies

## Problem

Telegram reply-keyboard presses arrive as ordinary short text messages. Hermes
adds only an 80 ms aggregation window, but the Sign402 plugin then sends its
reply with a synchronous `urllib` request from the gateway event loop. A
production probe reproduced the long tail: four Telegram API requests completed
in 37–52 ms and one completed after 15.05 seconds. While that request is in
progress, the shared event loop cannot process the next button, so even the
first `Loading…` acknowledgement appears roughly 10–15 seconds late.

The Bitrefill catalog and MCP are not on this blocking path. They run only after
the acknowledgement has been scheduled.

## Goal

- A button handler must return without waiting for Telegram network I/O.
- Reuse Hermes's already connected asynchronous Telegram client and connection
  pool instead of opening a fresh synchronous HTTPS connection per reply.
- Preserve reply keyboards, message chunking, and message order within a chat.
- A slow send in one chat must not block input or output scheduling in another
  chat.
- Preserve the current synchronous direct request only as an off-event-loop
  fallback when the active Hermes Telegram client is unavailable.

## Design

### Scheduling

`_send_fixed_reply` remains a synchronous plugin API, but Telegram delivery
becomes scheduling-only:

1. Resolve the Telegram adapter and chat ID.
2. Capture the running Hermes event loop when called from gateway dispatch.
3. Schedule an asynchronous send on that loop. Calls made by background worker
   threads use `loop.call_soon_threadsafe` to schedule on the captured loop.
4. Return immediately; no DNS, TLS, HTTP, or response-body work may run in the
   caller.

The plugin keeps a tail task per chat. A new send awaits only the previous send
for that same chat. This preserves `Loading…` before the final result without a
global queue that would let one user's slow network response delay everyone.
Completed tails are removed to keep the map bounded by active chats.

### Delivery

The asynchronous sender uses the active Telegram bot object owned by the Hermes
adapter. This reuses Hermes's established async HTTP connection pool and network
fallback behavior.

- Plain text and chunk boundaries retain the current plugin behavior.
- Reply-keyboard dictionaries are converted lazily to Telegram
  `ReplyKeyboardMarkup` objects inside the Hermes runtime, so the project test
  environment does not acquire a new hard dependency.
- Each chunk is awaited by the per-chat task before the next chunk or reply is
  sent.

### Fallback and errors

If there is no active async Telegram bot, the existing direct Bot API sender is
run with `asyncio.to_thread`. It may wait for its existing timeout, but it cannot
block gateway dispatch. If both paths fail, the plugin logs only the exception
type and never logs the bot token, message secrets, or Bitrefill redemption
data.

Queueing failures fall back to the existing Hermes adapter for plain text.
Keyboard-bearing messages are not silently resent through two transports after
an ambiguous timeout, avoiding duplicate messages.

## Concurrency and lifecycle

- Per-chat ordering is isolated; different chats may send concurrently.
- The task map and captured loop are protected from background-thread access by
  scheduling all mutations onto the Hermes loop.
- Plugin reload or gateway restart discards only unsent in-memory tasks; there
  is no new persistent state.
- Existing Bitrefill operation generations still discard stale catalog or
  product results before they are scheduled for delivery.

## Tests

1. A fake async Telegram send that remains blocked for 15 seconds must not keep
   the button dispatch call from returning promptly.
2. Two replies in one chat must be delivered in submission order.
3. A blocked send in one chat must not prevent another chat's reply.
4. Reply keyboard fields and message chunks must be preserved.
5. Background-thread completions must schedule safely onto the captured loop.
6. The fallback must run outside the event loop and must not duplicate an
   ambiguously timed-out keyboard message.
7. Run the complete Sign402 wallet plugin and gateway test suites.

## Production validation

- Deploy without performing a purchase.
- Confirm both services are active and the deployed commit matches GitHub.
- Run a synthetic 15-second transport stall and verify button dispatch returns
  in under 50 ms.
- Measure normal scheduling for representative menu, navigation, wallet-read,
  and Bitrefill catalog buttons.
- Ask the operator to confirm that `Loading…` or the immediate menu response is
  visible without the previous 10–15 second pause.

## Non-goals

- Changing the Bitrefill MCP purchase flow.
- Removing Hermes's 80 ms short-text aggregation window.
- Retrying real purchases or generating test transactions.
- Guaranteeing delivery during a Telegram-wide outage; the requirement is that
  a network stall never freezes button processing.
