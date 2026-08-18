# Venice AI Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a paid AI chat to the Telegram bot. A user gets five free messages with no phone number and no wallet, then approves one standing policy — "up to $5 per day, Venice AI only" — and chats without further approvals.

**Architecture:** The gateway gains a Venice client that reaches the provider over the existing x402 path, a chat store that tracks per-user session state and spend windows, and three agent-facing endpoints. The Hermes plugin gains a chat mode: while a user is in it, message text bypasses the command parser and goes to the gateway. Settlement is prefunded in chunks and metered locally, never once per message.

**Tech Stack:** Python 3.11+, `unittest`, SQLite, urllib, existing CDP x402 lane, systemd.

Design reference: `docs/superpowers/specs/2026-08-11-venice-daily-budget-design.md`.

## Global Constraints

- Preserve every existing command and flow. Chat is additive.
- Free messages move no money and require no approval channel, no phone number, and no funded wallet.
- Paid chat requires an approved policy bound to Venice's `payTo`. A challenge with a different `payTo` pauses the policy and moves no funds.
- A prefund counts against the daily window when it is paid, not as it is consumed.
- Never settle once per message.
- `SIGN402_PURCHASES_PAUSED=1` stops all paid chat.
- `SIGN402_AI_CHAT_ENABLED` defaults to off. Every task must leave the bot working with the flag unset.
- Never log or persist prompt text, model output, wallet secrets, or the payment envelope beyond what settlement requires.
- The words x402, facilitator, settlement and prefund never reach the user.
- Automated tests never call the live Venice endpoint and never settle on-chain.

---

## File Map

- `sign402-gateway/sign402_gateway/venice_chat.py`: Venice client, 402 challenge validation against the bound merchant, chunked prefund accounting.
- `sign402-gateway/sign402_gateway/chat_store.py`: SQLite session state, free-message counter, UTC daily window, outstanding credit, pause flags.
- `sign402-gateway/sign402_gateway/server.py`: `/agent/chat/*` routes and production wiring.
- `hermes-plugins/sign402-wallet/client.py`: gateway calls for chat start, message and end.
- `hermes-plugins/sign402-wallet/__init__.py`: chat mode state machine, text interception, menu restructure, deferred approval-channel gate.
- `sign402-gateway/tests/test_venice_chat.py`
- `sign402-gateway/tests/test_chat_store.py`
- `sign402-gateway/tests/test_gateway_server.py`
- `hermes-plugins/sign402-wallet/tests/test_plugin.py`
- `hermes-plugins/sign402-wallet/tests/test_client.py`

---

### Task 0: Confirm the rail before writing code

Not code. Do this first; the rest of the plan is void if it fails.

- [ ] **Step 1: Resolve the Venice resource URL** from `x402-list.com/services`.

- [ ] **Step 2: Inspect the challenge through the existing lane**

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/inspect-x402 \
  -H "Content-Type: application/json" \
  -d '{"url":"<venice endpoint>"}'
```

- [ ] **Step 3: Record `network`, `asset`, `payTo`, and the price** into `SIGN402_AI_CHAT_*` env values. `payTo` is the binding the policy is built on.

If the challenge does not come back clean, stop and fix that before Task 1.

---

### Task 1: Chat Store

**Files:**
- Create: `sign402-gateway/sign402_gateway/chat_store.py`
- Test: `sign402-gateway/tests/test_chat_store.py`

**Interfaces:**
- Produces: `ChatStore` with `get_session(user_id)`, `consume_free_message(user_id)`, `record_prefund(user_id, atomic)`, `debit(user_id, atomic)`, `pause(user_id, reason)`, `resume(user_id)`.
- Window rollover is computed on read, never by a timer.

- [ ] **Step 1: Write failing window tests**

```python
def test_window_rolls_over_and_zeroes_spend_but_keeps_credit(self):
    store = ChatStore(":memory:", now=lambda: DAY_ONE_NOON)
    store.record_prefund("u1", 500_000)
    store.debit("u1", 3_000)
    store.now = lambda: DAY_ONE_NOON + 86_400
    session = store.get_session("u1")
    self.assertEqual(session.spent_atomic_this_window, 0)
    self.assertEqual(session.outstanding_atomic, 497_000)

