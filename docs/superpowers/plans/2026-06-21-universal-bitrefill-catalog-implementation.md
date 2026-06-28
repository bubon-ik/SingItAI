# Universal Bitrefill Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Amazon-only dry-run client with a universal, safe Bitrefill-compatible test catalog that supports searching, product details, explicit packages, phone recipients, quoting, Firefly approval, and fulfillment without spending real money.

**Architecture:** Keep the existing commerce orchestration and Bankr endpoint, but expand the `BitrefillClient` boundary and add catalog services in focused modules. The Gateway selects an offline `TestBitrefillClient` by default; `live` mode is explicitly gated on `BITREFILL_API_KEY` and remains unavailable until credentials exist for official contract testing.

**Tech Stack:** Python 3 standard library, `unittest`, SQLite, Sign402 Gateway HTTP server, existing Bankr x402 Node handler.

---

## File map

- Modify `sign402-gateway/sign402_gateway/bitrefill.py` — normalized catalog models, test catalog, package and recipient validation, test fulfillment.
- Modify `sign402-gateway/sign402_gateway/bitrefill_quote.py` — explicit selected-product quote and expanded purchase commitment.
- Modify `sign402-gateway/sign402_gateway/bitrefill_runner.py` — search/details services and explicit quote orchestration.
- Modify `sign402-gateway/sign402_gateway/server.py` — catalog routes and safe client mode selection.
- Modify `sign402-gateway/tests/test_bitrefill_client.py` — catalog, package, recipient, and fulfillment tests.
- Modify `sign402-gateway/tests/test_bitrefill_quote.py` — universal quote and commitment tests.
- Modify `sign402-gateway/tests/test_bitrefill_runner.py` — service and non-Amazon commerce-flow tests.
- Modify `sign402-gateway/tests/test_gateway_server.py` — route and startup-mode tests.
- Modify `Hermes Sign402 - Project Spec.md` — agent-facing universal catalog commands.

### Task 1: Universal test catalog

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`
- Test: `sign402-gateway/tests/test_bitrefill_client.py`

- [ ] **Step 1: Write failing search and details tests**

Add tests that require all three official free product identifiers and verify product-type filtering:

```python
from sign402_gateway.bitrefill import TestBitrefillClient


def test_test_catalog_searches_multiple_product_types(self):
    client = TestBitrefillClient()

    all_products = client.search_products(
        query="",
        country="US",
        category="",
        product_type="",
        include_test_products=True,
    )
    phone_products = client.search_products(
        query="",
        country="US",
        category="",
        product_type="phone_refill",
        include_test_products=True,
    )

    self.assertEqual(
        {product["productId"] for product in all_products},
        {"test-gift-card-link", "test-gift-card-code", "test-phone-refill"},
    )
    self.assertEqual([product["productId"] for product in phone_products], ["test-phone-refill"])


def test_phone_refill_details_expose_packages_and_recipient_requirement(self):
    details = TestBitrefillClient().get_product_details(
        product_id="test-phone-refill",
        country="US",
    )

    self.assertEqual(details["productType"], "phone_refill")
    self.assertEqual(details["recipientType"], "phone")
    self.assertIn("phone", details["requiredRecipientFields"])
    self.assertTrue(details["packages"])
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd sign402-gateway
python3 -m unittest tests.test_bitrefill_client -v
```

Expected: import or attribute failures because `TestBitrefillClient`, `search_products`, and `get_product_details` do not exist.

- [ ] **Step 3: Implement normalized test catalog**

Define a module-level immutable-style catalog with normalized keys:

```python
TEST_PRODUCTS = {
    "test-gift-card-link": {
        "productId": "test-gift-card-link",
        "name": "Test Gift Card Link",
        "country": "US",
        "currency": "USD",
        "category": "gift_card",
        "productType": "gift_card",
        "recipientType": "none",
        "requiredRecipientFields": [],
        "packages": [{"packageId": "1", "value": "1", "priceUsd": "1.00"}],
    },
    "test-gift-card-code": {
        "productId": "test-gift-card-code",
        "name": "Test Gift Card Code",
        "country": "US",
        "currency": "USD",
        "category": "gift_card",
        "productType": "gift_card",
        "recipientType": "none",
        "requiredRecipientFields": [],
        "packages": [{"packageId": "1", "value": "1", "priceUsd": "1.00"}],
    },
    "test-phone-refill": {
        "productId": "test-phone-refill",
        "name": "Test Phone Refill",
        "country": "US",
        "currency": "USD",
        "category": "refill",
        "productType": "phone_refill",
        "recipientType": "phone",
        "requiredRecipientFields": ["phone"],
        "packages": [{"packageId": "1", "value": "1", "priceUsd": "1.00"}],
    },
}
```

Implement `search_products` with case-insensitive query matching and exact optional filters. Return deep copies so callers cannot mutate the shared catalog. Implement `get_product_details` with exact product ID lookup and a clear `unknown Bitrefill product` error.

- [ ] **Step 4: Add package and recipient validation tests**

```python
def test_quote_product_validates_package_and_phone(self):
    client = TestBitrefillClient()
    selected = client.quote_product(
        product_id="test-phone-refill",
        package_id="1",
        country="US",
        recipient={"phone": "+12025550123"},
    )
    self.assertEqual(selected["productId"], "test-phone-refill")
    self.assertEqual(selected["packageId"], "1")
    self.assertEqual(selected["priceUsd"], "1.00")


