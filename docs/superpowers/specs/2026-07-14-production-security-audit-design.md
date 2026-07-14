# Sign402 production security audit design

## Objective

Perform a risk-first security audit of the Sign402 production payment path,
including source code, the Ubuntu VPS, Cloudflare Tunnel, Telegram/Hermes,
Photon iMessage approvals, Meta WhatsApp Cloud approvals, managed Base wallets,
and Bitrefill fulfillment. Fix confirmed Critical and High findings immediately
with isolated regression tests and reversible deployment steps. Verify the
result with negative tests and one explicitly approved live purchase costing no
more than USD 0.10.

The audit reduces known risk; it does not claim that any system can be proven
free of every vulnerability.

## Scope and decomposition

The audit is split into five bounded workstreams, executed in dependency order:

1. **Secrets and supply chain** — Git history and working-tree secret exposure,
   dependency vulnerabilities, unsafe runtime configuration, and backup
   handling.
2. **Gateway identity and authorization** — route inventory, shared and
   per-user token boundaries, Telegram identity binding, request bounds, CORS,
   rate limits, error redaction, replay resistance, and test/legacy endpoints.
3. **Wallet and commerce invariants** — encrypted private keys, filesystem
   permissions, withdrawal authorization, spend limits, approval commitments,
   transaction idempotency, reconciliation, Bitrefill redemption secrecy, and
   prevention of duplicate funding or fulfillment.
4. **Approval channels** — separate attack matrices for Photon iMessage and
   WhatsApp Cloud, covering pairing, sender binding, delivery, Confirm/Reject,
   expiry, duplicate decisions, wrong-channel decisions, and isolation from the
   general Hermes agent.
5. **Production infrastructure and end-to-end proof** — listening sockets,
   firewall policy, systemd isolation, service users, env/database/backup file
   permissions, Cloudflare Tunnel routing, Meta webhook signature verification,
   and a final purchase up to USD 0.10.

Each workstream produces evidence independently. A failure in one workstream
does not authorize unrelated refactoring in another.

## Audit method

Use four evidence layers:

- **Static inspection:** source, configuration schemas, service units, route
  handlers, cryptographic boundaries, dependency manifests, and Git metadata.
- **Automated verification:** existing unit/integration suites plus security
  regression tests written before every code fix.
- **Read-only production inspection:** process environment names with values
  redacted, file ownership/modes, listening sockets, firewall state, unit
  hardening, tunnel routes, health counters, and bounded logs.
- **Controlled active tests:** invalid bearer tokens, mismatched user identity,
  malformed/oversized requests, expired and replayed decisions, wrong-channel
  replies, and one final live purchase. Tests must not brute-force credentials,
  scan unrelated hosts, or exceed the approved USD 0.10 spend.

Commands shown to the operator must avoid printing secret values. When a local
secret must be checked, report only presence, length, hash comparison, owner,
and permissions. Never copy private keys, bearer tokens, redemption values, or
Meta credentials into chat.

## Severity and response policy

- **Critical:** plausible unauthorized key access, wallet action, payment,
  redemption disclosure, or remote code execution. Stop active testing, preserve
  evidence, rotate exposed credentials when applicable, add a failing test,
  implement one focused fix, verify, deploy, and retest before continuing.
- **High:** bypass of user/channel authorization, replay with financial impact,
  externally reachable internal service, or sensitive-data disclosure with
  meaningful prerequisites. Handle with the same isolated fix workflow before
  the final live purchase.
- **Medium:** meaningful defense-in-depth weakness without a demonstrated direct
  financial path. Record first, then fix in small reviewed batches.
- **Low:** hardening, observability, documentation, or operational hygiene with
  limited direct impact. Record and prioritize after functional security gates.

No production mutation is bundled with another finding. Every server change
must include the current-state capture, exact command, verification command,
and rollback command.

## Identity and approval-channel requirements

Both iMessage and WhatsApp must pass the same functional and adversarial gates:

- pairing begins from an authenticated Telegram identity;
- a pairing code is single-use, short-lived, and bound to one channel;
- the resulting phone identity maps to exactly one Telegram user;
- an approval displays the committed action, amount, token, wallet, reference,
  and expiry without exposing secrets;
- Confirm approves only the displayed pending commitment;
- Reject cannot execute a payment;
- expired, duplicate, stale, malformed, wrong-user, and wrong-channel decisions
  fail closed;
- handled approval messages do not reach the general LLM dispatch path;
- channel preference changes are explicit and do not leave two active approvers
  able to authorize the same request;
- delivery failure cannot be interpreted as approval.

The final verification must exercise one successful approval on iMessage and
one successful approval on WhatsApp. Only one of those flows may perform the
approved live purchase; the other uses a no-funds test commitment or a rejected
request so total live spend remains at or below USD 0.10.

## Wallet and payment safety gates

Before the live purchase, evidence must show:

- private keys are encrypted at rest and never returned by an API;
- master keys and API credentials are readable only by their required service
  account and root where necessary;
- body-supplied `telegramUserId` cannot override the authenticated per-user
  token identity;
- withdrawal, x402, and Bitrefill operations re-check wallet ownership, token,
  amount, receiver, spend limit, approval commitment, and expiry at execution;
- a repeated request or decision cannot produce a second transfer or provider
  order;
- reconciliation distinguishes no-transfer, transferred-not-fulfilled, and
  fulfilled-response-failed states without prompting an unsafe retry;
- redemption material is available only through the authenticated user path and
  never through public events, logs, prompts, or health responses.

## Infrastructure safety gates

The production gateway and databases remain private to the VPS. The Cloudflare
route exposes only the Hermes WhatsApp webhook origin on port 8090, not the
Sign402 gateway on port 8099. The audit verifies host firewall rules, loopback
binding, Cloudflare route configuration, service restart policy, process and
filesystem privileges, backup confidentiality, log redaction, patch status,
and that legacy/test endpoints are disabled.

Meta verification covers callback URL ownership, webhook subscription, HMAC
signature rejection counters, app publication status, and token scope without
printing token values. Photon verification covers only the trusted sidecar
boundary and does not treat general iMessage text as agent input.

## Verification and deliverables

The audit is complete only when it produces:

1. A dated risk register containing severity, affected component, evidence,
   exploit scenario, status, fix commit or server change, verification, and
   residual risk.
2. Passing baseline and post-fix test results for every modified component.
3. Negative-test evidence for authentication, user binding, replay, expiry,
   wrong-channel decisions, public exposure, and secret redaction.
4. Successful functional verification of both iMessage and WhatsApp approval
   channels.
5. One successful live purchase at or below USD 0.10 after all Critical and High
   gates pass, with the transaction and Bitrefill order recorded but redemption
   kept private.
6. A final production checklist and rollback/recovery notes for every applied
   operational change.
