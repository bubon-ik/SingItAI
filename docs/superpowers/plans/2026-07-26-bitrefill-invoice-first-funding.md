# Bitrefill Invoice-First Funding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create and validate an unpaid Bitrefill invoice before any user-wallet transfer or CDP swap, then pay that exact invoice at most once.

**Architecture:** Split the Bitrefill client into `prepare_purchase` and `complete_purchase`. The wallet runner prepares and persists a safe invoice snapshot before user funding; the fulfillment runner then swaps if needed and completes the already-prepared invoice. CDP pays Base USDC through the existing receipt-confirmed ERC-20 transfer primitive with a stable invoice-derived idempotency key.

**Tech Stack:** Python 3.11, `unittest`, SQLite, MCP client, Node.js 20+, Coinbase CDP SDK, `viem`, systemd.

## Global Constraints

- Preserve the existing 1% service fee.
- Preserve the existing 5% maximum payment-token buffer.
- No invoice ID means no user-wallet transfer and no CDP swap.
- The invoice product, denomination, payment method, Base network, USDC asset, and amount must match the approved quote.
- The invoice amount must not exceed the approved USDC total.
- Provider preparation failure is `FULFILLMENT_FAILED`, with no funds moved.
- Any ambiguous failure after an on-chain action is `RECONCILIATION_REQUIRED`.
- A stored invoice payment transaction or a provider payment-detected state must never cause a second broadcast.
- Never log or persist payment addresses, payment links, raw provider bodies, redemption values, eSIM/QR activation data, API keys, wallet secrets, or private keys.
- Production verification must not create or pay a live Bitrefill invoice.

---

## File Map

- `sign402-gateway/sign402_gateway/diagnostics.py`: provider-safe diagnostic parsing and bearer-value filtering.
- `sign402-gateway/sign402_gateway/bitrefill.py`: two-phase Bitrefill client protocol and deterministic test client.
- `sign402-gateway/sign402_gateway/bitrefill_mcp.py`: invoice preparation, invoice validation, idempotent completion, and polling.
- `sign402-gateway/sign402_gateway/commerce_store.py`: `INVOICE_CREATED` state and strict safe invoice checkpoint.
- `sign402-gateway/sign402_gateway/bitrefill_runner.py`: invoice-first wallet/fulfillment orchestration and failure-state decisions.
- `sign402-gateway/sign402_gateway/server.py`: receipt-confirmed idempotent Base USDC transfer and runner wiring.
- `cdp-x402-service/src/index.mjs`: keep the existing `transfer-token` command as the generic exact-atomic ERC-20 transfer route.
- `sign402-gateway/tests/test_diagnostics.py`: allowlist and bearer-value regression tests.
- `sign402-gateway/tests/test_bitrefill_mcp.py`: prepare/complete, validation, and idempotency tests.
- `sign402-gateway/tests/test_commerce_store.py`: state ordering and safe checkpoint tests.
- `sign402-gateway/tests/test_bitrefill_runner.py`: orchestration order, no-funds-on-rejection, retry, and reconciliation tests.
- `sign402-gateway/tests/test_gateway_server.py`: CDP command construction and production wiring tests.

### Task 1: Provider-Safe Diagnostics

**Files:**
- Modify: `sign402-gateway/sign402_gateway/diagnostics.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_mcp.py`
- Test: `sign402-gateway/tests/test_diagnostics.py`
- Test: `sign402-gateway/tests/test_bitrefill_mcp.py`

**Interfaces:**
- Consumes: MCP error content as an arbitrary string.
- Produces: `safe_provider_diagnostic(detail: str, *, env: Mapping[str, str] | None = None) -> dict[str, str | int]`.

- [ ] **Step 1: Write failing allowlist/filter tests**