def test_quote_product_requires_phone_for_phone_refill(self):
    with self.assertRaisesRegex(ValueError, "recipient.phone is required"):
        TestBitrefillClient().quote_product(
            product_id="test-phone-refill",
            package_id="1",
            country="US",
            recipient={},
        )
```

- [ ] **Step 5: Run tests, implement validation, and verify GREEN**

`quote_product` must select an exact package, validate required recipient fields, and return a flat normalized snapshot containing `productId`, `name`, `productType`, `packageId`, `packageValue`, `priceUsd`, `currency`, and recipient requirements.

Run the Task 1 test command and expect all tests to pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add sign402-gateway/sign402_gateway/bitrefill.py sign402-gateway/tests/test_bitrefill_client.py
git commit -m "feat: add universal Bitrefill test catalog"
```

### Task 2: Explicit universal quotes and commitments

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_quote.py`
- Test: `sign402-gateway/tests/test_bitrefill_quote.py`

- [ ] **Step 1: Replace the US/Amazon-shaped quote test with an explicit product snapshot test**

```python
def test_build_quote_accepts_explicit_phone_refill_snapshot(self):
    quote = build_quote(
        request={
            "productId": "test-phone-refill",
            "packageId": "1",
            "country": "US",
        },
        product={
            "productId": "test-phone-refill",
            "name": "Test Phone Refill",
            "productType": "phone_refill",
            "packageId": "1",
            "packageValue": "1",
            "country": "US",
            "currency": "USD",
            "priceUsd": "1.00",
        },
        singit_usd_price="0.01",
        margin_bps=500,
        quote_id="quote_fixed",
        now_epoch=1_719_000_000,
    )

    self.assertEqual(quote["productId"], "test-phone-refill")
    self.assertEqual(quote["productType"], "phone_refill")
    self.assertEqual(quote["packageId"], "1")
    self.assertEqual(quote["singitAmount"], "105")
```

- [ ] **Step 2: Run the quote tests and verify RED**

Expected failure: missing `productType` and `packageId`, or the existing country restriction rejects valid normalized inputs.

- [ ] **Step 3: Generalize `build_quote`**

Remove the hardcoded US restriction. Validate that the request `productId` and `packageId` exactly match the normalized product snapshot. Store `productType`, `packageId`, `packageValue`, `country`, and currency in the quote.

- [ ] **Step 4: Bind the expanded fields in the commitment**

Add a failing assertion, then include these fields in `build_purchase_commitment`:

```python
{
    "type": "singit-bitrefill-purchase",
    "quoteId": str(quote["quoteId"]),
    "productId": str(quote["productId"]),
    "productType": str(quote["productType"]),
    "packageId": str(quote["packageId"]),
    "packageValue": str(quote["packageValue"]),
    "priceUsd": str(quote["priceUsd"]),
    "maxSingitAtomic": str(quote["maxSingitAtomic"]),
    "recipientCommitment": recipient_commitment(recipient or {}),
    "expiresAt": str(quote["expiresAt"]),
}
```

- [ ] **Step 5: Run quote tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_bitrefill_quote -v
```

- [ ] **Step 6: Commit Task 2**

```bash
git add sign402-gateway/sign402_gateway/bitrefill_quote.py sign402-gateway/tests/test_bitrefill_quote.py
git commit -m "feat: generalize Bitrefill quotes"
```

### Task 3: Search, details, and explicit quote services

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`

- [ ] **Step 1: Write failing service tests**

```python
def test_catalog_services_search_and_return_product_details(self):
    client = TestBitrefillClient()
    search = BitrefillSearchService(bitrefill_client=client)(
        {"query": "phone", "country": "US", "includeTestProducts": True}
    )
    details = BitrefillProductDetailsService(bitrefill_client=client)(
        {"productId": "test-phone-refill", "country": "US"}
    )
    self.assertEqual(search["products"][0]["productId"], "test-phone-refill")
    self.assertEqual(details["recipientType"], "phone")
```

- [ ] **Step 2: Verify RED and implement the two read-only services**

The search service maps camelCase HTTP input to the client interface and returns `{"ok": True, "products": [...]}`. The details service requires `productId` and returns the normalized detail object with `ok: true`.

- [ ] **Step 3: Write a failing explicit quote-service test**

```python
def test_quote_service_quotes_selected_phone_refill(self):
    quote = service.quote(
        {
            "productId": "test-phone-refill",
            "packageId": "1",
            "country": "US",
            "recipient": {"phone": "+12025550123"},
        }
    )
    self.assertEqual(quote["productId"], "test-phone-refill")
    self.assertEqual(quote["packageId"], "1")
