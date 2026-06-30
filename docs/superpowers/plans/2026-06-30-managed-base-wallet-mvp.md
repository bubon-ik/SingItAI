# Managed Base Wallet MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build create-only managed Base/EVM wallets for Telegram users through the Sign402 Gateway, with encrypted private-key storage and spending disabled.

**Architecture:** Add a focused wallet service inside `sign402-gateway` that owns EVM key generation, encrypted storage, and safe metadata responses. Expose internal agent-facing HTTP endpoints that Hermes can call using Telegram user IDs. Keep signing, import, export, and real spending outside this implementation.

**Tech Stack:** Python 3.11+, SQLite, `cryptography.Fernet` for authenticated symmetric encryption, `eth-account` for EVM wallet generation, `unittest` for tests, existing `ThreadingHTTPServer` gateway patterns.

---

## File Structure

- Create `sign402-gateway/sign402_gateway/user_wallets.py`
  - Owns `UserWalletStore`, `ManagedBaseWalletService`, EVM key generation, encryption helpers, safe response formatting, and balance provider interface.
- Create `sign402-gateway/tests/test_user_wallets.py`
  - Unit tests for encrypted create-only wallet behavior and safe metadata.
- Modify `sign402-gateway/sign402_gateway/server.py`
  - Add `/agent/wallet`, `/agent/create-wallet`, and `/agent/wallet-balance`.
  - Wire `ManagedBaseWalletService` into `build_server`.
  - Add endpoints to `/health`.
- Modify `sign402-gateway/tests/test_gateway_server.py`
  - Focused server endpoint tests for missing Telegram user IDs, idempotent create, safe metadata, and balance degradation.
- Modify `sign402-gateway/pyproject.toml`
  - Add `cryptography` and `eth-account` dependencies.
- Modify `README.md` or `sign402-gateway/README.md`
  - Add operator notes for `SIGN402_WALLET_MASTER_KEY` and Telegram commands.

---

### Task 1: Add Wallet Dependencies

**Files:**
- Modify: `sign402-gateway/pyproject.toml`

- [ ] **Step 1: Update project dependencies**

Change:

```toml
dependencies = [
  "pyserial>=3.5"
]
```

to:

```toml
dependencies = [
  "cryptography>=42.0.0",
  "eth-account>=0.13.0",
  "pyserial>=3.5"
]
```

- [ ] **Step 2: Install editable gateway dependencies locally**

Run:

```bash
cd sign402-gateway
python3 -m pip install -e .
```

Expected: installation succeeds and imports for `cryptography` and `eth_account` are available.

- [ ] **Step 3: Commit dependency update**

```bash
git add sign402-gateway/pyproject.toml
git commit -m "Add managed wallet dependencies"
```

---

### Task 2: Write User Wallet Service Tests

**Files:**
- Create: `sign402-gateway/tests/test_user_wallets.py`
- Create later in Task 3: `sign402-gateway/sign402_gateway/user_wallets.py`

- [ ] **Step 1: Create failing tests**

Create `sign402-gateway/tests/test_user_wallets.py`:

```python
import base64
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from sign402_gateway.user_wallets import (
    ManagedBaseWalletService,
    UserWalletStore,
    WalletEncryptionError,
    build_wallet_service_from_env,
)


def test_master_key() -> str:
    return Fernet.generate_key().decode("ascii")


class UserWalletTests(unittest.TestCase):
    def make_service(self, master_key: str | None = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = UserWalletStore(Path(tmp.name) / "wallets.db")
        service = ManagedBaseWalletService(
            store=store,
            master_key=master_key or test_master_key(),
        )
        return service, store

    def test_create_wallet_encrypts_private_key_and_returns_safe_metadata(self):
        service, store = self.make_service()

        result = service.create_wallet(
            telegram_user_id="1045618308",
            telegram_username="AlpskyKnedlik",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        self.assertEqual(result["wallet"]["chain"], "base")
        self.assertEqual(result["wallet"]["spendingEnabled"], False)
        self.assertRegex(result["wallet"]["address"], r"^0x[a-fA-F0-9]{40}$")
        self.assertIn("Spending is disabled", result["telegramText"])
        self.assertNotIn("private", result["wallet"])

        row = store.get_wallet_by_telegram_user_id("1045618308")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["telegram_user_id"], "1045618308")
        self.assertEqual(row["telegram_username"], "AlpskyKnedlik")
        self.assertEqual(row["wallet_address"], result["wallet"]["address"])
        self.assertNotRegex(row["encrypted_private_key"], r"^0x[a-fA-F0-9]{64}$")

    def test_create_wallet_is_idempotent_for_same_telegram_user(self):
        service, _store = self.make_service()

        first = service.create_wallet("1045618308")
        second = service.create_wallet("1045618308")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["wallet"]["address"], second["wallet"]["address"])

    def test_wallet_status_without_wallet_returns_clear_message(self):
        service, _store = self.make_service()

        result = service.wallet_status("1045618308")

        self.assertFalse(result["ok"])
        self.assertEqual(result["wallet"], None)
        self.assertIn("No Base agent wallet yet", result["telegramText"])

    def test_wallet_status_existing_wallet_returns_safe_metadata(self):
        service, _store = self.make_service()
        created = service.create_wallet("1045618308")

        result = service.wallet_status("1045618308")

        self.assertTrue(result["ok"])
        self.assertEqual(result["wallet"]["address"], created["wallet"]["address"])
        self.assertEqual(result["wallet"]["spendingEnabled"], False)
        self.assertNotIn("private", str(result).lower())

    def test_missing_master_key_blocks_create(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = UserWalletStore(Path(tmp.name) / "wallets.db")
        service = ManagedBaseWalletService(store=store, master_key="")

        with self.assertRaisesRegex(WalletEncryptionError, "SIGN402_WALLET_MASTER_KEY"):
            service.create_wallet("1045618308")

    def test_build_wallet_service_from_env_accepts_master_key(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = build_wallet_service_from_env(
            env={"SIGN402_WALLET_MASTER_KEY": test_master_key()},
            store_path=Path(tmp.name) / "wallets.db",
        )

        result = service.create_wallet("1045618308")

        self.assertTrue(result["ok"])
        self.assertRegex(result["wallet"]["address"], r"^0x[a-fA-F0-9]{40}$")

    def test_invalid_master_key_fails_with_clear_error(self):
        service, _store = self.make_service(master_key=base64.urlsafe_b64encode(b"short").decode("ascii"))

        with self.assertRaisesRegex(WalletEncryptionError, "valid Fernet key"):
            service.create_wallet("1045618308")

    def test_balance_degrades_when_provider_is_not_configured(self):
        service, _store = self.make_service()
        created = service.create_wallet("1045618308")

        result = service.wallet_balance("1045618308")

        self.assertTrue(result["ok"])
        self.assertEqual(result["wallet"]["address"], created["wallet"]["address"])
        self.assertTrue(result["balanceUnavailable"])
        self.assertIn("Balance lookup is not configured", result["telegramText"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd sign402-gateway
python3 -m unittest tests.test_user_wallets -v
```

Expected: import failure because `sign402_gateway.user_wallets` does not exist.

- [ ] **Step 3: Commit failing tests**

```bash
git add sign402-gateway/tests/test_user_wallets.py
git commit -m "Test managed Base wallet service"
```

---

### Task 3: Implement User Wallet Service

**Files:**
- Create: `sign402-gateway/sign402_gateway/user_wallets.py`
- Test: `sign402-gateway/tests/test_user_wallets.py`

- [ ] **Step 1: Create wallet service implementation**

Create `sign402-gateway/sign402_gateway/user_wallets.py`:

```python
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from cryptography.fernet import Fernet, InvalidToken
from eth_account import Account


DEFAULT_USER_WALLET_STORE_PATH = Path.home() / ".sign402" / "user-wallets.db"


class WalletEncryptionError(RuntimeError):
    pass


class UserWalletStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def get_wallet_by_telegram_user_id(self, telegram_user_id: str) -> dict[str, Any] | None:
        with self.lock, self._database() as db:
            row = db.execute(
                """
                SELECT telegram_user_id, telegram_username, chain, wallet_address,
                       encrypted_private_key, status, created_at, updated_at
                FROM user_wallets
                WHERE telegram_user_id = ?
                """,
                (str(telegram_user_id),),
            ).fetchone()
        return _row_to_dict(row)

    def insert_wallet(
        self,
        *,
        telegram_user_id: str,
        telegram_username: str,
        chain: str,
        wallet_address: str,
        encrypted_private_key: str,
        status: str,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO user_wallets (
                    telegram_user_id, telegram_username, chain, wallet_address,
                    encrypted_private_key, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(telegram_user_id),
                    str(telegram_username or ""),
                    chain,
                    wallet_address,
                    encrypted_private_key,
                    status,
                    now,
                    now,
                ),
            )
        wallet = self.get_wallet_by_telegram_user_id(telegram_user_id)
        if wallet is None:
            raise RuntimeError("wallet insert failed")
        return wallet

    def record_audit_event(
        self,
        *,
        telegram_user_id: str,
        event_type: str,
        wallet_address: str = "",
        metadata_json: str = "{}",
    ) -> None:
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO user_wallet_audit (
                    telegram_user_id, event_type, wallet_address, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(telegram_user_id),
                    str(event_type),
                    str(wallet_address or ""),
                    str(metadata_json or "{}"),
                    int(time.time()),
                ),
            )

    def _init_db(self) -> None:
        with self._database() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_wallets (
                    telegram_user_id TEXT PRIMARY KEY,
                    telegram_username TEXT NOT NULL DEFAULT '',
                    chain TEXT NOT NULL,
                    wallet_address TEXT NOT NULL UNIQUE,
                    encrypted_private_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_wallet_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    wallet_address TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=5.0)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()


class ManagedBaseWalletService:
    def __init__(
        self,
        *,
        store: UserWalletStore,
        master_key: str,
        balance_provider: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.store = store
        self.master_key = str(master_key or "")
        self.balance_provider = balance_provider

    def create_wallet(
        self,
        telegram_user_id: str,
        telegram_username: str = "",
    ) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        existing = self.store.get_wallet_by_telegram_user_id(user_id)
        if existing is not None:
            self.store.record_audit_event(
                telegram_user_id=user_id,
                event_type="wallet_create_idempotent",
                wallet_address=existing["wallet_address"],
            )
            return _wallet_response(existing, created=False)

        fernet = self._fernet()
        account = Account.create()
        private_key = account.key.hex()
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        encrypted_private_key = fernet.encrypt(private_key.encode("utf-8")).decode("ascii")
        wallet = self.store.insert_wallet(
            telegram_user_id=user_id,
            telegram_username=telegram_username,
            chain="base",
            wallet_address=account.address,
            encrypted_private_key=encrypted_private_key,
            status="created",
        )
        self.store.record_audit_event(
            telegram_user_id=user_id,
            event_type="wallet_created",
            wallet_address=wallet["wallet_address"],
        )
        return _wallet_response(wallet, created=True)

    def wallet_status(self, telegram_user_id: str) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id)
        if wallet is None:
            return {
                "ok": False,
                "wallet": None,
                "telegramText": "No Base agent wallet yet. Send /create_wallet to create one.",
            }
        self.store.record_audit_event(
            telegram_user_id=user_id,
            event_type="wallet_status_read",
            wallet_address=wallet["wallet_address"],
        )
        return _wallet_response(wallet, created=False)

    def wallet_balance(self, telegram_user_id: str) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id)
        if wallet is None:
            return {
                "ok": False,
                "wallet": None,
                "balanceUnavailable": True,
                "telegramText": "No Base agent wallet yet. Send /create_wallet to create one.",
            }
        safe_wallet = _safe_wallet(wallet)
        self.store.record_audit_event(
            telegram_user_id=user_id,
            event_type="wallet_balance_read",
            wallet_address=wallet["wallet_address"],
        )
        if self.balance_provider is None:
            return {
                "ok": True,
                "wallet": safe_wallet,
                "balanceUnavailable": True,
                "telegramText": (
                    f"Base agent wallet: {safe_wallet['address']}\n\n"
                    "Balance lookup is not configured yet. Spending is disabled until iMessage approval is configured."
                ),
            }
        balances = self.balance_provider(wallet["wallet_address"])
        return {
            "ok": True,
            "wallet": safe_wallet,
            "balanceUnavailable": False,
            "balances": balances,
            "telegramText": _balance_text(safe_wallet["address"], balances),
        }

    def decrypt_private_key_for_future_signing(self, telegram_user_id: str) -> str:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id)
        if wallet is None:
            raise ValueError("wallet not found")
        try:
            return self._fernet().decrypt(
                wallet["encrypted_private_key"].encode("ascii")
            ).decode("utf-8")
        except InvalidToken as exc:
            raise WalletEncryptionError("wallet private key could not be decrypted") from exc

    def _fernet(self) -> Fernet:
        if not self.master_key:
            raise WalletEncryptionError(
                "SIGN402_WALLET_MASTER_KEY is required to create managed wallets"
            )
        try:
            return Fernet(self.master_key.encode("ascii"))
        except Exception as exc:
            raise WalletEncryptionError(
                "SIGN402_WALLET_MASTER_KEY must be a valid Fernet key"
            ) from exc


def build_wallet_service_from_env(
    *,
    env: dict[str, str],
    store_path: Path | None = None,
) -> ManagedBaseWalletService:
    return ManagedBaseWalletService(
        store=UserWalletStore(store_path or DEFAULT_USER_WALLET_STORE_PATH),
        master_key=env.get("SIGN402_WALLET_MASTER_KEY", ""),
    )


def _require_telegram_user_id(telegram_user_id: str) -> str:
    user_id = str(telegram_user_id or "").strip()
    if not user_id:
        raise ValueError("telegramUserId is required")
    return user_id


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _safe_wallet(wallet: dict[str, Any]) -> dict[str, Any]:
    return {
        "chain": str(wallet["chain"]),
        "address": str(wallet["wallet_address"]),
        "status": str(wallet["status"]),
        "spendingEnabled": False,
    }


def _wallet_response(wallet: dict[str, Any], *, created: bool) -> dict[str, Any]:
    safe_wallet = _safe_wallet(wallet)
    if created:
        text = (
            f"Your Base agent wallet is ready:\n{safe_wallet['address']}\n\n"
            "Fund this wallet with a small amount only.\n"
            "Spending is disabled until iMessage approval is configured."
        )
    else:
        text = (
            f"Your Base agent wallet:\n{safe_wallet['address']}\n\n"
            "Spending is disabled until iMessage approval is configured."
        )
    return {
        "ok": True,
        "created": created,
        "wallet": safe_wallet,
        "telegramText": text,
    }


def _balance_text(address: str, balances: dict[str, Any]) -> str:
    if not balances:
        return f"Base agent wallet: {address}\n\nNo balances found."
    lines = [f"Base agent wallet: {address}", "", "Balances:"]
    for symbol, value in sorted(balances.items()):
        lines.append(f"- {symbol}: {value}")
    lines.append("")
    lines.append("Spending is disabled until iMessage approval is configured.")
    return "\n".join(lines)
```

