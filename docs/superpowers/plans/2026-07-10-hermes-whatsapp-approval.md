# Hermes WhatsApp Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each Telegram wallet user select iMessage or WhatsApp as their single approval channel and receive every WhatsApp payment approval as a Meta utility template with Approve/Reject buttons.

**Architecture:** Keep Photon unchanged for iMessage. Use Hermes `whatsapp_cloud` for verified inbound pairing/button events, while a focused Sign402 Graph API client sends the approved Meta template because Hermes does not yet support outbound templates. Store one active approval channel per Telegram user and fail closed on wrong-channel, stale, duplicate, expired, or undelivered decisions.

**Tech Stack:** Python 3.11+, `urllib.request`, SQLite, `cryptography.Fernet`, Hermes plugin hooks, Meta WhatsApp Cloud API v25.0, `unittest`.

---

### Task 1: Add the Meta WhatsApp template sender

**Files:**
- Create: `sign402-gateway/sign402_gateway/whatsapp_cloud.py`
- Create: `sign402-gateway/tests/test_whatsapp_cloud.py`

- [ ] **Step 1: Write failing request-construction tests**

Add tests for a `MetaWhatsAppTemplateNotifier` that posts to
`https://graph.facebook.com/v25.0/{phone_number_id}/messages`, sends template
`sign402_payment_approval`, targets a digits-only `wa_id`, includes body
parameters plus two quick-reply payloads, and never exposes its access token in
the result.

```python
result = notifier.send_approval(
    wa_id="420777111222",
    approval_id="approval-123",
    context_lines=["Merchant: Bitrefill", "Amount: 10 USDC"],
    expires_at=1_800_000_600,
)
self.assertTrue(result["ok"])
self.assertEqual(payload["type"], "template")
self.assertEqual(
    payload["template"]["components"][-2]["parameters"][0]["payload"],
    "sign402:approve:approval-123",
)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_whatsapp_cloud -v
```

Expected: import failure for `sign402_gateway.whatsapp_cloud`.

- [ ] **Step 3: Implement the focused sender**

Create a module containing:

```python
class MetaWhatsAppTemplateNotifier:
    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        template_name: str = "sign402_payment_approval",
        template_language: str = "en_US",
        graph_api_version: str = "v25.0",
        opener=urlopen,
        timeout: float = 20.0,
    ): ...

    def send_approval(
        self,
        *,
        wa_id: str,
        approval_id: str,
        context_lines: list[str],
        expires_at: int,
    ) -> dict[str, object]: ...
```

Validate the phone-number ID, API version, recipient `wa_id`, approval ID, and
template name before network I/O. Build quick-reply payloads
`sign402:approve:{approval_id}` and `sign402:reject:{approval_id}`. Return only
`ok`, `messageId`, and a fixed safe error code.

- [ ] **Step 4: Add timeout, HTTP-error, malformed-response, and validation tests**

Use fake openers and assert every failure returns `ok: false` without the bearer
token or upstream response body.

- [ ] **Step 5: Run the focused tests**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_whatsapp_cloud -v
```

Expected: all WhatsApp sender tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add sign402-gateway/sign402_gateway/whatsapp_cloud.py sign402-gateway/tests/test_whatsapp_cloud.py
git commit -m "Add Meta WhatsApp approval template sender"
```

### Task 2: Make approval identity and channel selection explicit

**Files:**
- Modify: `sign402-gateway/sign402_gateway/imessage_approvals.py`
- Modify: `sign402-gateway/tests/test_imessage_approvals.py`

- [ ] **Step 1: Replace the disabled-WhatsApp tests with failing channel tests**

Cover these cases:

```python
pairing = service.create_pairing("1045618308", channel="whatsapp")
linked = service.link_sender(
    pairing["code"],
    "420777111222",
    channel="whatsapp",
)
self.assertTrue(linked["ok"])
self.assertEqual(service.active_channel("1045618308"), "whatsapp")
```

Also assert that linking WhatsApp after iMessage switches the active channel,
only the active channel receives the next approval, and a WhatsApp decision
cannot resolve an iMessage-bound request.

- [ ] **Step 2: Run the existing approval test module and verify failure**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_imessage_approvals -v
```

Expected: missing `link_sender`/`active_channel` and rejected WhatsApp pairing.

- [ ] **Step 3: Add channel preference persistence**

Add an idempotent SQLite table:

```sql
CREATE TABLE IF NOT EXISTS approval_channel_preferences (
    telegram_user_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)
