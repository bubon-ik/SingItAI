# Sign402 Production Security Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce evidence that the Sign402 production payment path fails closed across code, VPS infrastructure, iMessage, and WhatsApp; immediately fix confirmed Critical and High findings; and finish with one live purchase costing no more than USD 0.10.

**Architecture:** Execute five security workstreams in dependency order and record every result in one dated risk register. Read-only and negative tests run before any live payment; every confirmed Critical or High code defect is isolated behind a failing regression test and its own commit, while every production configuration change includes a current-state capture, verification, and rollback command.

**Tech Stack:** Python 3.11+, `unittest`, Node.js built-in test runner, npm audit, SQLite, Ubuntu 24.04, systemd, Cloudflare Tunnel, Meta WhatsApp Cloud API, Photon iMessage, Base Mainnet, Bitrefill REST v2.

## Global Constraints

- Never print or copy private keys, wallet master keys, bearer tokens, Meta credentials, Photon secrets, Bitrefill redemption values, or complete environment-file contents.
- Stop active testing immediately on a plausible unauthorized payment, key exposure, redemption disclosure, or remote-code-execution path.
- Do not proceed to the live purchase while any Critical or High finding remains open.
- Total live audit spend must not exceed USD 0.10.
- iMessage and WhatsApp must each complete one approval flow; only one flow may execute the live purchase.
- Preserve unrelated tracked and untracked user files.
- Run the gateway single-process during the audit because SQLite claim-once guarantees are not designed for multiple worker processes.

---

### Task 1: Establish the evidence baseline and risk register

**Files:**
- Create: `docs/security-audit/2026-07-14-risk-register.md`
- Inspect: `.gitignore`
- Inspect: all tracked `*.env.example`, `package.json`, `package-lock.json`, and `pyproject.toml` files

**Interfaces:**
- Consumes: the current `x402Bnkr` checkout and all component test suites.
- Produces: a dated risk register whose finding IDs are referenced by later fixes and production changes.

- [ ] **Step 1: Create the risk-register skeleton**

Create the document with these exact sections:

```markdown
# Sign402 security audit — 2026-07-14

## Scope

Code, Ubuntu VPS, Cloudflare Tunnel, Telegram/Hermes, Photon iMessage, Meta
WhatsApp Cloud, managed Base wallets, and Bitrefill.

## Baseline

| Check | Evidence | Result |
|---|---|---|

## Findings

| ID | Severity | Component | Evidence | Exploit scenario | Status | Fix/mitigation | Verification | Residual risk |
|---|---|---|---|---|---|---|---|---|

## Approval-channel matrix

| Test | iMessage | WhatsApp | Evidence |
|---|---|---|---|

## Production changes and rollback

| Change | Before | Command | Verification | Rollback |
|---|---|---|---|---|

## Final gates

- [ ] No open Critical findings
- [ ] No open High findings
- [ ] iMessage approval path verified
- [ ] WhatsApp approval path verified
- [ ] Public exposure verified
- [ ] Secret and log redaction verified
- [ ] Live purchase at or below USD 0.10 verified
```

- [ ] **Step 2: Run the clean local baseline**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. python3 -m unittest discover -s tests -q

cd ../hermes-plugins/sign402-wallet
env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=. \
  python3 -m unittest discover -s tests -q

cd ../../cdp-x402-service
npm test

cd ../singit-risk-check
npm test
```

Expected: every suite exits zero. Record the exact test counts. Any baseline
failure is a finding and blocks active production tests until understood.

- [ ] **Step 3: Check tracked and historical secret exposure without printing values**

Run from the repository root:

```bash
git ls-files | rg '(^|/)(\.env($|\.)|.*\.env$|.*bitrefill-api-key.*|.*wallet-master.*|.*private-key.*)' || true

git grep -Il -E \
  '(BEGIN (RSA|EC|OPENSSH) PRIVATE KEY|WHATSAPP_CLOUD_ACCESS_TOKEN=[^<]|SIGN402_WALLET_MASTER_KEY=[^<]|BITREFILL_API_KEY=[^<]|EAA[A-Za-z0-9]{30,})' \
  -- ':!*.env.example' ':!docs/**' || true

