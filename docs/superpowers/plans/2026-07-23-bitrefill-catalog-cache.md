# Bitrefill Catalog Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Telegram Bitrefill catalog pages return from a durable 10-minute stale-while-revalidate cache, with non-blocking warm-up and no minute-long cold request.

**Architecture:** `McpBitrefillClient` will cache the complete normalized product list per country and test-product mode, persist only public catalog metadata, collapse concurrent misses, and refresh stale entries in the background. The Hermes plugin will schedule a read-only warm-up after showing the category keyboard; all product details, prices, purchases, payment, and fulfillment remain uncached live Bitrefill MCP calls.

**Tech Stack:** Python 3 standard library (`threading`, `time`, `json`, `pathlib`, `tempfile`), existing MCP/httpx client, `unittest`, Hermes plugin background runner.

## Global Constraints

- Bitrefill MCP remains the exclusive route for catalog reads and supported purchases.
- Catalog freshness lifetime defaults to exactly 600 seconds.
- Catalog-only upstream timeout defaults to exactly 8 seconds.
- Cache data contains public normalized product-list metadata only; never credentials, recipient data, invoices, product-detail prices, purchases, or redemption data.
- Product details, package price, availability, invoice creation, payment, and fulfillment always bypass the catalog cache.
- Existing unrelated working-tree changes in `sign402-gateway/.env.example`, `sign402-gateway/sign402_gateway/server.py`, and `sign402-gateway/tests/test_gateway_server.py` must remain unmodified and unstaged.
- Every production-code change follows red-green TDD.

---

## File Map

- Modify `sign402-gateway/sign402_gateway/bitrefill_mcp.py`: catalog cache state, persistence, single-flight loading, stale background refresh, and the separate catalog-only MCP caller timeout.
- Modify `sign402-gateway/tests/test_bitrefill_mcp.py`: deterministic cache, concurrency, persistence, failure, and live-path isolation tests.
- Modify `hermes-plugins/sign402-wallet/__init__.py`: non-blocking country catalog warm-up after the category prompt is sent.
- Modify `hermes-plugins/sign402-wallet/tests/test_plugin.py`: warm-up scheduling and failure-isolation tests.
- Modify `sign402-gateway/README.md`: non-secret cache settings and behavior.
- Modify `hermes-plugins/sign402-wallet/README.md`: Telegram catalog warm-up behavior.

### Task 1: Fresh Cache and Durable Snapshot

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_mcp.py`
- Test: `sign402-gateway/tests/test_bitrefill_mcp.py`

**Interfaces:**
- Consumes: existing `McpBitrefillClient.list_products(...) -> list[dict[str, Any]]`.
- Produces: constructor options `catalog_cache_ttl_seconds`, `catalog_cache_path`, `now_provider`, and internal country-snapshot lookup used by Task 2.

- [ ] **Step 1: Write failing tests for fresh reuse and category/page slicing**

Add tests that call `list_products` three times for the same country with different category/start values and assert one `search-products` call:

```python
def test_list_reuses_one_fresh_country_snapshot_for_categories_and_pages(self):
    caller = FakeMcpCaller([{"products": self.catalog_rows()}])
    with tempfile.TemporaryDirectory() as tmpdir:
        client = McpBitrefillClient(
            api_key="key_123",
            call_tool=caller,
            catalog_cache_path=Path(tmpdir) / "catalog.json",
            catalog_cache_ttl_seconds=600,
            now_provider=lambda: 1000.0,
        )
        first = client.list_products(country="NL,XI", category="all", start=0, limit=2, include_test_products=False)
        food = client.list_products(country="NL,XI", category="food,restaurants", start=0, limit=8, include_test_products=False)
        second_page = client.list_products(country="NL,XI", category="all", start=2, limit=2, include_test_products=False)

    self.assertEqual(len(caller.calls), 1)
    self.assertEqual([row["productId"] for row in second_page], ["p3", "p4"])
    self.assertTrue(all(row["category"] in {"food", "restaurants"} for row in food))
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `cd sign402-gateway && .venv/bin/python -m unittest tests.test_bitrefill_mcp.BitrefillMcpCatalogTests.test_list_reuses_one_fresh_country_snapshot_for_categories_and_pages -v`

