# Bitrefill Exact Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filter Telegram Bitrefill search responses so users see only products matching the requested company or eSIM intent, with a clear no-match response when MCP returns unrelated products.

**Architecture:** Add a deterministic, local relevance helper to the existing Telegram plugin. The existing gateway and MCP request remain unchanged; after normalization, the Telegram search worker filters the returned products before limiting, storing, and displaying them.

**Tech Stack:** Python 3, standard-library `re`, `unittest`, existing Hermes Telegram plugin test harness.

## Global Constraints

- Keep the project Bitrefill MCP server as the exclusive purchase-creation route.
- Do not add a second search request or any AI-based relevance call.
- Do not add fuzzy matching or automatic typo correction.
- Preserve MCP result order.
- Do not change catalog browsing, product details, pricing, purchase creation, payment, or confirmation safeguards.
- Do not touch or stage unrelated dirty worktree files.

---

### Task 1: Deterministic Bitrefill relevance filter

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: `query: str` and `products: list[dict]` returned by `_normalize_bitrefill_products`.
- Produces: `_filter_bitrefill_search_products(query: str, products: list[dict]) -> list[dict]`.

- [ ] **Step 1: Write failing unit tests for company and eSIM relevance**

Add these tests to `PluginRegistrationTests` in `hermes-plugins/sign402-wallet/tests/test_plugin.py`:

```python
def test_bitrefill_search_filter_matches_company_and_ignores_generic_words(self):
    plugin = load_plugin()
    amazon = {
        "productId": "amazon-nl",
        "name": "Amazon.nl Netherlands",
        "country": "NL",
        "productType": "gift_card",
    }
    products = [
        {
            "productId": "bitrefill-esim-europe",
            "name": "Bitrefill eSIM Europe",
            "country": "AT",
            "productType": "esim",
        },
        amazon,
    ]

    self.assertEqual(
        plugin._filter_bitrefill_search_products("Amazon gift card", products),
        [amazon],
    )
    self.assertEqual(
        plugin._filter_bitrefill_search_products("Biterfill gift card", products),
        [],
    )

def test_bitrefill_search_filter_keeps_only_matching_esims(self):
    plugin = load_plugin()
    europe_esim = {
        "productId": "bitrefill-esim-europe",
        "name": "Bitrefill eSIM Europe",
        "country": "AT",
        "productType": "esim",
    }
    products = [
        {
            "productId": "amazon-nl",
            "name": "Amazon.nl Netherlands",
            "country": "NL",
            "productType": "gift_card",
        },
        europe_esim,
        {
            "productId": "bitrefill-esim-usa",
            "name": "Bitrefill eSIM USA",
            "country": "US",
            "productType": "esim",
        },
    ]

    self.assertEqual(
        plugin._filter_bitrefill_search_products("eSIM Europe", products),
        [europe_esim],
    )
```

- [ ] **Step 2: Run the unit tests and verify RED**

Run:

```bash
python3 -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests \
  -p 'test_plugin.py' \
  -k 'test_bitrefill_search_filter'
```

Expected: both tests error with `AttributeError` because `_filter_bitrefill_search_products` does not exist.

- [ ] **Step 3: Implement the minimal deterministic filter**

Add the generic-word constant near the existing Bitrefill constants in `hermes-plugins/sign402-wallet/__init__.py`:

```python
_BITREFILL_GENERIC_SEARCH_TERMS = frozenset(
    {
        "gift",
        "gifts",
        "card",
        "cards",
        "giftcard",
        "giftcards",
        "voucher",
        "vouchers",
    }
)
```

Add these helpers immediately after `_normalize_bitrefill_products`:

```python
def _bitrefill_search_tokens(value: str) -> list[str]:
    normalized = re.sub(
        r"\be[\W_]*sim\b",
        " esim ",
        str(value or "").casefold(),
    )
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def _bitrefill_compact_search_text(value: str) -> str:
    return "".join(_bitrefill_search_tokens(value))


def _bitrefill_product_is_esim(product: dict) -> bool:
    product_type = str(
        product.get("productType") or product.get("category") or ""
    )
    return (
        _bitrefill_compact_search_text(product_type) == "esim"
        or "esim"
        in _bitrefill_compact_search_text(str(product.get("name") or ""))
    )


def _filter_bitrefill_search_products(
    query: str,
    products: list[dict],
) -> list[dict]:
    query_tokens = _bitrefill_search_tokens(query)
    esim_intent = "esim" in query_tokens
    meaningful_terms = [
        token
        for token in query_tokens
        if token not in _BITREFILL_GENERIC_SEARCH_TERMS and token != "esim"
    ]
    if not meaningful_terms and not esim_intent:
        return []

    matches: list[dict] = []
    for product in products:
        if esim_intent and not _bitrefill_product_is_esim(product):
            continue
        product_name = _bitrefill_compact_search_text(
            str(product.get("name") or "")
        )
        if all(
            _bitrefill_compact_search_text(term) in product_name
            for term in meaningful_terms
        ):
            matches.append(product)
    return matches
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2.

Expected: 2 tests pass.

- [ ] **Step 5: Run the complete plugin suite**

Run:

```bash
python3 -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests \
  -p 'test_*.py'
```

Expected: all existing plugin tests pass.

- [ ] **Step 6: Commit the helper and unit tests**

```bash
git add \
  hermes-plugins/sign402-wallet/__init__.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "feat: add exact Bitrefill search matching"
```

---

### Task 2: Apply relevance filtering to Telegram search

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: `_filter_bitrefill_search_products(query: str, products: list[dict]) -> list[dict]` from Task 1.
- Produces: the existing `_handle_bitrefill_search_input` flow stores and displays only filtered products.

- [ ] **Step 1: Write a failing integration test for irrelevant MCP results**

Add this test to `PluginRegistrationTests`:

```python
def test_bitrefill_search_reports_no_exact_match_for_irrelevant_results(self):
    plugin = load_plugin()
    context = FakeContext()
    client = FakeClient()
    client.bitrefill_search_result = {
        "ok": True,
        "products": [
            {
                "productId": "bitrefill-esim-europe",
                "name": "Bitrefill eSIM Europe",
                "country": "AT",
                "productType": "esim",
            }
        ],
    }
    plugin._client_factory = lambda: client
    plugin._background_runner = lambda callback: callback()
    plugin.register(context)
    gateway = FakeGateway(adapter_key="telegram")

    def dispatch(text):
        return context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                text,
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

    dispatch("Buy Bitrefill")
    dispatch("Search Products")
    dispatch("Biterfill gift card")

    response = gateway.adapters["telegram"].sent[-1][1]
    self.assertIn("No exact Bitrefill products found", response)
    self.assertNotIn("Bitrefill eSIM Europe", response)
    self.assertEqual(
        plugin._BITREFILL_SESSIONS["1045618308"],
        {"stage": "awaiting-search", "country": "CZ"},
    )
```

- [ ] **Step 2: Write a failing integration test proving selection uses the filtered list**

Add this test to `PluginRegistrationTests`:

```python
def test_bitrefill_search_stores_only_exact_company_matches(self):
    plugin = load_plugin()
    context = FakeContext()
    client = FakeClient()
    client.bitrefill_search_result = {
        "ok": True,
        "products": [
            {
                "productId": "bitrefill-esim-europe",
                "name": "Bitrefill eSIM Europe",
                "country": "AT",
                "productType": "esim",
            },
            {
                "productId": "amazon-nl",
                "name": "Amazon.nl Netherlands",
                "country": "NL",
                "productType": "gift_card",
            },
        ],
    }
    client.bitrefill_product_result = {
        "ok": True,
        "productId": "amazon-nl",
        "name": "Amazon.nl Netherlands",
        "country": "NL",
        "requiredRecipientFields": [],
        "packages": [
            {
                "packageId": "amazon-nl<&>10",
                "value": "10",
                "priceUsd": "10.00",
            }
        ],
    }
    plugin._client_factory = lambda: client
    plugin._background_runner = lambda callback: callback()
    plugin.register(context)
    gateway = FakeGateway(adapter_key="telegram")

    def dispatch(text):
        return context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                text,
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

    dispatch("Buy Bitrefill")
    dispatch("Search Products")
    dispatch("Amazon")

    response = gateway.adapters["telegram"].sent[-1][1]
    self.assertIn("1. Amazon.nl Netherlands", response)
    self.assertNotIn("Bitrefill eSIM Europe", response)

    dispatch("1")

    self.assertEqual(client.bitrefill_product_calls, [("amazon-nl", "NL")])
