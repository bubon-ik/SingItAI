# WhatsApp Public Route Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose only the Meta WhatsApp webhook at `whatsapp.singitai.app`, keep health diagnostics local to the VPS, and add a reversible HSTS policy scoped to the WhatsApp hostname.

**Architecture:** Cloudflare Tunnel path routing becomes the public allowlist: only the exact `/whatsapp/webhook` path is forwarded to Hermes on `127.0.0.1:8090`. A Cloudflare response-header transform rule adds HSTS only for `whatsapp.singitai.app`; Hermes code and server bindings do not change.

**Tech Stack:** Cloudflare Tunnel published application routes, Cloudflare Response Header Transform Rules, Meta WhatsApp Cloud webhook, Hermes gateway, curl.

## Global Constraints

- Keep the public hostname `whatsapp.singitai.app`.
- Keep the origin service `http://localhost:8090`.
- Publish only the path expression `^/whatsapp/webhook$`.
- Keep Hermes bound to `127.0.0.1:8090`.
- Set `Strict-Transport-Security` to `max-age=2592000`.
- Do not enable `includeSubDomains` or `preload`.
- Do not expose any verify token, app secret, access token, phone number, or linked-user identifier in logs or documentation.
- Do not change Meta's configured callback URL.

---

### Task 1: Capture the pre-change baseline

**Files:**
- Reference: `docs/security-audit/2026-07-14-risk-register.md`

**Interfaces:**
- Consumes: local Hermes health endpoint and current public Cloudflare hostname.
- Produces: baseline HTTP status and webhook counters used by later verification.

- [ ] **Step 1: Verify the origin remains loopback-only**

Run on the VPS:

```bash
ss -ltnp | grep ':8090'
```

Expected: the only listener is `127.0.0.1:8090`.

- [ ] **Step 2: Capture local health without printing credentials**

Run on the VPS:

```bash
curl -sS http://127.0.0.1:8090/health
```

Expected: HTTP response JSON with `"status": "ok"`.

- [ ] **Step 3: Confirm public health is currently exposed**

Run:

```bash
curl -sS -o /dev/null -w 'public_health_before=%{http_code}\n' \
  https://whatsapp.singitai.app/health
```

Expected before the change: `public_health_before=200`.

---

### Task 2: Restrict the Cloudflare Tunnel route

**Files:**
- No repository files changed.
- Cloudflare resource: tunnel `singitai-whatsapp`, published application route for `whatsapp.singitai.app`.

**Interfaces:**
- Consumes: current hostname-to-origin mapping.
- Produces: a public allowlist containing only the Meta webhook path.

- [ ] **Step 1: Open the existing published application route**

In Cloudflare Zero Trust:

```text
Networks
→ Connectors
→ singitai-whatsapp
→ Published application routes
→ whatsapp.singitai.app
→ Edit
```

- [ ] **Step 2: Apply the exact route values**

Set:

```text
Subdomain: whatsapp
Domain: singitai.app
Path: ^/whatsapp/webhook$
Service type: HTTP
Service URL: localhost:8090
```

Save the route. Do not create a second route for the same hostname.

- [ ] **Step 3: Verify public health is no longer forwarded**

Run:

```bash
code=$(curl -sS -o /dev/null -w '%{http_code}' \
  https://whatsapp.singitai.app/health || true)
echo "public_health_after=$code"
test "$code" != "200"
```

Expected: a non-`200` status and command exit `0`.

- [ ] **Step 4: Verify local health remains available**

Run on the VPS:

```bash
curl -sS -o /dev/null -w 'local_health=%{http_code}\n' \
  http://127.0.0.1:8090/health
```

Expected: `local_health=200`.

---

### Task 3: Add hostname-scoped HSTS

**Files:**
- No repository files changed.
- Cloudflare resource: response-header transform rule `whatsapp-hsts`.

**Interfaces:**
- Consumes: HTTPS responses for `whatsapp.singitai.app`.
- Produces: a reversible HSTS header limited to the WhatsApp hostname.

- [ ] **Step 1: Create a response-header transform rule**

In the Cloudflare dashboard for `singitai.app`:

```text
Rules
→ Transform Rules
→ Modify Response Header
→ Create rule
```

- [ ] **Step 2: Configure the rule**

Use:

```text
Rule name: whatsapp-hsts
Expression: (http.host eq "whatsapp.singitai.app")
Operation: Set static
Header name: Strict-Transport-Security
Value: max-age=2592000
```

Deploy the rule. Do not add `includeSubDomains` or `preload`.

- [ ] **Step 3: Verify the response header**

Run:

```bash
curl -sSI https://whatsapp.singitai.app/whatsapp/webhook \
  | tr -d '\r' \
  | grep -i '^strict-transport-security: max-age=2592000$'
```

Expected: exactly one matching header line.

---

### Task 4: Verify Meta webhook behavior and fail-closed security

**Files:**
- No repository files changed.

**Interfaces:**
- Consumes: configured `WHATSAPP_CLOUD_VERIFY_TOKEN` and Hermes app secret.
- Produces: evidence that legitimate verification works and invalid signatures are rejected.

- [ ] **Step 1: Verify the Meta callback GET**

Run on the VPS without printing the token:

```bash
set -a
source ~/.hermes/.env
set +a

curl -sS -G \
  'https://whatsapp.singitai.app/whatsapp/webhook' \
  --data-urlencode 'hub.mode=subscribe' \
  --data-urlencode "hub.verify_token=$WHATSAPP_CLOUD_VERIFY_TOKEN" \
  --data-urlencode 'hub.challenge=route_hardening_ok' \
  -w '\nHTTP %{http_code}\n'
```

Expected:

```text
route_hardening_ok
HTTP 200
```

- [ ] **Step 2: Reject an invalid webhook signature**

Run on the VPS:

```bash
before=$(curl -sS http://127.0.0.1:8090/health)

code=$(curl -sS -o /tmp/sign402-invalid-signature.json -w '%{http_code}' \
  -X POST 'https://whatsapp.singitai.app/whatsapp/webhook' \
  -H 'Content-Type: application/json' \
  -H 'X-Hub-Signature-256: sha256=0000000000000000000000000000000000000000000000000000000000000000' \
  --data '{"object":"whatsapp_business_account","entry":[]}')

after=$(curl -sS http://127.0.0.1:8090/health)

BEFORE="$before" AFTER="$after" CODE="$code" python3 - <<'PY'
import json
import os

before = json.loads(os.environ["BEFORE"])
after = json.loads(os.environ["AFTER"])

assert os.environ["CODE"] == "401"
assert after["accepted"] == before["accepted"]
assert after["rejected_signature"] == before["rejected_signature"] + 1
print("invalid_signature_rejected=yes")
PY
```

Expected: `invalid_signature_rejected=yes`.

- [ ] **Step 3: Verify a legitimate event**

Send one ordinary message to the SingIt WhatsApp line, then run on the VPS:

```bash
curl -sS http://127.0.0.1:8090/health
```

Expected: `accepted` increases by one, `rejected_signature` does not increase,
and the message receives the normal SingIt response.

---

### Task 5: Close the audit finding

**Files:**
- Modify: `docs/security-audit/2026-07-14-risk-register.md`

**Interfaces:**
- Consumes: verified results from Tasks 1-4.
- Produces: final evidence and rollback instructions for `SEC-005`.

- [ ] **Step 1: Update `SEC-005`**

Change its status from `CONFIRMED` to `FIXED` and record:

```text
Cloudflare Tunnel forwards only ^/whatsapp/webhook$.
Public /health no longer returns 200.
Local /health remains 200.
Meta callback verification returns 200.
Invalid signatures return 401 and increment rejected_signature only.
The WhatsApp hostname returns Strict-Transport-Security: max-age=2592000.
```

- [ ] **Step 2: Record the rollback**

Add:

```text
Rollback: clear the route Path field and disable the whatsapp-hsts transform rule.
```

- [ ] **Step 3: Validate the documentation diff**

Run:

```bash
git diff --check
git diff -- docs/security-audit/2026-07-14-risk-register.md
```

Expected: no whitespace errors and only the intended `SEC-005` evidence change.

- [ ] **Step 4: Commit**

Run:

```bash
git add docs/security-audit/2026-07-14-risk-register.md
git commit -m "Close public WhatsApp route finding"
```

- [ ] **Step 5: Push**

Run:

```bash
git push singitai x402Bnkr
```

Expected: the new audit commit is present on `singitai/x402Bnkr`.
