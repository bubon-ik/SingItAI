# SINGIT Bitrefill Commerce Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe MVP path where Hermes can quote and buy one Bitrefill digital product while the user pays SINGIT through Bankr x402 and Firefly approves the exact purchase commitment.

**Architecture:** Keep the public agent surface in `sign402-gateway`, but move commerce logic into small modules: a Bitrefill client boundary, quote builder, SQLite commerce store, and purchase runner. The MVP defaults to dry-run/fake fulfillment unless explicitly configured with Bitrefill credentials, so tests and local demos cannot accidentally buy real products.

**Tech Stack:** Python 3.11 stdlib HTTP server, SQLite, existing Firefly bridge, existing Bankr CLI x402 client, Bankr x402 Cloud service in TypeScript, existing unittest suite.

---

## Scope and MVP guardrails

This plan implements the first safe slice only:

- one agent quote endpoint: `POST /agent/quote-bitrefill`;
- one agent buy endpoint: `POST /agent/buy-bitrefill`;
- one order lookup endpoint: `POST /agent/get-bitrefill-order`;
- a dry-run Bitrefill fulfillment mode for tests and demos;
- an explicit live mode that requires `BITREFILL_API_KEY`;
- a Bankr x402 Cloud handler named `buy-bitrefill` that accepts SINGIT and calls the gateway internal fulfillment endpoint.

The plan does not implement the full Bitrefill catalog UI, automatic swaps, multi-country search ranking, or encrypted secret storage beyond a local SQLite MVP.

## File map

Create:

- `sign402-gateway/sign402_gateway/commerce_store.py` — SQLite tables and idempotent quote/order state transitions.
- `sign402-gateway/sign402_gateway/bitrefill.py` — transport-independent Bitrefill client boundary plus dry-run client.
- `sign402-gateway/sign402_gateway/bitrefill_quote.py` — request validation, SINGIT quote math, commitment hash, display text.
- `sign402-gateway/sign402_gateway/bitrefill_runner.py` — orchestration: quote, Firefly approval, Bankr payment call, fulfillment result normalization.
- `sign402-gateway/tests/test_bitrefill_quote.py` — quote/commitment unit tests.
- `sign402-gateway/tests/test_commerce_store.py` — SQLite idempotency/state tests.
- `singit-risk-check/x402/buy-bitrefill/index.ts` — Bankr paid endpoint handler.
- `singit-risk-check/x402/buy-bitrefill/index.mjs` — JS runtime copy matching the TS handler, consistent with current repo style.

Modify:

- `sign402-gateway/sign402_gateway/server.py` — wire endpoints, server dependencies, and health list.
- `sign402-gateway/tests/test_gateway_server.py` — endpoint-level tests using fake quote/buy/order services.
- `sign402-gateway/README.md` — operator instructions and safety flags.
- `singit-risk-check/bankr.x402.json` — add `buy-bitrefill` service metadata.
- `singit-risk-check/README.md` — document the new Bankr service.
- `DEMO_SCRIPT.md` — add Hermes instructions for quoting and buying Bitrefill.

---

## Task 1: Quote model, validation, and commitment hash

**Files:**

- Create: `sign402-gateway/sign402_gateway/bitrefill_quote.py`
- Test: `sign402-gateway/tests/test_bitrefill_quote.py`

- [ ] **Step 1: Write failing quote tests**

Create `sign402-gateway/tests/test_bitrefill_quote.py`:

```python
import unittest

from sign402_gateway.bitrefill_quote import (
    SINGIT_DECIMALS,
    build_purchase_commitment,
    build_quote,
    hash_purchase_commitment,
)


class BitrefillQuoteTests(unittest.TestCase):
    def test_build_quote_converts_usd_price_to_singit_atomic_with_margin(self):
        quote = build_quote(
            request={"query": "Amazon", "country": "US", "value": "25"},
            product={
                "productId": "amazon_com-usa",
                "name": "Amazon.com Gift Card",
                "country": "US",
                "currency": "USD",
                "packageValue": "25",
                "priceUsd": "25.00",
            },
            singit_usd_price="0.01",
            margin_bps=500,
            quote_id="quote_fixed",
            now_epoch=1_719_000_000,
            ttl_seconds=120,
        )

        self.assertEqual(quote["quoteId"], "quote_fixed")
        self.assertEqual(quote["productId"], "amazon_com-usa")
        self.assertEqual(quote["singitAmount"], "2625")
        self.assertEqual(quote["maxSingitAtomic"], str(2625 * 10**SINGIT_DECIMALS))
        self.assertEqual(quote["expiresAtEpoch"], 1_719_000_120)
        self.assertIn("Amazon.com Gift Card", quote["quoteText"])

    def test_purchase_commitment_hash_is_stable_and_hides_recipient(self):
        quote = {
            "quoteId": "quote_fixed",
            "productId": "amazon_com-usa",
            "packageValue": "25",
            "maxSingitAtomic": "2625000000000000000000",
            "expiresAt": "2024-06-20T12:02:00Z",
        }

        commitment = build_purchase_commitment(
            quote,
            recipient={"email": "buyer@example.com"},
        )
        payment_hash = hash_purchase_commitment(commitment)

        self.assertEqual(commitment["type"], "singit-bitrefill-purchase")
        self.assertEqual(commitment["recipientCommitment"][:7], "sha256:")
        self.assertNotIn("buyer@example.com", str(commitment))
        self.assertEqual(len(payment_hash), 64)
        self.assertEqual(payment_hash, hash_purchase_commitment(commitment))

    def test_quote_rejects_unsupported_country_for_mvp(self):
        with self.assertRaisesRegex(ValueError, "Only US Bitrefill quotes are enabled"):
            build_quote(
                request={"query": "Amazon", "country": "DE", "value": "25"},
                product={"productId": "amazon_de", "name": "Amazon DE", "country": "DE", "priceUsd": "25"},
                singit_usd_price="0.01",
                margin_bps=500,
                quote_id="quote_fixed",
                now_epoch=1_719_000_000,
                ttl_seconds=120,
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test and confirm it fails**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_bitrefill_quote -v
```