```

- [ ] **Step 3: Run both integration tests and verify RED**

Run:

```bash
python3 -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests \
  -p 'test_plugin.py' \
  -k 'test_bitrefill_search_reports_no_exact_match_for_irrelevant_results'
python3 -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests \
  -p 'test_plugin.py' \
  -k 'test_bitrefill_search_stores_only_exact_company_matches'
```

Expected: the first test fails because the eSIM is displayed; the second fails because item 1 resolves to `bitrefill-esim-europe`.

- [ ] **Step 4: Apply the filter before the existing empty-result branch**

In `_handle_bitrefill_search_input.work`, replace:

```python
products = _normalize_bitrefill_products(result.get("products"))
```

with:

```python
products = _filter_bitrefill_search_products(
    clean_query,
    _normalize_bitrefill_products(result.get("products")),
)
```

Change the no-match copy from:

```python
f"No Bitrefill products found for \"{clean_query}\" in {country}.\n\nTry another search."
```

to:

```python
(
    f"No exact Bitrefill products found for \"{clean_query}\" "
    f"in {country}.\n\nTry another company name."
)
```

- [ ] **Step 5: Run both integration tests and verify GREEN**

Run the commands from Step 3.

Expected: both tests pass.

- [ ] **Step 6: Run regression suites**

Run:

```bash
python3 -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests \
  -p 'test_*.py'
python3 -m unittest discover \
  -s sign402-gateway/tests \
  -p 'test_*.py'
```

Expected: all plugin and gateway tests pass.

- [ ] **Step 7: Verify the diff is scoped and clean**

Run:

```bash
git diff --check
git diff -- \
  hermes-plugins/sign402-wallet/__init__.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
```

Expected: no whitespace errors; the diff contains only relevance filtering, no-match copy, and tests.

- [ ] **Step 8: Commit the integration**

```bash
git add \
  hermes-plugins/sign402-wallet/__init__.py \
  hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "fix: hide unrelated Bitrefill search results"
```

---

### Task 3: Production handoff

**Files:**
- No code changes.

**Interfaces:**
- Consumes: the two verified implementation commits.
- Produces: a pushed branch ready for server deployment.

- [ ] **Step 1: Confirm repository state**

Run:

```bash
git status --short
git log --oneline -5
git branch --show-current
```

Expected: only pre-existing unrelated dirty files remain; the current branch is `x402Bnkr`; the exact-search commits are at the tip.

- [ ] **Step 2: Push the current branch after user authorization**

Run:

```bash
git push singitai x402Bnkr
```

Expected: the remote branch advances to the exact-search implementation commit.

- [ ] **Step 3: Deploy the Telegram plugin after user authorization**

Run:

```bash
ssh hermes@164.68.104.44 '
  set -e
  cd /home/hermes/apps/sign402
  git fetch singitai x402Bnkr
  git merge --ff-only singitai/x402Bnkr
  rsync -a --delete \
    hermes-plugins/sign402-wallet/ \
    /home/hermes/.hermes/plugins/sign402-wallet/
  systemctl --user restart hermes-gateway
  systemctl --user is-active hermes-gateway
  git rev-parse HEAD
'
```

Expected: the merge is fast-forward only, `hermes-gateway` reports `active`, and the printed revision equals the pushed local revision. The command does not copy `.env` files or overwrite server-only secrets.

- [ ] **Step 4: Perform a read-only production verification**

Search for `Biterfill gift card`, `Amazon`, and `eSIM` through the deployed Telegram flow without selecting a product or creating an invoice.

Expected:

- `Biterfill gift card` shows the no-exact-match response;
- `Amazon` shows only Amazon products;
- `eSIM` shows only eSIM products;
- no purchase or payment is created.