```

- [ ] **Step 4: Replace `find_product` orchestration with `quote_product`**

`BitrefillQuoteService.quote` must reject legacy `query/value` payloads, validate `recipient` as an object, call the client with explicit selection fields, build the quote, and persist both the quote and recipient only through the existing approved purchase flow.

- [ ] **Step 5: Update fulfillment tests to use `TestBitrefillClient`**

Keep all existing expiration, token, replay, approved-recipient, failure-state, and redaction assertions. Replace Amazon fixtures with `test-gift-card-link` or `test-phone-refill` fixtures created through the explicit quote service.

- [ ] **Step 6: Run runner tests and verify GREEN**

```bash
python3 -m unittest tests.test_bitrefill_runner -v
```

- [ ] **Step 7: Commit Task 3**

```bash
git add sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "feat: add Bitrefill catalog services"
```

### Task 4: Gateway routes and safe mode selection

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`

- [ ] **Step 1: Write failing route tests**

Add tests for:

```text
POST /agent/search-bitrefill
POST /agent/get-bitrefill-product
```

Each test must assert exact delegation payload and a `200` JSON response. Add both routes to the `/health` endpoint list.

- [ ] **Step 2: Verify RED and implement handlers**

Follow the existing quote handler pattern: parse JSON, call the injected service, return JSON, and normalize validation exceptions to HTTP 400 without acquiring the Firefly lock.

- [ ] **Step 3: Write mode-selection tests**

Extract a focused factory:

```python
def build_bitrefill_client_from_env(env: dict[str, str] | None = None) -> BitrefillClient:
    values = os.environ if env is None else env
    mode = values.get("SIGN402_BITREFILL_MODE", "test").strip().lower()
    if mode == "test":
        return TestBitrefillClient()
    if mode == "live":
        if not values.get("BITREFILL_API_KEY", "").strip():
            raise ValueError("BITREFILL_API_KEY is required in live Bitrefill mode")
        raise ValueError("live Bitrefill client is unavailable until API contract testing")
    raise ValueError(f"unsupported SIGN402_BITREFILL_MODE: {mode}")
```

Tests must verify default test mode, missing-key rejection, and unknown-mode rejection. The explicit unavailable error prevents a credential from accidentally activating untested live purchasing.

- [ ] **Step 4: Wire services into `Sign402GatewayServer` and `build_server`**

Inject `bitrefill_search_service` and `bitrefill_product_details_service`. Replace direct `DryRunBitrefillClient()` construction with the safe factory.

- [ ] **Step 5: Run Gateway tests and verify GREEN**

```bash
python3 -m unittest tests.test_gateway_server -v
```

- [ ] **Step 6: Commit Task 4**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "feat: expose universal Bitrefill catalog routes"
```

### Task 5: Documentation and end-to-end verification

**Files:**
- Modify: `Hermes Sign402 - Project Spec.md`

- [ ] **Step 1: Document the safe test flow**

Add exact commands for search, details, quote, and buy using `test-phone-refill`. State clearly that test mode never spends Bitrefill balance and live mode is intentionally unavailable until an API key exists and the official contract is tested.

- [ ] **Step 2: Run all verification commands**

```bash
cd sign402-gateway
python3 -m unittest discover -s tests -v

cd ../singit-risk-check
node --test tests/buy-bitrefill.test.mjs
node -e "JSON.parse(require('fs').readFileSync('bankr.x402.json','utf8')); console.log('bankr.x402.json OK')"
```

Expected: all Python and Node tests pass with zero failures and no ResourceWarnings.

- [ ] **Step 3: Restart Gateway and smoke-test read-only routes**

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/search-bitrefill \
  -H "Content-Type: application/json" \
  -d '{"query":"phone","country":"US","includeTestProducts":true}'

curl -sS -X POST http://127.0.0.1:8099/agent/get-bitrefill-product \
  -H "Content-Type: application/json" \
  -d '{"productId":"test-phone-refill","country":"US"}'
```

Expected: the first response contains `test-phone-refill`; the second contains packages and `recipientType: phone`.

- [ ] **Step 4: Smoke-test quote without spending funds**

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/quote-bitrefill \
  -H "Content-Type: application/json" \
  -d '{"productId":"test-phone-refill","packageId":"1","country":"US","recipient":{"phone":"+12025550123"}}'
```

Expected: a fresh quote for `test-phone-refill`, package `1`, and a bounded SINGIT amount.

- [ ] **Step 5: Commit documentation**

```bash
git add "Hermes Sign402 - Project Spec.md"
git commit -m "docs: add universal Bitrefill test flow"
```