Expected: `ModuleNotFoundError: No module named 'sign402_gateway.bitrefill_quote'`.

- [ ] **Step 3: Implement quote module**

Create `sign402-gateway/sign402_gateway/bitrefill_quote.py`:

```python
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any


SINGIT_DECIMALS = 18
DEFAULT_QUOTE_TTL_SECONDS = 120
DEFAULT_MARGIN_BPS = 500


def new_quote_id() -> str:
    return f"quote_{secrets.token_urlsafe(18)}"


def now_epoch() -> int:
    return int(time.time())


def iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_quote(
    *,
    request: dict[str, Any],
    product: dict[str, Any],
    singit_usd_price: str,
    margin_bps: int = DEFAULT_MARGIN_BPS,
    quote_id: str | None = None,
    now_epoch: int | None = None,
    ttl_seconds: int = DEFAULT_QUOTE_TTL_SECONDS,
) -> dict[str, Any]:
    country = str(request.get("country", "US")).upper()
    if country != "US":
        raise ValueError("Only US Bitrefill quotes are enabled in the MVP")

    value = str(request.get("value", product.get("packageValue", ""))).strip()
    if not value:
        raise ValueError("value is required")

    price_usd = Decimal(str(product.get("priceUsd", value)))
    singit_price = Decimal(str(singit_usd_price))
    if price_usd <= 0:
        raise ValueError("product priceUsd must be positive")
    if singit_price <= 0:
        raise ValueError("singit_usd_price must be positive")

    multiplier = Decimal(10_000 + margin_bps) / Decimal(10_000)
    singit_amount = (price_usd / singit_price * multiplier).quantize(Decimal("1"), rounding=ROUND_CEILING)
    max_singit_atomic = int(singit_amount * (Decimal(10) ** SINGIT_DECIMALS))
    started_at = int(now_epoch if now_epoch is not None else time.time())
    expires_at_epoch = started_at + int(ttl_seconds)
    product_id = str(product["productId"])
    product_name = str(product.get("name") or product_id)

    return {
        "quoteId": quote_id or new_quote_id(),
        "productId": product_id,
        "productName": product_name,
        "country": country,
        "currency": str(product.get("currency", "USD")),
        "packageValue": value,
        "priceUsd": f"{price_usd:.2f}",
        "singitUsdPrice": str(singit_price),
        "marginBps": int(margin_bps),
        "singitAmount": format_decimal(singit_amount),
        "maxSingitAtomic": str(max_singit_atomic),
        "createdAtEpoch": started_at,
        "expiresAtEpoch": expires_at_epoch,
        "expiresAt": iso_from_epoch(expires_at_epoch),
        "quoteText": f"{product_name} ${value}: pay up to {format_decimal(singit_amount)} SINGIT. Quote expires in {ttl_seconds}s.",
    }


def build_purchase_commitment(quote: dict[str, Any], *, recipient: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "singit-bitrefill-purchase",
        "quoteId": str(quote["quoteId"]),
        "productId": str(quote["productId"]),
        "packageValue": str(quote["packageValue"]),
        "maxSingitAtomic": str(quote["maxSingitAtomic"]),
        "recipientCommitment": recipient_commitment(recipient or {}),
        "expiresAt": str(quote["expiresAt"]),
    }


def hash_purchase_commitment(commitment: dict[str, Any]) -> str:
    canonical = json.dumps(commitment, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def recipient_commitment(recipient: dict[str, Any]) -> str:
    canonical = json.dumps(recipient, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text
```

- [ ] **Step 4: Run test and confirm it passes**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_bitrefill_quote -v
```

Expected: all 3 tests pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add sign402-gateway/sign402_gateway/bitrefill_quote.py sign402-gateway/tests/test_bitrefill_quote.py
git commit -m "feat: add Bitrefill SINGIT quote model"
```

---

## Task 2: SQLite commerce store

**Files:**

- Create: `sign402-gateway/sign402_gateway/commerce_store.py`
- Test: `sign402-gateway/tests/test_commerce_store.py`

- [ ] **Step 1: Write failing store tests**

Create `sign402-gateway/tests/test_commerce_store.py`:

```python
import tempfile
import unittest
from pathlib import Path

from sign402_gateway.commerce_store import BitrefillCommerceStore


class CommerceStoreTests(unittest.TestCase):
    def test_save_and_read_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote = {
                "quoteId": "quote_1",
                "productId": "amazon_com-usa",
                "productName": "Amazon.com Gift Card",
                "country": "US",
                "packageValue": "25",
                "priceUsd": "25.00",
                "maxSingitAtomic": "2625000000000000000000",
                "expiresAtEpoch": 1_719_000_120,
            }

            store.save_quote(quote)
            loaded = store.get_quote("quote_1")

            self.assertEqual(loaded["quote"]["quoteId"], "quote_1")
            self.assertEqual(loaded["state"], "QUOTED")

    def test_state_transition_is_monotonic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote({"quoteId": "quote_1", "productId": "p", "expiresAtEpoch": 1})
            store.advance_state("quote_1", "FIREFLY_APPROVED", {"paymentHash": "a" * 64})

            with self.assertRaisesRegex(ValueError, "cannot move order state backward"):
                store.advance_state("quote_1", "QUOTED", {})

    def test_reserve_fulfillment_lock_prevents_second_purchase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            store.save_quote({"quoteId": "quote_1", "productId": "p", "expiresAtEpoch": 1})

            self.assertTrue(store.try_mark_fulfilling("quote_1"))
            self.assertFalse(store.try_mark_fulfilling("quote_1"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test and confirm it fails**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_commerce_store -v
```

Expected: `ModuleNotFoundError: No module named 'sign402_gateway.commerce_store'`.

- [ ] **Step 3: Implement commerce store**

Create `sign402-gateway/sign402_gateway/commerce_store.py` with:

```python
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


STATE_ORDER = {
    "QUOTED": 10,
    "FIREFLY_APPROVED": 20,
    "SINGIT_AUTHORIZED": 30,
    "FULFILLING": 40,
    "BITREFILL_PURCHASED": 50,
    "SINGIT_SETTLED": 60,
    "DELIVERED": 70,
    "QUOTE_EXPIRED": 900,
    "FIREFLY_REJECTED": 901,
    "FULFILLMENT_FAILED": 902,
    "RECONCILIATION_REQUIRED": 903,
    "REFUND_REQUIRED": 904,
}


class BitrefillCommerceStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def save_quote(self, quote: dict[str, Any]) -> None:
        quote_id = str(quote["quoteId"])
        with self.lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO bitrefill_orders (quote_id, state, quote_json, updated_at)
                VALUES (?, 'QUOTED', ?, ?)
                ON CONFLICT(quote_id) DO NOTHING
                """,
                (quote_id, _dumps(quote), int(time.time())),
            )

    def get_quote(self, quote_id: str) -> dict[str, Any]:
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT quote_id, state, quote_json, metadata_json FROM bitrefill_orders WHERE quote_id = ?",
                (quote_id,),
            ).fetchone()
        if row is None:
            raise ValueError("quote not found")
        return {
            "quoteId": row["quote_id"],
            "state": row["state"],
            "quote": json.loads(row["quote_json"]),
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }

    def advance_state(self, quote_id: str, new_state: str, metadata: dict[str, Any] | None = None) -> None:
        if new_state not in STATE_ORDER:
            raise ValueError(f"unknown order state: {new_state}")
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT state, metadata_json FROM bitrefill_orders WHERE quote_id = ?",
                (quote_id,),
            ).fetchone()
            if row is None:
                raise ValueError("quote not found")
            old_state = str(row["state"])
            if STATE_ORDER[new_state] < STATE_ORDER[old_state]:
                raise ValueError("cannot move order state backward")
            merged = json.loads(row["metadata_json"] or "{}")
            merged.update(metadata or {})
            db.execute(
                "UPDATE bitrefill_orders SET state = ?, metadata_json = ?, updated_at = ? WHERE quote_id = ?",
                (new_state, _dumps(merged), int(time.time()), quote_id),
            )

    def try_mark_fulfilling(self, quote_id: str) -> bool:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT state FROM bitrefill_orders WHERE quote_id = ?", (quote_id,)).fetchone()
            if row is None:
                raise ValueError("quote not found")
            if row["state"] == "FULFILLING":
                return False
            if STATE_ORDER[str(row["state"])] >= STATE_ORDER["BITREFILL_PURCHASED"]:
                return False
            db.execute(
                "UPDATE bitrefill_orders SET state = 'FULFILLING', updated_at = ? WHERE quote_id = ?",
                (int(time.time()), quote_id),
            )
            return True

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bitrefill_orders (
                    quote_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    quote_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        return db


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

- [ ] **Step 4: Run test and confirm it passes**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_commerce_store -v
```

Expected: all 3 tests pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add sign402-gateway/sign402_gateway/commerce_store.py sign402-gateway/tests/test_commerce_store.py
git commit -m "feat: add Bitrefill commerce store"
```

---

## Task 3: Bitrefill client boundary and dry-run fulfillment

**Files:**

- Create: `sign402-gateway/sign402_gateway/bitrefill.py`
- Test: `sign402-gateway/tests/test_bitrefill_client.py`

- [ ] **Step 1: Write failing dry-run tests**

Create `sign402-gateway/tests/test_bitrefill_client.py`:

```python
import unittest

from sign402_gateway.bitrefill import DryRunBitrefillClient