```

Keep legacy iMessage rows valid: if a linked legacy user has no preference,
`active_channel()` returns `imessage`. Set/update the preference only after a
successful pairing.

- [ ] **Step 4: Generalize identity linking and lookup**

Set `SUPPORTED_APPROVAL_CHANNELS = {"imessage", "whatsapp"}`. Introduce:

```python
def link_sender(
    self,
    code: str,
    sender_user_id: str,
    *,
    channel: str,
) -> dict[str, Any]: ...

def active_channel(self, telegram_user_id: str) -> str | None: ...
```

Keep `link_photon_sender()` as a backward-compatible iMessage wrapper. Normalize
iMessage identities with E.164 and WhatsApp `wa_id` values as digits-only.
Digest identities with a channel-specific namespace and store the encrypted
identity in the existing `approval_channel_links` table.

- [ ] **Step 5: Bind pending/decision operations to channel and approval ID**

Add optional `channel` parameters to pending lookup and `record_decision`.
Require an exact `approval_id` for WhatsApp button decisions. Query both channel
and identity digest so the same phone cannot cross-resolve another channel.

- [ ] **Step 6: Send only to the active channel and use a 10-minute TTL**

Change `PURCHASE_APPROVAL_TTL_SECONDS` to `10 * 60`. Update
`_linked_approval_channels()` to return at most the selected channel. Extend the
notifier call with `approval_id`, `context_lines`, and `expires_at`, while
preserving the current Photon text delivery contract.

- [ ] **Step 7: Run approval tests**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_imessage_approvals -v
```

Expected: iMessage regression tests and new WhatsApp selection/idempotency tests
all pass.

- [ ] **Step 8: Commit Task 2**

```bash
git add sign402-gateway/sign402_gateway/imessage_approvals.py sign402-gateway/tests/test_imessage_approvals.py
git commit -m "Add selectable WhatsApp approval identities"
```

### Task 3: Compose the provider and expose channel-neutral gateway operations

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/.env.example`

- [ ] **Step 1: Add failing gateway route tests**

Exercise the existing protected approval endpoints with explicit channel data:

```json
{"telegramUserId":"1045618308","channel":"whatsapp"}
{"code":"ABCDEFGH","approvalUserId":"420777111222","channel":"whatsapp"}
{"approvalUserId":"420777111222","channel":"whatsapp","decision":"YES","approvalId":"approval-123"}
```

Assert authentication remains the independent Photon/approval bearer token and
private values are not returned.

- [ ] **Step 2: Run focused server tests and verify failure**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_gateway_server -v
```

Expected: generic identity/channel payloads are not yet accepted.

- [ ] **Step 3: Add a channel-routing notifier**

Build the approval service with a notifier that routes:

```python
if channel == "imessage":
    return hermes_cli_notifier.send(...)
if channel == "whatsapp":
    return whatsapp_template_notifier.send_approval(...)
return {"ok": False, "error": "approval_channel_not_configured"}
```

Instantiate `MetaWhatsAppTemplateNotifier` only when all required WhatsApp env
values are present. Otherwise WhatsApp pairing fails closed while iMessage keeps
working.

- [ ] **Step 4: Accept generic approval identity fields**

Update link, pending, decision, and unlink handlers to accept
`approvalUserId` plus `channel`, while retaining `photonUserId` for existing
iMessage callers. Pass the channel through to service methods and keep the
routes localhost-only and bearer-token protected.

- [ ] **Step 5: Document gateway secrets**

Add these empty/example settings without real credentials:

```env
SIGN402_WHATSAPP_ACCESS_TOKEN=
SIGN402_WHATSAPP_PHONE_NUMBER_ID=
SIGN402_WHATSAPP_TEMPLATE_NAME=sign402_payment_approval
SIGN402_WHATSAPP_TEMPLATE_LANGUAGE=en_US
SIGN402_WHATSAPP_GRAPH_API_VERSION=v25.0
```

- [ ] **Step 6: Run server and approval tests**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_gateway_server tests.test_imessage_approvals tests.test_whatsapp_cloud -v
```

Expected: all selected modules pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py sign402-gateway/.env.example
git commit -m "Wire WhatsApp templates into approval gateway"
```

### Task 4: Add WhatsApp onboarding and fail-closed Hermes routing

**Files:**
- Modify: `hermes-plugins/sign402-wallet/client.py`
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_client.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

- [ ] **Step 1: Write failing plugin tests**

Replace the disabled-channel test and cover:

- `Connect WhatsApp` and `/connect_whatsapp` request a WhatsApp pairing code;
- a `whatsapp_cloud` pairing-code message calls link with trusted `source.user_id`;
- button payload `sign402:approve:{approval_id}` records `YES` for that exact ID;
- reject records `NO`;
- plain text, stale buttons, malformed payloads, and exceptions are consumed and
  never reach general Hermes dispatch;