```python
def test_provider_diagnostic_keeps_code_but_removes_bearer_values(self):
    detail = json.dumps({
        "code": "PACKAGE_VALUE_INVALID",
        "message": (
            "invalid package; pay https://pay.example/inv "
            "to 0x1111111111111111111111111111111111111111 "
            "pin=1234 esim=LPA:1$secret"
        ),
        "status": 422,
        "request_id": "req_123",
        "payment_link": "https://pay.example/secret",
    })
    diagnostic = safe_provider_diagnostic(detail, env={})
    assert diagnostic["code"] == "PACKAGE_VALUE_INVALID"
    assert diagnostic["status"] == "422"
    assert diagnostic["requestId"] == "req_123"
    rendered = str(diagnostic)
    for secret in ("https://", "0x1111", "1234", "LPA:"):
        assert secret not in rendered


def test_unparseable_provider_body_returns_only_metadata(self):
    detail = "raw payment address 0x1111111111111111111111111111111111111111"
    diagnostic = safe_provider_diagnostic(detail, env={})
    assert diagnostic["type"] == "unparseable"
    assert diagnostic["bytes"] == len(detail.encode("utf-8"))
    assert diagnostic["sha256"] == hashlib.sha256(detail.encode()).hexdigest()
    assert detail not in str(diagnostic)
```

- [ ] **Step 2: Run tests and verify they fail for the missing API**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_diagnostics -v
python -m unittest tests.test_bitrefill_mcp.BitrefillMcpDecodeTests -v
```

Expected: FAIL because `safe_provider_diagnostic` does not exist and MCP errors still log raw text.

- [ ] **Step 3: Implement strict diagnostic parsing and filtering**

```python
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_EVM_ADDRESS = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_BEARER_PAIR = re.compile(
    r"(?i)\b(pin|code|redemption|activation|esim|qr|secret|token|key)"
    r"\s*[:=]\s*\S+"
)


def safe_provider_diagnostic(
    detail: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str | int]:
    raw = str(detail)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {
            "type": "unparseable",
            "bytes": len(raw.encode("utf-8")),
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }
    if not isinstance(parsed, Mapping):
        return {
            "type": "non_object",
            "bytes": len(raw.encode("utf-8")),
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }
    result: dict[str, str | int] = {}
    # Copy only code/error_code, message, status, request_id/trace_id.
    # Apply env redaction, then URL/address/key-value filtering to every value.
    return result
```

Change `decode_mcp_tool_result` to log `safe_provider_diagnostic(raw_detail)` rather than `raw_detail`. Do not pass the raw body to `log_hidden_detail`, exception messages, or context fields.

- [ ] **Step 4: Run focused tests and verify safe diagnostics pass**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_diagnostics tests.test_bitrefill_mcp -v
```

Expected: PASS; useful code/status/request ID remain, bearer values and raw malformed content are absent.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/diagnostics.py \
  sign402-gateway/sign402_gateway/bitrefill_mcp.py \
  sign402-gateway/tests/test_diagnostics.py \
  sign402-gateway/tests/test_bitrefill_mcp.py
git commit -m "fix: sanitize Bitrefill provider diagnostics"
```

### Task 2: Two-Phase Bitrefill Client

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_mcp.py`
- Test: `sign402-gateway/tests/test_bitrefill_client.py`
- Test: `sign402-gateway/tests/test_bitrefill_mcp.py`

**Interfaces:**
- Consumes: approved `quote`, committed `recipient`, and optional prepared checkpoint.
- Produces:
  - `prepare_purchase(*, quote: dict[str, Any], recipient: dict[str, Any]) -> dict[str, Any]`
  - `complete_purchase(*, quote: dict[str, Any], prepared: dict[str, Any], checkpoint_callback: Callable | None = None) -> dict[str, Any]`
  - `validate_prepared_purchase(prepared: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]`

- [ ] **Step 1: Write failing preparation and validation tests**