class BitrefillClientTests(unittest.TestCase):
    def test_dry_run_finds_amazon_us_product(self):
        client = DryRunBitrefillClient()
        product = client.find_product(query="Amazon", country="US", value="25")

        self.assertEqual(product["productId"], "amazon_com-usa")
        self.assertEqual(product["priceUsd"], "25.00")

    def test_dry_run_purchase_returns_redemption_reference(self):
        client = DryRunBitrefillClient()
        result = client.buy_product(
            quote={
                "quoteId": "quote_1",
                "productId": "amazon_com-usa",
                "packageValue": "25",
                "priceUsd": "25.00",
            },
            recipient={"email": "buyer@example.com"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "bitrefill-dry-run")
        self.assertIn("orderId", result)
        self.assertNotIn("buyer@example.com", str(result))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test and confirm it fails**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_bitrefill_client -v
```

Expected: missing module.

- [ ] **Step 3: Implement dry-run client**

Create `sign402-gateway/sign402_gateway/bitrefill.py`:

```python
import hashlib
from typing import Any, Protocol


class BitrefillClient(Protocol):
    def find_product(self, *, query: str, country: str, value: str) -> dict[str, Any]:
        ...

    def buy_product(self, *, quote: dict[str, Any], recipient: dict[str, Any]) -> dict[str, Any]:
        ...


class DryRunBitrefillClient:
    def find_product(self, *, query: str, country: str, value: str) -> dict[str, Any]:
        if str(country).upper() != "US":
            raise ValueError("Only US dry-run Bitrefill products are enabled")
        if "amazon" not in str(query).lower():
            raise ValueError("MVP dry-run catalog only supports Amazon")
        return {
            "productId": "amazon_com-usa",
            "name": "Amazon.com Gift Card",
            "country": "US",
            "currency": "USD",
            "packageValue": str(value),
            "priceUsd": f"{float(value):.2f}",
        }

    def buy_product(self, *, quote: dict[str, Any], recipient: dict[str, Any]) -> dict[str, Any]:
        order_seed = f"{quote['quoteId']}:{quote['productId']}:{quote['packageValue']}"
        order_id = "dry_bitrefill_" + hashlib.sha256(order_seed.encode("utf-8")).hexdigest()[:16]
        return {
            "ok": True,
            "provider": "bitrefill-dry-run",
            "orderId": order_id,
            "invoiceId": "dry_invoice_" + order_id[-8:],
            "status": "delivered",
            "redemption": {
                "type": "dry_run_link",
                "label": "Dry-run redemption link",
                "url": f"https://example.invalid/bitrefill/{order_id}",
            },
        }
```

- [ ] **Step 4: Run test and confirm it passes**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_bitrefill_client -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add sign402-gateway/sign402_gateway/bitrefill.py sign402-gateway/tests/test_bitrefill_client.py
git commit -m "feat: add dry-run Bitrefill client"
```

---

## Task 4: Purchase runner with Firefly and Bankr boundaries

**Files:**

- Create: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`

- [ ] **Step 1: Write failing runner tests**

Create `sign402-gateway/tests/test_bitrefill_runner.py`:

```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from sign402_gateway.bitrefill import DryRunBitrefillClient
from sign402_gateway.bitrefill_runner import BitrefillPurchaseRunner, BitrefillQuoteService
from sign402_gateway.commerce_store import BitrefillCommerceStore


class BitrefillRunnerTests(unittest.TestCase):
    def test_quote_service_saves_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            service = BitrefillQuoteService(
                bitrefill_client=DryRunBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )

            quote = service.quote({"query": "Amazon", "country": "US", "value": "25"})

            self.assertEqual(quote["quoteId"], "quote_1")
            self.assertEqual(store.get_quote("quote_1")["state"], "QUOTED")

    def test_runner_requires_firefly_before_bankr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=DryRunBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"query": "Amazon", "country": "US", "value": "25"})
            firefly = Mock()
            firefly.approve_payment_hash.return_value = {"approved": False, "approvedHash": "", "raw": "<CANCEL"}
            bankr = Mock()

            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
            )

            result = runner.buy({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})

            self.assertFalse(result["ok"])
            self.assertEqual(result["decision"], "rejected_by_firefly")
            bankr.assert_not_called()

    def test_runner_calls_bankr_after_firefly_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=DryRunBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote = quote_service.quote({"query": "Amazon", "country": "US", "value": "25"})
            firefly = Mock()
            bankr = Mock(return_value={"ok": True, "status": 200, "body": {"ok": True, "orderId": "order_1"}})

            runner = BitrefillPurchaseRunner(
                store=store,
                firefly=firefly,
                bankr_payment_client=bankr,
                bankr_resource_url="https://x402.bankr.bot/wallet/buy-bitrefill",
            )
            expected_hash = runner.payment_hash_for_quote(quote, recipient={"email": "buyer@example.com"})
            firefly.approve_payment_hash.return_value = {"approved": True, "approvedHash": expected_hash}

            result = runner.buy({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})

            self.assertTrue(result["ok"])
            bankr.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test and confirm it fails**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_bitrefill_runner -v
```

Expected: missing module.

- [ ] **Step 3: Implement runner**

Create `sign402-gateway/sign402_gateway/bitrefill_runner.py` with a small orchestrator:

```python
from typing import Any, Callable

