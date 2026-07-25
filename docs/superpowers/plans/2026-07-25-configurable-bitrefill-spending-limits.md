# Configurable Bitrefill Spending Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users select practical wallet limits up to 1,020 USDC per transaction and 5,000 USDC per UTC day, permit Bitrefill products up to $1,000 before the service fee, and show every applicable limit without the current hidden `$5` confusion.

**Architecture:** Keep the existing `UserSpendLimitStore`, operator ceilings, atomic daily-budget reservation, approval, and Bitrefill invoice checks authoritative. Add a non-secret Bitrefill limit snapshot to the spending-limits response, revise the Telegram text to distinguish personal limits from platform maximums, and configure production with the approved values. The separate Bitrefill live cap remains as a fail-closed disaster limit for both user and legacy/operator purchase paths.

**Tech Stack:** Python 3, standard-library `unittest`, Sign402 Gateway HTTP handlers, Hermes Sign402 wallet plugin, systemd on the production VPS.

## Global Constraints

- Follow the approved design in `docs/superpowers/specs/2026-07-25-configurable-bitrefill-spending-limits-design.md`.
- `SIGN402_BITREFILL_LIVE_MAX_USD=1000.00`.
- `SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX=1020000000` (1,020 USDC).
- `SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC=5000000000` (5,000 USDC).
- Personal and platform wallet limits apply to `totalUsd`, including the existing 2% service fee.
- The Bitrefill live cap applies to product price and invoice amount before the service fee.
- Preserve existing stored user limits; never raise them automatically.
- Keep explicit approval, funding, settlement, redemption, reservation, rate-limit, and invoice-overage behavior unchanged.
- Do not log or commit API keys, wallet secrets, tokens, recipient data, or redemption data.
- Do not call `buy-products`, a buy endpoint, withdrawal execution, or any other real fund-moving operation during verification.
- Preserve and do not stage unrelated untracked workspace files.

---

## File Map

- `sign402-gateway/sign402_gateway/server.py` — expose the active Bitrefill live cap in the spending-limit response and render unambiguous limit text.
- `sign402-gateway/tests/test_gateway_server.py` — test live/test cap reporting, platform ceilings, high-value boundaries, and exact user-facing text.
- `sign402-gateway/tests/test_bitrefill_mcp.py` — lock the exact $1,000 provider-side quote boundary.
- `hermes-plugins/sign402-wallet/client.py` — clarify that a Bitrefill live-cap rejection is separate from personal `/limits`.
- `hermes-plugins/sign402-wallet/tests/test_client.py` — test the revised error and full multiline limit-text pass-through.
- `hermes-plugins/sign402-wallet/tests/test_plugin.py` — prove `/limits 200 1000` reaches the gateway unchanged.
- `sign402-gateway/.env.example` — document the approved production limit profile without secrets.
- `sign402-gateway/.env.wallet-bitrefill.example` — keep the low local smoke-test cap and explain the production override.
- `docs/production-beta-checklist.md` — document amount semantics, safe rollout, non-purchase checks, and rollback.

---