```python
def test_prepare_creates_unpaid_invoice_without_treasury_transfer(self):
    caller = FakeMcpCaller([invoice_payload()])
    treasury = FakeTreasuryClient()
    client = McpBitrefillClient(
        api_key="key_123",
        max_purchase_usd="50.00",
        payment_method="usdc_base",
        treasury_client=treasury,
        call_tool=caller,
    )
    prepared = client.prepare_purchase(quote=APPROVED_QUOTE, recipient={})
    assert [name for name, _ in caller.calls] == ["buy-products"]
    assert caller.calls[0][1]["cart_items"] == [
        {"product_id": "steam-usa", "package_value": "50"}
    ]
    assert "package_id" not in caller.calls[0][1]["cart_items"][0]
    assert treasury.transfers == []
    assert prepared == {
        "invoiceId": "inv_1",
        "status": "unpaid",
        "productId": "steam-usa",
        "packageValue": "50",
        "paymentMethod": "usdc_base",
        "paymentAmount": "50.00",
        "paymentAsset": "USDC",
        "paymentNetwork": "base",
    }


def test_prepare_rejects_invoice_mismatch_before_funding(self):
    for field, bad_value in (
        ("product_id", "other"),
        ("package_value", "25"),
        ("currency", "BTC"),
        ("network", "ethereum"),
        ("amount", "50.01"),
    ):
        payload = invoice_payload()
        mutate_invoice(payload, field, bad_value)
        with self.assertRaises(ValueError):
            client_for(payload).prepare_purchase(
                quote=APPROVED_QUOTE,
                recipient={},
            )
```

Add separate named tests for missing invoice ID, wrong payment method, and invoice amount greater than the approved `totalUsd`/`priceUsd`.

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
cd sign402-gateway
python -m unittest \
  tests.test_bitrefill_client \
  tests.test_bitrefill_mcp.BitrefillMcpPurchaseTests -v
```

Expected: FAIL because only the single-phase `buy_product` interface exists.

- [ ] **Step 3: Implement preparation and strict safe snapshot validation**

```python
class BitrefillClient(Protocol):
    def prepare_purchase(
        self,
        *,
        quote: dict[str, Any],
        recipient: dict[str, Any],
    ) -> dict[str, Any]: ...

    def complete_purchase(
        self,
        *,
        quote: dict[str, Any],
        prepared: dict[str, Any],
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]: ...
```

In `McpBitrefillClient.prepare_purchase`, call `buy-products` once with `package_value`, normalize the invoice, require an invoice ID, validate product/package/payment method/amount/asset/network, and return only the safe snapshot. The payment address remains only in the local normalized invoice and is discarded when the method returns.

In `complete_purchase`, require the safe prepared snapshot, reload the invoice using `get-invoice-by-id`, verify the returned invoice ID equals `prepared["invoiceId"]`, and re-run the same validations before any payment.

Keep `buy_product` temporarily as a compatibility wrapper:

```python
def buy_product(self, *, quote, recipient, checkpoint_callback=None):
    prepared = self.prepare_purchase(quote=quote, recipient=recipient)
    if checkpoint_callback is not None:
        checkpoint_callback(prepared)
    return self.complete_purchase(
        quote=quote,
        prepared=prepared,
        checkpoint_callback=checkpoint_callback,
    )
```

Implement deterministic `prepare_purchase` and `complete_purchase` in `TestBitrefillClient` so test mode follows the production control flow without real side effects.

- [ ] **Step 4: Run focused client tests**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_bitrefill_client tests.test_bitrefill_mcp -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/bitrefill.py \
  sign402-gateway/sign402_gateway/bitrefill_mcp.py \
  sign402-gateway/tests/test_bitrefill_client.py \
  sign402-gateway/tests/test_bitrefill_mcp.py
git commit -m "feat: split Bitrefill invoice preparation from payment"
```

### Task 3: Safe `INVOICE_CREATED` State

**Files:**
- Modify: `sign402-gateway/sign402_gateway/commerce_store.py`
- Test: `sign402-gateway/tests/test_commerce_store.py`

**Interfaces:**
- Consumes: the safe result of `prepare_purchase`.
- Produces: state `INVOICE_CREATED` with canonical `bitrefillCheckpoint`.

- [ ] **Step 1: Write failing state and persistence tests**