git log --all --name-only --pretty=format: | sort -u | rg \
  '(^|/)(\.env$|.*\.pem$|.*\.key$|\.bitrefill-api-key$|quantoz-mainnet-wallet\.env$)' || true
```

Expected: only example/config-document paths are tracked, the content scan
prints no filenames, and Git history contains no real secret files. Do not run
`git show` on any suspicious historical path in chat; record only its path and
commit ID locally, mark it Critical, and rotate the affected credential.

- [ ] **Step 4: Audit dependency manifests**

Run:

```bash
cd cdp-x402-service
npm audit --omit=dev --audit-level=moderate

cd ../singit-risk-check
npm audit --omit=dev --audit-level=moderate

cd ../sign402-gateway
python3 -m pip check
python3 -m pip list --outdated --format=json > /tmp/sign402-python-outdated.json
python3 -c 'import json; p=json.load(open("/tmp/sign402-python-outdated.json")); print("outdated_python_packages=", len(p))'
```

Expected: npm reports zero known moderate-or-higher production advisories and
`pip check` reports no broken requirements. Record advisory IDs without copying
registry credentials. An unavailable advisory service is recorded as an audit
limitation rather than a pass.

- [ ] **Step 5: Commit the audit baseline artifact**

```bash
git add docs/security-audit/2026-07-14-risk-register.md
git commit -m "Start Sign402 production security audit"
```

---

### Task 2: Audit source-level identity, authorization, and payment invariants

**Files:**
- Inspect: `sign402-gateway/sign402_gateway/server.py`
- Inspect: `sign402-gateway/sign402_gateway/user_wallets.py`
- Inspect: `sign402-gateway/sign402_gateway/imessage_approvals.py`
- Inspect: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Inspect: `sign402-gateway/sign402_gateway/commerce_store.py`
- Inspect: `hermes-plugins/sign402-wallet/__init__.py`
- Inspect: `hermes-plugins/sign402-wallet/client.py`
- Inspect: `cdp-x402-service/src/index.mjs`
- Inspect: `cdp-x402-service/src/user-token-transfer.mjs`
- Test: `sign402-gateway/tests/test_gateway_server.py`
- Test: `sign402-gateway/tests/test_imessage_approvals.py`
- Test: `sign402-gateway/tests/test_user_wallets.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_client.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`
- Update: `docs/security-audit/2026-07-14-risk-register.md`

**Interfaces:**
- Consumes: the route handlers, trusted identity sources, wallet service, approval service, and commerce state machine.
- Produces: a route/auth matrix and evidence for every financial safety gate.

- [ ] **Step 1: Inventory every HTTP route and its guard**

Run:

```bash
rg -n 'if path == |def _handle_|_require_authenticated_user|_legacy_operator_request_allowed|_service_secret_authorized|_require_approval_api_token' \
  sign402-gateway/sign402_gateway/server.py \
  > /tmp/sign402-route-auth-inventory.txt
