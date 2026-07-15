# Existing Approval Channel Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing Connect iMessage and Connect WhatsApp controls select an already linked approval channel immediately and run pairing only when the requested channel is not linked.

**Architecture:** Add one authenticated localhost selector that updates only `approval_channel_preferences` after proving the trusted Telegram user owns the requested channel link. The Hermes plugin calls this selector before its existing connection flow; a selected channel returns immediately, while `requiresPairing` preserves the current first-time behavior.

**Tech Stack:** Python 3.12, SQLite, `http.server`, `unittest`, Hermes Python plugin, Telegram reply keyboards.

## Global Constraints

- No new Telegram commands or buttons.
- Do not remove or rewrite either encrypted channel link when switching.
- Only an already linked channel may become active.
- The Telegram user ID must come from the trusted Telegram event.
- The selector uses `SIGN402_PHOTON_API_TOKEN` and remains loopback-only.
- Responses must not expose phone identities, digests, ciphertext, tokens, stack traces, or filesystem paths.
- Pairing and approval replay protections remain unchanged.

---

### Task 1: Approval service selector

**Files:**
- Modify: `sign402-gateway/sign402_gateway/imessage_approvals.py`
- Test: `sign402-gateway/tests/test_imessage_approvals.py`

**Interfaces:**
- Consumes: `ImessageApprovalService`, `approval_channel_links`, `approval_channel_preferences`, `_record_audit()`.
- Produces: `ImessageApprovalService.select_existing_channel(telegram_user_id: str, channel: str) -> dict[str, Any]`.

- [ ] **Step 1: Write failing service tests**

Add tests that link both channels for one wallet, leave WhatsApp active, and call:

```python
selected = service.select_existing_channel("1045618308", "imessage")

self.assertEqual(
    selected,
    {
        "ok": True,
        "selected": True,
        "requiresPairing": False,
        "channel": "imessage",
        "telegramText": "iMessage selected for Sign402 approvals.",
    },
)
self.assertEqual(service.active_channel("1045618308"), "imessage")
```

Query `approval_channel_links` and assert both `imessage` and `whatsapp` remain. Add an idempotency test that selects the already active channel twice and an unlinked test expecting:

```python
{
    "ok": True,
    "selected": False,
    "requiresPairing": True,
    "channel": "whatsapp",
}
```

Add an unsupported-channel test expecting `ValueError`.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest \
  tests.test_imessage_approvals.ImessageApprovalTests.test_select_existing_channel_switches_preference_without_removing_links \
  tests.test_imessage_approvals.ImessageApprovalTests.test_select_existing_channel_is_idempotent \
  tests.test_imessage_approvals.ImessageApprovalTests.test_select_existing_channel_requires_pairing_when_unlinked \
  tests.test_imessage_approvals.ImessageApprovalTests.test_select_existing_channel_rejects_unsupported_channel -v
```

Expected: four failures because `select_existing_channel` does not exist.

- [ ] **Step 3: Implement the minimal selector**

Add this public service method next to `active_channel`:

```python
def select_existing_channel(
    self,
    telegram_user_id: str,
    channel: str,
) -> dict[str, Any]:
    user_id = _require_telegram_user_id(telegram_user_id)
    approval_channel = _normalize_approval_channel(channel)
    now = self._now()
    with self.store.lock, self.store._database() as db:
        linked = db.execute(
            """
            SELECT 1
            FROM approval_channel_links
            WHERE telegram_user_id = ? AND channel = ?
            """,
            (user_id, approval_channel),
        ).fetchone()
        if linked is None:
            return {
                "ok": True,
                "selected": False,
                "requiresPairing": True,
                "channel": approval_channel,
            }
        db.execute(
            """
            INSERT INTO approval_channel_preferences (
                telegram_user_id, channel, created_at, updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_user_id)
            DO UPDATE SET channel = excluded.channel, updated_at = excluded.updated_at
            """,
            (user_id, approval_channel, now, now),
        )
        self._record_audit(
            db,
            telegram_user_id=user_id,
            event_type="channel_selected",
            status="selected",
            metadata={"channel": approval_channel},
        )
    channel_label = _approval_channel_label(approval_channel)
    return {
        "ok": True,
        "selected": True,
        "requiresPairing": False,
        "channel": approval_channel,
        "telegramText": f"{channel_label} selected for Sign402 approvals.",
    }
