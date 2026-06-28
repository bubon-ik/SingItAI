# Bankr x402 → Bitrefill Automatic Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a Bitrefill purchase automatically recover a missing Bankr x402 transaction hash, verify the exact SINGIT transfer, and wait for a usable redemption result without repeating x402 or USDC payments.

**Architecture:** Capture the Base start block and Bankr `paymentMade` metadata around the x402 CLI call, then let a strict settlement resolver discover exactly one matching SINGIT Transfer log when no hash is returned. Keep Bitrefill fulfillment idempotent by checkpointing invoice/payment identifiers and refreshing the existing order until redemption is available.

**Tech Stack:** Python 3.14, `unittest`, Base JSON-RPC, ERC-20 Transfer logs, Bankr CLI, Bitrefill REST v2, SQLite.

---

## File Structure

- Modify `sign402-gateway/sign402_gateway/server.py`: Base RPC helpers, Bankr CLI parsing, x402 invocation metadata, and strict SINGIT verifier wiring.
- Modify `sign402-gateway/sign402_gateway/bitrefill.py`: delivery polling and resumable provider-result refresh.
- Modify `sign402-gateway/sign402_gateway/bitrefill_runner.py`: persist provider checkpoints and refresh pending orders without purchasing again.
- Modify `sign402-gateway/sign402_gateway/commerce_store.py`: add an atomic metadata checkpoint method that does not move the order state.
- Modify `sign402-gateway/tests/test_gateway_server.py`: Bankr parsing, RPC discovery, and strict verifier tests.
- Modify `sign402-gateway/tests/test_bitrefill_client.py`: transient redemption and pending refresh tests.
- Modify `sign402-gateway/tests/test_bitrefill_runner.py`: idempotent pending-order refresh test.

### Task 1: Preserve Bankr payment metadata and parse CLI transaction hashes

**Files:**
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`

- [ ] **Step 1: Add failing tests for the two observed Bankr output shapes**

Add these cases to `GatewayServerTests`:

```python
def test_bankr_transaction_hash_parser_accepts_tx_hash_line(self):
    self.assertEqual(
        _bankr_cli_transaction_hash(
            "Tx Hash:  0x453cff05c73f8fc70a9418520bec12ec538cb2cee7a7fbcac8751d177f94483d"
        ),
        "0x453cff05c73f8fc70a9418520bec12ec538cb2cee7a7fbcac8751d177f94483d",
    )

def test_bankr_x402_client_preserves_payment_made_and_start_block(self):
    completed = subprocess_completed(stdout='''{
      "success": true,
      "status": 200,
      "paymentMade": {
        "amountUsd": 0.0057,
        "network": "eip155:8453",
        "payTo": "0x8AEE621035D93Deb3C0C1177fac252dC2dd501a0"
      },
      "response": {"ok": true}
    }''')
    with patch("subprocess.run", return_value=completed):
        client = BankrCliX402PaymentClient(
            bankr_cli="/tmp/bankr",
            block_number_fetcher=Mock(return_value=47_751_000),
        )
        result = client("https://x402.bankr.bot/wallet/buy-bitrefill")

    self.assertEqual(result["startBlock"], 47_751_000)
    self.assertEqual(
        result["paymentMade"]["payTo"],
        "0x8AEE621035D93Deb3C0C1177fac252dC2dd501a0",
    )
```

Import `_bankr_cli_transaction_hash` in the existing server import list.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_bankr_transaction_hash_parser_accepts_tx_hash_line \
  tests.test_gateway_server.GatewayServerTests.test_bankr_x402_client_preserves_payment_made_and_start_block -v
```

Expected: the hash test fails with `None != 0x...`; constructing the x402 client fails because `block_number_fetcher` is not accepted.

- [ ] **Step 3: Implement minimal parsing and metadata capture**

Update `_bankr_cli_transaction_hash` so it accepts either output form:

```python
def _bankr_cli_transaction_hash(stdout: str) -> str | None:
    patterns = (
        r"https://basescan\.org/tx/(0x[a-fA-F0-9]{64})",
        r"\bTx Hash:\s*(0x[a-fA-F0-9]{64})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, stdout)
        if match:
            return match.group(1)
    return None
```

Add a Base block-number helper using the same JSON-RPC request path as receipt lookup:

```python
def fetch_base_block_number() -> int:
    payload = _base_rpc_call("eth_blockNumber", [])
    if not isinstance(payload, str) or not payload.startswith("0x"):
        raise ValueError("Base RPC returned an invalid block number")
    return int(payload, 16)
```