def test_prefund_counts_against_the_window_when_paid(self):
    store = ChatStore(":memory:", now=lambda: DAY_ONE_NOON)
    store.record_prefund("u1", 500_000)
    self.assertEqual(store.get_session("u1").spent_atomic_this_window, 500_000)
```

- [ ] **Step 2: Write failing free-message tests**

```python
def test_free_messages_are_capped_and_do_not_touch_the_window(self):
    store = ChatStore(":memory:", now=lambda: DAY_ONE_NOON, free_messages=5)
    for _ in range(5):
        self.assertTrue(store.consume_free_message("u1"))
    self.assertFalse(store.consume_free_message("u1"))
    self.assertEqual(store.get_session("u1").spent_atomic_this_window, 0)
```

- [ ] **Step 3: Implement `ChatStore`.** Schema: `user_id` primary key, `window_start`, `spent_atomic`, `outstanding_atomic`, `free_used`, `paused`, `pause_reason`, `policy_hash`, `bound_pay_to`, `updated_at`. Directory `0700`, database `0600`, matching `user_wallets.py`.

- [ ] **Step 4: Add a row-level claim** used by the prefund path, so two concurrent messages cannot both prefund. Test that a second claim on a held row fails rather than waits.

---

### Task 2: Venice Client

**Files:**
- Create: `sign402-gateway/sign402_gateway/venice_chat.py`
- Test: `sign402-gateway/tests/test_venice_chat.py`

**Interfaces:**
- Consumes: an injected x402 buy callable, so tests never settle.
- Produces: `VeniceChatClient.send(session, prompt) -> ChatResult(text, cost_atomic, prefunded)`.

- [ ] **Step 1: Write failing merchant-binding tests**

```python
def test_challenge_with_unexpected_pay_to_pauses_and_moves_no_funds(self):
    client = self.make_client(challenge_pay_to="0xdead")
    with self.assertRaises(MerchantChanged):
        client.send(self.session_bound_to("0xbeef"), "hi")
    self.assertEqual(self.buy_calls, [])

def test_network_or_asset_mismatch_is_refused(self):
    ...
```

- [ ] **Step 2: Write failing prefund tests**

```python
def test_local_credit_is_used_without_settling(self):
    client = self.make_client()
    session = self.session_with_credit(500_000)
    client.send(session, "hi")
    self.assertEqual(self.buy_calls, [])

def test_prefund_exceeding_remaining_window_is_refused_before_settlement(self):
    ...

def test_prefund_exceeding_max_outstanding_is_refused(self):
    ...
```

- [ ] **Step 3: Implement the send path** in the order given by the design spec: pause checks, local credit, challenge fetch, binding validation, cap checks, claim, settle, call, debit actual cost.

- [ ] **Step 4: Map failures to the spec's states** — `WINDOW_EXHAUSTED`, `MERCHANT_CHANGED`, `PROVIDER_UNAVAILABLE`, `PREFUND_FAILED`, `RECONCILIATION_REQUIRED`. Test that only the last one pauses without an automatic retry.

---

### Task 3: Gateway Endpoints

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`

**Interfaces:**
- Produces: `POST /agent/chat/start`, `POST /agent/chat/message`, `POST /agent/chat/end`.
- All three require the wallet bearer token, like the other `/agent/wallet*` routes.

- [ ] **Step 1: Write failing route tests** covering: flag off returns 404; missing bearer returns 401; `SIGN402_PURCHASES_PAUSED=1` refuses paid messages but still serves free ones; response never contains prompt text in logs.

- [ ] **Step 2: Implement the routes.** `/start` returns free messages remaining, whether a policy exists, and the daily cap. `/message` returns answer text, cost, remaining window. `/end` clears mode.

- [ ] **Step 3: Add the endpoints to the `/health` listing** behind the same flag.

---

### Task 4: Plugin Chat Mode

**Files:**
- Modify: `hermes-plugins/sign402-wallet/client.py`, `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_client.py`, `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Produces: per-user chat mode flag, bounded like `_TELEGRAM_OPERATION_MAX_USERS`.

- [ ] **Step 1: Write failing interception tests**

```python
def test_text_in_chat_mode_goes_to_gateway_not_command_parser(self):
    plugin = self.make_plugin(chat_mode={"42"})
    result = plugin.handle_text("42", "balance")
    self.assertEqual(self.gateway_calls[-1][0], "chat-message")