```

- [ ] **Step 4: Run the focused and full service tests**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_imessage_approvals -q
```

Expected: all `test_imessage_approvals` tests pass.

- [ ] **Step 5: Commit the service selector**

```bash
git add sign402-gateway/sign402_gateway/imessage_approvals.py \
  sign402-gateway/tests/test_imessage_approvals.py
git commit -m "Add existing approval channel selector"
```

---

### Task 2: Authenticated localhost endpoint and client mapping

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `hermes-plugins/sign402-wallet/client.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_client.py`

**Interfaces:**
- Consumes: `ImessageApprovalService.select_existing_channel()` and the existing `_require_imessage_approval_api_token()` boundary.
- Produces: `POST /agent/approval-channel/select-existing` and client operation `select-existing`.

- [ ] **Step 1: Write failing HTTP and client tests**

Add a gateway test with Photon authorization that posts:

```python
{"telegramUserId": "1045618308", "channel": "imessage"}
```

Assert HTTP 200 and:

```python
server.imessage_approval_service.select_existing_channel.assert_called_once_with(
    "1045618308", "imessage"
)
```

Add a no-auth test expecting HTTP 401 and no service call. Add a client mapping case:

```python
"select-existing": "/agent/approval-channel/select-existing"
```

and assert the Photon token is used.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_select_existing_approval_channel_uses_photon_auth \
  tests.test_gateway_server.GatewayServerTests.test_select_existing_approval_channel_rejects_wallet_token -v
cd ../hermes-plugins/sign402-wallet
PYTHONPATH=. python3 -m unittest \
  tests.test_client.GatewayClientTests.test_execute_imessage_maps_operations_to_expected_endpoints -v
```

Expected: failures because the route and client mapping do not exist.

- [ ] **Step 3: Implement the route and handler**

Add `/agent/approval-channel/select-existing` to health output and POST routing. Add:

```python
def _handle_agent_select_existing_approval_channel(self) -> None:
    try:
        _require_imessage_approval_api_token(self)
        payload = self._read_json()
        result = self.server.imessage_approval_service.select_existing_channel(
            _read_telegram_user_id(payload),
            _read_required_text(payload, "channel"),
        )
        self._send_json(_without_private_key_material(result), status=200)
    except ImessageApprovalApiTokenNotConfiguredError as exc:
        self._send_json({"ok": False, "error": str(exc)}, status=503)
    except ImessageApprovalApiAuthError as exc:
        self._send_json({"ok": False, "error": str(exc)}, status=401)
    except Exception:
        self._send_json(
            {"ok": False, "error": "invalid approval channel selection"},
            status=400,
        )
```

Extend `_IMESSAGE_OPERATION_PATHS`:

```python
"select-existing": "/agent/approval-channel/select-existing",
```

- [ ] **Step 4: Run endpoint, client, and authorization regressions**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_gateway_server -q
cd ../hermes-plugins/sign402-wallet
PYTHONPATH=. python3 -m unittest tests.test_client -q
```

Expected: both suites pass; wallet-only or missing authorization cannot select a channel.

- [ ] **Step 5: Commit endpoint and client**

```bash
git add sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/tests/test_gateway_server.py \
  hermes-plugins/sign402-wallet/client.py \
  hermes-plugins/sign402-wallet/tests/test_client.py
git commit -m "Expose authenticated approval channel selection"
```

---

### Task 3: Existing Telegram controls select before pairing

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`
- Modify: `hermes-plugins/sign402-wallet/README.md`

**Interfaces:**
- Consumes: client operation `select-existing` returning `selected`, `requiresPairing`, `channel`, and optional `telegramText`.
- Produces: existing connect commands/buttons that either select immediately or enter the existing pairing flow.

- [ ] **Step 1: Write failing plugin tests**

For both `Connect iMessage` and `Connect WhatsApp`, configure the fake selector response as:

```python
client.approval_results["select-existing"] = {
    "ok": True,
    "selected": True,
    "requiresPairing": False,
    "channel": "imessage",
    "telegramText": "iMessage selected for Sign402 approvals.",
}
```

Assert the fixed text is sent, no phone registration session is created, and no `connect-imessage` call follows. Add fallback tests with:

```python
{
    "ok": True,
    "selected": False,
    "requiresPairing": True,
    "channel": "imessage",
}
```

and assert the existing prompt/pairing behavior remains. Assert raw command arguments cannot replace the trusted Telegram ID in the selector payload.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd hermes-plugins/sign402-wallet
PYTHONPATH=. python3 -m unittest \
  tests.test_plugin.PluginRegistrationTests.test_connect_imessage_selects_existing_link_without_phone_prompt \
  tests.test_plugin.PluginRegistrationTests.test_connect_whatsapp_selects_existing_link_without_pairing \
  tests.test_plugin.PluginRegistrationTests.test_connect_imessage_unlinked_falls_back_to_phone_prompt -v
```