Refactor receipt lookup through `_base_rpc_call`, always sending
`User-Agent: Sign402/1.0`. Extend `BankrCliX402PaymentClient.__init__` with
`block_number_fetcher=fetch_base_block_number`; call it immediately before
`subprocess.run`. Store `startBlock` and a deep copy of
`raw_payload.get("paymentMade", {})` in the result.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2. Expected: two tests pass.

- [ ] **Step 5: Commit the isolated change**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "Capture Bankr x402 settlement metadata"
```

### Task 2: Discover and strictly verify a missing SINGIT transaction hash

**Files:**
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`

- [ ] **Step 1: Add a failing exact-match discovery test**

```python
def test_singit_verifier_discovers_exact_transfer_when_hash_is_missing(self):
    payer = "0x3b3e349e6cfee692b69d2c63ce86f7d444667d98"
    pay_to = "0x8aee621035d93deb3c0c1177fac252dc2dd501a0"
    tx_hash = "0x" + "ab" * 32
    resolver = Mock(return_value=tx_hash)
    receipt = erc20_receipt(
        token=DEFAULT_SINGIT_TOKEN_ADDRESS,
        sender=payer,
        recipient=pay_to,
        amount=11_000_000_000_000_000_000,
        status="0x1",
    )
    verifier = SingitSettlementVerifier(
        receipt_fetcher=Mock(return_value=receipt),
        transaction_resolver=resolver,
        payer_address=payer,
    )

    result = verifier(
        bankr_result={
            "transactionHash": None,
            "startBlock": 47_751_000,
            "paymentMade": {"payTo": pay_to},
        },
        quote={"maxSingitAtomic": "11000000000000000000"},
    )

    self.assertEqual(result["transactionHash"], tx_hash)
    resolver.assert_called_once_with(
        token_address=DEFAULT_SINGIT_TOKEN_ADDRESS,
        sender=payer,
        recipient=pay_to,
        amount_atomic="11000000000000000000",
        from_block=47_751_000,
    )
```

Add sibling tests where the receipt has a wrong sender, wrong recipient, wrong
amount, wrong token, or `status: 0x0`; each must raise a specific `ValueError`.

- [ ] **Step 2: Run the strict verifier tests and verify RED**

Run:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_singit_verifier_discovers_exact_transfer_when_hash_is_missing \
  tests.test_gateway_server.GatewayServerTests.test_singit_verifier_rejects_wrong_sender \
  tests.test_gateway_server.GatewayServerTests.test_singit_verifier_rejects_wrong_recipient \
  tests.test_gateway_server.GatewayServerTests.test_singit_verifier_rejects_wrong_amount -v
```

Expected: the first test fails because `transaction_resolver` is unsupported;
the strict tests expose the current amount-only matching behavior.

- [ ] **Step 3: Add bounded ERC-20 log lookup**

Implement:

```python
def fetch_base_erc20_transfer_logs(
    *, token_address: str, sender: str, recipient: str, from_block: int
) -> list[dict[str, Any]]:
    return _base_rpc_call(
        "eth_getLogs",
        [{
            "address": token_address,
            "fromBlock": hex(from_block),
            "toBlock": "latest",
            "topics": [
                ERC20_TRANSFER_TOPIC,
                "0x" + sender.lower().removeprefix("0x").rjust(64, "0"),
                "0x" + recipient.lower().removeprefix("0x").rjust(64, "0"),
            ],
        }],
    )
```

Add `BaseErc20TransactionResolver` with injectable `log_fetcher`, `sleeper`,
`attempts=6`, and `interval_seconds=2`. On each attempt, filter logs by exact
decoded `data == amount_atomic`. Deduplicate transaction hashes. Return one
hash, retry zero matches, and raise `ambiguous SINGIT settlement` for more than
one.

- [ ] **Step 4: Tighten `SingitSettlementVerifier`**

Give the verifier `transaction_resolver` and `payer_address`. If the Bankr hash
is missing, require `startBlock` and `paymentMade.payTo`, then call the resolver.
After fetching the receipt, require one exact transfer whose token, sender,
recipient, and amount all equal the expected values. Return:

```python
{
    "network": "base-mainnet",
    "transactionHash": tx_hash,
    "tokenAddress": self.singit_token_address,
    "from": self.payer_address,
    "payTo": pay_to,
    "amountAtomic": str(expected_amount),
    "requiredAmountAtomic": str(expected_amount),
    "discovered": bankr_hash_was_missing,
}
```

- [ ] **Step 5: Run focused and full gateway tests**

Run the focused command from Step 2, then:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest discover -s tests -v
```

Expected: all tests pass with zero failures.