- [ ] **Step 2: Run user wallet tests**

Run:

```bash
cd sign402-gateway
python3 -m unittest tests.test_user_wallets -v
```

Expected: all user wallet tests pass.

- [ ] **Step 3: Commit service implementation**

```bash
git add sign402-gateway/sign402_gateway/user_wallets.py sign402-gateway/tests/test_user_wallets.py
git commit -m "Add managed Base wallet service"
```

---

### Task 4: Add Gateway Endpoint Tests

**Files:**
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify later in Task 5: `sign402-gateway/sign402_gateway/server.py`

- [ ] **Step 1: Add imports to server tests**

In `sign402-gateway/tests/test_gateway_server.py`, add to the existing import block from `sign402_gateway.server`:

```python
    build_server,
```

If `build_server` is already imported, do not add a duplicate.

- [ ] **Step 2: Add endpoint tests**

Append these tests to `GatewayServerTests`:

```python
    def test_agent_create_wallet_requires_telegram_user_id(self):
        server = DummyServer()
        server.user_wallet_service.create_wallet.side_effect = AssertionError(
            "should not create without user id"
        )
        handler = self.make_handler("/agent/create-wallet", {})

        with patch.object(handler, "server", server):
            handler.do_POST()

        response = handler.wfile.getvalue().decode()
        self.assertIn("telegramUserId is required", response)

    def test_agent_create_wallet_returns_safe_metadata(self):
        server = DummyServer()
        server.user_wallet_service.create_wallet.return_value = {
            "ok": True,
            "created": True,
            "wallet": {
                "chain": "base",
                "address": "0x1111111111111111111111111111111111111111",
                "status": "created",
                "spendingEnabled": False,
            },
            "telegramText": "Your Base agent wallet is ready",
        }
        handler = self.make_handler(
            "/agent/create-wallet",
            {"telegramUserId": "1045618308", "telegramUsername": "AlpskyKnedlik"},
        )

        with patch.object(handler, "server", server):
            handler.do_POST()

        response = handler.wfile.getvalue().decode()
        self.assertIn('"created": true', response)
        self.assertIn("0x1111111111111111111111111111111111111111", response)
        self.assertNotIn("private", response.lower())
        server.user_wallet_service.create_wallet.assert_called_once_with(
            telegram_user_id="1045618308",
            telegram_username="AlpskyKnedlik",
        )

    def test_agent_wallet_status_uses_user_wallet_service(self):
        server = DummyServer()
        server.user_wallet_service.wallet_status.return_value = {
            "ok": False,
            "wallet": None,
            "telegramText": "No Base agent wallet yet. Send /create_wallet to create one.",
        }
        handler = self.make_handler("/agent/wallet", {"telegramUserId": "1045618308"})

        with patch.object(handler, "server", server):
            handler.do_POST()

        response = handler.wfile.getvalue().decode()
        self.assertIn("No Base agent wallet yet", response)
        server.user_wallet_service.wallet_status.assert_called_once_with("1045618308")

    def test_agent_wallet_balance_degrades_safely(self):
        server = DummyServer()
        server.user_wallet_service.wallet_balance.return_value = {
            "ok": True,
            "wallet": {
                "chain": "base",
                "address": "0x1111111111111111111111111111111111111111",
                "status": "created",
                "spendingEnabled": False,
            },
            "balanceUnavailable": True,
            "telegramText": "Balance lookup is not configured yet.",
        }
        handler = self.make_handler("/agent/wallet-balance", {"telegramUserId": "1045618308"})

        with patch.object(handler, "server", server):
            handler.do_POST()

        response = handler.wfile.getvalue().decode()
        self.assertIn('"balanceUnavailable": true', response)
        self.assertIn("Balance lookup is not configured", response)
        server.user_wallet_service.wallet_balance.assert_called_once_with("1045618308")
```

