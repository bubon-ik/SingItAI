# Telegram Smooth Button Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Sign402 Telegram button acknowledge one tap immediately while slow gateway work runs outside the shared Telegram event loop and duplicate taps remain idempotent.

**Architecture:** Add a bounded per-user background-operation coordinator to the existing wallet plugin, canonicalize duplicate lines produced by Hermes text batching, and place every network-bound menu operation behind the coordinator. Wizard completions carry a generation token so Back or a newer action suppresses stale results.

**Tech Stack:** Python 3.11, Hermes plugin hooks, `threading.RLock`, `unittest`, systemd user service configuration.

## Global Constraints

- Navigation-only button handlers remain synchronous and perform no network I/O.
- A network-bound button posts one action-specific acknowledgement before starting background work.
- At most one operation for the same user and action may be active.
- Back or a new top-level action invalidates the previous operation generation.
- Purchase confirmation, spending limits, and payment safeguards remain unchanged.
- Production verification must not call purchase or payment operations.

---

### Task 1: Canonical button input and operation coordinator

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Produces: `_canonical_button_text(text: str) -> str`
- Produces: `_reserve_telegram_operation(user_id: str, action: str) -> int | None`
- Produces: `_telegram_operation_is_current(user_id: str, generation: int) -> bool`
- Produces: `_finish_telegram_operation(user_id: str, generation: int) -> bool`
- Produces: `_invalidate_telegram_operation(user_id: str) -> None`

- [ ] **Step 1: Write failing tests for batched taps and single-flight operations**

Add tests that dispatch `Change Country\nChange Country` after opening Bitrefill and assert exactly one country prompt, then reserve the same `(user, action)` twice and assert the second reservation returns `None`.

```python
def test_bitrefill_batched_double_tap_is_one_button_press(self):
    dispatch("Buy Bitrefill")
    before = len(gateway.adapters["telegram"].sent)
    self.assertEqual(dispatch("Change Country\nChange Country"), plugin._SKIP_RESULT)
    self.assertEqual(len(gateway.adapters["telegram"].sent), before + 1)
    self.assertIn("Send a two-letter country code", gateway.adapters["telegram"].sent[-1][1])

def test_telegram_operation_reservation_is_single_flight(self):
    first = plugin._reserve_telegram_operation("user-1", "balance")
    self.assertIsInstance(first, int)
    self.assertIsNone(plugin._reserve_telegram_operation("user-1", "balance"))
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
payment-executor/.venv/bin/python -m unittest \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
```

Expected: FAIL because duplicate lines are not recognized and coordinator helpers do not exist.

- [ ] **Step 3: Implement canonicalization and the bounded coordinator**

Use an `RLock`, monotonically increasing generation counter, and dictionaries capped at 4096 users. `_canonical_button_text` must split non-empty lines, normalize each line with `_normalize_button_text`, and return one value only when every line is identical; otherwise it returns the normal whole-text normalization.

Coordinator semantics:

```python
generation = _reserve_telegram_operation(user_id, action)
if generation is None:
    return dict(_SKIP_RESULT)
if _finish_telegram_operation(user_id, generation):
    _send_fixed_reply(...)
```

`_invalidate_telegram_operation` increments the user's generation and removes its active reservation. Update Bitrefill, withdraw, and global Back handlers to invalidate before changing screens.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all plugin tests pass.

- [ ] **Step 5: Commit the isolated deliverable**

```bash
git add hermes-plugins/sign402-wallet/__init__.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "Add Telegram button operation coordinator"
```

### Task 2: Non-blocking top-level menu operations

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: coordinator functions from Task 1.
- Produces: `_start_telegram_background_operation(*, identity, action, started_text, source, gateway, work, reply_markup=None) -> dict`
- Produces: `_telegram_public_command_result(command: str, args: str, identity: TelegramIdentity) -> tuple[str, dict | None]`

- [ ] **Step 1: Write a failing non-blocking dispatch test**

Use a `threading.Event` in a fake balance client. The dispatch must return and send `Checking balance…` before the event is released; a `Change Country` event for another user must still complete while the fake balance call is blocked.

```python
started = threading.Event()
release = threading.Event()

def blocking_execute(*args, **kwargs):
    started.set()
    release.wait(2)
    return "Balance ready."

self.assertEqual(dispatch_user_1("Balance"), plugin._SKIP_RESULT)
self.assertTrue(started.wait(1))
self.assertIn("Checking balance", sent_for_user_1[-1])
self.assertEqual(dispatch_user_2("Buy Bitrefill"), plugin._SKIP_RESULT)
release.set()
```