```python
def test_invoice_created_is_between_approval_and_funding(self):
    self.assertGreater(STATE_ORDER["INVOICE_CREATED"], STATE_ORDER["USER_APPROVED"])
    self.assertLess(STATE_ORDER["INVOICE_CREATED"], STATE_ORDER["FULFILLING"])


def test_invoice_checkpoint_keeps_only_safe_validated_fields(self):
    checkpoint = {
        "invoiceId": "inv_1",
        "status": "unpaid",
        "productId": "steam-usa",
        "packageValue": "50",
        "paymentMethod": "usdc_base",
        "paymentAmount": "50.00",
        "paymentAsset": "USDC",
        "paymentNetwork": "base",
        "paymentAddress": "0x1111111111111111111111111111111111111111",
        "paymentLink": "https://pay.example/secret",
    }
    store.advance_state(
        "quote_1",
        "INVOICE_CREATED",
        {"bitrefillCheckpoint": checkpoint},
    )
    persisted = store.get_quote("quote_1")["metadata"]["bitrefillCheckpoint"]
    assert persisted["invoiceId"] == "inv_1"
    assert persisted["paymentAmount"] == "50.00"
    assert "paymentAddress" not in persisted
    assert "paymentLink" not in persisted
    assert "0x111111" not in sqlite_text(path)
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_commerce_store -v
```

Expected: FAIL because `INVOICE_CREATED` and the new safe checkpoint fields do not exist.

- [ ] **Step 3: Add the state and strict canonical sanitizer**

```python
STATE_ORDER = {
    "QUOTED": 10,
    "FIREFLY_APPROVED": 20,
    "USER_APPROVED": 20,
    "INVOICE_CREATED": 25,
    "SINGIT_AUTHORIZED": 30,
    "SINGIT_SETTLED": 35,
    "FULFILLING": 40,
    # terminal states unchanged
}
```

Extend `sanitize_bitrefill_checkpoint` with only:

```python
for key in (
    "invoiceId",
    "status",
    "productId",
    "packageValue",
    "paymentMethod",
    "paymentAmount",
    "paymentAsset",
    "paymentNetwork",
):
    _copy_scalar(snapshot, checkpoint, key, field=f"bitrefillCheckpoint.{key}")
```

Do not add address/link/raw response fields. Keep canonical-state checks active so unsafe historical metadata cannot be silently retained.

- [ ] **Step 4: Run commerce-store tests**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_commerce_store -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/commerce_store.py \
  sign402-gateway/tests/test_commerce_store.py
git commit -m "feat: persist safe Bitrefill invoice checkpoint"
```

### Task 4: Invoice-First Wallet Orchestration

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`

**Interfaces:**
- Consumes: `BitrefillClient.prepare_purchase` and persisted `bitrefillCheckpoint`.
- Produces:
  - `BitrefillFulfillmentRunner.prepare(payload: dict[str, Any]) -> dict[str, Any]`
  - `BitrefillFulfillmentRunner.fulfill(payload: dict[str, Any]) -> dict[str, Any]`

- [ ] **Step 1: Write failing order/no-funds regression tests**

```python
def test_wallet_runner_prepares_invoice_before_user_funding(self):
    events = []
    bitrefill = Mock()
    bitrefill.prepare_purchase.side_effect = lambda **_: (
        events.append("invoice") or prepared_invoice()
    )
    user_funding = Mock(side_effect=lambda **_: events.append("user_transfer") or funding())
    fulfillment = BitrefillFulfillmentRunner(
        store=store,
        bitrefill_client=bitrefill,
        funding_runner=Mock(
            side_effect=lambda quote: events.append("swap") or swap_result()
        ),
        now_provider=lambda: NOW,
    )
    runner = approved_wallet_runner(
        store=store,
        fulfillment=fulfillment,
        user_funding=user_funding,
    )
    runner.buy({"quoteId": "quote_1", "telegramUserId": "123"})
    assert events[:3] == ["invoice", "user_transfer", "swap"]


def test_provider_rejection_moves_no_user_or_cdp_funds(self):
    bitrefill = Mock()
    bitrefill.prepare_purchase.side_effect = ValueError("provider rejected")
    user_funding = Mock()
    cdp_swap = Mock()
    runner = approved_wallet_runner(
        store=store,
        fulfillment=BitrefillFulfillmentRunner(
            store=store,
            bitrefill_client=bitrefill,
            funding_runner=cdp_swap,
            now_provider=lambda: NOW,
        ),
        user_funding=user_funding,
    )
    with self.assertRaisesRegex(ValueError, "Bitrefill provider request failed"):
        runner.buy({"quoteId": "quote_1", "telegramUserId": "123"})
    user_funding.assert_not_called()
    cdp_swap.assert_not_called()
    assert store.get_quote("quote_1")["state"] == "FULFILLMENT_FAILED"
```