- iMessage still follows the current Photon path.

- [ ] **Step 2: Run plugin/client tests and verify failure**

Run:

```bash
cd hermes-plugins/sign402-wallet
python -m unittest tests.test_client tests.test_plugin -v
```

Expected: `/connect_whatsapp` still returns the disabled message and
`whatsapp_cloud` events are dropped without gateway calls.

- [ ] **Step 3: Extend the localhost client payloads**

Keep the existing protected endpoint paths but add a generic helper:

```python
def execute_approval(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...
```

Make `execute_imessage()` a compatibility wrapper. Use the same independent
approval bearer token and preserve localhost URL validation.

- [ ] **Step 4: Add Telegram WhatsApp onboarding UI**

Register `/connect_whatsapp`, add `Connect WhatsApp` to the main keyboard/help,
and create pairing with `channel=whatsapp`. Telegram instructs the user to send
the code to `SIGN402_WHATSAPP_PUBLIC_LINE`, which contains only the public
business number/contact link.

- [ ] **Step 5: Consume WhatsApp Cloud events before agent dispatch**

Recognize platform names `whatsapp_cloud`, `whatsapp-cloud`, and
`platforms/whatsapp_cloud`. Pair only when the inbound text is an active-looking
eight-character code. Parse button payloads exactly with:

```python
match = re.fullmatch(r"sign402:(approve|reject):([A-Za-z0-9_-]{8,128})", payload)
```

Send `approvalUserId=source.user_id`, `channel=whatsapp`, the exact approval ID,
and `YES`/`NO` to the gateway. Consume all other WhatsApp events with `_SKIP_RESULT`.

- [ ] **Step 6: Make Telegram progress copy channel-neutral**

Change purchase, Bitrefill, LLM-credit, and withdrawal start messages from
"Approve it in iMessage" to "Approve it in your selected approval channel".

- [ ] **Step 7: Run plugin/client tests**

Run:

```bash
cd hermes-plugins/sign402-wallet
python -m unittest tests.test_client tests.test_plugin -v
```

Expected: all existing Telegram/Photon tests and new WhatsApp tests pass.

- [ ] **Step 8: Commit Task 4**

```bash
git add hermes-plugins/sign402-wallet/client.py hermes-plugins/sign402-wallet/__init__.py hermes-plugins/sign402-wallet/tests/test_client.py hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "Add WhatsApp approval onboarding to Hermes plugin"
```

### Task 5: Document and verify the deployment flow

**Files:**
- Modify: `hermes-plugins/sign402-wallet/README.md`
- Modify: `docs/production-beta-checklist.md`
- Modify: `sign402-gateway/SECURITY.md`

- [ ] **Step 1: Document the Hermes Cloud wizard and webhook**

Document `hermes whatsapp-cloud`, the generated
`WHATSAPP_CLOUD_VERIFY_TOKEN`, signed webhook requirement, temporary Cloudflare
tunnel for testing, Meta `messages` subscription, and the fact that no domain is
required for the first test.

- [ ] **Step 2: Document the Meta utility template**

Specify template name `sign402_payment_approval`, category `UTILITY`, language
`en_US`, safe body variables, and two quick-reply buttons. State that public
beta stays disabled until Meta approves the template and a permanent System User
token replaces the 24-hour test token.

- [ ] **Step 3: Run the full relevant suites**

Run:

```bash
cd sign402-gateway
python -m unittest discover -s tests -v
cd ../hermes-plugins/sign402-wallet
python -m unittest discover -s tests -v
```

Expected: both suites pass with zero failures and errors.

- [ ] **Step 4: Run secret and placeholder scans**

Run:

```bash
rg -n "EAA[A-Za-z0-9]{40,}|SIGN402_WHATSAPP_ACCESS_TOKEN=.+" .
rg -n "WhatsApp approvals are not configured|connect_whatsapp.*not" hermes-plugins sign402-gateway
git diff --check
```

Expected: no real Meta token, no stale disabled-channel text in runtime code,
and no whitespace errors.

- [ ] **Step 5: Commit Task 5**

```bash
git add hermes-plugins/sign402-wallet/README.md docs/production-beta-checklist.md sign402-gateway/SECURITY.md
git commit -m "Document WhatsApp approval deployment"
```

- [ ] **Step 6: Perform the manual test after deployment**

On the VPS, configure Hermes Cloud and the Sign402 gateway secrets, start the
Cloudflare tunnel, verify/save the webhook in Meta, subscribe to `messages`, and
run one low-value approve plus one reject flow with the Meta test recipient.
Expected: only the selected WhatsApp channel receives the template; approve
executes once, reject never moves funds, and ordinary WhatsApp text never reaches
the Hermes agent.
