# Approval Confirmation Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace internal approval-status wording with action-appropriate copy for purchases, withdrawals, test checks, and unknown requests.

**Architecture:** Keep decision-state handling unchanged and make `_decision_text()` the single copy-mapping boundary. Gateway responses continue exposing `imessageText`, while Hermes continues forwarding that text without channel-specific rewriting.

**Tech Stack:** Python 3.11+, `unittest`, existing Sign402 gateway and Hermes wallet plugin.

## Global Constraints

- Test approval: `✅ Approval confirmed. You're ready to approve payments.`
- Test denial: `Approval declined. No changes were made.`
- Purchase approval: `✅ Payment approved. Your purchase is being processed.`
- Purchase denial: `Payment declined. No funds were moved.`
- Withdrawal approval: `✅ Withdrawal approved. Your transfer is being processed.`
- Withdrawal denial: `Withdrawal declined. No funds were moved.`
- Unknown actions use neutral request copy.
- Unexpected statuses use `Sign402 approval <status>.`
- Do not change persistence, audit events, API fields, approval state, or payment behavior.

---

## File Structure

- Modify `sign402-gateway/sign402_gateway/imessage_approvals.py`: own the complete action/status-to-copy mapping.
- Modify `sign402-gateway/tests/test_imessage_approvals.py`: protect the public decision-copy contract with literal expected values.
- Modify `hermes-plugins/sign402-wallet/tests/test_plugin.py`: keep the fake gateway response aligned with real Bitrefill copy and prove Hermes forwards it unchanged.

### Task 1: Map Approval Decisions to User-Facing Copy

**Files:**
- Modify: `sign402-gateway/sign402_gateway/imessage_approvals.py:1886-1889`
- Test: `sign402-gateway/tests/test_imessage_approvals.py:10-20`
- Test: `sign402-gateway/tests/test_imessage_approvals.py:190-220`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py:3545-3610`

**Interfaces:**
- Consumes: `_decision_text(action_type: str, final_status: str)`.
- Produces: the exact `imessageText` forwarded by gateway decision APIs and Hermes channel adapters.

- [ ] **Step 1: Import the real decision formatter into the gateway test module**

Add `_decision_text` to the existing import from
`sign402_gateway.imessage_approvals`:

```python
    _decision_text,
```

- [ ] **Step 2: Write the failing action-aware copy test**

Add this test to `ImessageApprovalTests`:

```python
    def test_decision_text_uses_action_appropriate_copy(self):
        cases = (
            (
                "sign402_test",
                "approved",
                "✅ Approval confirmed. You're ready to approve payments.",
            ),
            (
                "sign402_test",
                "denied",
                "Approval declined. No changes were made.",
            ),
            (
                "sign402_purchase",
                "approved",
                "✅ Payment approved. Your purchase is being processed.",
            ),
            (
                "sign402_bitrefill",
                "approved",
                "✅ Payment approved. Your purchase is being processed.",
            ),
            (
                "sign402_bankr_llm",
                "denied",
                "Payment declined. No funds were moved.",
            ),
            (
                "sign402_withdrawal",
                "approved",
                "✅ Withdrawal approved. Your transfer is being processed.",
            ),
            (
                "sign402_withdrawal",
                "denied",
                "Withdrawal declined. No funds were moved.",
            ),
            (
                "sign402_external",
                "approved",
                "✅ Approval confirmed. Your request is being processed.",
            ),
            (
                "sign402_external",
                "denied",
                "Approval declined. No changes were made.",
            ),
            (
                "sign402_bitrefill",
                "pending",
                "Sign402 approval pending.",
            ),
        )

        for action_type, status, expected in cases:
            with self.subTest(action_type=action_type, status=status):
                self.assertEqual(_decision_text(action_type, status), expected)
```

The production regression this catches is collapsing real action types into
the `Sign402 test approval <status>.` fallback.

- [ ] **Step 3: Run the test and verify the expected RED state**

Run:

```bash
cd sign402-gateway
'/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' -m unittest \
  tests.test_imessage_approvals.ImessageApprovalTests.test_decision_text_uses_action_appropriate_copy \
  -v
```

Expected: FAIL because the current formatter emits technical fallback text and
the old purchase text.

- [ ] **Step 4: Implement the minimal copy mapping**

Replace `_decision_text()` with:

```python
def _decision_text(action_type: str, final_status: str) -> str:
    if final_status not in {"approved", "denied"}:
        return f"Sign402 approval {final_status}."
    if action_type == "sign402_test":
        if final_status == "approved":
            return "✅ Approval confirmed. You're ready to approve payments."
        return "Approval declined. No changes were made."
    if action_type in {
        "sign402_purchase",
        "sign402_bitrefill",
        "sign402_bankr_llm",
    }:
        if final_status == "approved":
            return "✅ Payment approved. Your purchase is being processed."
        return "Payment declined. No funds were moved."
    if action_type == "sign402_withdrawal":
        if final_status == "approved":
            return "✅ Withdrawal approved. Your transfer is being processed."
        return "Withdrawal declined. No funds were moved."
    if final_status == "approved":
        return "✅ Approval confirmed. Your request is being processed."
    return "Approval declined. No changes were made."
```

- [ ] **Step 5: Run the gateway test and verify the GREEN state**

Run:

```bash
cd sign402-gateway
'/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' -m unittest \
  tests.test_imessage_approvals.ImessageApprovalTests.test_decision_text_uses_action_appropriate_copy \
  tests.test_imessage_approvals.ImessageApprovalTests.test_test_approval_sends_canonical_message_and_accepts_yes_once \
  -v
```

Expected: both tests pass.

- [ ] **Step 6: Align Hermes forwarding fixtures with the Bitrefill response**

In the three `imessageText`/adapter expectations in
`test_photon_yes_with_pending_is_decided_and_consumed` and
`test_photon_yes_resolves_shared_user_id_before_decision`, replace:

```python
"Sign402 test approval approved."
```

with:

```python
"✅ Payment approved. Your purchase is being processed."
```

This keeps the forwarding test realistic without adding copy logic to Hermes.

- [ ] **Step 7: Run focused gateway and Hermes tests**

Run:

```bash
cd sign402-gateway
'/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' -m unittest \
  tests.test_imessage_approvals \
  -v
```

Then run from the repository root:

```bash
python3 -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests \
  -p 'test_plugin.py' \
  -v
```

Expected: both commands exit `0` with no failures or errors.

- [ ] **Step 8: Run complete gateway and Hermes plugin suites**

From `sign402-gateway`, run:

```bash
'/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' -m unittest discover -s tests
```

From the repository root, run:

```bash
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests
```

Expected: both suites exit `0` with no failures or errors.

- [ ] **Step 9: Inspect and commit the implementation**

Run:

```bash
git diff --check
git diff -- \
  sign402-gateway/sign402_gateway/imessage_approvals.py \
  sign402-gateway/tests/test_imessage_approvals.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
git add \
  sign402-gateway/sign402_gateway/imessage_approvals.py \
  sign402-gateway/tests/test_imessage_approvals.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "fix: clarify approval confirmation messages"
```

Expected: the diff contains one copy-mapping function, one gateway regression
test, and three aligned Hermes fixture strings.