Also dispatch Balance twice while pending and assert the fake client is called once.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
payment-executor/.venv/bin/python -m unittest \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
```

Expected: FAIL because Balance blocks the dispatching thread.

- [ ] **Step 3: Implement the background wrapper and migrate public commands**

The wrapper reserves an operation, sends the acknowledgement, invokes `_background_runner`, translates `GatewayClientError` to `exc.user_message`, translates unexpected exceptions to `_UNEXPECTED_ERROR_MESSAGE`, verifies the generation before sending, and releases the reservation in every branch.

Move wallet creation, balance, last purchase, limits, approval-channel connection, LLM credit calls, direct Bitrefill token lookup, and withdraw token lookup into `work` callbacks. Keep Help, menu navigation, argument validation, and country/category prompts synchronous.

Use action-specific acknowledgements:

```python
{
    "start": "Loading wallet…",
    "wallet": "Loading wallet…",
    "balance": "Checking balance…",
    "last-purchase": "Loading last purchase…",
    "limits": "Loading spending limits…",
    "set-limits": "Updating spending limits…",
    "connect-imessage": "Loading approval settings…",
    "connect-whatsapp": "Loading approval settings…",
    "llm-buy": "Preparing LLM credits…",
    "llm-terms": "Updating LLM terms…",
    "llm-credits": "Checking LLM credits…",
}
```

- [ ] **Step 4: Run plugin tests and verify GREEN**

Run the command from Step 2. Expected: all plugin tests pass with no duplicate gateway calls.

- [ ] **Step 5: Commit the isolated deliverable**

```bash
git add hermes-plugins/sign402-wallet/__init__.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "Run Telegram menu requests without blocking"
```

### Task 3: Non-blocking Bitrefill and withdraw wizard reads

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: background wrapper and generation coordinator from Tasks 1–2.
- Produces: loading stages `loading-search`, `loading-catalog`, `loading-product`, `loading-payment-tokens`, and `loading-withdraw-tokens` containing `operationGeneration`.

- [ ] **Step 1: Write failing tests for catalog responsiveness and stale-result suppression**

Block `list_bitrefill_products` with events. Assert the category press immediately posts `Loading catalog…`, a second identical press makes no second list call, Back returns to the prior menu, and releasing the blocked call does not post a stale product page.

Add equivalent focused tests for product details and payment-token lookup. Keep purchase fakes uncalled.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
payment-executor/.venv/bin/python -m unittest \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
```

Expected: FAIL because the current catalog/details/token calls block inline and late results overwrite sessions.

- [ ] **Step 3: Move wizard reads behind generation-checked workers**

Before scheduling, store a loading-stage session with immutable request inputs and the reserved generation. Each worker performs only the gateway read first. It then calls `_telegram_operation_is_current`; only a current worker may commit the next session and send its result. Back calls `_invalidate_telegram_operation` and restores the appropriate parent screen.

Use `Searching products…`, `Loading catalog…`, `Loading product…`, `Loading payment options…`, and `Loading assets…` acknowledgements. Do not move the existing validated purchase or withdrawal execution out of their current background paths; add only the same single-flight reservation around their start so a duplicate selection cannot schedule a second transaction.

- [ ] **Step 4: Run plugin tests and verify GREEN**

Run:

```bash
payment-executor/.venv/bin/python -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests
```

Expected: 0 failures and no purchase call from responsiveness tests.

- [ ] **Step 5: Commit the isolated deliverable**

```bash
git add hermes-plugins/sign402-wallet/__init__.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "Make Bitrefill wizard reads responsive"
```

### Task 4: Production tuning and full verification

**Files:**
- Modify: `hermes-plugins/sign402-wallet/README.md`
- Modify on server: `/home/hermes/.hermes/.env`

**Interfaces:**
- Consumes: all behavior from Tasks 1–3.
- Produces: `HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS=0.08` in production.

- [ ] **Step 1: Document the production latency setting**

Add the following operator setting near the Telegram public-beta environment block:

```dotenv
HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS=0.08
```

Explain that 0.08 is Hermes' supported minimum for short text while long split messages retain their separate delay.

- [ ] **Step 2: Run complete local verification**

Run:

```bash
payment-executor/.venv/bin/python -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests
payment-executor/.venv/bin/python -m unittest discover \
  -s sign402-gateway/tests
git diff --check
```

Expected: every suite exits 0 and `git diff --check` prints nothing.

- [ ] **Step 3: Verify the exact committed snapshot in a clean worktree**

Create a detached temporary worktree at HEAD and repeat both complete test commands using `/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python`. Expected: every test passes from the exact commit without relying on unrelated local changes.

- [ ] **Step 4: Push and deploy**

Push `x402Bnkr` to `singitai`, fast-forward `/home/hermes/apps/sign402`, set the batch delay in `/home/hermes/.hermes/.env` without printing any secrets, and restart `hermes-gateway.service` plus `sign402-gateway.service` only if its code changed.

- [ ] **Step 5: Production verification without spending**

Confirm both services are active, the server commit equals local HEAD, the Telegram batch setting resolves to `0.08`, and read-only `/health`, wallet balance, and Bitrefill catalog operations succeed. Inspect logs for duplicate-operation, event-loop, and traceback errors. Do not call Bitrefill purchase, withdrawal execution, or any payment operation.
