# Conversation and Telegram Menu — September 19, 2026

Implementation: [`37f7863`](https://github.com/bubon-ik/singit-solana/commit/37f7863).
This builds on the native Telegram interface milestone. It adds no Mini App and
changes no chain settlement implementation.

## Behavior

- The native Telegram **Menu** contains Shop, Chat, Wallet, Purchases, Settings
  and Help. Chat is hidden when its existing feature flag is disabled. Existing
  slash commands and old button labels remain accepted.
- Home and AI controls appear under messages. The first private reply after a
  restart removes the old persistent keyboard, then attaches inline controls
  to the same message. Older Hermes adapters use a one-time text keyboard.
- Ordinary private text checks the user's gateway policy and enters AI without
  a Chat/Stop chat mode. Explicit commands and active phone, withdrawal and
  shopping forms take priority. Unknown slash commands never become AI prompts.
- With no active policy, the original question is held in bounded process
  memory for up to 15 minutes of setup validity: confirm/select a model,
  choose a daily top-up limit, review terms, then request approval on the linked
  phone channel. Only an explicit successful approval sends the saved question.
- Model selection is free. AI settings display catalog input/output token
  prices, last recorded Venice credit, remaining daily top-up allowance and
  approval expiry. The daily allowance caps new funding, not consumption of
  previously paid Venice credit. The old misleading usage warning was removed.
- The current presets are $5/$10/$20 per day for 30 days. They are application
  choices. Budget approval itself is not a payment; a subsequent question may
  trigger a Base USDC x402 top-up under the existing payment policy.
- `/cancel` or navigation discards the deferred question. An external approval
  or payment already submitted can still finish. Existing standing approvals
  and paid provider credit are not revoked or refunded by navigation.
- AI work runs off the incoming-message hook. Per-user guards prevent a second
  chat payment while the first request is running, including after navigation.
  A paid answer arriving after navigation is still delivered. Reconciliation
  pauses cannot be cleared through the new budget screen.
- Short explicit phrases such as `Buy Alza 200 CZK` or `Купить Alza 200 CZK`
  start catalog search only. Product, country, available denomination, payment
  token and order review remain part of the existing purchase flow. This is a
  small deterministic shortcut, not a general natural-language shopping agent.

## Verification

- 1,246 gateway tests passed locally.
- 332 plugin tests passed locally with PTB 22.5, including actual PTB application,
  callback and markup objects with mocked transport.
- New checks cover consent before the saved question, negative/missing approval,
  cancellation before and during external approval, expiry, user/chat isolation,
  bounded drafts, duplicate input, status/scheduling failure, navigation during
  inference, preserved late answers, form precedence, private-only AI, expired
  policy, reconciliation pause, free model selection and catalog-only shortcuts.
- Transport checks cover keyboard removal, same-message inline attachment,
  attachment failure recovery, subsequent inline Home and deleted-card fallback.
- All payment/provider responses and Telegram transport in tests are fixtures.
  No real inference, top-up, gift-card purchase or redemption is part of testing.

## Manual check on the existing bot

1. Send `/start`: the large bottom keyboard disappears; Home buttons sit below
   the message and native **Menu** lists the main sections.
2. Send a question. A user with an existing active budget receives a paid answer;
   a new user first sees model setup. To test without spending, stop before
   **Request budget approval**. An approved user's ordinary question can spend.
3. Open `/chat`: compare Venice credit, top-up allowance, model prices and expiry.
4. During setup open `/wallet` or send `/cancel`: the saved question is discarded.
5. Send `Buy Alza 200 CZK`: inspect catalog results and available denominations.
   Stop at order review to avoid a purchase.
6. Open Purchases and Settings through Menu while chatting; both remain reachable.

## Limits

The deployed agent's chat and shop payment execution still uses Base. Solana
wallet/deposit/balance support and the standalone Solana client are separate
milestones; this interface does not claim integrated Solana purchases. The
existing Venice inference request remains per-question; this change does not
add multi-turn model memory. Drafts are neither durable nor replayed after a
restart. A restart invalidates old inline controls; `/start` restores navigation.
