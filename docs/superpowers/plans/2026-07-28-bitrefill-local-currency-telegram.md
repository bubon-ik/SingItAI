# Bitrefill Local-Currency Telegram Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each Bitrefill product denomination in its catalog currency in both Telegram completion-message paths.

**Architecture:** Keep currency data in the existing persisted quote and add one private formatter in `bitrefill_runner.py`. Both the immediate purchase message and `/last_purchase` delivery message will call that formatter, eliminating their duplicated USD assumption without changing settlement-price or payment-token formatting.

**Tech Stack:** Python 3.11+, `unittest`, existing `sign402_gateway` quote and Telegram message helpers.

## Global Constraints

- USD denominations retain the existing `$100` format.
- Every non-USD denomination uses `<packageValue> <UPPERCASE_CURRENCY>`.
- Missing currency retains the legacy `$100` fallback for old persisted quotes.
- `priceUsd`, service-fee output, payment-token output, and `Spent: …` remain unchanged.
- Do not add a currency-symbol or locale-formatting dependency.

---

## File Structure

- Modify `sign402-gateway/sign402_gateway/bitrefill_runner.py`: own the shared denomination formatter and use it in both Telegram completion-message helpers.
- Modify `sign402-gateway/tests/test_bitrefill_runner.py`: exercise both real message helpers with non-USD quotes while retaining existing USD and missing-currency coverage.

### Task 1: Share Currency-Aware Denomination Formatting Across Telegram Messages

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py:1555-1648`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py:1-30`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py:229`

**Interfaces:**
- Consumes: quote mappings containing optional `packageValue` and `currency` values.
- Produces: `_bitrefill_denomination_text(quote: dict[str, Any]) -> str`, used by `_bitrefill_purchase_telegram_text` and `_bitrefill_delivery_telegram_text`.

- [ ] **Step 1: Import the real Telegram message helpers into the test module**

Add these names to the existing import from
`sign402_gateway.bitrefill_runner`:

```python
    _bitrefill_delivery_telegram_text,
    _bitrefill_purchase_telegram_text,
```

- [ ] **Step 2: Write failing regression tests for two non-USD currencies**

Add these tests to `BitrefillRunnerTests`:

```python
    def test_purchase_telegram_text_uses_product_currency_for_local_denomination(self):
        text = _bitrefill_purchase_telegram_text(
            {
                "productName": "Wolt Czech Republic",
                "packageValue": "500",
                "currency": "CZK",
                "paymentTokenSymbol": "SINGIT",
            }
        )

        self.assertIn("✅ Wolt Czech Republic 500 CZK is ready.", text)
        self.assertNotIn("$500", text)

    def test_delivery_telegram_text_uses_product_currency_for_local_denomination(self):
        text = _bitrefill_delivery_telegram_text(
            {
                "productName": "Example Euro Gift Card",
                "packageValue": "25",
                "currency": " eur ",
            },
            redemption={"value": {"code": "SECRET-CODE"}},
        )

        self.assertIn("✅ Example Euro Gift Card 25 EUR is ready.", text)
        self.assertNotIn("$25", text)
```

The production change that makes these tests pass is replacing the unconditional
`$` prefix in both real message paths with formatting derived from the quote's
currency.

- [ ] **Step 3: Run the two tests and verify the expected RED state**

Run:

```bash
cd sign402-gateway
python3 -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_purchase_telegram_text_uses_product_currency_for_local_denomination \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_delivery_telegram_text_uses_product_currency_for_local_denomination \
  -v
```

Expected: both tests fail because the messages contain `$500` and `$25`
instead of `500 CZK` and `25 EUR`.

- [ ] **Step 4: Add the minimal shared formatter and use it in both paths**

Add this helper immediately before `_bitrefill_purchase_telegram_text`:

```python
def _bitrefill_denomination_text(quote: dict[str, Any]) -> str:
    package_value = str(quote.get("packageValue") or "").strip()
    if not package_value:
        return ""
    currency = str(quote.get("currency") or "").strip().upper()
    if not currency or currency == "USD":
        return f" ${package_value}"
    return f" {package_value} {currency}"
```

In both `_bitrefill_purchase_telegram_text` and
`_bitrefill_delivery_telegram_text`, replace:

```python
    package_value = str(quote.get("packageValue") or "").strip()
    value_text = f" ${package_value}" if package_value else ""
```

with:

```python
    value_text = _bitrefill_denomination_text(quote)
```

- [ ] **Step 5: Run focused tests and verify the GREEN state**

Run:

```bash
cd sign402-gateway
python3 -m unittest \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_purchase_telegram_text_uses_product_currency_for_local_denomination \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_delivery_telegram_text_uses_product_currency_for_local_denomination \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_runner_calls_bankr_after_firefly_approval \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_order_lookup_can_reveal_redemption_when_recipient_matches \
  -v
```

Expected: all four tests pass. The first two prove generic non-USD formatting;
the existing purchase test proves USD remains `$1`; the existing reveal test
proves a legacy quote without `currency` remains `$25`.

- [ ] **Step 6: Run the complete gateway test suite**

Run:

```bash
cd sign402-gateway
python3 -m unittest discover -s tests
```

Expected: exit code `0` with no failures or errors.

- [ ] **Step 7: Inspect the diff and commit the implementation**

Run:

```bash
git diff --check
git diff -- sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_bitrefill_runner.py
git add sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "fix: show Bitrefill denominations in catalog currency"
```

Expected: the diff contains only the shared formatter, its two call sites, and
the two regression tests.