- [ ] **Step 6: Commit the settlement resolver**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "Resolve missing Bankr SINGIT transactions"
```

### Task 3: Wait for usable Bitrefill redemption data

**Files:**
- Modify: `sign402-gateway/tests/test_bitrefill_client.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`

- [ ] **Step 1: Add a failing transient-order test**

```python
def test_live_buy_waits_until_gift_card_redemption_is_available(self):
    transport = FakeBitrefillTransport([
        {"data": {
            "id": "invoice_1", "status": "unpaid",
            "payment": {"address": "0xInvoice", "price": 100000, "currency": "USDC"},
        }},
        {"data": {"id": "invoice_1", "status": "all_delivered", "orders": [{"id": "order_1"}]}},
        {"data": {"id": "order_1", "status": "created", "redemption_info": None}},
        {"data": {"id": "order_1", "status": "delivered", "redemption_info": {"code": "READY-123"}}},
    ])
    client = LiveBitrefillClient(
        api_key="key", max_purchase_usd="0.20", payment_method="usdc_base",
        refund_address="0xRefund", treasury_client=FakeTreasuryClient(),
        invoice_poll_attempts=2, invoice_poll_interval_seconds=0,
        request_json=transport,
    )

    result = client.buy_product(
        quote={
            "quoteId": "quote_1", "productId": "bitrefill-giftcard-usd",
            "productType": "gift_card", "packageId": "0.1",
            "packageValue": "0.1", "priceUsd": "0.10",
        },
        recipient={},
    )

    self.assertEqual(result["status"], "delivered")
    self.assertEqual(result["redemption"]["value"]["code"], "READY-123")
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_client.BitrefillClientTests.test_live_buy_waits_until_gift_card_redemption_is_available -v
```

Expected: FAIL because the client returns the first `created` order with null
redemption.

- [ ] **Step 3: Implement order polling**

Replace the one-shot order fetch with:

```python
def _poll_order_until_usable(self, order_id: str, *, product_type: str) -> dict[str, Any]:
    last_order: dict[str, Any] = {}
    for attempt in range(max(1, self.invoice_poll_attempts)):
        payload = self._request_json(
            "GET", f"/orders/{urllib.parse.quote(order_id, safe='')}",
            query={}, body=None,
        )
        order = payload.get("data", {})
        if isinstance(order, dict):
            last_order = order
            status = str(order.get("status", "")).lower()
            requires_redemption = product_type in {"gift_card", "esim"}
            if status == "delivered" and (
                not requires_redemption or order.get("redemption_info") is not None
            ):
                return order
        if attempt < self.invoice_poll_attempts - 1:
            self.sleeper(self.invoice_poll_interval_seconds)
    return last_order
```

Pass `quote["productType"]` from both balance and USDC purchase paths. Preserve
the last order as pending instead of presenting null redemption as delivered.

- [ ] **Step 4: Run focused and client suites**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bitrefill_client.BitrefillClientTests.test_live_buy_waits_until_gift_card_redemption_is_available -v
../payment-executor/.venv/bin/python -m unittest tests.test_bitrefill_client -v
```

Expected: all client tests pass.

- [ ] **Step 5: Commit delivery polling**

```bash
git add sign402-gateway/sign402_gateway/bitrefill.py sign402-gateway/tests/test_bitrefill_client.py
git commit -m "Wait for Bitrefill redemption delivery"
```

### Task 4: Persist pending Bitrefill checkpoints and refresh without repurchase

**Files:**
- Modify: `sign402-gateway/tests/test_commerce_store.py`
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/commerce_store.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`

- [ ] **Step 1: Add a failing metadata checkpoint test**

```python
def test_checkpoint_metadata_does_not_change_state(self):
    store.save_quote({"quoteId": "quote_1"})
    store.checkpoint("quote_1", {"bitrefillCheckpoint": {"invoiceId": "invoice_1"}})
    record = store.get_quote("quote_1")
    self.assertEqual(record["state"], "QUOTED")
    self.assertEqual(record["metadata"]["bitrefillCheckpoint"]["invoiceId"], "invoice_1")
```

- [ ] **Step 2: Add a failing no-repurchase refresh test**

Create a fake client whose `buy_product` returns a pending provider result with
`invoiceId: invoice_1`, `orderId: order_1`, and increments `buy_calls`; its
`refresh_purchase` returns a delivered result with code `READY-123` and
increments `refresh_calls`. Fulfill once, then call `lookup_bitrefill_order`
with the fake client:

```python
result = lookup_bitrefill_order(
    store, "quote_1", include_redemption=True, recipient={},
    bitrefill_client=bitrefill,
)
self.assertEqual(result["state"], "DELIVERED")
self.assertEqual(result["redemption"]["value"]["code"], "READY-123")
self.assertEqual(bitrefill.buy_calls, 1)
self.assertEqual(bitrefill.refresh_calls, 1)
```

- [ ] **Step 3: Run both tests and verify RED**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_commerce_store.CommerceStoreTests.test_checkpoint_metadata_does_not_change_state \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_pending_order_refreshes_without_repurchase -v
```

