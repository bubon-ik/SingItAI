# Hermes Telegram Wallet Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic `/wallet`, `/create_wallet`, and `/balance` commands to the hosted Hermes Telegram bot while binding every request to the trusted Telegram sender identity.

**Architecture:** A standalone Hermes plugin captures the inbound Telegram `MessageEvent` in `pre_gateway_dispatch`, stores its trusted source in a task-local `ContextVar`, and consumes that identity from registered slash-command handlers after Hermes authorization. The handlers call the existing localhost Sign402 wallet API with a server-side bearer token and return only the gateway's `telegramText`.

**Tech Stack:** Python 3.11+, Hermes plugin API, `contextvars`, standard-library `urllib`, `unittest`, POSIX shell.

---

## File Structure

- Create `hermes-plugins/sign402-wallet/identity.py`
  - Captures and consumes task-local Telegram sender identity.
- Create `hermes-plugins/sign402-wallet/client.py`
  - Calls the protected localhost wallet API and converts failures to safe messages.
- Create `hermes-plugins/sign402-wallet/__init__.py`
  - Registers the gateway hook and three deterministic slash commands.
- Create `hermes-plugins/sign402-wallet/plugin.yaml`
  - Declares plugin metadata, hook, and required environment variables.
- Create `hermes-plugins/sign402-wallet/tests/test_identity.py`
  - Verifies trusted-source validation, cleanup, and task isolation.
- Create `hermes-plugins/sign402-wallet/tests/test_client.py`
  - Verifies request shaping, authentication, response bounds, and safe errors.
- Create `hermes-plugins/sign402-wallet/tests/test_plugin.py`
  - Verifies Hermes registration and that raw arguments cannot replace identity.
- Create `hermes-plugins/sign402-wallet/README.md`
  - Documents installation, configuration, commands, and diagnostics.
- Create `scripts/install-hermes-wallet-plugin.sh`
  - Installs an idempotent symlink and enables the plugin.
- Modify `scripts/README.md`
  - Links the VPS plugin deployment flow.

### Task 1: Trusted Telegram Identity

**Files:**
- Create: `hermes-plugins/sign402-wallet/tests/test_identity.py`
- Create: `hermes-plugins/sign402-wallet/identity.py`

- [ ] **Step 1: Write failing identity tests**

Cover a Telegram wallet event, a non-wallet command, a non-Telegram source,
missing user ID, one-time consumption, and two concurrent asyncio tasks.
Use small fake `Source` and `Event` objects; no Hermes import is required.

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests -p 'test_identity.py' -v
```

Expected: `ModuleNotFoundError` for `identity`.

- [ ] **Step 3: Implement the identity boundary**

Create:

```python
@dataclass(frozen=True)
class TelegramIdentity:
    user_id: str
    username: str | None = None


def capture_gateway_identity(*, event, **_kwargs):
    # Bind only recognized Telegram wallet commands from event.source.
    ...


def consume_gateway_identity() -> TelegramIdentity | None:
    # Return the current value and immediately clear the ContextVar.
    ...
```

Recognize `wallet`, `create-wallet`, and `balance` after replacing
underscores with hyphens. Accept only decimal Telegram IDs.

- [ ] **Step 4: Verify identity tests pass**

Run the Task 1 command and expect all tests to pass.

### Task 2: Protected Gateway Client

**Files:**
- Create: `hermes-plugins/sign402-wallet/tests/test_client.py`
- Create: `hermes-plugins/sign402-wallet/client.py`

- [ ] **Step 1: Write failing client tests**

Use a fake opener and response object to verify:

- operation-to-path mapping;
- JSON payload uses the supplied trusted identity;
- bearer authorization header;
- extraction of non-empty `telegramText`;
- missing URL/token configuration;
- HTTP 401/403;
- timeout and connection errors;
- invalid JSON, non-object JSON, missing text, and oversized bodies;
- returned messages never contain token or upstream body.

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests -p 'test_client.py' -v
```

Expected: `ModuleNotFoundError` for `client`.

- [ ] **Step 3: Implement the client**

Add `GatewayClient.from_env()` and:

```python
def execute(self, operation: str, identity: TelegramIdentity) -> str:
    path = {
        "wallet": "/agent/wallet",
        "create-wallet": "/agent/create-wallet",
        "balance": "/agent/wallet-balance",
    }[operation]
```

Use `urllib.request.Request`, a five-second timeout, a 64 KiB response cap,
and `json.loads`. Raise a typed exception carrying only a fixed safe
user-facing message. Log operation and failure category without response
body, bearer token, or request payload.

- [ ] **Step 4: Verify client tests pass**