Also add tests that `fulfill` rejects a missing/mismatched checkpoint without funding, and that the existing `pre_swap` exact-token return path still passes.

- [ ] **Step 2: Run tests and verify the current funds-first order fails**

Run:

```bash
cd sign402-gateway
python -m unittest \
  tests.test_bitrefill_runner.BitrefillWalletPurchaseTests \
  tests.test_bitrefill_runner.BitrefillRunnerTests -v
```

Expected: FAIL with `user_transfer`/`swap` occurring before invoice preparation.

- [ ] **Step 3: Add explicit preparation entry point**

```python
def prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
    quote_id, record, effective_quote = self._authorized_context(payload)
    prepared = self.bitrefill_client.prepare_purchase(
        quote=effective_quote,
        recipient=self._stored_recipient(record),
    )
    self.store.advance_state(
        quote_id,
        "INVOICE_CREATED",
        {"bitrefillCheckpoint": prepared},
    )
    return prepared
```

Preparation failures transition to `FULFILLMENT_FAILED`, log only generic local context plus the sanitized provider diagnostic, and re-raise `ValueError("Bitrefill provider request failed")`.

- [ ] **Step 4: Move preparation before user funding**

Immediately after persisting `USER_APPROVED`, call:

```python
prepared = self.fulfillment_runner.prepare(
    {"quoteId": quote_id, "fulfillmentToken": fulfillment_token}
)
wallet_checkout["bitrefillInvoiceId"] = prepared["invoiceId"]
```

Only after `prepare` succeeds may `user_funding_runner` execute. Persist only the invoice ID in `walletCheckout`; the canonical details stay in `bitrefillCheckpoint`.

Update `fulfill` to require state `INVOICE_CREATED`, load the stored checkpoint, then:

```python
funding_result = self.funding_runner(effective_quote)
self.store.checkpoint(quote_id, {"bankrSwap": funding_result})
result = self.bitrefill_client.complete_purchase(
    quote=effective_quote,
    prepared=prepared,
    checkpoint_callback=lambda value: self.store.checkpoint(
        quote_id,
        {"bitrefillCheckpoint": value},
    ),
)
```

Do not call `prepare_purchase` from `fulfill`; no retry may create a second invoice after funds move.

- [ ] **Step 5: Correct failure states**

- Preparation/validation failure before user funding: `FULFILLMENT_FAILED`.
- User-funding failure before a transaction hash: `FULFILLMENT_FAILED`.
- Confirmed user transfer or later failure: keep `RECONCILIATION_REQUIRED`, except the proven existing `pre_swap` return path.
- Polling timeout after a confirmed invoice payment: `RECONCILIATION_REQUIRED`.

Implement this with an explicit `funds_moved` check based on validated transaction checkpoints, never on exception text.

- [ ] **Step 6: Run focused orchestration tests**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_bitrefill_runner -v
```

Expected: PASS, including old refund/repricing/replay tests.

- [ ] **Step 7: Commit**

```bash
git add sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "fix: prepare Bitrefill invoice before moving funds"
```

### Task 5: Idempotent Exact-Atomic Invoice Payment

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_mcp.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`
- Test: `sign402-gateway/tests/test_bitrefill_mcp.py`
- Verify: `cdp-x402-service/src/token-return.mjs`
- Verify: `cdp-x402-service/test/token-return.test.mjs`

**Interfaces:**
- Consumes: invoice ID, Base USDC payment address, and exact six-decimal atomic amount.
- Produces: `CdpWalletClient.transfer_token_exact(..., idempotency_key: str) -> dict[str, Any]`.