- [ ] **Step 3: Add mock service to DummyServer**

Find the test `DummyServer` setup in `test_gateway_server.py` and add:

```python
        self.user_wallet_service = Mock()
```

Place it with the other service mocks.

- [ ] **Step 4: Run tests to verify endpoint tests fail**

Run:

```bash
cd sign402-gateway
python3 -m unittest tests.test_gateway_server.GatewayServerTests.test_agent_create_wallet_requires_telegram_user_id tests.test_gateway_server.GatewayServerTests.test_agent_create_wallet_returns_safe_metadata tests.test_gateway_server.GatewayServerTests.test_agent_wallet_status_uses_user_wallet_service tests.test_gateway_server.GatewayServerTests.test_agent_wallet_balance_degrades_safely -v
```

Expected: tests fail because the new routes do not exist in `server.py`.

- [ ] **Step 5: Commit failing endpoint tests**

```bash
git add sign402-gateway/tests/test_gateway_server.py
git commit -m "Test managed wallet gateway endpoints"
```

---

### Task 5: Implement Gateway Endpoints

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`

- [ ] **Step 1: Import wallet service builder**

Near other imports in `server.py`, add:

```python
from .user_wallets import (
    DEFAULT_USER_WALLET_STORE_PATH,
    build_wallet_service_from_env,
)
```

- [ ] **Step 2: Add health endpoints**

In the `/health` endpoint list, add:

```python
                        "/agent/wallet",
                        "/agent/create-wallet",
                        "/agent/wallet-balance",