Expected: FAIL because every `list_products` call invokes `search-products`.

- [ ] **Step 3: Implement the minimal fresh in-memory cache**

Add normalized cache keys and immutable-on-read snapshots under an `RLock`:

```python
self.catalog_cache_ttl_seconds = float(catalog_cache_ttl_seconds)
self.catalog_cache_path = Path(catalog_cache_path).expanduser()
self._now = now_provider or time.time
self._catalog_lock = threading.RLock()
self._catalog_cache: dict[tuple[str, bool], dict[str, Any]] = {}

def _catalog_key(self, country: str, include_test_products: bool) -> tuple[str, bool]:
    return (str(country).strip().upper(), bool(include_test_products))

def _fresh_catalog_snapshot(self, key):
    with self._catalog_lock:
        entry = self._catalog_cache.get(key)
        if entry and self._now() - entry["storedAt"] <= self.catalog_cache_ttl_seconds:
            return deepcopy(entry["products"])
    return None
```

Move the current `search-products` + normalization operation into `_fetch_catalog_snapshot(country, include_test_products)` and let `list_products` filter/slice the returned full snapshot.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command.

Expected: PASS and exactly one recorded MCP read.

- [ ] **Step 5: Write failing persistence reconstruction tests**

Create one client, load a snapshot, create a second client pointing at the same path, and assert the second returns products without calling its failing transport. Add malformed, oversized, future-timestamp, and excessive-product-count files and assert they are ignored without exposing file contents in errors.

- [ ] **Step 6: Run persistence tests and verify RED**

Run: `cd sign402-gateway && .venv/bin/python -m unittest tests.test_bitrefill_mcp.BitrefillMcpCatalogTests.test_catalog_snapshot_survives_client_reconstruction tests.test_bitrefill_mcp.BitrefillMcpCatalogTests.test_catalog_cache_ignores_invalid_persistence -v`

Expected: FAIL because no snapshot is written or loaded.

- [ ] **Step 7: Implement bounded atomic persistence**

Use a versioned JSON object with a maximum 5 MiB file, maximum 256 entries, and maximum 200 products per entry. Write to a same-directory temporary file, `chmod(0o600)`, `os.replace`, and never log serialized data:

```python
{
    "version": 1,
    "entries": [
        {"country": "NL", "includeTestProducts": False, "storedAt": 1000.0, "products": [...]}
    ],
}
```

Load only structurally valid entries with finite timestamps not in the future. Persistence errors are best-effort and leave the in-memory cache valid.

- [ ] **Step 8: Run focused and module tests**

Run: `cd sign402-gateway && .venv/bin/python -m unittest tests.test_bitrefill_mcp -v`

Expected: all Bitrefill MCP tests PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add sign402-gateway/sign402_gateway/bitrefill_mcp.py sign402-gateway/tests/test_bitrefill_mcp.py
git commit -m "feat: cache Bitrefill catalog snapshots"
```

### Task 2: Stale-While-Revalidate, Single Flight, and Short Catalog Timeout

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_mcp.py`
- Test: `sign402-gateway/tests/test_bitrefill_mcp.py`

**Interfaces:**
- Consumes: the full country snapshot cache from Task 1.
- Produces: `warm_catalog(country: str, include_test_products: bool = False) -> None`, stale immediate returns, and one in-flight fetch per cache key.

- [ ] **Step 1: Write failing stale-return and failed-refresh tests**

Inject a controllable `now_provider` and background runner. Advance beyond 600 seconds, make the refresh transport block or raise, and assert `list_products` returns the old products before the runner completes. After failure, assert the old snapshot is still returned.

```python
started = threading.Event()
release = threading.Event()
def slow_refresh(name, arguments):
    started.set()
    release.wait(1)
    raise ValueError("upstream unavailable")

begin = time.monotonic()
products = client.list_products(country="NL", category="all", start=0, limit=8, include_test_products=False)
self.assertLess(time.monotonic() - begin, 0.05)
self.assertEqual(products[0]["productId"], "cached-product")
```

