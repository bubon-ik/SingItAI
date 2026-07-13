# WhatsApp Template Parameter Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SingIt WhatsApp approval templates acceptable to Meta and preserve the real delivery-failure message in Telegram.

**Architecture:** Keep approval context structured as a list until it reaches the channel notifier. Serialize only the WhatsApp template body parameter as a single line separated by ` | `; leave iMessage formatting unchanged. When the wallet purchase is rejected before funding, promote the approval service's safe `telegramText` to the top-level response consumed by the Hermes plugin.

**Tech Stack:** Python 3, `unittest`, WhatsApp Cloud API template payloads, Hermes Sign402 wallet plugin.

## Global Constraints

- Do not change template name, language, variable count, buttons, approval IDs, payment flow, or order-state transitions.
- The value bound to WhatsApp body variable `{{1}}` must contain no newline or tab characters.
- iMessage rendering must remain unchanged.
- Wallet funding must remain blocked when approval delivery fails.

---

### Task 1: Normalize the WhatsApp template context parameter

**Files:**
- Modify: `sign402-gateway/tests/test_whatsapp_cloud.py`
- Modify: `sign402-gateway/sign402_gateway/whatsapp_cloud.py`

**Interfaces:**
- Consumes: `MetaWhatsAppTemplateNotifier.send_approval(..., context_lines: list[str], ...)`
- Produces: `_safe_context(context_lines: list[str]) -> str` containing a single Meta-compatible line.

- [ ] **Step 1: Write the failing regression test**

Update the existing payload assertion and add explicit character guards:

```python
context = payload["template"]["components"][0]["parameters"][0]["text"]
self.assertEqual(context, "Merchant: Bitrefill | Amount: 10 USDC")
self.assertNotIn("\n", context)
self.assertNotIn("\t", context)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. python3 -m unittest tests.test_whatsapp_cloud.MetaWhatsAppTemplateNotifierTests.test_send_approval_posts_template_with_bound_quick_reply_payloads -q
```

Expected: FAIL because the current value is `Merchant: Bitrefill\nAmount: 10 USDC`.

- [ ] **Step 3: Implement the minimal serialization change**

In `_safe_context`, replace the channel-specific newline join with a single-line delimiter:

```python
return " | ".join(lines)[:960]
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the same command. Expected: `Ran 1 test` and `OK`.

### Task 2: Preserve structured approval failures for Telegram

**Files:**
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`

**Interfaces:**
- Consumes: `approval_client(...) -> dict[str, Any]` with `approved=False` and optional `telegramText`.
- Produces: wallet Bitrefill result with top-level `telegramText` and no call to `user_funding_runner`.

- [ ] **Step 1: Write the failing rejection-message test**

Extend the rejected-wallet purchase test:

```python
approval = Mock(
    return_value={
        "approved": False,
        "telegramText": "could not deliver the approval. No action was approved.",
    }
)
result = runner.buy({"quoteId": "quote_wallet_1"})
self.assertEqual(
    result["telegramText"],
    "could not deliver the approval. No action was approved.",
)
fulfillment.assert_not_called()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. python3 -m unittest tests.test_bitrefill_runner.BitrefillRunnerTests.test_wallet_runner_rejects_unconfirmed_checkout_before_fulfillment -q
```

Expected: FAIL with missing key `telegramText`.

- [ ] **Step 3: Promote only the safe approval response text**

Build the rejection response as today, then add a top-level value only when it is a non-empty string:

```python
telegram_text = approval.get("telegramText")
if isinstance(telegram_text, str) and telegram_text.strip():
    result["telegramText"] = telegram_text.strip()
return result
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the same command. Expected: `Ran 1 test` and `OK`.

### Task 3: Full verification and integration

**Files:**
- Verify: `sign402-gateway/`
- Verify: `hermes-plugins/sign402-wallet/`
- Verify: `cdp-x402-service/`

**Interfaces:**
- Consumes: both completed fixes.
- Produces: a tested commit ready for `x402Bnkr`.

- [ ] **Step 1: Run formatting and diff checks**

```bash
git diff --check
```

Expected: no output and exit code `0`.

- [ ] **Step 2: Run all gateway tests**

```bash
cd sign402-gateway
PYTHONPATH=. python3 -m unittest discover -s tests -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Run all Hermes plugin tests in a clean environment**

```bash
cd hermes-plugins/sign402-wallet
env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=. python3 -m unittest discover -s tests -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 4: Run CDP Node tests**

```bash
cd cdp-x402-service
npm test
```

Expected: 13 tests pass with zero failures.

- [ ] **Step 5: Commit the implementation**

```bash
git add sign402-gateway/sign402_gateway/whatsapp_cloud.py \
  sign402-gateway/tests/test_whatsapp_cloud.py \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "Fix WhatsApp approval template delivery"
```

- [ ] **Step 6: Fast-forward `x402Bnkr`, verify again, and push**

Merge the isolated branch with `git merge --ff-only`, repeat all three test suites in the main checkout, then run:

```bash
git push singitai x402Bnkr
```