def test_exit_button_leaves_chat_mode_and_restores_main_menu(self):
    ...

def test_text_outside_chat_mode_is_unchanged(self):
    ...
```

- [ ] **Step 2: Implement the mode.** Entering swaps the keyboard to a single `Stop chat` button; leaving restores `_TELEGRAM_MAIN_MENU_BUTTONS`. The mode check runs before the existing button and command dispatch.

- [ ] **Step 3: Append the footer** to each answer: cost of this message and remaining daily budget. One 80%-of-cap warning per window, not per message.

- [ ] **Step 4: Verify the pre-dispatch hook.** With `SIGN402_TELEGRAM_SIGN402_ONLY=1`, unknown text currently returns the menu; in chat mode it must reach Venice instead. Test both.

---

### Task 5: Deferred Approval-Channel Gate

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

- [ ] **Step 1: Write failing gate tests**

```python
def test_free_messages_need_no_approval_channel_or_wallet(self):
    ...

def test_first_paid_action_asks_for_the_approval_channel(self):
    ...

def test_catalog_browsing_needs_no_approval_channel(self):
    ...
```

- [ ] **Step 2: Move the channel prompt** out of `/start` and into the first action that moves money. `/start` shows two actions and nothing else.

- [ ] **Step 3: Reword the prompt** to explain the benefit, not the requirement: approvals arrive on a channel separate from Telegram, so a compromised Telegram account cannot spend. State that the number is not verified and not shared. Keep iMessage and WhatsApp equal — Android users have no iMessage.

- [ ] **Step 4: Add the channel status line** to the wallet view rather than the main menu.

---

### Task 6: Daily Budget Policy

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`, `chat_store.py`, `venice_chat.py`
- Test: `sign402-gateway/tests/test_chat_store.py`, `test_gateway_server.py`

- [ ] **Step 1: Write failing approval tests** — a policy without `expiresAt` is rejected; the approval context names the merchant, the daily cap and the expiry; the device line contains `/DAY`.

- [ ] **Step 2: Implement policy approval** reusing the existing approval-channel path, binding `payTo` at approval time.

- [ ] **Step 3: Implement the raise-limit flow.** Raising requires a new approval and never silently continues.

- [ ] **Step 4: Implement refunds.** On pause, expiry or revocation, outstanding credit is displayed and stays claimable. Test that it survives a restart.

---

### Task 7: payTo Watcher

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`, `venice_chat.py`
- Test: `sign402-gateway/tests/test_venice_chat.py`

- [ ] **Step 1: Write failing watcher tests** — a `payto_changed` event pauses every policy bound to the old address and notifies once, not per policy per poll.

- [ ] **Step 2: Poll the x402-list changes feed** for `payto_changed`, filtered to bound merchants. Treat an unexpected live `payTo` as the same event, since the feed can lag.

- [ ] **Step 3: Require fresh approval** showing the new address. Never migrate silently.

---

### Task 8: Rollout

- [ ] **Step 1: Full test run**

```bash
python3 -m pytest sign402-gateway/tests/ hermes-plugins/sign402-wallet/tests/
```

- [ ] **Step 2: Verify the flag-off path.** With `SIGN402_AI_CHAT_ENABLED` unset, every existing command behaves exactly as before and `/agent/chat/*` returns 404.

- [ ] **Step 3: Back up state** before touching the VPS: `./scripts/backup-sign402-state.sh`.

- [ ] **Step 4: Deploy free tier only.** Enable the flag with the daily cap at zero so only free messages work. Confirm on a Telegram account that has never used the bot.

- [ ] **Step 5: Enable paid chat** for the operator account alone, with a $0.10 cap and a $0.02 chunk. Confirm the cap message, the next-day reset, and that a `payTo` mismatch pauses.

- [ ] **Step 6: Raise caps and open to beta users.** Update `docs/production-beta-checklist.md` with the new flow and the corrected 1% service fee figure.

---

## Open Questions

1. Free messages: funded from the operator wallet through the same x402 path, or from a separate Venice account outside it? Affects Task 2's shape and must be settled before Task 1.
2. Does unused credit roll into the next day or expire with the window?
3. `/llm_buy` (Bankr credits for the user's own agent) and chat (Venice, per message) are two different AI-money features. Confirm they stay in separate menu levels so support does not spend its life explaining the difference.
