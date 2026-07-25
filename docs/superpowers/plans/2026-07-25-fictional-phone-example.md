# Fictional Phone Example Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the reported personal phone number from the current repository tree and use `+12025550123` for public E.164 examples and related fixtures.

**Architecture:** Keep the existing prompt, validation, registration, and pairing interfaces unchanged. Strengthen tests around the two user-facing examples, replace only the reported-number literal in fixtures, and verify its absence without recording it in the new plan.

**Tech Stack:** Python 3.14, `unittest`, Hermes Sign402 wallet plugin, Sign402 operator CLI.

## Global Constraints

- Use `+12025550123` as the fictional United States E.164 example.
- Do not change phone validation, Photon registration, pairing, or approval behavior.
- Remove the reported personal number from every file in the current Git tree.
- Do not rewrite Git history or force-push a shared branch.
- Do not change configured production numbers or secrets.

---

### Task 1: Replace Personal Phone Examples and Fixtures

**Files:**
- Modify: `hermes-plugins/sign402-wallet/tests/test_plugin.py`
- Modify: `scripts/tests/test_sign402_operator.py`
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Modify: `scripts/sign402-operator.py`

**Interfaces:**
- Consumes: `_imessage_phone_prompt(channel: str = "imessage") -> str` and `normalize_e164(value: str) -> str`.
- Produces: unchanged interfaces whose error/prompt copy uses `+12025550123`; test fixtures no longer contain the reported personal number.

- [ ] **Step 1: Write the failing regression tests**

In `test_connect_imessage_auto_register_prompts_for_phone`, replace the broad
country-code assertion with:

```python
self.assertIn("Example: +12025550123", text)
self.assertNotIn("Example: +420", text)
```

Add this method to `Sign402OperatorTests`:

```python
def test_normalize_e164_uses_fictional_us_example(self):
    operator = load_operator()

    with self.assertRaisesRegex(ValueError, r"\+12025550123"):
        operator.normalize_e164("not-a-phone")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  '/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover -s hermes-plugins/sign402-wallet/tests \
  -p test_plugin.py -k test_connect_imessage_auto_register_prompts_for_phone -v

env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  '/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover -s scripts/tests -p test_sign402_operator.py \
  -k test_normalize_e164_uses_fictional_us_example -v
```

Expected: both tests fail because the current prompt and validation error still
contain the old example.

- [ ] **Step 3: Implement the minimal copy and fixture changes**

Change `_imessage_phone_prompt` to emit:

```python
"Example: +12025550123"
```

Change the `normalize_e164` validation error to:

```python
raise ValueError("phone must be E.164, for example +12025550123")
```

Replace every occurrence of the reported personal-number literal in
`hermes-plugins/sign402-wallet/tests/test_plugin.py` with
`+12025550123`. Do not alter unrelated configured or synthetic test numbers.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the two commands from Step 2 again.

Expected: both focused tests pass.

- [ ] **Step 5: Run full regression and privacy verification**

Run:

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  '/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover -s hermes-plugins/sign402-wallet/tests -q

env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  '/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover -s scripts/tests -q

reported_number="$(
  git show 96fe44b:hermes-plugins/sign402-wallet/__init__.py |
  sed -n 's/.*Example: \(\+[0-9][0-9]*\).*/\1/p'
)"
test -n "$reported_number"
test -z "$(git grep -l -F "$reported_number" -- .)"
git diff --check
```

Expected: all plugin and operator tests pass, the privacy scan returns no file,
and `git diff --check` reports no errors.

- [ ] **Step 6: Commit the privacy patch**

```bash
git add \
  hermes-plugins/sign402-wallet/__init__.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py \
  scripts/sign402-operator.py \
  scripts/tests/test_sign402_operator.py
git commit -m "fix: replace personal phone example"
```
