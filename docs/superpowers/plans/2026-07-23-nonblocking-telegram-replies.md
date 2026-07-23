# Non-blocking Telegram Replies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Sign402 Telegram button handler return without waiting for Telegram network I/O while preserving keyboards and per-chat message order.

**Architecture:** Replace synchronous Bot API sends inside the Hermes event loop with per-chat asynchronous task chains scheduled on Hermes's running loop. Reuse the active Telegram adapter bot and its connection pool; retain the current direct HTTP sender only as an `asyncio.to_thread` fallback.

**Tech Stack:** Python 3.12, `asyncio`, `threading`, Hermes Telegram adapter, python-telegram-bot at runtime, `unittest`.

## Global Constraints

- Do not change the Bitrefill MCP purchase flow or perform a purchase.
- Preserve reply keyboards, 3,900-character chunking, stale-result suppression, and per-chat order.
- No DNS, TLS, HTTP, or response-body work may run in the gateway event-loop caller.
- A stalled send in one chat must not block scheduling or delivery in another chat.
- Do not add python-telegram-bot as a project dependency; import `ReplyKeyboardMarkup` lazily only in the Hermes runtime.
- Do not stage or modify the user's unrelated dirty files.

---

### Task 1: Reproduce the event-loop blockage

**Files:**
- Modify: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: existing `load_plugin()`, `FakeGateway`, `FakeEvent`, and `pre_gateway_dispatch` hook.
- Produces: regression tests that require non-blocking scheduling, same-chat FIFO ordering, and cross-chat isolation.

- [ ] **Step 1: Add a controllable asynchronous Telegram bot fake**

Add this helper beside `FakeAdapter`:

```python
class ControlledTelegramBot:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        return object()


class FakeAdapter:
    def __init__(self, bot=None):
        self.sent = []
        self._bot = bot

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))
```

Allow `FakeGateway` to receive a prepared adapter:

```python
class FakeGateway:
    def __init__(self, adapter_key="photon", adapter=None):
        self.adapters = {adapter_key: adapter or FakeAdapter()}
        self.pairing_store = FakePairingStore()
```

- [ ] **Step 2: Write the failing non-blocking dispatch test**

Add an isolated async test class:

```python
class TelegramAsyncReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_dispatch_does_not_wait_for_slow_telegram_send(self):
        plugin = load_plugin()
        context = FakeContext()
        callbacks = []
        plugin._background_runner = callbacks.append
        plugin.register(context)
        bot = ControlledTelegramBot()
        gateway = FakeGateway(adapter_key="telegram", adapter=FakeAdapter(bot))

        started = asyncio.get_running_loop().time()
        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("Balance", "1045618308", chat_id="telegram-chat"),
            gateway=gateway,
        )
        elapsed = asyncio.get_running_loop().time() - started

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertLess(elapsed, 0.05)
        await asyncio.wait_for(bot.started.wait(), timeout=0.2)
        self.assertEqual(bot.calls[0]["text"], "Checking balance…")
        bot.release.set()
```

- [ ] **Step 3: Write same-chat order and cross-chat isolation tests**

Use one bot fake that records calls and exposes a release event per chat:

```python
class PerChatControlledTelegramBot:
    def __init__(self):
        self.calls = []
        self.started = {}
        self.release = {}

    def started_event(self, chat_id):
        return self.started.setdefault(str(chat_id), asyncio.Event())

    def release_event(self, chat_id):
        return self.release.setdefault(str(chat_id), asyncio.Event())

    async def send_message(self, **kwargs):
        chat_id = str(kwargs["chat_id"])
        self.calls.append((chat_id, kwargs["text"], kwargs.get("reply_markup")))
        self.started_event(chat_id).set()
        await self.release_event(chat_id).wait()
        return object()
```

Add these methods to `TelegramAsyncReplyTests`:

```python
async def test_replies_are_ordered_per_chat_without_cross_chat_blocking(self):
    plugin = load_plugin()
    bot = PerChatControlledTelegramBot()
    gateway = FakeGateway(adapter_key="telegram", adapter=FakeAdapter(bot))
    source_a = FakeSource(FakePlatform("telegram"), "user-a", chat_id="chat-a")
    source_b = FakeSource(FakePlatform("telegram"), "user-b", chat_id="chat-b")

    plugin._send_fixed_reply(gateway, source_a, "first")
    plugin._send_fixed_reply(gateway, source_a, "second")
    await asyncio.wait_for(bot.started_event("chat-a").wait(), timeout=0.2)
    self.assertEqual(
        [text for chat, text, _ in bot.calls if chat == "chat-a"],
        ["first"],
    )

    plugin._send_fixed_reply(gateway, source_b, "other chat")
    await asyncio.wait_for(bot.started_event("chat-b").wait(), timeout=0.2)
    self.assertIn(("chat-b", "other chat", None), bot.calls)

    bot.release_event("chat-b").set()
    bot.release_event("chat-a").set()
    for _ in range(10):
        if len([call for call in bot.calls if call[0] == "chat-a"]) == 2:
            break
        await asyncio.sleep(0)
    self.assertEqual(
        [text for chat, text, _ in bot.calls if chat == "chat-a"],
        ["first", "second"],
    )

async def test_background_thread_schedules_on_captured_gateway_loop(self):
    plugin = load_plugin()
    bot = PerChatControlledTelegramBot()
    gateway = FakeGateway(adapter_key="telegram", adapter=FakeAdapter(bot))
    source = FakeSource(FakePlatform("telegram"), "user-a", chat_id="chat-a")

    plugin._send_fixed_reply(gateway, source, "loop reply")
    await asyncio.wait_for(bot.started_event("chat-a").wait(), timeout=0.2)

    worker = threading.Thread(
        target=plugin._send_fixed_reply,
        args=(gateway, source, "thread reply"),
    )
    worker.start()
    worker.join(timeout=0.2)
    self.assertFalse(worker.is_alive())

    bot.release_event("chat-a").set()
    for _ in range(10):
        if len(bot.calls) == 2:
            break
        await asyncio.sleep(0)
    self.assertEqual([call[1] for call in bot.calls], ["loop reply", "thread reply"])
```

- [ ] **Step 4: Run the regression tests and verify RED**

Run:

```bash
../../payment-executor/.venv/bin/python -m unittest \
  tests.test_plugin.TelegramAsyncReplyTests
```

Expected: FAIL because `_send_fixed_reply` invokes the synchronous direct Bot
API path before returning or because no active-bot scheduling path exists.

---

### Task 2: Add per-chat asynchronous delivery

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: `gateway.adapters["telegram"]`, adapter `_bot.send_message`, `_telegram_message_chunks`, `_send_telegram_reply_direct`, and reply-keyboard dictionaries.
- Produces: `_schedule_telegram_reply(...) -> bool`, `_enqueue_telegram_reply_on_loop(...) -> None`, `_send_telegram_reply_async(...) -> None`, and `_telegram_reply_markup_object(...) -> object | None`.

- [ ] **Step 1: Add delivery-loop state**

Add these globals beside the Telegram operation state:

```python
_TELEGRAM_DELIVERY_LOOP: asyncio.AbstractEventLoop | None = None
_TELEGRAM_DELIVERY_LOOP_LOCK = threading.RLock()
_TELEGRAM_SEND_TAILS: dict[str, asyncio.Task] = {}
```

- [ ] **Step 2: Replace the blocking branch in `_send_fixed_reply`**

Use scheduling before the generic adapter fallback:

```python
def _send_fixed_reply(gateway, source, text: str, *, reply_markup: dict | None = None) -> None:
    if _is_telegram_source(source) and _schedule_telegram_reply(
        gateway,
        source,
        text,
        reply_markup=reply_markup,
    ):
        return
    if _is_telegram_source(source) and _send_telegram_reply_direct(
        source,
        text,
        reply_markup=reply_markup,
    ):
        return
    _send_via_gateway_adapter(gateway, source, text)
```

Move the existing adapter-send block into:

```python
def _send_via_gateway_adapter(gateway, source, text: str) -> None:
    if gateway is None:
        return
    adapters = getattr(gateway, "adapters", {}) or {}
    adapter = adapters.get(_platform_name(source))
    if adapter is None:
        adapter = adapters.get(getattr(source, "platform", None))
    send = getattr(adapter, "send", None)
    if not callable(send):
        return
    chat_id = str(
        getattr(source, "chat_id", "")
        or getattr(source, "user_id", "")
        or ""
    )
    if not chat_id:
        return
    coroutine = send(chat_id, text)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coroutine)
    else:
        task = loop.create_task(coroutine)
        task.add_done_callback(_log_send_task_failure)
```