Expected: `checkpoint` and the `bitrefill_client` lookup parameter do not exist.

- [ ] **Step 4: Implement checkpoint storage and pending state**

Add `BitrefillCommerceStore.checkpoint` using the same lock/transaction and
metadata merge as `advance_state`, but update only `metadata_json` and
`updated_at`.

Have `LiveBitrefillClient.buy_product` call an optional `checkpoint_callback`
immediately after invoice creation and immediately after the treasury transfer.
Each checkpoint contains only invoice/order/payment identifiers and the
treasury transaction hash—never redemption data or secrets.

In `BitrefillFulfillmentRunner`, pass a callback that stores
`bitrefillCheckpoint`. Store the returned provider result as
`BITREFILL_PURCHASED`. Advance to `DELIVERED` only when `_provider_is_delivered`
returns true:

```python
def _provider_is_delivered(result: dict[str, Any], quote: dict[str, Any]) -> bool:
    if str(result.get("status", "")).lower() != "delivered":
        return False
    if quote.get("productType") in {"gift_card", "esim"}:
        redemption = result.get("redemption")
        return isinstance(redemption, dict) and redemption.get("value") is not None
    return True
```

- [ ] **Step 5: Implement refresh-by-ID**

Add `LiveBitrefillClient.refresh_purchase(provider_result, quote)` that GETs the
stored invoice and order IDs and rebuilds the provider result without creating
an invoice or calling the treasury client. Extend `lookup_bitrefill_order` with
an optional `bitrefill_client`; when state is `BITREFILL_PURCHASED`, refresh,
store the result, and advance to `DELIVERED` only when usable.

Update `build_server` to pass the live/test Bitrefill client into the lookup
lambda.

- [ ] **Step 6: Run focused and runner/store suites**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_commerce_store \
  tests.test_bitrefill_runner -v
```

Expected: all store and runner tests pass; buy count remains exactly one.

- [ ] **Step 7: Commit idempotent fulfillment**

```bash
git add \
  sign402-gateway/sign402_gateway/commerce_store.py \
  sign402-gateway/sign402_gateway/bitrefill.py \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/tests/test_commerce_store.py \
  sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "Resume pending Bitrefill delivery safely"
```

### Task 5: Wire live configuration and verify the complete flow

**Files:**
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`

- [ ] **Step 1: Add failing live configuration validation tests**

Add tests proving live mode rejects an absent or malformed
`SIGN402_BANKR_WALLET_ADDRESS`, and accepts the current Base payer address.

```python
with self.assertRaisesRegex(ValueError, "SIGN402_BANKR_WALLET_ADDRESS"):
    build_singit_settlement_verifier_from_env({
        "SIGN402_BITREFILL_MODE": "live",
    })
```

- [ ] **Step 2: Run the configuration tests and verify RED**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_live_settlement_requires_bankr_wallet \
  tests.test_gateway_server.GatewayServerTests.test_live_settlement_accepts_base_bankr_wallet -v
```

Expected: FAIL because the environment builder does not exist.

- [ ] **Step 3: Implement environment wiring**

Add `build_singit_settlement_verifier_from_env`. Validate addresses with
`re.fullmatch(r"0x[a-fA-F0-9]{40}", value)`. Build the strict verifier with
`BaseErc20TransactionResolver` and use it from `build_server`. Keep tests and
non-live mode injectable without requiring a real RPC call.

- [ ] **Step 4: Run all automated verification**

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest discover -s tests -v
cd ../singit-risk-check
node --test tests/buy-bitrefill.test.mjs
```

Expected: Python suite reports zero failures; Node suite reports all tests pass.

- [ ] **Step 5: Run a no-spend dry reconciliation probe**

Start the gateway in test Bitrefill mode with a fake Bankr x402 result that has
`transactionHash: null`, a bounded start block, and an injected exact Transfer
log. Submit a test quote and buy request.

Expected: the order reaches `DELIVERED`, settlement proof contains
`discovered: true`, the fake Bitrefill client is called once, and no live Bankr
or Bitrefill network request occurs.

- [ ] **Step 6: Review the final diff and commit**

```bash
git diff --check
git status --short
git add sign402-gateway/sign402_gateway sign402-gateway/tests
git commit -m "Automate Bankr Bitrefill reconciliation"
```

Do not run another live purchase during verification. A new live purchase must
be quoted and explicitly confirmed by the user with product, SINGIT maximum,
and USDC cap.