- [ ] **Step 1: Write failing CDP command/idempotency tests**

```python
def test_transfer_token_exact_uses_receipt_confirmed_idempotent_command(self):
    client = CdpWalletClient(service_dir=Path("/srv/cdp"))
    with patch("subprocess.run") as run:
        run.return_value = completed('{"ok":true,"transactionHash":"0xPAY"}')
        result = client.transfer_token_exact(
            token_address=BASE_USDC_MAINNET,
            to_address="0x1111111111111111111111111111111111111111",
            amount_atomic="50000000",
            chain="base",
            idempotency_key="bitrefill-pay:inv_1",
        )
    command = run.call_args.args[0]
    assert command[2] == "transfer-token"
    assert command[-2:] == ["--idempotency-key", "bitrefill-pay:inv_1"]
    assert result["txId"] == "0xPAY"
```

```python
def test_complete_purchase_does_not_pay_twice_when_checkpoint_has_tx(self):
    prepared = prepared_invoice()
    prepared["treasuryPayment"] = {
        "txId": "0xPAY",
        "amountAtomic": "50000000",
        "asset": "USDC",
        "network": "base",
    }
    result = client.complete_purchase(quote=APPROVED_QUOTE, prepared=prepared)
    treasury.transfer_token_exact.assert_not_called()
    assert result["treasuryPayment"]["txId"] == "0xPAY"
```

Add a provider-status test for `payment_detected`, `paid`, `pending`, `complete`, and `delivered`, each proving no new transfer.

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
cd sign402-gateway
python -m unittest \
  tests.test_gateway_server.CdpWalletClientTests \
  tests.test_bitrefill_mcp.BitrefillMcpPurchaseTests -v
```

Expected: FAIL because `transfer_token_exact` and prepared-payment replay protection do not exist.

- [ ] **Step 3: Add generic exact-token CDP client method**

```python
def transfer_token_exact(
    self,
    *,
    token_address: str,
    to_address: str,
    amount_atomic: str,
    idempotency_key: str,
    chain: str = "base",
) -> dict[str, Any]:
    payload = self._run([
        "transfer-token",
        "--token", str(token_address),
        "--to", str(to_address),
        "--amount-atomic", str(amount_atomic),
        "--chain", str(chain),
        "--idempotency-key", str(idempotency_key),
    ])
    return self._with_tx_id(payload)
```

Refactor `return_token` to delegate to this method with `bitrefill-return:<quoteId>`.

- [ ] **Step 4: Pay invoices in atomic units once**

In `_pay_usdc_invoice`, convert the validated decimal amount exactly:

```python
amount_atomic_decimal = payment_amount * Decimal(1_000_000)
if amount_atomic_decimal != amount_atomic_decimal.to_integral_value():
    raise ValueError("Bitrefill invoice amount exceeds USDC precision")
amount_atomic = str(int(amount_atomic_decimal))
transfer = self.treasury_client.transfer_token_exact(
    token_address=BASE_USDC_MAINNET,
    to_address=address,
    amount_atomic=amount_atomic,
    chain="base",
    idempotency_key=f"bitrefill-pay:{invoice_id}",
)
```

Before broadcasting, return the stored `treasuryPayment` when it has a validated transaction hash. If no stored transaction exists but the reloaded invoice status indicates payment detected/pending/complete, poll only and do not broadcast. Checkpoint the confirmed transaction before polling.

- [ ] **Step 5: Run Python and Node payment tests**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_gateway_server tests.test_bitrefill_mcp -v
cd ../cdp-x402-service
npm test
```

Expected: PASS; existing Node `returnErc20` tests still prove idempotency key forwarding and successful receipt enforcement.

- [ ] **Step 6: Commit**

```bash
git add sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/sign402_gateway/bitrefill_mcp.py \
  sign402-gateway/tests/test_gateway_server.py \
  sign402-gateway/tests/test_bitrefill_mcp.py
git commit -m "fix: pay Bitrefill invoices idempotently"
```

