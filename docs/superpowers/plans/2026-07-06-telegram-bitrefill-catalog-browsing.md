# Telegram Bitrefill Catalog Browsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Telegram users browse paginated local and international Bitrefill products by category, then continue through the existing package and purchase flow.

**Architecture:** Add a normalized catalog-page operation to the Bitrefill client boundary and expose it through a dedicated gateway endpoint. Extend the Hermes Telegram plugin with category and pagination session states while reusing its existing product-details, recipient, iMessage approval, and SINGIT payment path.

**Tech Stack:** Python 3.11+, standard-library HTTP and `unittest`, Hermes plugin hooks, Telegram reply keyboards, Bitrefill REST API v2.

---

## File Map

- Modify `sign402-gateway/sign402_gateway/bitrefill.py`: add test and live `/products` listing implementations.
- Modify `sign402-gateway/sign402_gateway/bitrefill_runner.py`: validate and normalize catalog page requests.
- Modify `sign402-gateway/sign402_gateway/server.py`: expose and wire `/agent/list-bitrefill-products`.
- Modify `sign402-gateway/tests/test_bitrefill_client.py`: verify provider request filters and normalization.
- Modify `sign402-gateway/tests/test_bitrefill_runner.py`: verify category mapping and pagination.
- Modify `sign402-gateway/tests/test_gateway_server.py`: verify the new route.
- Modify `hermes-plugins/sign402-wallet/client.py`: call the new localhost gateway endpoint.
- Modify `hermes-plugins/sign402-wallet/__init__.py`: add category and page interaction states.
- Modify `hermes-plugins/sign402-wallet/tests/test_client.py`: verify catalog request authentication and payload.
- Modify `hermes-plugins/sign402-wallet/tests/test_plugin.py`: verify category browsing, navigation, and product selection.

### Task 1: Bitrefill Catalog Page Boundary

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`
- Test: `sign402-gateway/tests/test_bitrefill_client.py`

- [ ] **Step 1: Write failing client tests**

Add tests requiring `list_products` on both clients:

```python
def test_live_list_products_uses_official_listing_filters(self):
    transport = FakeBitrefillTransport([{"data": []}])
    client = LiveBitrefillClient(api_key="key_123", request_json=transport)

    products = client.list_products(
        country="CZ,XI",
        category="food,restaurants",
        start=8,
        limit=9,
        include_test_products=False,
    )

    self.assertEqual(products, [])
    self.assertEqual(
        transport.calls[0],
        {
            "method": "GET",
            "path": "/products",
            "query": {
                "country": "CZ,XI",
                "category": "food,restaurants",
                "start": "8",
                "limit": "9",
                "include_test_products": "false",
            },
            "body": None,
        },
    )
```

Also assert that `TestBitrefillClient.list_products` filters comma-separated
countries and categories before slicing by `start` and `limit`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest sign402-gateway.tests.test_bitrefill_client -v
```

Expected: failures because `list_products` does not exist.

- [ ] **Step 3: Implement the minimal client methods**

Add this operation to the protocol and both implementations:

```python
def list_products(
    self,
    *,
    country: str,
    category: str,
    start: int,
    limit: int,
    include_test_products: bool,
) -> list[dict[str, Any]]:
    ...
```

The live client calls `GET /products` with all five query parameters and
normalizes every item through `_normalize_product`. The test client splits
comma-separated filters case-insensitively, filters `TEST_PRODUCTS`, then
returns `matches[start:start + limit]`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Task 1 test command. Expected: all Bitrefill client tests pass.