- [ ] **Step 3: Schedule every Telegram reply on the Hermes loop**

Implement loop capture and background-thread handoff:

```python
def _schedule_telegram_reply(
    gateway,
    source,
    text: str,
    *,
    reply_markup: dict | None = None,
) -> bool:
    global _TELEGRAM_DELIVERY_LOOP

    adapter = _telegram_adapter(gateway, source)
    chat_id = str(getattr(source, "chat_id", "") or getattr(source, "user_id", "") or "")
    if adapter is None or not chat_id:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with _TELEGRAM_DELIVERY_LOOP_LOCK:
            loop = _TELEGRAM_DELIVERY_LOOP
        if loop is None or not loop.is_running():
            return False
        loop.call_soon_threadsafe(
            _enqueue_telegram_reply_on_loop,
            loop,
            adapter,
            source,
            chat_id,
            str(text),
            reply_markup,
        )
        return True
    with _TELEGRAM_DELIVERY_LOOP_LOCK:
        _TELEGRAM_DELIVERY_LOOP = loop
    _enqueue_telegram_reply_on_loop(
        loop, adapter, source, chat_id, str(text), reply_markup
    )
    return True
```

Resolve the adapter without duplicating transport logic:

```python
def _telegram_adapter(gateway, source):
    if gateway is None:
        return None
    adapters = getattr(gateway, "adapters", {}) or {}
    adapter = adapters.get("telegram")
    if adapter is None:
        adapter = adapters.get(getattr(source, "platform", None))
    return adapter
```

- [ ] **Step 4: Chain sends only within the same chat**

Implement:

```python
def _enqueue_telegram_reply_on_loop(
    loop,
    adapter,
    source,
    chat_id: str,
    text: str,
    reply_markup: dict | None,
) -> None:
    previous = _TELEGRAM_SEND_TAILS.get(chat_id)

    async def deliver() -> None:
        if previous is not None:
            try:
                await asyncio.shield(previous)
            except Exception:
                pass
        await _send_telegram_reply_async(
            adapter,
            source,
            chat_id,
            text,
            reply_markup=reply_markup,
        )

    task = loop.create_task(deliver())
    _TELEGRAM_SEND_TAILS[chat_id] = task

    def finished(done_task) -> None:
        if _TELEGRAM_SEND_TAILS.get(chat_id) is done_task:
            _TELEGRAM_SEND_TAILS.pop(chat_id, None)
        _log_send_task_failure(done_task)

    task.add_done_callback(finished)
```

- [ ] **Step 5: Deliver through the active Hermes Telegram bot**

Implement active-bot delivery and off-loop fallback:

```python
async def _send_telegram_reply_async(
    adapter,
    source,
    chat_id: str,
    text: str,
    *,
    reply_markup: dict | None,
) -> None:
    bot = getattr(adapter, "_bot", None)
    send_message = getattr(bot, "send_message", None)
    if callable(send_message):
        markup = _telegram_reply_markup_object(reply_markup)
        for chunk in _telegram_message_chunks(text):
            await send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        return
    sent = await asyncio.to_thread(
        _send_telegram_reply_direct,
        source,
        text,
        reply_markup=reply_markup,
    )
    if not sent:
        send = getattr(adapter, "send", None)
        if callable(send):
            await send(chat_id, text)
```

Lazily construct the keyboard:

```python
def _telegram_reply_markup_object(reply_markup: dict | None):
    if reply_markup is None:
        return None
    try:
        from telegram import ReplyKeyboardMarkup
    except ImportError:
        return reply_markup
    keyboard = [
        [
            str(button.get("text", "")) if isinstance(button, dict) else str(button)
            for button in row
        ]
        for row in reply_markup.get("keyboard", [])
        if isinstance(row, list)
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=bool(reply_markup.get("resize_keyboard", False)),
        one_time_keyboard=bool(reply_markup.get("one_time_keyboard", False)),
        input_field_placeholder=reply_markup.get("input_field_placeholder"),
        is_persistent=bool(reply_markup.get("is_persistent", False)),
    )
```

- [ ] **Step 6: Update the direct-send test for asynchronous active-bot delivery**