```

Place them near the other `/agent/...` endpoints.

- [ ] **Step 3: Add POST route branches**

In `do_POST`, before Bitrefill routes, add:

```python
        if path == "/agent/wallet":
            self._handle_agent_wallet()
            return
        if path == "/agent/create-wallet":
            self._handle_agent_create_wallet()
            return
        if path == "/agent/wallet-balance":
            self._handle_agent_wallet_balance()
            return
```

- [ ] **Step 4: Add handler methods**

Inside `Sign402GatewayHandler`, near other `_handle_agent_*` methods, add:

```python
    def _handle_agent_wallet(self) -> None:
        try:
            payload = self._read_json()
            telegram_user_id = _telegram_user_id_from_payload(payload)
            result = self.server.user_wallet_service.wallet_status(telegram_user_id)
            self._send_json(result, status=200 if result.get("ok") else 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_create_wallet(self) -> None:
        try:
            payload = self._read_json()
            telegram_user_id = _telegram_user_id_from_payload(payload)
            telegram_username = str(payload.get("telegramUsername") or "").strip()
            result = self.server.user_wallet_service.create_wallet(
                telegram_user_id=telegram_user_id,
                telegram_username=telegram_username,
            )
            self._send_json(result)
        except Exception as exc:
            status = 503 if "SIGN402_WALLET_MASTER_KEY" in str(exc) else 400
            self._send_json({"ok": False, "error": str(exc)}, status=status)

    def _handle_agent_wallet_balance(self) -> None:
        try:
            payload = self._read_json()
            telegram_user_id = _telegram_user_id_from_payload(payload)
            result = self.server.user_wallet_service.wallet_balance(telegram_user_id)
            self._send_json(result, status=200 if result.get("ok") else 404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
```

- [ ] **Step 5: Add payload helper**

Near other helper functions in `server.py`, add:

```python
def _telegram_user_id_from_payload(payload: dict[str, Any]) -> str:
    telegram_user_id = str(
        payload.get("telegramUserId")
        or payload.get("telegram_user_id")
        or payload.get("userId")
        or ""
    ).strip()
    if not telegram_user_id:
        raise ValueError("telegramUserId is required")
    return telegram_user_id
```

- [ ] **Step 6: Extend server constructor**

Add `user_wallet_service` to `Sign402GatewayServer.__init__` parameters:

```python
        user_wallet_service,
```

Assign it:

```python
        self.user_wallet_service = user_wallet_service
```

- [ ] **Step 7: Wire service in `build_server`**

Add a parameter to `build_server`:

```python
    user_wallet_store_path: Path = DEFAULT_USER_WALLET_STORE_PATH,
```

Before constructing `Sign402GatewayServer`, create:

```python
    user_wallet_service = build_wallet_service_from_env(
        env=os.environ,
        store_path=user_wallet_store_path,
    )
```

Pass it into the server constructor:

```python
        user_wallet_service=user_wallet_service,
```

- [ ] **Step 8: Add CLI env/path option**

In `main()`, add:

```python
    parser.add_argument(
        "--user-wallet-store-path",
        type=Path,
        default=Path(os.getenv("SIGN402_USER_WALLET_STORE_PATH", DEFAULT_USER_WALLET_STORE_PATH)),
    )
```

Pass to `build_server`:

```python
        user_wallet_store_path=args.user_wallet_store_path,
```

Print after startup:

```python
    print(f"User wallet store path: {args.user_wallet_store_path}")
```

- [ ] **Step 9: Run focused endpoint tests**

Run:

```bash
cd sign402-gateway
python3 -m unittest tests.test_gateway_server.GatewayServerTests.test_agent_create_wallet_requires_telegram_user_id tests.test_gateway_server.GatewayServerTests.test_agent_create_wallet_returns_safe_metadata tests.test_gateway_server.GatewayServerTests.test_agent_wallet_status_uses_user_wallet_service tests.test_gateway_server.GatewayServerTests.test_agent_wallet_balance_degrades_safely -v
```

Expected: all four tests pass.

- [ ] **Step 10: Run user wallet tests**

Run:

```bash
cd sign402-gateway
python3 -m unittest tests.test_user_wallets -v
```

Expected: all user wallet tests pass.

- [ ] **Step 11: Commit endpoint implementation**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/sign402_gateway/user_wallets.py sign402-gateway/tests/test_gateway_server.py sign402-gateway/tests/test_user_wallets.py
git commit -m "Expose managed wallet gateway endpoints"
```

---

### Task 6: Add Operator Documentation

**Files:**
- Modify: `sign402-gateway/README.md`

- [ ] **Step 1: Add Managed Base Wallet section**

Add this section after the gateway setup section:

```markdown
## Managed Base Wallet MVP

The hosted Telegram bot can create one managed Base agent wallet per Telegram user.
This wallet is custodial and intended for small agent budgets only.

Required server secret:

```bash
python3 - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

Store the printed value in the gateway service environment:

```env
SIGN402_WALLET_MASTER_KEY=...
SIGN402_USER_WALLET_STORE_PATH=/home/hermes/.sign402/user-wallets.db
```

Agent-facing endpoints:

```text
POST /agent/wallet
POST /agent/create-wallet
POST /agent/wallet-balance
```

Example:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/create-wallet \
  -H "Content-Type: application/json" \
  -d '{"telegramUserId":"1045618308","telegramUsername":"AlpskyKnedlik"}'
```

The response never includes private key material. Spending remains disabled until
the iMessage approval provider and per-user spend limits are implemented.
```
```

- [ ] **Step 2: Run a markdown sanity read**

Run:

```bash
sed -n '/Managed Base Wallet MVP/,+45p' sign402-gateway/README.md
```

Expected: section renders with closed code fences.

- [ ] **Step 3: Commit docs**

```bash
git add sign402-gateway/README.md
git commit -m "Document managed Base wallet MVP"
```

---

### Task 7: Full Verification

**Files:**
- All modified gateway files.

- [ ] **Step 1: Run full gateway suite**

Run:

```bash
cd sign402-gateway
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 2: Verify no private key appears in test output**

Run:

```bash
cd sign402-gateway
python3 -m unittest tests.test_user_wallets -v 2>&1 | rg -i "private|0x[a-fA-F0-9]{64}" || true
```

Expected: no private key value is printed. Test names may include the word `private`; no raw `0x` private key should appear.

- [ ] **Step 3: Check working tree**

Run:

```bash
git status --short
```

Expected: clean working tree after commits.

---

### Task 8: Deploy To VPS

**Files on server:**
- `/home/hermes/apps/sign402/sign402-gateway`
- `/etc/systemd/system/sign402-gateway.service`

- [ ] **Step 1: Push branch from local machine**

Run locally:

```bash
git push singitai x402Bnkr
```

Expected: push succeeds.

- [ ] **Step 2: Pull on VPS**

Run on VPS:

```bash
cd ~/apps/sign402
git pull
```

Expected: latest wallet commits are pulled.

- [ ] **Step 3: Install updated Python dependencies**

Run on VPS:

```bash
cd ~/apps/sign402/sign402-gateway
. .venv/bin/activate
pip install -e .
```

Expected: `cryptography` and `eth-account` are installed in the gateway venv.

- [ ] **Step 4: Generate wallet master key**

Run on VPS:

```bash
cd ~/apps/sign402/sign402-gateway
. .venv/bin/activate
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

Expected: prints one Fernet key. Copy it for the next step.

- [ ] **Step 5: Add service environment**

Edit:

```bash
sudo nano /etc/systemd/system/sign402-gateway.service
```

Add to `[Service]`:

```ini
Environment=SIGN402_WALLET_MASTER_KEY=paste_generated_key_here
Environment=SIGN402_USER_WALLET_STORE_PATH=/home/hermes/.sign402/user-wallets.db
```

Keep existing disabled spend safety lines:

```ini
Environment=SIGN402_APPROVAL_PROVIDER=disabled
Environment=SIGN402_PAYMENT_EXECUTOR_MODE=disabled
```

- [ ] **Step 6: Restart Sign402 gateway**

Run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart sign402-gateway
sudo systemctl status sign402-gateway --no-pager
```

Expected: service is `active (running)`.

- [ ] **Step 7: Smoke test wallet endpoint**

Run:

```bash
curl -sS -X POST http://127.0.0.1:8099/agent/create-wallet \
  -H "Content-Type: application/json" \
  -d '{"telegramUserId":"1045618308","telegramUsername":"AlpskyKnedlik"}'
```

Expected: JSON includes `ok: true`, an address beginning with `0x`, and `spendingEnabled: false`.

- [ ] **Step 8: Restart Hermes Telegram gateway**

Run:

```bash
hermes gateway restart
hermes gateway status
```

Expected: gateway service is running.

---

### Task 9: Configure Hermes Wallet Instructions

**Files on server:**
- `~/.hermes/SOUL.md` or Hermes project instruction file selected by current Hermes setup.

- [ ] **Step 1: Add a short wallet instruction block**

Append this operator instruction to Hermes' active persona/project instruction file:

```markdown
## Sign402 Managed Wallet Commands

When the Telegram user asks about wallet setup:

- For `/wallet` or "show my wallet", call `POST http://127.0.0.1:8099/agent/wallet` with the Telegram user ID.
- For `/create_wallet` or "create wallet", call `POST http://127.0.0.1:8099/agent/create-wallet` with the Telegram user ID and username.
- For `/balance` or "wallet balance", call `POST http://127.0.0.1:8099/agent/wallet-balance` with the Telegram user ID.
- Reply with `telegramText` from the gateway when present.
- Never ask for a seed phrase or private key.
- Never claim spending is enabled. Managed wallet spending remains disabled until iMessage approval is configured.
```

- [ ] **Step 2: Restart Hermes gateway**

Run:

```bash
hermes gateway restart
```

Expected: restart succeeds.

- [ ] **Step 3: Telegram manual smoke test**

In Telegram, send:

```text
/wallet
```

Expected: Hermes says no wallet exists or shows the existing created wallet.

Then send:

```text
/create_wallet
```

Expected: Hermes returns the gateway `telegramText` with the Base address and disabled spending warning.

Then send:

```text
/balance
```

Expected: Hermes returns the address and says balance lookup is not configured if RPC is not configured.

---

## Self-Review

- Spec coverage: create-only Base/EVM wallet, encrypted storage, one wallet per Telegram user, safe metadata endpoints, missing master key behavior, disabled spending, docs, VPS rollout, and Telegram instructions are covered.
- Scope check: private key import/export, iMessage approval, and payment signing are explicitly excluded from implementation tasks.
- Type consistency: plan uses `telegramUserId`, `telegramUsername`, `wallet.address`, `spendingEnabled`, `balanceUnavailable`, and `telegramText` consistently across tests, service, server, and docs.