### Task 2: Gateway Catalog Service and Route

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`

- [ ] **Step 1: Write failing service tests**

Add tests for:

```python
page = BitrefillCatalogService(bitrefill_client=client)(
    {
        "country": "CZ",
        "category": "Food",
        "start": 8,
        "limit": 8,
        "includeInternational": True,
        "includeTestProducts": False,
    }
)
```

Assert the client receives `country="CZ,XI"`,
`category="food,restaurants,food-delivery,groceries"`, `start=8`, and
`limit=9`. Return nine fake products and assert the response exposes only
eight plus `hasPrevious=True` and `hasNext=True`.

Add rejection tests for negative `start`, malformed country, unknown category,
and non-positive `limit`.

- [ ] **Step 2: Run service tests and verify RED**

Run:

```bash
python3 -m unittest sign402-gateway.tests.test_bitrefill_runner -v
```

Expected: import failure for `BitrefillCatalogService`.

- [ ] **Step 3: Implement `BitrefillCatalogService`**

Define the exact category map:

```python
BITREFILL_BROWSE_CATEGORIES = {
    "all": "",
    "shopping": "retail,ecommerce,gifts,giftcard,electronics,apparel",
    "food": "food,restaurants,food-delivery,groceries",
    "games": "games",
    "mobile": "refill,phone,data,bundles",
    "travel": "travel,flights,experiences",
    "entertainment": "entertainment,streaming,music",
}
```

Validate a two-letter country, integer `start >= 0`, and integer
`1 <= limit <= 20`. Request `limit + 1` products, trim to `limit`, and return
`products`, `start`, `limit`, `hasPrevious`, and `hasNext`.

- [ ] **Step 4: Write the failing route test**

Add `/agent/list-bitrefill-products` to the dummy server and assert the handler
passes the JSON payload unchanged to `bitrefill_catalog_service`.

- [ ] **Step 5: Wire the route and server**

Add the endpoint to health output and POST routing. Add
`bitrefill_catalog_service` to `Sign402GatewayServer.__init__`, instantiate it
in `build_server`, and pass it into the server constructor.

- [ ] **Step 6: Run gateway tests and verify GREEN**

Run:

```bash
python3 -m unittest sign402-gateway.tests.test_bitrefill_runner sign402-gateway.tests.test_gateway_server -v
```

Expected: all selected gateway tests pass.

### Task 3: Hermes Gateway Client

**Files:**
- Modify: `hermes-plugins/sign402-wallet/client.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_client.py`

- [ ] **Step 1: Write a failing request test**

Call:

```python
result = client.list_bitrefill_products(
    country="CZ",
    category="Food",
    start=8,
    limit=8,
    include_international=True,
    include_test_products=False,
)
```

Assert the request targets `/agent/list-bitrefill-products`, uses the operator
Bearer token, and sends the exact camelCase JSON payload.

- [ ] **Step 2: Run the client test and verify RED**

Run:

```bash
python3 -m unittest hermes-plugins/sign402-wallet/tests/test_client.py -v
```

Expected: `GatewayClient` has no `list_bitrefill_products`.

- [ ] **Step 3: Implement the gateway method**

Add `_BITREFILL_LIST_PATH` and a method that posts the normalized payload with
the existing purchase timeout and `operation="list-bitrefill-products"`.

- [ ] **Step 4: Run the client tests and verify GREEN**

Run the Task 3 test command. Expected: all plugin client tests pass.

### Task 4: Telegram Category and Pagination Flow

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

- [ ] **Step 1: Extend the fake client and write a failing browse-flow test**

The fake client records catalog calls and returns eight products with
`hasNext=True`. Drive this interaction:

```text
Buy Bitrefill
Browse Catalog
Food
Next
Previous
1
```

Assert:

- the Bitrefill menu contains `Browse Catalog`;
- category selection is shown;
- calls use country `CZ`, category `Food`, starts `0`, `8`, then `0`;
- page text contains products and page numbers;
- selecting `1` calls existing `get_bitrefill_product`.

- [ ] **Step 2: Run the plugin test and verify RED**

Run:

```bash
python3 -m unittest hermes-plugins/sign402-wallet/tests/test_plugin.py -v
```

Expected: the menu and catalog state do not exist.

- [ ] **Step 3: Add category and page helpers**

Add:

```python
_BITREFILL_CATALOG_PAGE_SIZE = 8
_BITREFILL_CATEGORY_BUTTONS = (
    ("All", "Shopping"),
    ("Food", "Games"),
    ("Mobile", "Travel"),
    ("Entertainment", "Back"),
)
```

Implement focused helpers to:

- show categories;
- load a page with `list_bitrefill_products`;
- format `Bitrefill in CZ - Food - Page 2`;
- create a numbered keyboard with only valid Previous/Next controls.

- [ ] **Step 4: Add session transitions**

`Browse Catalog` enters `select-category`. A category loads offset zero and
stores `source="catalog"`, category, page products, offset, and navigation
flags. `Next` and `Previous` load adjacent pages. `Back` from a catalog product
page returns to categories; `Back` from categories returns to the Bitrefill
menu. Product numbers continue through `_handle_bitrefill_product_choice`.

- [ ] **Step 5: Add empty-page and invalid-choice tests**

Verify an empty first page returns to category selection, and invalid numbers
retain the same page and its navigation keyboard.

- [ ] **Step 6: Run plugin tests and verify GREEN**

Run:

```bash
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests -q
```

Expected: all plugin tests pass.

### Task 5: Full Verification and Delivery

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run full gateway tests**

```bash
python3 -m unittest discover -s sign402-gateway/tests -q
```

Expected: all gateway tests pass.

- [ ] **Step 2: Run full plugin tests**

```bash
python3 -m unittest discover -s hermes-plugins/sign402-wallet/tests -q
```

Expected: all plugin tests pass.

- [ ] **Step 3: Check formatting and scope**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only intended source, tests, docs, and the
pre-existing untracked `assets/` appear.

- [ ] **Step 4: Commit and push**

Stage only the catalog feature and the existing non-USD display fix, commit
with `Add paginated Bitrefill catalog browsing`, then push branch `x402Bnkr`
to remote `singitai`.