Expected: failures because connect flows do not call the selector.

- [ ] **Step 3: Add the shared selection helper**

Add:

```python
def _select_existing_approval_channel(
    client: GatewayClient,
    identity: TelegramIdentity,
    channel: str,
) -> str | None:
    result = client.execute_approval(
        "select-existing",
        {"telegramUserId": identity.user_id, "channel": channel},
    )
    if result.get("selected") is True:
        text = result.get("telegramText")
        if not isinstance(text, str) or not text.strip():
            raise GatewayClientError(_IMESSAGE_UNEXPECTED_ERROR_MESSAGE)
        return text.strip()
    if result.get("requiresPairing") is True:
        return None
    raise GatewayClientError(_IMESSAGE_UNEXPECTED_ERROR_MESSAGE)
```

Call it at the beginning of both connect handlers, including before the Photon phone prompt. If it returns text, reply immediately. If it returns `None`, execute the existing channel-specific pairing logic unchanged.

- [ ] **Step 4: Update existing test expectations and documentation**

Make the fake client return `requiresPairing: True` by default for `select-existing`, update connect-call assertions to include the selector first, and document:

```text
Connect iMessage and Connect WhatsApp select an existing link immediately.
Only an unlinked channel starts its pairing flow.
```

- [ ] **Step 5: Run full plugin tests**

Run:

```bash
cd hermes-plugins/sign402-wallet
env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=. \
  python3 -m unittest discover -s tests -q
```

Expected: all plugin tests pass with no failures.

- [ ] **Step 6: Commit plugin behavior**

```bash
git add hermes-plugins/sign402-wallet/__init__.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py \
  hermes-plugins/sign402-wallet/README.md
git commit -m "Select linked approval channels from connect controls"
```

---

### Task 4: Full regression, deployment, and no-funds channel verification

**Files:**
- Modify: `docs/security-audit/2026-07-14-risk-register.md`

**Interfaces:**
- Consumes: the completed selector and existing authenticated test-approval endpoint.
- Produces: production evidence that both linked channels can be selected and approve once without moving funds.

- [ ] **Step 1: Run local regressions**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest discover -s tests -q
cd ../hermes-plugins/sign402-wallet
env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=. \
  python3 -m unittest discover -s tests -q
```

Expected: all gateway and plugin tests pass.

- [ ] **Step 2: Push and deploy in dependency order**

Push `x402Bnkr`, pull with `--ff-only` on the VPS, restart `sign402-gateway`, copy the plugin into `~/.hermes/plugins/sign402-wallet`, and restart `hermes-gateway`. Verify ports 8099 and 8090 return HTTP 200.

- [ ] **Step 3: Verify iMessage without moving funds**

Press the existing `Connect iMessage` button. Expect `iMessage selected for Sign402 approvals.` without a phone prompt. Trigger `/agent/test-imessage-approval` with the authenticated localhost token, approve once in iMessage, and repeat the same reply. Query only approval ID, channel, status, and timestamps; expect one approved iMessage record and no second decision.

- [ ] **Step 4: Verify WhatsApp without moving funds**

Press the existing `Connect WhatsApp` button. Expect `WhatsApp selected for Sign402 approvals.` without a pairing code. Trigger the same no-funds approval, confirm once in WhatsApp, and press the stale button again. Expect one approved WhatsApp record and no replay.

- [ ] **Step 5: Restore production configuration**

Restore `/etc/sign402-gateway.env` from the audit backup, restart the gateway, confirm `/agent/test-imessage-approval` is absent from `/health`, and delete the backup only after that check succeeds.

- [ ] **Step 6: Update the audit evidence and commit**

Record both channel-selection and replay results in the risk register without phone identities or secrets. Run `git diff --check`, commit the risk register, and push the final branch.
