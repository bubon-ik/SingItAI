# Photon iMessage Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Telegram wallet users link one Photon iMessage sender and approve or deny a no-funds Sign402 test action by replying `YES` or `NO`.

**Architecture:** Sign402 Gateway owns pairing, encrypted Photon identity mapping, approval state, canonical hashes, and notification delivery. The Hermes wallet plugin captures trusted Telegram and Photon source identities, calls localhost Gateway endpoints with bearer tokens, and consumes pairing/approval messages before LLM dispatch.

**Tech Stack:** Python 3.11, stdlib `http.server`, SQLite, `cryptography.Fernet`, Hermes plugin hooks, `unittest`, local subprocess invocation of `hermes send`.

---

## File Structure

- Create `sign402-gateway/sign402_gateway/imessage_approvals.py`
  - SQLite store, E.164 normalization, pairing-code HMACs, encrypted phone storage, approval creation/decision logic, and Hermes CLI notifier.
- Modify `sign402-gateway/sign402_gateway/server.py`
  - Add `/agent/imessage/*` and `/agent/test-imessage-approval` endpoints, health list, service wiring, and `SIGN402_PHOTON_API_TOKEN` auth.
- Create `sign402-gateway/tests/test_imessage_approvals.py`
  - Unit tests for identity linking, approval state, expiry, replay, safe redaction, and notifier command construction.
- Modify `sign402-gateway/tests/test_gateway_server.py`
  - Endpoint auth and response tests for the new Gateway API.
- Modify `hermes-plugins/sign402-wallet/identity.py`
  - Capture Telegram identity for `/connect_imessage` and `/test_approval`; expose trusted Photon source capture for pre-dispatch handling.
- Modify `hermes-plugins/sign402-wallet/client.py`
  - Support wallet-token operations plus Photon-token iMessage operations with localhost-only URL checks and fixed safe errors.
- Modify `hermes-plugins/sign402-wallet/__init__.py`
  - Register new Telegram commands and pre-dispatch Photon handler; schedule fixed replies through active adapters and return `{"action":"skip"}` only for handled pairing/decisions.
- Modify `hermes-plugins/sign402-wallet/tests/test_client.py`
  - Verify new endpoint paths, token selection, payloads, and safe failure handling.
- Modify `hermes-plugins/sign402-wallet/tests/test_plugin.py`
  - Verify trusted identity capture, Telegram command behavior, Photon pairing, `YES`/`NO` consumption, and pass-through when no pending approval exists.
- Modify `hermes-plugins/sign402-wallet/README.md`
  - Document env vars and manual deployment smoke test.

## Task 1: Gateway Domain Tests

**Files:**
- Create: `sign402-gateway/tests/test_imessage_approvals.py`
- Create: `sign402-gateway/sign402_gateway/imessage_approvals.py`

- [ ] **Step 1: Write failing tests for pairing and one-to-one linking**

```python
def test_pairing_code_links_encrypted_phone_to_existing_wallet(self):
    service = self.make_service()
    service.wallet_service.create_wallet("1045618308")
    pairing = service.create_pairing("1045618308")
    result = service.link_photon_sender(pairing["code"], "+1 (555) 123-4567")
    self.assertTrue(result["ok"])
    self.assertEqual(result["telegramUserId"], "1045618308")
    self.assertNotIn("+15551234567", self.store_debug_dump())
```

- [ ] **Step 2: Run the test and verify it fails because the module is missing**

Run: `PYTHONPATH=sign402-gateway python -m pytest sign402-gateway/tests/test_imessage_approvals.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'sign402_gateway.imessage_approvals'`.

- [ ] **Step 3: Implement the minimal store/service for pairing**

Implement `ImessageApprovalStore`, `ImessageApprovalService`, `normalize_e164()`, code HMAC storage, encrypted phone storage, and fixed Telegram/iMessage texts.

- [ ] **Step 4: Run tests and verify green**

Run: `PYTHONPATH=sign402-gateway python -m pytest sign402-gateway/tests/test_imessage_approvals.py -q`

Expected: PASS for pairing tests.

## Task 2: Gateway Approval State Tests

**Files:**
- Modify: `sign402-gateway/tests/test_imessage_approvals.py`
- Modify: `sign402-gateway/sign402_gateway/imessage_approvals.py`

- [ ] **Step 1: Write failing tests for test approval creation and decisions**

```python
def test_test_approval_sends_canonical_message_and_accepts_yes_once(self):
    service = self.make_linked_service()
    created = service.create_test_approval("1045618308")
    self.assertTrue(created["ok"])
    self.assertIn("Reply YES or NO.", self.notifier.messages[0]["message"])
    pending = service.pending_for_photon_sender("+15551234567")
    self.assertTrue(pending["pending"])
    decided = service.record_decision("+15551234567", "YES")
    self.assertEqual(decided["status"], "approved")
    replay = service.record_decision("+15551234567", "YES")
    self.assertFalse(replay["ok"])
```

- [ ] **Step 2: Run test and verify it fails on missing approval methods**

Run: `PYTHONPATH=sign402-gateway python -m pytest sign402-gateway/tests/test_imessage_approvals.py -q`

Expected: FAIL with missing `create_test_approval` or `pending_for_photon_sender`.

- [ ] **Step 3: Implement approval queue**

Add canonical JSON hashing, one live pending approval per Telegram user, two-minute expiry, `delivery_failed`, `approved`, `denied`, and replay-safe conditional updates.

- [ ] **Step 4: Run tests and verify green**

Run: `PYTHONPATH=sign402-gateway python -m pytest sign402-gateway/tests/test_imessage_approvals.py -q`

Expected: PASS.