Keep the synchronous direct-fallback tests. Add this active-bot keyboard test to
`TelegramAsyncReplyTests`:

```python
async def test_active_bot_send_preserves_reply_keyboard_without_direct_http(self):
    plugin = load_plugin()
    bot = ControlledTelegramBot()
    gateway = FakeGateway(adapter_key="telegram", adapter=FakeAdapter(bot))
    source = FakeSource(FakePlatform("telegram"), "user-a", chat_id="chat-a")
    requests = []
    plugin._telegram_api_opener = lambda request, timeout: requests.append(
        (request, timeout)
    )

    plugin._send_fixed_reply(
        gateway,
        source,
        "Choose",
        reply_markup=plugin._reply_keyboard((("One", "Two"),)),
    )
    await asyncio.wait_for(bot.started.wait(), timeout=0.2)

    self.assertEqual(bot.calls[0]["chat_id"], "chat-a")
    self.assertEqual(bot.calls[0]["text"], "Choose")
    self.assertIsNotNone(bot.calls[0]["reply_markup"])
    self.assertEqual(requests, [])
    bot.release.set()
```

- [ ] **Step 7: Run focused tests and verify GREEN**

Run:

```bash
../../payment-executor/.venv/bin/python -m unittest \
  tests.test_plugin.TelegramAsyncReplyTests \
  tests.test_plugin.PluginRegistrationTests.test_help_direct_reply_includes_reply_keyboard
```

Expected: PASS with no pending-task warnings.

- [ ] **Step 8: Commit the implementation**

```bash
git add hermes-plugins/sign402-wallet/__init__.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "fix: make Telegram replies nonblocking"
```

---

### Task 3: Document, verify, and deploy

**Files:**
- Modify: `hermes-plugins/sign402-wallet/README.md`

**Interfaces:**
- Consumes: completed async delivery implementation.
- Produces: operator documentation and production verification evidence.

- [ ] **Step 1: Update latency documentation**

Replace the batching-only latency note with:

```markdown
`HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS=0.08` uses Hermes's supported minimum
quiet period for short Telegram messages and buttons. Sign402 replies are
scheduled through Hermes's active asynchronous Telegram client, so Telegram
network stalls never block gateway button dispatch. Long messages near
Telegram's 4096-character split boundary keep their separate aggregation delay.
```

- [ ] **Step 2: Run complete verification**

Run:

```bash
cd hermes-plugins/sign402-wallet
../../payment-executor/.venv/bin/python -m unittest discover -s tests
cd ../../sign402-gateway
../payment-executor/.venv/bin/python -m unittest discover -s tests
```

Expected: all 128+ plugin tests and 465 gateway tests pass, with the final count
updated for any newly added tests.

- [ ] **Step 3: Commit documentation**

```bash
git add hermes-plugins/sign402-wallet/README.md
git commit -m "docs: describe asynchronous Telegram replies"
```

- [ ] **Step 4: Push both remotes**

```bash
git push origin x402Bnkr
git push singitai x402Bnkr
```

Expected: both remote branch heads equal local `git rev-parse HEAD`.

- [ ] **Step 5: Fast-forward production and restart Hermes only**

```bash
ssh hermes@164.68.104.44 \
  'cd /home/hermes/apps/sign402 && git pull --ff-only origin x402Bnkr && systemctl --user restart hermes-gateway'
```

Do not restart `sign402-gateway`; the changed code is loaded by
`hermes-gateway` through the project plugin symlink.

- [ ] **Step 6: Verify production**

Confirm deployed commit and services:

```bash
ssh hermes@164.68.104.44 \
  'cd /home/hermes/apps/sign402 && git rev-parse HEAD && systemctl --user is-active hermes-gateway && systemctl is-active sign402-gateway'
```

Run the plugin suite on the deployed tree and a synthetic blocked-send dispatch
probe. The probe must report handler scheduling under 50 ms while the fake send
remains blocked. Do not call Bitrefill purchase tools or send test messages to
Telegram users.

- [ ] **Step 7: Operator confirmation**

Ask the operator to press representative buttons (`Balance`, `Buy Bitrefill`,
`Browse Catalog`, `All`, and `Back`) and confirm that the immediate response or
`Loading…` appears without the previous 10–15 second pause.