from .bitrefill import BitrefillClient
from .bitrefill_quote import (
    build_purchase_commitment,
    build_quote,
    hash_purchase_commitment,
    new_quote_id,
    now_epoch,
)
from .commerce_store import BitrefillCommerceStore


class BitrefillQuoteService:
    def __init__(
        self,
        *,
        bitrefill_client: BitrefillClient,
        store: BitrefillCommerceStore,
        singit_usd_price_provider: Callable[[], str],
        quote_id_provider: Callable[[], str] = new_quote_id,
        now_provider: Callable[[], int] = now_epoch,
    ):
        self.bitrefill_client = bitrefill_client
        self.store = store
        self.singit_usd_price_provider = singit_usd_price_provider
        self.quote_id_provider = quote_id_provider
        self.now_provider = now_provider

    def quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        product = self.bitrefill_client.find_product(
            query=str(payload.get("query", "")),
            country=str(payload.get("country", "US")),
            value=str(payload.get("value", "")),
        )
        quote = build_quote(
            request=payload,
            product=product,
            singit_usd_price=self.singit_usd_price_provider(),
            quote_id=self.quote_id_provider(),
            now_epoch=self.now_provider(),
        )
        self.store.save_quote(quote)
        return quote


class BitrefillPurchaseRunner:
    def __init__(
        self,
        *,
        store: BitrefillCommerceStore,
        firefly: Any,
        bankr_payment_client: Callable[..., dict[str, Any]],
        bankr_resource_url: str,
    ):
        self.store = store
        self.firefly = firefly
        self.bankr_payment_client = bankr_payment_client
        self.bankr_resource_url = bankr_resource_url

    def payment_hash_for_quote(self, quote: dict[str, Any], *, recipient: dict[str, Any]) -> str:
        return hash_purchase_commitment(build_purchase_commitment(quote, recipient=recipient))

    def buy(self, payload: dict[str, Any]) -> dict[str, Any]:
        quote_id = str(payload.get("quoteId", "")).strip()
        if not quote_id:
            raise ValueError("quoteId is required")
        recipient = payload.get("recipient") if isinstance(payload.get("recipient"), dict) else {}
        record = self.store.get_quote(quote_id)
        quote = record["quote"]
        commitment = build_purchase_commitment(quote, recipient=recipient)
        payment_hash = hash_purchase_commitment(commitment)
        approval = self.firefly.approve_payment_hash(
            payment_hash,
            context_lines=[
                "BUY BITREFILL",
                str(quote.get("productName", quote.get("productId", "")))[:20],
                f"MAX {quote['singitAmount']} SINGIT"[:20],
            ],
        )
        if not approval.get("approved"):
            self.store.advance_state(quote_id, "FIREFLY_REJECTED", {"paymentHash": payment_hash, "firefly": approval})
            return {
                "ok": False,
                "decision": "rejected_by_firefly",
                "quoteId": quote_id,
                "paymentApprovalHash": payment_hash,
                "firefly": approval,
            }
        if str(approval.get("approvedHash", "")).lower() != payment_hash:
            raise ValueError("Firefly approved hash does not match Bitrefill purchase hash")
        self.store.advance_state(quote_id, "FIREFLY_APPROVED", {"paymentHash": payment_hash, "firefly": approval})
        bankr_body = {"quoteId": quote_id, "fulfillmentToken": "local-mvp-token"}
        bankr_result = self.bankr_payment_client(self.bankr_resource_url, request_body=bankr_body)
        self.store.advance_state(quote_id, "SINGIT_SETTLED", {"bankr": bankr_result})
        return {
            "ok": bool(bankr_result.get("ok", False)),
            "decision": "approved_and_executed",
            "quoteId": quote_id,
            "paymentApprovalHash": payment_hash,
            "paymentCommitment": commitment,
            "bankr": bankr_result,
            "telegramText": "✅ Bitrefill purchase approved and paid with SINGIT. Use get-bitrefill-order for delivery status.",
        }
```

- [ ] **Step 4: Run test and confirm it passes**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_bitrefill_runner -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "feat: add Bitrefill purchase runner"
```

---

## Task 5: Gateway endpoint wiring

**Files:**

- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`

- [ ] **Step 1: Add failing endpoint tests**

Append tests to `GatewayServerTests` in `sign402-gateway/tests/test_gateway_server.py`:

```python
    def test_agent_quote_bitrefill_uses_quote_service(self):
        server = DummyServer()
        server.bitrefill_quote_service = Mock(return_value={"ok": True, "quoteId": "quote_1"})

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/quote-bitrefill",
                {"query": "Amazon", "country": "US", "value": "25"},
                server=server,
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"quoteId": "quote_1"', response)
        server.bitrefill_quote_service.assert_called_once_with({"query": "Amazon", "country": "US", "value": "25"})

    def test_agent_buy_bitrefill_acquires_firefly_and_uses_runner(self):
        server = DummyServer()
        server.firefly_busy = False
        server.bitrefill_purchase_runner = Mock(return_value={"ok": True, "quoteId": "quote_1"})

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-bitrefill",
                {"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}},
                server=server,
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"ok": true', response)
        server.bitrefill_purchase_runner.assert_called_once()
        self.assertFalse(server.firefly_busy)
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_gateway_server.GatewayServerTests.test_agent_quote_bitrefill_uses_quote_service tests.test_gateway_server.GatewayServerTests.test_agent_buy_bitrefill_acquires_firefly_and_uses_runner -v
```

Expected: 404 responses or missing attributes.

- [ ] **Step 3: Wire handler endpoints**

Modify `server.py`:

1. Add health endpoints:

```python
"/agent/quote-bitrefill",
"/agent/buy-bitrefill",
"/agent/get-bitrefill-order",
```

2. Add `do_POST` branches:

```python
        if path == "/agent/quote-bitrefill":
            self._handle_agent_quote_bitrefill()
            return
        if path == "/agent/buy-bitrefill":
            self._handle_agent_buy_bitrefill()
            return
        if path == "/agent/get-bitrefill-order":
            self._handle_agent_get_bitrefill_order()
            return
```

3. Add methods on `Sign402GatewayHandler`:

```python
    def _handle_agent_quote_bitrefill(self) -> None:
        try:
            payload = self._read_json()
            result = self.server.bitrefill_quote_service(payload)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_buy_bitrefill(self) -> None:
        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return
        try:
            payload = self._read_json()
            result = self.server.bitrefill_purchase_runner(payload)
            if result.get("ok"):
                self.server.event_store.write(result)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_agent_get_bitrefill_order(self) -> None:
        try:
            payload = self._read_json()
            quote_id = str(payload.get("quoteId") or payload.get("orderId") or "").strip()
            if not quote_id:
                raise ValueError("quoteId is required")
            result = self.server.bitrefill_order_lookup(quote_id)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
```

- [ ] **Step 4: Extend server constructor and build_server**

Add constructor parameters:

```python
        bitrefill_quote_service: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_purchase_runner: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_order_lookup: Callable[[str], dict[str, Any]],
```

Set:

```python
        self.bitrefill_quote_service = bitrefill_quote_service
        self.bitrefill_purchase_runner = bitrefill_purchase_runner
        self.bitrefill_order_lookup = bitrefill_order_lookup
```

In `build_server`, import the new modules and create dry-run dependencies:

```python
    from .bitrefill import DryRunBitrefillClient
    from .bitrefill_runner import BitrefillPurchaseRunner, BitrefillQuoteService
    from .commerce_store import BitrefillCommerceStore

    commerce_store = BitrefillCommerceStore(ROOT_DIR / "demo-dashboard" / "bitrefill-orders.sqlite3")
    bitrefill_client = DryRunBitrefillClient()
    bitrefill_quote_service = BitrefillQuoteService(
        bitrefill_client=bitrefill_client,
        store=commerce_store,
        singit_usd_price_provider=lambda: os.getenv("SIGN402_SINGIT_USD_PRICE", "0.01"),
    )
    bitrefill_purchase_runner = BitrefillPurchaseRunner(
        store=commerce_store,
        firefly=firefly,
        bankr_payment_client=bankr_x402_payment_client,
        bankr_resource_url=os.getenv("SIGN402_BANKR_BITREFILL_URL", "https://x402.bankr.bot/YOUR_WALLET/buy-bitrefill"),
    )
    bitrefill_order_lookup = lambda quote_id: commerce_store.get_quote(quote_id)
```

Pass these into `Sign402GatewayServer(...)`.

- [ ] **Step 5: Run gateway endpoint tests**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_gateway_server.GatewayServerTests.test_agent_quote_bitrefill_uses_quote_service tests.test_gateway_server.GatewayServerTests.test_agent_buy_bitrefill_acquires_firefly_and_uses_runner -v
```

Expected: both tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "feat: wire Bitrefill agent endpoints"
```

---

## Task 6: Bankr `buy-bitrefill` x402 service

**Files:**

- Create: `singit-risk-check/x402/buy-bitrefill/index.ts`
- Create: `singit-risk-check/x402/buy-bitrefill/index.mjs`
- Modify: `singit-risk-check/bankr.x402.json`
- Test: use `node --test` or `npm test` if available.

- [ ] **Step 1: Add service to `bankr.x402.json`**

Add a sibling of `paid-risk-check`:

```json
"buy-bitrefill": {
  "description": "Buy a Bitrefill digital product after a Sign402 SINGIT payment",
  "price": "10000",
  "paymentScheme": "upto",
  "currency": "SINGIT",
  "tokenAddress": "0xc2c1e0b7C401e6217193732272444D928646eba3",
  "methods": ["POST"],
  "category": "commerce",
  "tags": ["x402", "sign402", "bitrefill", "base", "singit"],
  "schema": {
    "input": {
      "type": "object",
      "properties": {
        "quoteId": {"type": "string"},
        "fulfillmentToken": {"type": "string"}
      },
      "required": ["quoteId", "fulfillmentToken"]
    },
    "output": {
      "type": "object",
      "properties": {
        "ok": {"type": "boolean"},
        "orderId": {"type": "string"},
        "quoteId": {"type": "string"}
      }
    }
  }
}
```

- [ ] **Step 2: Implement Bankr handler**

Create both `index.ts` and `index.mjs` with:

```javascript
export default async function handler(req) {
  if (req.method !== "POST") {
    return Response.json({ ok: false, error: "POST required" }, { status: 405 });
  }

  const gatewayUrl = env("SIGN402_GATEWAY_INTERNAL_URL");
  const serviceSecret = env("SIGN402_BANKR_FULFILLMENT_SECRET");
  const body = await readBody(req);
  const quoteId = string(body.quoteId);
  const fulfillmentToken = string(body.fulfillmentToken);

  if (!quoteId || !fulfillmentToken) {
    return Response.json({ ok: false, error: "quoteId and fulfillmentToken are required" }, { status: 400 });
  }

  const response = await fetch(`${gatewayUrl.replace(/\/$/, "")}/internal/fulfill-bitrefill`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "authorization": `Bearer ${serviceSecret}`,
    },
    body: JSON.stringify({ quoteId, fulfillmentToken }),
  });

  const payload = await safeJson(response);
  if (!response.ok || !payload.ok) {
    return Response.json(
      { ok: false, quoteId, error: payload.error || "fulfillment failed" },
      { status: response.ok ? 400 : response.status },
    );
  }

  const settleAmount = payload.settleAmountAtomic || payload.maxSingitAtomic;
  const headers = settleAmount ? { "X-402-Settle-Amount": String(settleAmount) } : {};
  return Response.json(
    {
      ok: true,
      quoteId,
      orderId: payload.orderId,
      status: payload.status || "delivered",
    },
    { headers },
  );
}