## Task 3: Gateway HTTP API

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`

- [ ] **Step 1: Write failing endpoint tests**

```python
def test_imessage_pairing_requires_photon_token(self):
    response = self.post_json("/agent/imessage/pairing", {"telegramUserId": "1045618308"})
    self.assertEqual(response.status, 503)

def test_imessage_pairing_endpoint_returns_telegram_text(self):
    self.server.imessage_approval_api_token = "photon-token"
    response = self.post_json(
        "/agent/imessage/pairing",
        {"telegramUserId": "1045618308"},
        token="photon-token",
    )
    self.assertEqual(response.status, 200)
    self.assertIn("telegramText", response.json)
```

- [ ] **Step 2: Run test and verify 404/failure**

Run: `PYTHONPATH=sign402-gateway python -m pytest sign402-gateway/tests/test_gateway_server.py -q`

Expected: FAIL because new endpoints are not routed.

- [ ] **Step 3: Implement routes and auth**

Add handlers for `/agent/imessage/pairing`, `/agent/imessage/link`, `/agent/imessage/pending`, `/agent/imessage/decision`, and `/agent/test-imessage-approval`. Add `SIGN402_PHOTON_API_TOKEN` auth separate from `SIGN402_WALLET_API_TOKEN`.

- [ ] **Step 4: Run gateway tests**

Run: `PYTHONPATH=sign402-gateway python -m pytest sign402-gateway/tests/test_imessage_approvals.py sign402-gateway/tests/test_gateway_server.py -q`

Expected: PASS.

## Task 4: Hermes Plugin Client Tests

**Files:**
- Modify: `hermes-plugins/sign402-wallet/client.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_client.py`

- [ ] **Step 1: Write failing client tests for Photon token operations**

```python
def test_imessage_operation_uses_photon_token_and_sender_payload(self):
    client = GatewayClient(base_url="http://127.0.0.1:8099", api_token="wallet", photon_api_token="photon", opener=opener)
    result = client.execute_imessage("link", {"code": "ABCDEFGH", "photonUserId": "+15551234567"})
    self.assertEqual(request.get_header("Authorization"), "Bearer photon")
```

- [ ] **Step 2: Run test and verify it fails on missing API**

Run: `python -m pytest hermes-plugins/sign402-wallet/tests/test_client.py -q`

Expected: FAIL with missing `execute_imessage`.

- [ ] **Step 3: Implement client support**

Add endpoint maps for `connect-imessage`, `test-imessage-approval`, `link`, `pending`, and `decision`. Read `SIGN402_PHOTON_API_TOKEN` from env while preserving existing wallet token behavior.

- [ ] **Step 4: Run plugin client tests**

Run: `python -m pytest hermes-plugins/sign402-wallet/tests/test_client.py -q`

Expected: PASS.

## Task 5: Hermes Plugin Hook and Commands

**Files:**
- Modify: `hermes-plugins/sign402-wallet/identity.py`
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

- [ ] **Step 1: Write failing tests for `/connect_imessage`, `/test_approval`, pairing, and decisions**

```python
def test_photon_pairing_code_is_consumed_before_llm(self):
    result = context.hooks["pre_gateway_dispatch"](event=FakePhotonEvent("ABCDEFGH"), gateway=gateway)
    self.assertEqual(result, {"action": "skip", "reason": "sign402-imessage-handled"})
    self.assertEqual(gateway.sent_messages[0], ("photon-chat", "iMessage linked."))
```

- [ ] **Step 2: Run test and verify it fails**

Run: `python -m pytest hermes-plugins/sign402-wallet/tests/test_plugin.py -q`

Expected: FAIL because Photon hook handling does not exist.

- [ ] **Step 3: Implement hook and commands**

Register `connect_imessage` and `test_approval`. In pre-dispatch, capture Telegram command identity for commands, handle eight-character Photon pairing codes, query pending status before consuming exact `YES`/`NO`, schedule fixed adapter replies, and return skip only for handled messages.

- [ ] **Step 4: Run plugin tests**

Run: `python -m pytest hermes-plugins/sign402-wallet/tests/test_plugin.py hermes-plugins/sign402-wallet/tests/test_identity.py -q`

Expected: PASS.

## Task 6: Documentation, Full Verification, Commit

**Files:**
- Modify: `hermes-plugins/sign402-wallet/README.md`
- Modify: `docs/superpowers/plans/2026-07-01-photon-imessage-approval.md`

- [ ] **Step 1: Document env vars and smoke test**

Add the exact env names and smoke test commands:

```text
SIGN402_PHOTON_API_TOKEN
SIGN402_IMESSAGE_APPROVAL_STORE_PATH
SIGN402_HERMES_CLI
SIGN402_HERMES_HOME
```

- [ ] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=sign402-gateway python -m pytest sign402-gateway/tests -q
python -m pytest hermes-plugins/sign402-wallet/tests -q
git diff --check
```

Expected: all tests PASS and `git diff --check` has no output.

- [ ] **Step 3: Commit and push**

```bash
git status --short
git add docs/superpowers/plans/2026-07-01-photon-imessage-approval.md sign402-gateway hermes-plugins/sign402-wallet
git commit -m "Add Photon iMessage approval flow"
git push singitai x402Bnkr
```

Expected: branch pushed with the new implementation commit.

## Self-Review

- Spec coverage: pairing, one-to-one identity, generic approval queue, fixed notifications, pre-LLM `YES`/`NO`, no-funds test approval, token auth, failure-closed behavior, and tests are covered.
- Placeholder scan: no `TBD`, `TODO`, `FIXME`, or "implement later" placeholders are present.
- Type consistency: Gateway endpoints use `telegramUserId`, `photonUserId`, `code`, and `decision`; plugin client methods use those same payload keys.