sed -n '1,260p' /tmp/sign402-route-auth-inventory.txt
```

For each `/agent/*`, `/internal/*`, and legacy route, record one of these guards
in the risk register: `public-safe`, `per-user-token`, `wallet-shared-token`,
`photon-token`, `service-secret`, `legacy-operator-token`, or `disabled`. Any
financial or redemption-bearing route without an explicit guard is Critical.

- [ ] **Step 2: Verify existing authorization and replay regressions**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. python3 -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_user_scoped_endpoints_require_per_user_token \
  tests.test_gateway_server.GatewayServerTests.test_imessage_pairing_requires_photon_api_token \
  tests.test_gateway_server.GatewayServerTests.test_test_imessage_approval_endpoint_is_disabled_by_default \
  tests.test_imessage_approvals.ImessageApprovalTests \
  tests.test_user_wallets \
  tests.test_bitrefill_runner -v

cd ../hermes-plugins/sign402-wallet
env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=. \
  python3 -m unittest discover -s tests -q
```

Expected: zero failures. Confirm tests cover mismatched `telegramUserId`, stale
and duplicate decisions, channel binding, withdrawal approval commitment,
Bitrefill duplicate fulfillment, private-key redaction, and safe upstream errors.

- [ ] **Step 3: Review request bounds and outbound destinations**

Run:

```bash
rg -n 'Content-Length|read\(|max_response|max.*bytes|timeout=|urlopen|Request\(|subprocess\.(run|Popen)|shell=True|SIGN402_GATEWAY_URL|127\.0\.0\.1|localhost' \
  sign402-gateway hermes-plugins/sign402-wallet cdp-x402-service/src \
  -g '*.py' -g '*.mjs' -g '!**/node_modules/**'
```

Record whether inbound body size, upstream response size, subprocess timeout,
and loopback-only gateway URL are bounded. `shell=True`, an unbounded public
request body, or a user-controlled outbound URL reaching private networks is at
least High until proven unreachable.

- [ ] **Step 4: Apply the immediate-fix loop for each confirmed Critical/High code finding**

For one finding at a time:

1. Add one row with status `CONFIRMED` to the risk register.
2. Use `superpowers:systematic-debugging` to trace the failing boundary.
3. Use `superpowers:test-driven-development` to add the smallest reproducer and
   run it to observe the expected failure.
4. Change only the affected boundary; do not bundle unrelated hardening.
5. Run the focused test and all suites from Task 1.
6. Update the row to `FIXED`, including the test name and commit hash.
7. Commit with `Fix <finding ID>: <short cause>`.

Do not continue to the next finding while the current focused or regression
suite is failing.

---

### Task 3: Capture a read-only production security snapshot

**Files:**
- Inspect on VPS: `/etc/sign402-gateway.env`
- Inspect on VPS: `~/.hermes/.env`
- Inspect on VPS: `~/.sign402/`
- Inspect on VPS: `~/sign402-backups/`
- Inspect on VPS: systemd units for `sign402-gateway`, `hermes-gateway`, and `cloudflared`
- Update: `docs/security-audit/2026-07-14-risk-register.md`

**Interfaces:**
- Consumes: operator-pasted command output with secrets redacted.
- Produces: evidence for public exposure, service privilege, secret permissions, and runtime hardening.

- [ ] **Step 1: Capture identity, services, and listening sockets**

Run on the VPS:

```bash
date -Is
uname -a
cat /etc/os-release

systemctl is-active sign402-gateway cloudflared
systemctl --user is-active hermes-gateway

sudo ss -lntup
sudo ufw status verbose || true
sudo nft list ruleset | sed -n '1,260p'
```

Expected: Sign402 listens only on `127.0.0.1:8099`, Hermes only on
`127.0.0.1:8090`, cloudflared makes outbound connections, and no public socket
exposes either internal port. Record every `0.0.0.0` or `[::]` listener and its
owning process.

- [ ] **Step 2: Capture systemd privilege and restart policy**

Run:

```bash
sudo systemctl show sign402-gateway \
  -p FragmentPath -p DropInPaths -p User -p Group -p MainPID -p EnvironmentFiles \
  -p NoNewPrivileges -p PrivateTmp -p ProtectSystem -p ProtectHome \
  -p RestrictAddressFamilies -p CapabilityBoundingSet -p Restart

systemctl --user show hermes-gateway \
  -p FragmentPath -p DropInPaths -p MainPID -p EnvironmentFiles -p Restart

sudo systemctl show cloudflared \
  -p FragmentPath -p DropInPaths -p User -p Group -p MainPID -p Restart
sudo systemd-analyze security sign402-gateway.service --no-pager || true
```

Expected: `sign402-gateway` runs as the dedicated non-root `hermes` user, uses
one process, restarts on failure, and has no unnecessary capabilities. Do not
print complete unit contents during the audit: a remotely managed cloudflared
unit may contain its tunnel token in `ExecStart`.

- [ ] **Step 3: Check secret, database, and backup permissions without reading contents**

Run:

```bash
stat -c '%a %U:%G %n' \
  /etc/sign402-gateway.env \
  "$HOME/.hermes/.env" \
  "$HOME/.sign402/user-wallets.db" \
  "$HOME/.sign402/imessage-approvals.db" \
  "$HOME/.sign402/user-spend-limits.json"

sudo awk -F= '
  /^[[:space:]]*#/ || NF < 2 {next}
  {gsub(/[[:space:]]/, "", $1); print $1 "=<set>"}
' /etc/sign402-gateway.env | sort

awk -F= '
  /^[[:space:]]*#/ || NF < 2 {next}
  {gsub(/[[:space:]]/, "", $1); print $1 "=<set>"}
' "$HOME/.hermes/.env" | sort

find "$HOME/sign402-backups" -maxdepth 3 -type f \
  -printf '%m %u:%g %p\n' | sort | tail -n 120
```

Expected: environment files and databases are mode `600` or stricter; their
directories and backups are not readable by group/other. The output shows only
variable names, never values.

- [ ] **Step 4: Verify runtime flags and safe health output**

Run:

```bash
sudo bash -lc '
  set -a; source /etc/sign402-gateway.env; set +a
  for name in \
    SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR \
    SIGN402_ENABLE_TEST_ENDPOINTS \
    SIGN402_ENABLE_CORS; do
    value=${!name:-}
    if [ -n "$value" ]; then state=set; else state=unset; fi
    printf "%s=%s\n" "$name" "$state"
  done
'

curl -sS http://127.0.0.1:8099/health | python3 -m json.tool
curl -sS http://127.0.0.1:8090/health | python3 -m json.tool
curl -sS https://whatsapp.singitai.app/health | python3 -m json.tool
```

Expected: legacy payment executor, test endpoints, and CORS are unset; health
responses expose no tokens, private keys, phone identities, redemption values,
database paths, or internal exception text.

- [ ] **Step 5: Check public route isolation**

Run from a machine outside the VPS or through the public hostname:

```bash
curl -sS -o /dev/null -w 'health=%{http_code}\n' \
  https://whatsapp.singitai.app/health
curl -sS -o /dev/null -w 'gateway_wallet=%{http_code}\n' \
  -X POST https://whatsapp.singitai.app/agent/wallet \
  -H 'Content-Type: application/json' \
  -d '{"telegramUserId":"1045618308"}'
curl -sS -o /dev/null -w 'internal_fulfill=%{http_code}\n' \
  -X POST https://whatsapp.singitai.app/internal/fulfill-bitrefill \
  -H 'Content-Type: application/json' -d '{}'
```

Expected: health may return `200`; Sign402 gateway and internal fulfillment
paths must return `404` or another non-success response from Hermes, never the
gateway API.

- [ ] **Step 6: Apply the immediate-fix loop for each confirmed Critical/High production finding**

Before each change, append its current state and rollback to the production
changes table. Apply one command group, verify the expected socket/status/mode,
and roll back immediately if health or service activation fails. Code changes
follow Task 2's TDD loop; systemd/firewall/file-mode changes use the exact
before/after evidence captured in Steps 1–4.

---

### Task 4: Execute controlled authentication and replay negative tests

**Files:**
- Inspect on VPS: `/etc/sign402-gateway.env` variable names only
- Inspect on VPS: `~/.sign402/imessage-approvals.db` through bounded SQL queries
- Update: `docs/security-audit/2026-07-14-risk-register.md`

**Interfaces:**
- Consumes: localhost services and existing production credentials inside root-owned subshells.
- Produces: status-code and database-state evidence without exposing credentials.

- [ ] **Step 1: Verify unauthenticated and wrong-token requests fail closed**

Run on the VPS:

```bash
curl -sS -o /tmp/sign402-no-auth.json -w 'no_auth=%{http_code}\n' \
  -X POST http://127.0.0.1:8099/agent/wallet \
  -H 'Content-Type: application/json' \
  -d '{"telegramUserId":"1045618308"}'

curl -sS -o /tmp/sign402-bad-auth.json -w 'bad_auth=%{http_code}\n' \
  -X POST http://127.0.0.1:8099/agent/wallet \
  -H 'Authorization: Bearer audit-invalid-token' \
  -H 'Content-Type: application/json' \
  -d '{"telegramUserId":"1045618308"}'

python3 -c '
import json
for path in ("/tmp/sign402-no-auth.json", "/tmp/sign402-bad-auth.json"):
    body=json.load(open(path))
    print(path, {"ok": body.get("ok"), "error": body.get("error")})
'
```

Expected: both return `401`; bodies contain fixed authentication errors and no
configuration, stack trace, token, wallet, or user data.

- [ ] **Step 2: Verify the shared wallet token cannot impersonate a user**

Run:

```bash
sudo bash -lc '
  set -a; source /etc/sign402-gateway.env; set +a
  curl -sS -o /tmp/sign402-shared-only.json -w "shared_only=%{http_code}\n" \
    -X POST http://127.0.0.1:8099/agent/wallet-balance \
    -H "Authorization: Bearer ${SIGN402_WALLET_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"telegramUserId\":\"1045618308\"}"
'
python3 -c 'import json; b=json.load(open("/tmp/sign402-shared-only.json")); print({"ok":b.get("ok"),"error":b.get("error")})'
```

Expected: `401` because `X-Sign402-User-Token` is absent. A `200` response is
Critical and stops the audit.

- [ ] **Step 3: Verify approval endpoints reject the wallet token**

Run:

```bash
sudo bash -lc '
  set -a; source /etc/sign402-gateway.env; set +a
  curl -sS -o /tmp/sign402-wrong-boundary.json -w "wrong_boundary=%{http_code}\n" \
    -X POST http://127.0.0.1:8099/agent/imessage/pairing \
    -H "Authorization: Bearer ${SIGN402_WALLET_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"telegramUserId\":\"1045618308\"}"
'
python3 -c 'import json; b=json.load(open("/tmp/sign402-wrong-boundary.json")); print({"ok":b.get("ok"),"error":b.get("error")})'
```

Expected: `401`; wallet and approval bearer tokens are not interchangeable.

- [ ] **Step 4: Verify test and legacy endpoints are absent**

Run:

```bash
curl -sS http://127.0.0.1:8099/health | python3 -c '
import json,sys
b=json.load(sys.stdin)
paths=set(b.get("endpoints", []))
for forbidden in ("/agent/test-imessage-approval", "/execute-payment"):
    print(forbidden, "EXPOSED" if forbidden in paths else "disabled")
'
```

Expected: both print `disabled`.

---

### Task 5: Verify iMessage and WhatsApp approval channels

**Files:**
- Temporarily modify on VPS: `/etc/sign402-gateway.env`
- Inspect on VPS: `~/.sign402/imessage-approvals.db`
- Update: `docs/security-audit/2026-07-14-risk-register.md`

**Interfaces:**
- Consumes: Telegram user `1045618308`, the registered Photon line, and the Meta WhatsApp Business line.
- Produces: successful channel-specific test approvals plus replay/wrong-channel evidence with zero financial movement.

- [ ] **Step 1: Temporarily enable the authenticated no-funds test endpoint with rollback prepared**

Run on the VPS:

```bash
sudo cp -a /etc/sign402-gateway.env /etc/sign402-gateway.env.pre-audit-test
sudo chmod 600 /etc/sign402-gateway.env.pre-audit-test
sudo sed -i '/^SIGN402_ENABLE_TEST_ENDPOINTS=/d' /etc/sign402-gateway.env
printf '%s\n' 'SIGN402_ENABLE_TEST_ENDPOINTS=true' | \
  sudo tee -a /etc/sign402-gateway.env >/dev/null
sudo systemctl restart sign402-gateway
systemctl is-active sign402-gateway
curl -sS http://127.0.0.1:8099/health | \
  python3 -c 'import json,sys; print("/agent/test-imessage-approval" in json.load(sys.stdin).get("endpoints", []))'
```

Expected: service is `active` and the final command prints `True`. Rollback at
any point is:

```bash
sudo cp -a /etc/sign402-gateway.env.pre-audit-test /etc/sign402-gateway.env
sudo systemctl restart sign402-gateway
```

- [ ] **Step 2: Pair and verify iMessage**

In Telegram, run `/connect_imessage`, provide the same iMessage phone identity
used by the production account, and send the returned code from that identity.
Then trigger the no-funds approval:

```bash
sudo bash -lc '
  set -a; source /etc/sign402-gateway.env; set +a
  curl -sS -X POST http://127.0.0.1:8099/agent/test-imessage-approval \
    -H "Authorization: Bearer ${SIGN402_PHOTON_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"telegramUserId\":\"1045618308\"}"
'
```

Approve once using the action displayed in iMessage, then send the same
decision again. Query only safe
fields:

```bash
python3 -c '
import os,sqlite3
db=sqlite3.connect(os.path.expanduser("~/.sign402/imessage-approvals.db"))
print(*db.execute("SELECT approval_id,channel,status,datetime(created_at,\"unixepoch\",\"localtime\") FROM imessage_approvals ORDER BY created_at DESC LIMIT 3").fetchall(),sep="\n")
'
```

Expected: one `imessage` approval reaches `approved`; the duplicate reply does
not create or approve another record and does not reach the general Hermes bot.

- [ ] **Step 3: Pair and verify WhatsApp, including wrong-channel rejection**

In Telegram, run `/connect_whatsapp` and send the returned code to the Meta
business number. Trigger another no-funds approval with the same localhost curl
command from Step 2. While it is pending, send `YES` from the previously linked
iMessage identity; the pending WhatsApp approval must remain pending. Then press
press **Confirm** once in WhatsApp and press the same button again.

Run the safe query from Step 2 and:

```bash
curl -sS http://127.0.0.1:8090/health | python3 -m json.tool
```

Expected: one `whatsapp` approval reaches `approved`; the iMessage reply cannot
change it; the repeated WhatsApp payload cannot approve anything else; ordinary
WhatsApp text receives no general-agent response.

- [ ] **Step 4: Restore production configuration and prove the test endpoint is gone**

Run:

```bash
sudo cp -a /etc/sign402-gateway.env.pre-audit-test /etc/sign402-gateway.env
sudo systemctl restart sign402-gateway
systemctl is-active sign402-gateway
curl -sS http://127.0.0.1:8099/health | \
  python3 -c 'import json,sys; print("/agent/test-imessage-approval" in json.load(sys.stdin).get("endpoints", []))'
sudo rm /etc/sign402-gateway.env.pre-audit-test
```

Expected: service is `active` and the endpoint check prints `False` before the
temporary backup is removed.

---

### Task 6: Verify Meta webhook signature failure and tunnel isolation

**Files:**
- Inspect: Hermes health counters on port 8090
- Inspect: Cloudflare published route `whatsapp.singitai.app`
- Update: `docs/security-audit/2026-07-14-risk-register.md`

**Interfaces:**
- Consumes: the public WhatsApp webhook with a deliberately invalid signature.
- Produces: evidence that unsigned/forged callbacks are rejected without dispatch.

- [ ] **Step 1: Capture counters, submit one invalid signature, and capture counters again**

Run on the VPS:

```bash
curl -sS http://127.0.0.1:8090/health > /tmp/hermes-health-before.json

curl -sS -o /tmp/invalid-whatsapp-response.txt -w 'invalid_signature=%{http_code}\n' \
  -X POST https://whatsapp.singitai.app/whatsapp/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Hub-Signature-256: sha256=0000000000000000000000000000000000000000000000000000000000000000' \
  -d '{"object":"whatsapp_business_account","entry":[]}'

curl -sS http://127.0.0.1:8090/health > /tmp/hermes-health-after.json

python3 -c '
import json
a=json.load(open("/tmp/hermes-health-before.json"))
b=json.load(open("/tmp/hermes-health-after.json"))
print({"accepted_before":a.get("accepted"),"accepted_after":b.get("accepted"),"rejected_before":a.get("rejected_signature"),"rejected_after":b.get("rejected_signature")})
'
```

Expected: HTTP is non-2xx, `accepted` does not increase, and
`rejected_signature` increases by one. The response must not include the App
Secret, verify token, access token, stack trace, or filesystem path.

---

### Task 7: Execute the final live USD 0.10 purchase and close the audit

**Files:**
- Inspect on VPS: `~/apps/sign402/demo-dashboard/bitrefill-orders.sqlite3`
- Inspect on VPS: `~/.sign402/imessage-approvals.db`
- Update: `docs/security-audit/2026-07-14-risk-register.md`

**Interfaces:**
- Consumes: a zero-open-Critical/High risk register, the linked WhatsApp approval channel, and the authenticated Telegram wallet.
- Produces: one delivered Bitrefill order with one funding transaction and a safe Telegram result.

- [ ] **Step 1: Enforce the go/no-go gate**

Review the risk register. Continue only when the Findings table has no row with
severity `Critical` or `High` whose status is not `FIXED`. Lower-severity
accepted risks must have an explicit residual-risk reason. Confirm the active
approval preference is WhatsApp:

```bash
python3 -c '
import os,sqlite3
db=sqlite3.connect(os.path.expanduser("~/.sign402/imessage-approvals.db"))
print(db.execute("SELECT telegram_user_id,channel FROM approval_channel_preferences WHERE telegram_user_id=?",("1045618308",)).fetchone())
'
```

Expected: `('1045618308', 'whatsapp')`.

- [ ] **Step 2: Perform exactly one live purchase**

In Telegram, choose `Bitrefill Gift Card (USD)`, amount `0.1`, and USDC. Verify
the WhatsApp approval states the product, maximum `0.1 USDC`, wallet, reference,
and expiry. Press Approve once. Do not repeat the flow if Telegram reports an
error; inspect state first.

- [ ] **Step 3: Verify single funding, single fulfillment, and private redemption**

Run:

```bash
python3 -c '
import json,sqlite3
db=sqlite3.connect("/home/hermes/apps/sign402/demo-dashboard/bitrefill-orders.sqlite3")
r=db.execute("SELECT quote_id,state,quote_json,metadata_json FROM bitrefill_orders ORDER BY updated_at DESC LIMIT 1").fetchone()
q=json.loads(r[2]); m=json.loads(r[3] or "{}"); w=m.get("walletCheckout",{}); b=m.get("bitrefill",{})
print(json.dumps({"quoteId":r[0],"state":r[1],"token":q.get("paymentTokenSymbol"),"amount":q.get("paymentTokenAmount"),"fundingTx":w.get("userFunding",{}).get("txId"),"bitrefillOrderId":b.get("orderId"),"bitrefillStatus":b.get("status"),"redemptionStored":bool((b.get("redemption") or {}).get("value"))},indent=2))
'

curl -sS http://127.0.0.1:8099/health | python3 -m json.tool
```

Expected: one non-empty funding transaction, one Bitrefill order, state
`DELIVERED`, amount `0.1`, and `redemptionStored: true`. Do not print the
redemption value. Re-pressing the old WhatsApp button must not create another
funding transaction or order.

- [ ] **Step 4: Run final regression suites and close the report**

Run the four suites from Task 1 again. Update every finding with final status,
verification evidence, and residual risk. Check every Final gates box only when
its evidence is present. Then commit and push:

```bash
git add docs/security-audit/2026-07-14-risk-register.md
git commit -m "Complete Sign402 production security audit"
git push singitai x402Bnkr
```