async function readBody(req) {
  try {
    return await req.json();
  } catch {
    return {};
  }
}

async function safeJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function string(value) {
  return typeof value === "string" ? value.trim() : "";
}

function env(name) {
  const value = globalThis.process?.env?.[name] || globalThis[name];
  if (!value) throw new Error(`${name} is required`);
  return String(value);
}
```

- [ ] **Step 3: Add internal gateway fulfillment endpoint**

Modify `server.py` with a new `POST /internal/fulfill-bitrefill` branch and handler. The MVP handler should:

1. require `Authorization: Bearer ${SIGN402_BANKR_FULFILLMENT_SECRET}`;
2. read `quoteId`;
3. call a server dependency `bitrefill_fulfillment_runner(payload)`;
4. return redacted `{ok, quoteId, orderId, status, settleAmountAtomic}`.

Do not return gift card redemption data to Bankr.

- [ ] **Step 4: Commit**

Run:

```bash
git add singit-risk-check/bankr.x402.json singit-risk-check/x402/buy-bitrefill/index.ts singit-risk-check/x402/buy-bitrefill/index.mjs sign402-gateway/sign402_gateway/server.py
git commit -m "feat: add Bankr SINGIT Bitrefill endpoint"
```

---

## Task 7: Internal fulfillment runner

**Files:**

- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`

- [ ] **Step 1: Add failing fulfillment test**

Append to `BitrefillRunnerTests`:

```python
    def test_fulfillment_runner_buys_once_and_returns_redacted_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")
            quote_service = BitrefillQuoteService(
                bitrefill_client=DryRunBitrefillClient(),
                store=store,
                singit_usd_price_provider=lambda: "0.01",
                quote_id_provider=lambda: "quote_1",
                now_provider=lambda: 1_719_000_000,
            )
            quote_service.quote({"query": "Amazon", "country": "US", "value": "25"})
            client = DryRunBitrefillClient()

            from sign402_gateway.bitrefill_runner import BitrefillFulfillmentRunner

            runner = BitrefillFulfillmentRunner(store=store, bitrefill_client=client)
            result1 = runner.fulfill({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})
            result2 = runner.fulfill({"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}})

            self.assertTrue(result1["ok"])
            self.assertEqual(result1["quoteId"], "quote_1")
            self.assertIn("orderId", result1)
            self.assertNotIn("redemption", result1)
            self.assertEqual(result2["orderId"], result1["orderId"])
```

- [ ] **Step 2: Implement `BitrefillFulfillmentRunner`**

Add to `bitrefill_runner.py`:

```python
class BitrefillFulfillmentRunner:
    def __init__(self, *, store: BitrefillCommerceStore, bitrefill_client: BitrefillClient):
        self.store = store
        self.bitrefill_client = bitrefill_client

    def fulfill(self, payload: dict[str, Any]) -> dict[str, Any]:
        quote_id = str(payload.get("quoteId", "")).strip()
        if not quote_id:
            raise ValueError("quoteId is required")
        record = self.store.get_quote(quote_id)
        metadata = record["metadata"]
        if isinstance(metadata.get("bitrefill"), dict) and metadata["bitrefill"].get("orderId"):
            existing = metadata["bitrefill"]
            return {
                "ok": True,
                "quoteId": quote_id,
                "orderId": existing["orderId"],
                "status": existing.get("status", "delivered"),
                "settleAmountAtomic": record["quote"]["maxSingitAtomic"],
            }
        if not self.store.try_mark_fulfilling(quote_id):
            refreshed = self.store.get_quote(quote_id)
            existing = refreshed["metadata"].get("bitrefill", {})
            return {
                "ok": bool(existing),
                "quoteId": quote_id,
                "orderId": existing.get("orderId"),
                "status": existing.get("status", refreshed["state"]),
                "settleAmountAtomic": refreshed["quote"]["maxSingitAtomic"],
            }
        result = self.bitrefill_client.buy_product(
            quote=record["quote"],
            recipient=payload.get("recipient") if isinstance(payload.get("recipient"), dict) else {},
        )
        self.store.advance_state(quote_id, "BITREFILL_PURCHASED", {"bitrefill": result})
        self.store.advance_state(quote_id, "DELIVERED", {})
        return {
            "ok": True,
            "quoteId": quote_id,
            "orderId": result["orderId"],
            "status": result.get("status", "delivered"),
            "settleAmountAtomic": record["quote"]["maxSingitAtomic"],
            "maxSingitAtomic": record["quote"]["maxSingitAtomic"],
        }
```