### Task 1: Expose and explain every active limit

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`

**Interfaces:**
- Consumes: `SIGN402_BITREFILL_MODE`, `SIGN402_BITREFILL_LIVE_MAX_USD`, the existing `limit_settings()` result, and `format_decimal()`.
- Produces: `_bitrefill_live_limit_settings(env=None) -> dict[str, str | None]`; two new non-secret response fields named `bitrefillMode` and `bitrefillLiveMaxUsd`; revised `telegramText`.

- [ ] **Step 1: Write failing gateway tests for live and test-mode limit reporting**

Add focused tests to `GatewayServerTests` in `sign402-gateway/tests/test_gateway_server.py`:

```python
def test_agent_spending_limits_distinguishes_personal_platform_and_bitrefill_limits(self):
    server = DummyServer()
    server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UserSpendLimitStore(Path(tmpdir) / "limits.json")
        server.user_spend_limit_store = store
        store.set_limit_settings(
            "1045618308",
            max_per_tx_atomic=50_000_000,
            daily_cap_atomic=1_000_000_000,
            operator_max_per_tx_atomic=50_000_000,
            operator_daily_cap_atomic=500_000_000,
        )

        with patch.dict(
            os.environ,
            {
                "SIGN402_BITREFILL_MODE": "live",
                "SIGN402_BITREFILL_LIVE_MAX_USD": "1000.00",
                "SIGN402_USER_WALLET_MAX_ATOMIC_PER_TX": "50000000",
                "SIGN402_USER_WALLET_DAILY_ATOMIC_CAP": "500000000",
                "SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX": "1020000000",
                "SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC": "5000000000",
            },
        ):
            with patch("sys.stderr", io.StringIO()):
                handler = self.make_handler(
                    "/agent/spending-limits",
                    {"telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

    body = self.response_json(handler)
    self.assertEqual(body["limits"]["bitrefillMode"], "live")
    self.assertEqual(body["limits"]["bitrefillLiveMaxUsd"], "1000")
    self.assertEqual(
        body["telegramText"],
        "Current spending limits.\n\n"
        "Your spending limits:\n"
        "- Max per transaction: 50 USDC\n"
        "- Daily cap: 1000 USDC\n\n"
        "Platform maximums:\n"
        "- Max per transaction: 1020 USDC\n"
        "- Daily cap: 5000 USDC\n\n"
        "Bitrefill product maximum: 1000 USD before the 2% service fee.\n"
        "Service fees count toward your spending limits.\n"
        "The lowest applicable limit wins.\n\n"
        "To change: /limits <per-transaction> <daily>",
    )


def test_agent_spending_limits_marks_bitrefill_cap_inactive_outside_live_mode(self):
    server = DummyServer()
    server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
    with tempfile.TemporaryDirectory() as tmpdir:
        server.user_spend_limit_store = UserSpendLimitStore(
            Path(tmpdir) / "limits.json"
        )
        with patch.dict(
            os.environ,
            {
                "SIGN402_BITREFILL_MODE": "test",
                "SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX": "",
                "SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC": "",
            },
        ):
            with patch("sys.stderr", io.StringIO()):
                handler = self.make_handler(
                    "/agent/spending-limits",
                    {"telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

    body = self.response_json(handler)
    self.assertEqual(body["limits"]["bitrefillMode"], "test")
    self.assertIsNone(body["limits"]["bitrefillLiveMaxUsd"])
    self.assertIn("Platform maximums:", body["telegramText"])
    self.assertIn("- Max per transaction: unlimited", body["telegramText"])
    self.assertIn("- Daily cap: unlimited", body["telegramText"])
    self.assertIn("Bitrefill product maximum: inactive", body["telegramText"])
    self.assertNotIn("Default safety limits:", body["telegramText"])
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_agent_spending_limits_distinguishes_personal_platform_and_bitrefill_limits \
  tests.test_gateway_server.GatewayServerTests.test_agent_spending_limits_marks_bitrefill_cap_inactive_outside_live_mode \
  -v
```

Expected: both tests fail because `bitrefillMode`/`bitrefillLiveMaxUsd` and the revised sections do not exist yet.

- [ ] **Step 3: Add the minimal shared configuration snapshot**

In `sign402-gateway/sign402_gateway/server.py`, import `Mapping`, define the shared default, and add this helper:

```python
from typing import Any, Callable, Mapping

DEFAULT_BITREFILL_LIVE_MAX_USD = "5.00"


def _bitrefill_live_limit_settings(
    env: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    values = os.environ if env is None else env
    mode = str(values.get("SIGN402_BITREFILL_MODE", "test")).strip().lower()
    if mode != "live":
        return {
            "bitrefillMode": mode,
            "bitrefillLiveMaxUsd": None,
        }
    value = Decimal(
        str(
            values.get(
                "SIGN402_BITREFILL_LIVE_MAX_USD",
                DEFAULT_BITREFILL_LIVE_MAX_USD,
            )
        )
    )
    if not value.is_finite() or value <= 0:
        raise ValueError("SIGN402_BITREFILL_LIVE_MAX_USD must be positive")
    return {
        "bitrefillMode": mode,
        "bitrefillLiveMaxUsd": format_decimal(value),
    }
```

Replace the duplicate factory fallback:

```python
max_purchase_usd=values.get(
    "SIGN402_BITREFILL_LIVE_MAX_USD",
    DEFAULT_BITREFILL_LIVE_MAX_USD,
),
```

In `_handle_agent_spending_limits()`, merge the snapshot after reading or
updating the store and before building the response:

```python
limits = {
    **limits,
    **_bitrefill_live_limit_settings(),
}
```

- [ ] **Step 4: Render the agreed text from response fields**

Replace `_spending_limits_telegram_text()` with:

```python
def _spending_limits_telegram_text(
    limits: dict[str, Any],
    *,
    updated: bool,
) -> str:
    heading = "Spending limits updated." if updated else "Current spending limits."
    max_per_tx = str(limits.get("maxPerTxUsdc") or "unlimited")
    daily_cap = str(limits.get("dailyCapUsdc") or "unlimited")
    platform_max = _format_usdc_atomic(limits.get("operatorCeilingPerTxAtomic"))
    platform_daily = _format_usdc_atomic(limits.get("operatorCeilingDailyAtomic"))
    bitrefill_max = limits.get("bitrefillLiveMaxUsd")
    bitrefill_line = (
        f"Bitrefill product maximum: {bitrefill_max} USD before the 2% service fee."
        if bitrefill_max is not None
        else "Bitrefill product maximum: inactive."
    )
    return (
        f"{heading}\n\n"
        "Your spending limits:\n"
        f"- Max per transaction: {max_per_tx} USDC\n"
        f"- Daily cap: {daily_cap} USDC\n\n"
        "Platform maximums:\n"
        f"- Max per transaction: {platform_max} USDC\n"
        f"- Daily cap: {platform_daily} USDC\n\n"
        f"{bitrefill_line}\n"
        "Service fees count toward your spending limits.\n"
        "The lowest applicable limit wins.\n\n"
        "To change: /limits <per-transaction> <daily>"
    )
```

- [ ] **Step 5: Run targeted and full gateway tests and verify GREEN**

Run:

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_agent_spending_limits_distinguishes_personal_platform_and_bitrefill_limits \
  tests.test_gateway_server.GatewayServerTests.test_agent_spending_limits_marks_bitrefill_cap_inactive_outside_live_mode \
  -v

env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover \
  -s sign402-gateway/tests -p 'test_gateway_server.py' -q
```

Expected: targeted tests pass; the complete gateway-server test module passes with zero failures.

- [ ] **Step 6: Commit Task 1**

```bash
git add sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/tests/test_gateway_server.py
git commit -m "feat: expose effective Bitrefill spending limits"
```

---

### Task 2: Lock the approved high-value boundaries and Hermes behavior

**Files:**
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/tests/test_bitrefill_mcp.py`
- Modify: `hermes-plugins/sign402-wallet/client.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_client.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: `/agent/spending-limits`, gateway `telegramText`, `_BITREFILL_LIVE_MAX_RE`, and `/limits` command arguments.
- Produces: exact regression coverage for 1,020/5,000, exact `"200"`/`"1000"` transport, and a Bitrefill-cap error that identifies the separate control.

- [ ] **Step 1: Write the failing Hermes error-copy test**

Change `test_execute_bitrefill_purchase_translates_live_max_errors` in
`hermes-plugins/sign402-wallet/tests/test_client.py` to expect:

```python
self.assertEqual(
    raised.exception.user_message,
    "This product exceeds the Bitrefill product maximum ($5.00), "
    "which is separate from your wallet limits. Choose a smaller "
    "product or ask the operator to raise the Bitrefill limit.",
)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd hermes-plugins/sign402-wallet
env PYTHONPATH=.:../.. \
  ../../payment-executor/.venv/bin/python -m unittest \
  tests.test_client.GatewayClientTests.test_execute_bitrefill_purchase_translates_live_max_errors \
  -v
```

Expected: FAIL because the current message says only that the amount is above the live purchase limit.

- [ ] **Step 3: Implement the minimal error clarification**

In `hermes-plugins/sign402-wallet/client.py`, keep the existing regex and
replace only its translated message:

```python
return GatewayClientError(
    f"This product exceeds the Bitrefill product maximum (${match.group(1)}), "
    "which is separate from your wallet limits. Choose a smaller product "
    "or ask the operator to raise the Bitrefill limit."
)
```

- [ ] **Step 4: Add boundary and pass-through regressions**

In `sign402-gateway/tests/test_gateway_server.py`:

- change `test_agent_spending_limits_allows_user_limits_above_default_caps` to
  submit `maxPerTxUsdc="200"` and `dailyCapUsdc="1000"` and assert
  `200_000_000`/`1_000_000_000`;
- add `test_agent_spending_limits_accepts_values_equal_to_production_ceilings`
  with ceilings `1020000000`/`5000000000`, submit `1020`/`5000`, and assert a
  `200` response with those exact atomics;
- change the existing above-ceiling test to the same production ceilings,
  submit `1020.000001`/`5000`, and assert the exact `1020 USDC` ceiling appears
  in the error;
- add the daily counterpart with `1020`/`5000.000001` and assert the exact
  `5000 USDC` ceiling appears.

Add this exact provider-boundary regression to
`BitrefillMcpCatalogTests` in `sign402-gateway/tests/test_bitrefill_mcp.py`:

```python
def test_quote_accepts_price_equal_to_live_cap_and_rejects_price_above_it(self):
    product = {
        "product_id": "large-gift-card-us",
        "name": "Large Gift Card",
        "country": "US",
        "currency": "USD",
        "recipient_type": "none",
        "packages": [
            {
                "package_id": "large-gift-card-us<&>1000",
                "package_value": "1000",
                "price": "1000.00",
            },
            {
                "package_id": "large-gift-card-us<&>1000.01",
                "package_value": "1000.01",
                "price": "1000.01",
            },
        ],
    }
    client = McpBitrefillClient(
        api_key="key_123",
        max_purchase_usd="1000.00",
        call_tool=FakeMcpCaller([product, product]),
    )

    quote = client.quote_product(
        product_id="large-gift-card-us",
        package_id="large-gift-card-us<&>1000",
        country="US",
        recipient={},
    )
    self.assertEqual(quote["priceUsd"], "1000.00")

    with self.assertRaisesRegex(
        ValueError,
        r"exceeds live Bitrefill max \$1000\.00",
    ):
        client.quote_product(
            product_id="large-gift-card-us",
            package_id="large-gift-card-us<&>1000.01",
            country="US",
            recipient={},
        )
```

Add a gateway regression named
`test_quote_bitrefill_accepts_1020_total_at_personal_limit` using an
authenticated `DummyServer`, a stored personal limit of
`1_020_000_000`/`5_000_000_000`:

```python
def test_quote_bitrefill_accepts_1020_total_at_personal_limit(self):
    server = DummyServer()
    server.user_wallet_api_token = "wallet-token-secret-value"
    server.user_wallet_service.resolve_telegram_user_id = Mock(
        return_value="1045618308"
    )
    server.bitrefill_quote_service = Mock(
        return_value={
            "ok": True,
            "priceUsd": "1000.00",
            "serviceFeeUsd": "20.00",
            "totalUsd": "1020.00",
            "quoteId": "q-large",
        }
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UserSpendLimitStore(Path(tmpdir) / "limits.json")
        server.user_spend_limit_store = store
        store.set_limit_settings(
            "1045618308",
            max_per_tx_atomic=1_020_000_000,
            daily_cap_atomic=5_000_000_000,
            operator_max_per_tx_atomic=10_000,
            operator_daily_cap_atomic=100_000,
            operator_ceiling_per_tx_atomic=1_020_000_000,
            operator_ceiling_daily_atomic=5_000_000_000,
        )
        with patch.dict(
            os.environ,
            {
                "SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX": "1020000000",
                "SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC": "5000000000",
            },
        ):
            with patch("sys.stderr", io.StringIO()):
                handler = self.make_handler(
                    "/agent/quote-bitrefill",
                    {
                        "productId": "large-gift-card-us",
                        "packageId": "1000",
                        "telegramUserId": "1045618308",
                    },
                    headers={
                        "Authorization": "Bearer wallet-token-secret-value",
                        "X-Sign402-User-Token": "user-token-1",
                    },
                    server=server,
                )

    response = self.response_text(handler)
    self.assertIn("HTTP/1.0 200 OK", response)
    self.assertIn('"totalUsd": "1020.00"', response)
```

Assert HTTP `200`. Keep the existing `test_quote_bitrefill_rejects_amount_over_spend_cap`
as the proof that the same path rejects `totalUsd` above the personal limit.
The existing stored-limit clamp and spend-reservation concurrency tests remain
the regression coverage for lowered ceilings and parallel daily-budget use.

In `hermes-plugins/sign402-wallet/tests/test_client.py`, make
`test_execute_spending_limits_shows_current_limits` return the complete
multiline message from Task 1 and assert exact pass-through. Change
`test_execute_spending_limits_posts_requested_limits` to `"200"`/`"1000"` and
assert those exact strings in the JSON body.

In `hermes-plugins/sign402-wallet/tests/test_plugin.py`, change
`test_limits_with_two_numbers_updates_limits` to:

```python
event=FakeEvent(
    "/limits 200 1000",
    "1045618308",
    platform="telegram",
    chat_id="telegram-chat",
)
```

and assert:

```python
client.limits_calls == [
    ("1045618308", None, "200", "1000", "user-access-token")
]
```

These boundary/pass-through assertions document already-supported numeric
behavior; they require no additional production logic.

- [ ] **Step 5: Run focused and component test suites**

Run from the repository root:

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover \
  -s sign402-gateway/tests -p 'test_gateway_server.py' -q

env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover \
  -s sign402-gateway/tests -p 'test_bitrefill_mcp.py' -q

env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests -q
```

Expected: both suites pass with zero failures.

- [ ] **Step 6: Commit Task 2**

```bash
git add sign402-gateway/tests/test_gateway_server.py \
  sign402-gateway/tests/test_bitrefill_mcp.py \
  hermes-plugins/sign402-wallet/client.py \
  hermes-plugins/sign402-wallet/tests/test_client.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "fix: clarify configurable purchase limits"
```

---

### Task 3: Document the approved production profile

**Files:**
- Modify: `sign402-gateway/.env.example`
- Modify: `sign402-gateway/.env.wallet-bitrefill.example`
- Modify: `docs/production-beta-checklist.md`

**Interfaces:**
- Consumes: the exact approved values and the existing production environment-file workflow.
- Produces: a secret-free, operator-readable configuration and rollback procedure.

- [ ] **Step 1: Update the main environment example**

In `sign402-gateway/.env.example`, keep the low starting defaults and replace
the ceiling example with:

```env
# Production ceilings permit a $1,000 Bitrefill product plus the 2% fee.
# Users may choose lower personal limits through /limits.
SIGN402_USER_WALLET_MAX_ATOMIC_PER_TX=10000
SIGN402_USER_WALLET_DAILY_ATOMIC_CAP=100000
SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX=1020000000
SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC=5000000000

# Bitrefill product/invoice cap before the 2% Sign402 service fee.
SIGN402_BITREFILL_LIVE_MAX_USD=1000.00
```

- [ ] **Step 2: Preserve the low local smoke-test profile**

In `sign402-gateway/.env.wallet-bitrefill.example`, retain:

```env
SIGN402_BITREFILL_LIVE_MAX_USD=1.00
```

and change its comment to state that it is intentionally low for local smoke
tests and that production uses the separately reviewed value in
`/etc/sign402-gateway.env`.

- [ ] **Step 3: Add the production runbook**

In `docs/production-beta-checklist.md`, add a “Configurable spending limits”
section containing:

```env
SIGN402_BITREFILL_LIVE_MAX_USD=1000.00
SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX=1020000000
SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC=5000000000
```

State explicitly:

- `/limits 50 1000` means 50 USDC total per transaction and 1,000 USDC total
  per UTC day;
- wallet limits include the 2% fee;
- a $1,000 product therefore needs a 1,020 USDC personal transaction limit;
- the Bitrefill cap is checked before the fee;
- the lowest applicable limit wins;
- production validation keeps `SIGN402_PURCHASES_PAUSED=1` and never calls a
  buy or fulfillment endpoint;
- rollback restores the pre-change environment backup and restarts the service.

- [ ] **Step 4: Validate documentation without exposing secrets**

Run:

```bash
rg -n \
  'SIGN402_BITREFILL_LIVE_MAX_USD=1000\.00|SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX=1020000000|SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC=5000000000|2%|lowest applicable limit' \
  sign402-gateway/.env.example \
  sign402-gateway/.env.wallet-bitrefill.example \
  docs/production-beta-checklist.md

git diff --check
```

Expected: all three approved settings and amount semantics are present; no whitespace errors.

- [ ] **Step 5: Commit Task 3**

```bash
git add sign402-gateway/.env.example \
  sign402-gateway/.env.wallet-bitrefill.example \
  docs/production-beta-checklist.md
git commit -m "docs: configure production spending ceilings"
```

---

### Task 4: Full verification, push, and safe production rollout

**Files:**
- Verify: all files changed in Tasks 1–3
- Configure on VPS: `/etc/sign402-gateway.env`
- Service: `sign402-gateway.service`

**Interfaces:**
- Consumes: committed code, production environment file, systemd, loopback health endpoint.
- Produces: a pushed commit and a healthy production gateway running the approved limits without any real purchase probe.

- [ ] **Step 1: Run the complete offline regression gate**

Run from the repository root:

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover -s sign402-bridge/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover -s payment-executor/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover -s demo-resource-server/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover -s live-demo/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover -s hermes-plugins/sign402-wallet/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest discover -s scripts/tests -q
npm --prefix cdp-x402-service test
npm --prefix singit-risk-check test
```

Expected: every Python and Node suite passes with zero failures or errors.

- [ ] **Step 2: Inspect the exact commit range**

Run:

```bash
git status --short --branch
git diff --check HEAD~3..HEAD
git diff --stat HEAD~3..HEAD
git log -4 --oneline --decorate
```

Expected: only the planned files are committed; unrelated untracked files remain unmodified and unstaged.

- [ ] **Step 3: Push the current branch**

Run:

```bash
git push singitai x402Bnkr
```

Expected: remote `singitai/x402Bnkr` advances to the verified local `HEAD`.

- [ ] **Step 4: Pull and test the exact commit on the VPS**

Run on the VPS:

```bash
cd /home/hermes/apps/sign402
git fetch singitai
git merge --ff-only singitai/x402Bnkr
git rev-parse HEAD

env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. \
  payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_mcp tests.test_gateway_server -q
```

Expected: the VPS reports the same commit as local `HEAD`; gateway/Bitrefill tests pass and use only fake/injected provider responses.

- [ ] **Step 5: Back up and edit the root-owned environment**

Run on the VPS:

```bash
sudo test ! -e /etc/sign402-gateway.env.pre-configurable-limits
sudo cp -a /etc/sign402-gateway.env \
  /etc/sign402-gateway.env.pre-configurable-limits
sudoedit /etc/sign402-gateway.env
```

Ensure each key appears exactly once:

```env
SIGN402_BITREFILL_LIVE_MAX_USD=1000.00
SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX=1020000000
SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC=5000000000
SIGN402_PURCHASES_PAUSED=1
```

Do not print the rest of the environment file.

- [ ] **Step 6: Validate only selected configuration values**

Run:

```bash
sudo chmod 600 /etc/sign402-gateway.env
sudo bash -lc '
for expected in \
  "SIGN402_BITREFILL_LIVE_MAX_USD=1000.00" \
  "SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX=1020000000" \
  "SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC=5000000000" \
  "SIGN402_PURCHASES_PAUSED=1"
do
  count=$(grep -Fxc "$expected" /etc/sign402-gateway.env)
  test "$count" -eq 1 || {
    echo "configuration mismatch: ${expected%%=*}"
    exit 1
  }
done
echo "selected configuration values are valid"
'
```

Expected: only the success line is printed; no secret value is displayed.

- [ ] **Step 7: Restart while purchases are paused and verify stability**

Run:

```bash
sudo systemctl restart sign402-gateway
systemctl is-active sign402-gateway
pid_before=$(systemctl show sign402-gateway -p MainPID --value)
systemctl show sign402-gateway \
  -p ActiveState -p SubState -p MainPID -p NRestarts
curl -fsS http://127.0.0.1:8099/health | \
  python3 -c 'import json,sys; body=json.load(sys.stdin); assert body.get("ok") is True; print("gateway health ok")'
sleep 5
pid_after=$(systemctl show sign402-gateway -p MainPID --value)
test "$pid_before" -gt 0
test "$pid_before" = "$pid_after"
systemctl show sign402-gateway \
  -p ActiveState -p SubState -p MainPID -p NRestarts
curl -fsS http://127.0.0.1:8099/health | \
  python3 -c 'import json,sys; body=json.load(sys.stdin); assert body.get("ok") is True; print("gateway health stable")'
```

Expected: `active`, a stable non-zero PID, no restart loop, and two successful
health checks.

- [ ] **Step 8: Verify runtime limits without a purchase**

Run `/limits` for a trusted test user and confirm the response shows:

```text
Platform maximums:
- Max per transaction: 1020 USDC
- Daily cap: 5000 USDC

Bitrefill product maximum: 1000 USD before the 2% service fee.
```

Then run:

```text
/limits 1020 5000
/limits 1020.000001 5000
/limits 1020 5000.000001
```

Expected: the first update succeeds and both one-micro-USDC excesses fail.
Record and restore the test user's previous personal limits after this check.
Do not invoke any Bitrefill buy or fulfillment command.

- [ ] **Step 9: Unpause purchases and perform final health checks**

Use `sudoedit /etc/sign402-gateway.env` to set:

```env
SIGN402_PURCHASES_PAUSED=
```

Restart and re-run the stability and health checks from Step 7. Confirm the
runtime process contains the three approved limit keys without printing other
environment variables:

```bash
sudo systemctl restart sign402-gateway
systemctl is-active sign402-gateway
curl -fsS http://127.0.0.1:8099/health | \
  python3 -c 'import json,sys; body=json.load(sys.stdin); assert body.get("ok") is True; print("gateway health ok")'
sudo bash -lc '
pid=$(systemctl show sign402-gateway -p MainPID --value)
test "$pid" -gt 0
for expected in \
  "SIGN402_BITREFILL_LIVE_MAX_USD=1000.00" \
  "SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX=1020000000" \
  "SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC=5000000000"
do
  grep -Fzxq "$expected" "/proc/$pid/environ" || {
    echo "runtime value mismatch: ${expected%%=*}"
    exit 1
  }
done
echo "runtime limits loaded"
'
```

- [ ] **Step 10: Keep the rollback command ready**

If the service, health check, or limit display fails:

```bash
sudo cp -a /etc/sign402-gateway.env.pre-configurable-limits \
  /etc/sign402-gateway.env
sudo systemctl restart sign402-gateway
systemctl is-active sign402-gateway
curl -fsS http://127.0.0.1:8099/health
```

Expected: the prior environment is restored and the gateway returns healthy.