Run the Task 2 command and expect all tests to pass.

### Task 3: Hermes Plugin Registration

**Files:**
- Create: `hermes-plugins/sign402-wallet/tests/test_plugin.py`
- Create: `hermes-plugins/sign402-wallet/__init__.py`
- Create: `hermes-plugins/sign402-wallet/plugin.yaml`

- [ ] **Step 1: Write failing registration tests**

Load the plugin package with `importlib`, provide a fake Hermes context, and
assert registration of:

```text
hook: pre_gateway_dispatch
commands: wallet, create-wallet, balance
```

Invoke a command after binding Telegram user `1045618308`, pass raw text
containing a different ID, and assert that the fake client receives only
`1045618308`. Invoke the handler again without a new gateway event and
assert that it refuses the call.

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests -p 'test_plugin.py' -v
```

Expected: import or registration failure because the plugin does not exist.

- [ ] **Step 3: Implement registration and handlers**

Register `capture_gateway_identity` as `pre_gateway_dispatch`. Build one
async handler per operation:

```python
async def handler(_raw_args: str) -> str:
    identity = consume_gateway_identity()
    if identity is None:
        return TELEGRAM_ONLY_MESSAGE
    try:
        return await asyncio.to_thread(client_factory().execute, operation, identity)
    except GatewayClientError as exc:
        return exc.user_message
```

Ignore raw arguments. Declare `SIGN402_GATEWAY_URL` and
`SIGN402_WALLET_API_TOKEN` in `plugin.yaml`.

- [ ] **Step 4: Verify all plugin tests pass**

Run:

```bash
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests -v
```

Expected: all identity, client, and registration tests pass.

### Task 4: Installer And Operator Documentation

**Files:**
- Create: `scripts/install-hermes-wallet-plugin.sh`
- Create: `hermes-plugins/sign402-wallet/README.md`
- Modify: `scripts/README.md`

- [ ] **Step 1: Write an installer smoke test**

Add `hermes-plugins/sign402-wallet/tests/test_installer.py` that runs the
installer with temporary `HOME`, a fake `hermes` binary, and
`SIGN402_PLUGIN_SOURCE` pointing at the repository plugin. Verify the
symlink target and `hermes plugins enable sign402-wallet` invocation.

- [ ] **Step 2: Verify the smoke test fails**

Run:

```bash
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests -p 'test_installer.py' -v
```

Expected: failure because the installer does not exist.

- [ ] **Step 3: Implement the installer**

The shell script must:

- use `set -eu`;
- resolve the repository plugin directory or accept
  `SIGN402_PLUGIN_SOURCE`;
- create `~/.hermes/plugins`;
- create or preserve the correct symlink;
- refuse to overwrite an unrelated existing path;
- run `hermes plugins enable sign402-wallet`;
- never read, print, or write wallet secrets.

- [ ] **Step 4: Add operator documentation**

Document:

```env
SIGN402_GATEWAY_URL=http://127.0.0.1:8099
SIGN402_WALLET_API_TOKEN=<same value as sign402-gateway>
```

Keep the current Telegram allowlist during the first test. Include plugin
list, gateway restart, `/wallet`, `/create_wallet`, and `/balance`
verification commands. State that no domain or tunnel is required.

- [ ] **Step 5: Verify the complete plugin suite**

Run:

```bash
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests -v
```

Expected: all plugin tests pass.

### Task 5: Full Verification

**Files:**
- Verify only.

- [ ] **Step 1: Check formatting and accidental secrets**

Run:

```bash
git diff --check
rg -n 'SIGN402_WALLET_API_TOKEN=[A-Za-z0-9_-]{20,}|SIGN402_WALLET_MASTER_KEY=[A-Za-z0-9_-]{20,}' \
  hermes-plugins scripts docs
```

Expected: no whitespace errors and no concrete secret matches.

- [ ] **Step 2: Run the Sign402 Gateway regression suite**

Run:

```bash
cd sign402-gateway
python3 -m unittest discover -s tests -v
```

Expected: the existing gateway suite passes.

- [ ] **Step 3: Review the final diff**

Confirm that no Hermes core file, wallet private-key path, spending
endpoint, or Telegram allowlist was changed.

- [ ] **Step 4: Commit**

```bash
git add \
  docs/superpowers/specs/2026-06-30-hermes-telegram-wallet-plugin-design.md \
  docs/superpowers/plans/2026-06-30-hermes-telegram-wallet-plugin.md \
  hermes-plugins/sign402-wallet \
  scripts/install-hermes-wallet-plugin.sh \
  scripts/README.md
git commit -m "Add Hermes Telegram wallet plugin"
```