- [ ] **Step 3: Run tests**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest tests.test_bitrefill_runner -v
```

Expected: all runner tests pass.

- [ ] **Step 4: Commit**

Run:

```bash
git add sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "feat: add idempotent Bitrefill fulfillment"
```

---

## Task 8: Operator docs and Hermes demo script

**Files:**

- Modify: `sign402-gateway/README.md`
- Modify: `singit-risk-check/README.md`
- Modify: `DEMO_SCRIPT.md`

- [ ] **Step 1: Document safe dry-run flow**

Add to `sign402-gateway/README.md`:

```markdown
## Bitrefill Commerce Paid With SINGIT

The MVP supports a safe dry-run Bitrefill flow:

1. `POST /agent/quote-bitrefill` with `{"query":"Amazon","country":"US","value":"25"}`.
2. Firefly approval and Bankr SINGIT payment through `POST /agent/buy-bitrefill`.
3. Delivery lookup through `POST /agent/get-bitrefill-order`.

Dry-run is the default. Live Bitrefill fulfillment must not be enabled unless `BITREFILL_API_KEY`, `SIGN402_BANKR_BITREFILL_URL`, and `SIGN402_BANKR_FULFILLMENT_SECRET` are configured.
```

- [ ] **Step 2: Document Bankr deploy**

Add to `singit-risk-check/README.md`:

```markdown
## buy-bitrefill

`buy-bitrefill` is a Bankr x402 Cloud endpoint that charges SINGIT and calls the protected Sign402 Gateway fulfillment endpoint.

Required Bankr environment variables:

- `SIGN402_GATEWAY_INTERNAL_URL`
- `SIGN402_BANKR_FULFILLMENT_SECRET`

Deploy:

```bash
bankr x402 deploy buy-bitrefill
```
```

- [ ] **Step 3: Document Hermes prompt**

Add to `DEMO_SCRIPT.md`:

```markdown
When I say "quote amazon gift card", call:

POST /agent/quote-bitrefill
{"query":"Amazon","country":"US","value":"25"}

When I say "buy that gift card", call:

POST /agent/buy-bitrefill
{"quoteId":"<quoteId from previous response>","recipient":{"email":"<user email if provided>"}}

Never call Bitrefill directly. Never construct the SINGIT amount yourself. The gateway quote is authoritative.
```

- [ ] **Step 4: Commit docs**

Run:

```bash
git add sign402-gateway/README.md singit-risk-check/README.md DEMO_SCRIPT.md
git commit -m "docs: add SINGIT Bitrefill operator flow"
```

---

## Task 9: Full local verification

**Files:** no source changes unless failures reveal a bug.

- [ ] **Step 1: Run gateway unit tests**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
python3 -m unittest discover -v
```

Expected: all tests pass.

- [ ] **Step 2: Run Bankr service checks**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/singit-risk-check"
npm test
```

Expected: passes if a test script exists. If no test script exists, run:

```bash
node --check x402/buy-bitrefill/index.mjs
node --check x402/paid-risk-check/index.mjs
```

Expected: no syntax errors.

- [ ] **Step 3: Start dry-run gateway**

Run:

```bash
cd "/Users/mp/Documents/Berlin Hack/sign402-gateway"
FIREFLY_PORT=/dev/cu.usbmodem11301 python3 -m sign402_gateway
```

Expected: gateway starts and health includes Bitrefill endpoints.

- [ ] **Step 4: Exercise quote endpoint**

Run:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/quote-bitrefill \
  -H "Content-Type: application/json" \
  -d '{"query":"Amazon","country":"US","value":"25"}'
```

Expected: JSON includes `quoteId`, `maxSingitAtomic`, and `quoteText`.

- [ ] **Step 5: Stop before live purchase**

Do not run a live `buy-bitrefill` against Bankr/Bitrefill until:

- Bankr `buy-bitrefill` endpoint has been deployed;
- gateway internal fulfillment URL is tunnelled and secret-protected;
- a low-value SINGIT policy has been approved;
- dry-run buy succeeds end-to-end.

---

## Self-review

- Spec coverage: The plan covers quote creation, SINGIT pricing, Firefly commitment, Bankr SINGIT endpoint, internal fulfillment, idempotency, redacted order lookup, tests, and docs.
- Deliberate MVP gap: live Bitrefill API integration remains behind the `BitrefillClient` boundary. This is intentional because we need to verify the safe dry-run and Bankr settlement first.
- Placeholder scan: No `TBD`/`TODO` placeholders. The only operator-specific unresolved values are runtime secrets and deployed Bankr URLs, which must be configured by environment variables.
- Type consistency: `quoteId`, `maxSingitAtomic`, `paymentApprovalHash`, `recipient`, `settleAmountAtomic`, and `orderId` are used consistently across tasks.