### Task 6: Wiring, Full Verification, Integration, and Deployment

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: tests only if wiring coverage exposes a missing assertion.
- Verify: all changed files.

**Interfaces:**
- Consumes: the two-phase client and invoice-first runner.
- Produces: production wiring in which the wallet runner can call `prepare` and fulfillment can call `complete_purchase`.

- [ ] **Step 1: Write/extend the failing production wiring test**

```python
def test_production_wallet_runner_uses_shared_invoice_first_fulfillment_runner(self):
    server = build_server_with_live_bitrefill_env()
    assert server.bitrefill_wallet_purchase_runner.fulfillment_runner is (
        server.bitrefill_fulfillment_runner
    )
    assert callable(server.bitrefill_fulfillment_runner.prepare)
    assert server.bitrefill_fulfillment_runner.bitrefill_client is (
        server.bitrefill_client
    )
```

- [ ] **Step 2: Run the wiring test and verify it fails if wiring is incomplete**

Run:

```bash
cd sign402-gateway
python -m unittest tests.test_gateway_server -v
```

Expected: initial FAIL only if constructor/wiring signatures are incomplete; otherwise PASS and no server change is needed for this step.

- [ ] **Step 3: Complete constructor/wiring updates**

Ensure one shared `BitrefillFulfillmentRunner` owns the same live client used for quoting and is passed to `WalletBitrefillPurchaseRunner`. Keep:

```python
SIGN402_BITREFILL_SERVICE_FEE_BPS=100
SIGN402_BITREFILL_MAX_REPRICE_BPS=500
```

No new environment toggle may restore funds-first behavior.

- [ ] **Step 4: Run the full local suites**

Run:

```bash
cd sign402-gateway
python -m unittest discover -s tests -v
cd ../cdp-x402-service
npm test
```

Expected: all Python and Node tests PASS.

- [ ] **Step 5: Review the diff for secret/bearer leakage and scope**

Run:

```bash
git diff --check
git diff --stat x402Bnkr...
rg -n "paymentAddress|payment_link|redemption|LPA:|raw provider" \
  sign402-gateway/sign402_gateway \
  sign402-gateway/tests
```

Expected: no whitespace errors; production persistence/logging paths contain no forbidden raw fields; test fixtures may contain marker values only in assertions proving absence.

- [ ] **Step 6: Commit any final wiring-only change**

```bash
git add sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/tests/test_gateway_server.py
git commit -m "test: verify invoice-first production wiring"
```

Skip this commit if Step 3 required no source/test changes.

- [ ] **Step 7: Finish the development branch**

Use `superpowers:finishing-a-development-branch`, re-run the full suites, and merge the isolated feature branch into `x402Bnkr` with a fast-forward or reviewed merge that preserves all task commits.

- [ ] **Step 8: Push only `x402Bnkr`**

```bash
git push singitai x402Bnkr
```

Expected: remote `singitai/x402Bnkr` points to the verified local commit.

- [ ] **Step 9: Deploy the exact pushed commit**

On `hermes@164.68.104.44` in `/home/hermes/apps/sign402`:

```bash
git fetch singitai x402Bnkr
git checkout x402Bnkr
git pull --ff-only singitai x402Bnkr
cd sign402-gateway
.venv/bin/python -m unittest discover -s tests -v
cd ../cdp-x402-service
npm test
```

Expected: deployed commit equals `git rev-parse singitai/x402Bnkr`; all server tests PASS.

- [ ] **Step 10: Restart and perform read-only production verification**

Restart `sign402-gateway` through the existing systemd policy, then verify:

```bash
systemctl --user is-active sign402-gateway 2>/dev/null || true
curl --fail --silent http://127.0.0.1:8787/health
```

If the service is system-level and sudo is unavailable, send `SIGTERM` to the current service PID and allow `Restart=always` to create a new PID. Verify the new PID, start timestamp, deployed Git commit, and `{"ok": true}` health response.

Do not call `buy-products`, do not pay an invoice, and do not debit the replenished SINGIT balance during deployment verification.