- [ ] **Step 2: Verify stale tests RED**

Run the two new tests with `python -m unittest ... -v`.

Expected: FAIL because stale data currently triggers a foreground fetch.

- [ ] **Step 3: Implement stale immediate return and one daemon refresh**

Track `_catalog_flights` under the existing lock. A stale read returns a deep copy and starts refresh only when no flight exists. Refresh success replaces and persists the snapshot; refresh failure preserves the entry. All background exceptions are consumed internally with metadata-only warning text.

- [ ] **Step 4: Verify stale tests GREEN**

Run the Step 2 command.

Expected: PASS; elapsed assertion remains below 50 ms.

- [ ] **Step 5: Write a failing concurrent cold-miss single-flight test**

Start two threads calling the same cold key, block the fake transport, then release it. Assert both callers receive the same products and `FakeMcpCaller.calls` contains one `search-products` call.

- [ ] **Step 6: Verify the concurrent test RED**

Run: `cd sign402-gateway && .venv/bin/python -m unittest tests.test_bitrefill_mcp.BitrefillMcpCatalogTests.test_concurrent_cold_catalog_requests_are_single_flight -v`

Expected: FAIL with two transport calls.

- [ ] **Step 7: Implement cold single-flight sharing**

Represent each in-flight operation with an event and captured exception. The owner performs the fetch; waiters wait for that flight and reuse its stored cache result. Always signal waiters in `finally`; never retry automatically after the shared failure in the same call.

- [ ] **Step 8: Add the separate catalog caller timeout test**

Patch `McpToolCaller`, construct a live client without injected callers, and assert the catalog caller receives `timeout_seconds=8.0` while the commerce/detail caller retains `60.0`. With injected `call_tool`, use it for both so unit tests stay deterministic.

- [ ] **Step 9: Implement environment-backed non-secret defaults**

Resolve these optional values in `McpBitrefillClient.__init__` without changing dirty `server.py`:

```text
SIGN402_BITREFILL_CATALOG_CACHE_TTL_SECONDS=600
SIGN402_BITREFILL_CATALOG_TIMEOUT_SECONDS=8
SIGN402_BITREFILL_CATALOG_CACHE_PATH=~/.sign402/bitrefill-catalog-cache.json
```

Reject non-positive TTL/timeout values. Use `McpToolCaller(server_url, timeout_seconds=catalog_timeout)` only for catalog snapshot fetches. Continue using the existing caller for details and purchases.

- [ ] **Step 10: Verify Task 2 and commit**

Run: `cd sign402-gateway && .venv/bin/python -m unittest tests.test_bitrefill_mcp -v`

Expected: all tests PASS.

```bash
git add sign402-gateway/sign402_gateway/bitrefill_mcp.py sign402-gateway/tests/test_bitrefill_mcp.py
git commit -m "fix: keep Bitrefill catalog browsing responsive"
```

### Task 3: Non-Blocking Telegram Warm-Up

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: existing `GatewayClient.list_bitrefill_products(...)` and `_run_in_background`.
- Produces: `_warm_bitrefill_catalog(country: str) -> None`, scheduled only after the category prompt has been queued.

- [ ] **Step 1: Write a failing scheduling test**

Use `callbacks = []`, open `Buy Bitrefill`, then `Browse Catalog`. Assert the category prompt was sent immediately, exactly one warm-up callback was appended, and no catalog HTTP call occurred before executing that callback.

- [ ] **Step 2: Verify scheduling test RED**

Run: `cd hermes-plugins/sign402-wallet && python -m unittest tests.test_plugin.PluginRegistrationTests.test_browse_catalog_schedules_nonblocking_country_warmup -v`

Expected: FAIL because `Browse Catalog` does not schedule a warm-up.

- [ ] **Step 3: Implement minimal warm-up**

After `_send_fixed_reply` in `_send_bitrefill_category_prompt`, schedule:

```python
def _warm_bitrefill_catalog(country: str) -> None:
    try:
        _client_factory().list_bitrefill_products(
            country=country,
            category="all",
            start=0,
            limit=_BITREFILL_CATALOG_PAGE_SIZE,
            include_international=True,
            include_test_products=False,
        )
    except Exception as exc:
        logger.info("Bitrefill catalog warm-up skipped error=%s", type(exc).__name__)
```

The reply must be queued before `_run_in_background(...)`. Do not modify session state from warm-up and do not send a warm-up failure message.

- [ ] **Step 4: Verify scheduling test GREEN**

Run the Step 2 command.

Expected: PASS.

- [ ] **Step 5: Update existing single-flight/cancellation test**

Execute and remove the warm-up callback before asserting the user-triggered `All` callback count. Assert that the warm-up and `All` calls use the same country/category payload but only the user-triggered operation can send catalog results.

- [ ] **Step 6: Add warm-up failure isolation test**

Make the client raise `GatewayClientError`, execute the warm-up callback, and assert the already-sent category prompt and session remain unchanged and no error message is sent.

- [ ] **Step 7: Run plugin tests and commit**

Run: `cd hermes-plugins/sign402-wallet && python -m unittest discover -s tests -v`

Expected: all plugin tests PASS.

```bash
git add hermes-plugins/sign402-wallet/__init__.py hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "perf: warm Bitrefill catalog in background"
```

### Task 4: Documentation, Full Verification, and Production Rollout

**Files:**
- Modify: `sign402-gateway/README.md`
- Modify: `hermes-plugins/sign402-wallet/README.md`
- Verify only: all files changed in Tasks 1-3

**Interfaces:**
- Consumes: completed cache and warm-up behavior.
- Produces: operator documentation and production evidence.

- [ ] **Step 1: Document behavior and settings**

Document the three non-secret settings, 10-minute fresh lifetime, stale fallback, persistent public-data path, and that product details/prices/purchases are never cached. Document that opening the category picker schedules a silent warm-up.

- [ ] **Step 2: Run documentation checks**

Run:

```bash
rg -n "CATALOG_CACHE|CATALOG_TIMEOUT|10.minute|warm" sign402-gateway/README.md hermes-plugins/sign402-wallet/README.md
git diff --check
```

Expected: settings and behavior are present; `git diff --check` has no output.

- [ ] **Step 3: Run the complete relevant test suites**

Run:

```bash
cd sign402-gateway && .venv/bin/python -m unittest discover -s tests -v
cd ../hermes-plugins/sign402-wallet && python -m unittest discover -s tests -v
```

Expected: all gateway and plugin tests PASS with no new warnings.

- [ ] **Step 4: Commit documentation**

```bash
git add sign402-gateway/README.md hermes-plugins/sign402-wallet/README.md
git commit -m "docs: describe Bitrefill catalog cache"
```

- [ ] **Step 5: Verify commit scope and preserve user changes**

Run:

```bash
git status --short
git diff HEAD^ --name-only
git diff -- sign402-gateway/.env.example sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
```

Expected: the user's pre-existing kill-switch changes remain present and unstaged; feature commits contain only the files explicitly listed in this plan.

- [ ] **Step 6: Push and fast-forward production**

Push `x402Bnkr` to both configured remotes, fast-forward `/home/hermes/apps/sign402`, run the server-side focused test suites, and restart `sign402-gateway` plus `hermes-gateway`. Do not print service environment or credentials.

- [ ] **Step 7: Warm the production NL catalog without a purchase**

Issue one authenticated, server-local read-only request to `/agent/list-bitrefill-products` for country `NL`, category `all`, start `0`, limit `8`, discarding the response body and printing only HTTP status and total time.

- [ ] **Step 8: Measure warm performance and service health**

Repeat read-only requests for `NL/all`, `NL/food`, and `NL/all` page two. Expected: HTTP 200 and warm responses below 100 ms on the server-local gateway. Verify both services are `active/running`, recent warning logs are empty, and local/remote/server commit hashes match.

- [ ] **Step 9: Report exact evidence**

Report cold/warm timings, test totals, commit hash, service health, and confirm explicitly that no purchase or transaction call was made.
