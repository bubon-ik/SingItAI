# P0 Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make newly written Sign402 gateway state private, keep Bitrefill bearer values and redemption material out of persistent storage, and enforce one centralized incident kill switch before any transactional side effect.

**Architecture:** Add a focused `secure_state` module for atomic private JSON writes and Fernet encryption. Inject one `SensitiveStateCipher` into the user-purchase and Bitrefill commerce stores, enforce an allowlisted Bitrefill persistence boundary, refresh redemption only after authorization, and classify every transaction-oriented POST route in one gateway constant.

**Tech Stack:** Python 3.14, standard-library `unittest`, SQLite, `cryptography.fernet.Fernet`, `ThreadingHTTPServer`.

## Global Constraints

- Do not read, rewrite, chmod, migrate, rotate, or otherwise modify the real ignored `.env` files, current `demo-dashboard/bitrefill-orders.sqlite3`, wallet keys, or live state as part of implementation or tests.
- Preserve all pre-existing uncommitted user changes in the original worktree.
- Execute this plan in an isolated worktree from the committed documentation
  `HEAD` that contains this plan. That commit descends from `42664cd` but does
  not contain the user's unstaged kill-switch or single-reveal work.
- Reimplement the required kill-switch and single-reveal behavior in the
  isolated worktree from the tests and steps below; do not copy, stash, or
  otherwise mutate the user's partial work.
- Reuse `SIGN402_WALLET_MASTER_KEY`; do not introduce another encryption key.
- Never fall back to plaintext persistence when the key is missing, invalid, or ciphertext cannot be decrypted.
- Legacy plaintext token and recipient records remain readable, but an update that would reserialize them fails closed until the separately reviewed migration.
- Keep legacy payment routes disabled by default.
- No automated test may call Bitrefill, Bankr, CDP, a blockchain RPC, Firefly hardware, Telegram, WhatsApp, or Photon.
- Do not stage or commit real secrets, database files, redemption values, activation values, payment links, or generated test state.
- Start every shell block from the isolated worktree root unless that block
  explicitly says otherwise.

---

## File Map

- Create `sign402-gateway/sign402_gateway/secure_state.py`: private filesystem primitives and the shared Fernet wrapper.
- Create `sign402-gateway/tests/test_secure_state.py`: permissions, atomicity, and cipher tests.
- Modify `sign402-gateway/sign402_gateway/server.py`: encrypted user-purchase storage, private spend-state writes, cipher wiring, protected reveal clearing, and centralized kill switch.
- Modify `sign402-gateway/sign402_gateway/commerce_store.py`: private SQLite creation, encrypted recipient metadata, and strict Bitrefill provider/checkpoint snapshots.
- Modify `sign402-gateway/sign402_gateway/bitrefill_runner.py`: authorize before refresh, keep redemption in memory, and redact provider failures.
- Modify `sign402-gateway/sign402_gateway/bitrefill.py`: regenerate deterministic test redemption from a sanitized snapshot.
- Modify `sign402-gateway/sign402_gateway/bitrefill_mcp.py`: include normalized payment method and prevent raw treasury diagnostics from entering callbacks.
- Modify `sign402-gateway/tests/test_commerce_store.py`: raw-database, compatibility, and permission assertions.
- Modify `sign402-gateway/tests/test_bitrefill_runner.py`: protected on-demand reveal and no-persistence assertions.
- Modify `sign402-gateway/tests/test_bitrefill_mcp.py`: normalized provider-result and treasury-result assertions.
- Modify `sign402-gateway/tests/test_gateway_server.py`: user-store, token-clearing, and kill-switch route-matrix tests.
- Modify `sign402-gateway/.env.example`: clarify use of the master key and centralized pause semantics.
- Modify `sign402-gateway/SECURITY.md`: document encrypted state, legacy fail-closed behavior, and the deployment boundary.

---

### Task 0: Isolate Work and Record the No-Touch Baseline

**Files:**
- Metadata only: the original worktree and the five exact live-state targets
  below. Do not open or hash their contents.
- Create outside the repository: `/tmp/sign402-p0-live-state-before.txt`.

**Interfaces:**
- Consumes: the committed documentation `HEAD`, the
  `superpowers:using-git-worktrees` skill, and filesystem metadata only.
- Produces: an isolated `codex/p0-containment` worktree and a private
  before-manifest used only to prove that live state did not change.

- [ ] **Step 1: Create the isolated worktree**

Invoke `superpowers:using-git-worktrees`. Create branch
`codex/p0-containment` from the current committed `HEAD` containing this plan.
Do not stash, copy, stage, or clean files in the original `x402Bnkr` worktree.
Confirm that the new worktree has none of the original worktree's unstaged or
untracked files.

- [ ] **Step 2: Link the existing local test toolchain**

Ignored dependencies are intentionally absent from a new Git worktree. From
the isolated worktree root, create only these two ignored symlinks:

```bash
ln -s "/Users/mp/Documents/Berlin Hack/payment-executor/.venv" \
  payment-executor/.venv
ln -s "/Users/mp/Documents/Berlin Hack/cdp-x402-service/node_modules" \
  cdp-x402-service/node_modules
```

The targets are read-only inputs from the original checkout. Do not link or
copy any `.env`, database, state, key, or WIP file. Confirm `git status --short`
does not list either ignored symlink.

- [ ] **Step 3: Record a private metadata-only manifest**

From the original worktree, record only presence, mode, size, and mtime for
these exact paths:

```text
cdp-x402-service/.env
payment-executor/.env
sign402-gateway/.env.wallet-bitrefill
demo-dashboard/bitrefill-orders.sqlite3
demo-dashboard/user-purchases.json
```

Write the result to `/tmp/sign402-p0-live-state-before.txt` with mode `0600`.
Represent a missing target as `ABSENT`. Do not print file contents, secret
values, hashes, SQLite rows, or redemption material. `stat` is allowed;
`cat`, `shasum`, database clients, and any other content-reading command are
forbidden.

Use:

```bash
ORIGINAL_WORKTREE="/Users/mp/Documents/Berlin Hack"
STATE_MANIFEST="/tmp/sign402-p0-live-state-before.txt"
umask 077
: > "$STATE_MANIFEST"
for target in \
  "cdp-x402-service/.env" \
  "payment-executor/.env" \
  "sign402-gateway/.env.wallet-bitrefill" \
  "demo-dashboard/bitrefill-orders.sqlite3" \
  "demo-dashboard/user-purchases.json"; do
  if [ -e "$ORIGINAL_WORKTREE/$target" ]; then
    stat -f '%N|%Sp|%z|%m' "$ORIGINAL_WORKTREE/$target" \
      >> "$STATE_MANIFEST"
  else
    echo "$target|ABSENT" >> "$STATE_MANIFEST"
  fi
done
chmod 0600 "$STATE_MANIFEST"
```

- [ ] **Step 4: Establish the clean-code test baseline**

Run the complete Python and Node command set from Task 6 in the isolated
worktree before changing code. Record the clean committed test count and
command results. Expected: every suite passes. The earlier 676-test observation
included the dirty original worktree and is context, not an equality gate for
this clean worktree.

---

### Task 1: Private State and Cipher Primitives

**Files:**
- Create: `sign402-gateway/sign402_gateway/secure_state.py`
- Create: `sign402-gateway/tests/test_secure_state.py`

**Interfaces:**
- Consumes: `SIGN402_WALLET_MASTER_KEY` as a URL-safe Fernet key.
- Produces: `SensitiveStateError`, `SensitiveStateConfigurationError`, `SensitiveStateDecryptionError`, `SensitiveStateCipher`, `ensure_private_directory()`, `ensure_private_file()`, and `atomic_write_private_json()`.

- [ ] **Step 1: Write the failing secure-state tests**

Create `sign402-gateway/tests/test_secure_state.py`:

```python
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from sign402_gateway.secure_state import (
    SensitiveStateCipher,
    SensitiveStateConfigurationError,
    SensitiveStateDecryptionError,
    SensitiveStateError,
    atomic_write_private_json,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class SecureStateTests(unittest.TestCase):
    def test_atomic_write_uses_private_modes_under_umask_022(self):
        previous_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "private" / "state.json"
                observed_temp_modes: list[int] = []
                real_replace = os.replace

                def inspect_then_replace(source, target):
                    observed_temp_modes.append(mode(Path(source)))
                    real_replace(source, target)

                with patch(
                    "sign402_gateway.secure_state.os.replace",
                    side_effect=inspect_then_replace,
                ):
                    atomic_write_private_json(path, {"answer": 42})

                self.assertEqual(mode(path.parent), 0o700)
                self.assertEqual(observed_temp_modes, [0o600])
                self.assertEqual(mode(path), 0o600)
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    {"answer": 42},
                )
        finally:
            os.umask(previous_umask)

    def test_atomic_write_repairs_permissive_existing_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir(mode=0o755)
            path = parent / "state.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            os.chmod(path, 0o644)

            atomic_write_private_json(path, {"new": True})

            self.assertEqual(mode(parent), 0o700)
            self.assertEqual(mode(path), 0o600)

    def test_replace_failure_preserves_previous_document_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "state.json"
            atomic_write_private_json(path, {"version": 1})
            before = path.read_bytes()

            with patch(
                "sign402_gateway.secure_state.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    atomic_write_private_json(path, {"version": 2})

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_dangling_symlink_is_rejected_without_creating_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "state.json"
            path.parent.mkdir()
            outside = Path(tmp) / "outside.json"
            path.symlink_to(outside)

            with self.assertRaises(SensitiveStateError):
                atomic_write_private_json(path, {"secret": "value"})

            self.assertFalse(outside.exists())

    def test_cipher_round_trips_text_and_mapping(self):
        cipher = SensitiveStateCipher(Fernet.generate_key().decode("ascii"))

        encrypted_text = cipher.encrypt_text("reveal_secret")
        encrypted_json = cipher.encrypt_json({"email": "buyer@example.com"})

        self.assertNotIn("reveal_secret", encrypted_text)
        self.assertNotIn("buyer@example.com", encrypted_json)
        self.assertEqual(cipher.decrypt_text(encrypted_text), "reveal_secret")
        self.assertEqual(
            cipher.decrypt_json(encrypted_json),
            {"email": "buyer@example.com"},
        )

    def test_invalid_key_error_is_redacted(self):
        for secret in ("not-a-fernet-key", "не-ключ"):
            with self.subTest(secret=secret):
                with self.assertRaises(SensitiveStateConfigurationError) as captured:
                    SensitiveStateCipher(secret)
                self.assertIn(
                    "SIGN402_WALLET_MASTER_KEY",
                    str(captured.exception),
                )
                self.assertNotIn(secret, str(captured.exception))
                self.assertIsNone(captured.exception.__cause__)

    def test_invalid_ciphertext_and_non_object_json_fail_redacted(self):
        cipher = SensitiveStateCipher(Fernet.generate_key().decode("ascii"))
        for value in (
            "not-ciphertext",
            "не-шифротекст",
            cipher.encrypt_text('["not", "an", "object"]'),
        ):
            with self.subTest(value=value):
                with self.assertRaises(SensitiveStateDecryptionError) as captured:
                    cipher.decrypt_json(value)
                self.assertNotIn(value, str(captured.exception))
                self.assertIsNone(captured.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused tests and verify the RED state**

Run:

```bash
cd sign402-gateway
PYTHONPATH='.:../sign402-bridge:../payment-executor:../live-demo:../demo-resource-server' \
  ../payment-executor/.venv/bin/python -m unittest tests.test_secure_state -v
```

Expected: `ModuleNotFoundError: No module named 'sign402_gateway.secure_state'`.

- [ ] **Step 3: Implement the private state module**

Create `sign402-gateway/sign402_gateway/secure_state.py`:

```python
from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SensitiveStateError(RuntimeError):
    pass


class SensitiveStateConfigurationError(SensitiveStateError):
    pass


class SensitiveStateDecryptionError(SensitiveStateError):
    pass


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SensitiveStateError("sensitive state directory must not be a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    if _mode(path) != 0o700:
        raise SensitiveStateError("sensitive state directory is not mode 0700")


def ensure_private_file(path: Path) -> None:
    if path.is_symlink():
        raise SensitiveStateError("sensitive state file must not be a symlink")
    if not path.exists():
        return
    os.chmod(path, 0o600)
    if _mode(path) != 0o600:
        raise SensitiveStateError("sensitive state file is not mode 0600")


def atomic_write_private_json(
    path: Path,
    payload: Mapping[str, Any],
) -> None:
    serialized = (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    ensure_private_directory(path.parent)
    ensure_private_file(path)
    fd = -1
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_private_file(temp_path)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


class SensitiveStateCipher:
    def __init__(self, master_key: str):
        try:
            self._fernet = Fernet(str(master_key or "").encode("ascii"))
        except (TypeError, UnicodeEncodeError, ValueError):
            raise SensitiveStateConfigurationError(
                "SIGN402_WALLET_MASTER_KEY must be a valid Fernet key"
            ) from None

    def encrypt_text(self, value: str) -> str:
        return self._fernet.encrypt(str(value).encode("utf-8")).decode("ascii")

    def decrypt_text(self, value: str) -> str:
        try:
            return self._fernet.decrypt(str(value).encode("ascii")).decode("utf-8")
        except (
            InvalidToken,
            UnicodeDecodeError,
            UnicodeEncodeError,
            ValueError,
        ):
            raise SensitiveStateDecryptionError(
                "encrypted sensitive state could not be decrypted"
            ) from None

    def encrypt_json(self, value: Mapping[str, Any]) -> str:
        serialized = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return self.encrypt_text(serialized)

    def decrypt_json(self, value: str) -> dict[str, Any]:
        try:
            decoded = json.loads(self.decrypt_text(value))
        except (json.JSONDecodeError, SensitiveStateDecryptionError):
            raise SensitiveStateDecryptionError(
                "encrypted sensitive state could not be decrypted"
            ) from None
        if not isinstance(decoded, dict):
            raise SensitiveStateDecryptionError(
                "encrypted sensitive state could not be decrypted"
            ) from None
        return decoded
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2.

Expected: 7 tests run and `OK`.

- [ ] **Step 5: Commit the primitive**

```bash
git add sign402-gateway/sign402_gateway/secure_state.py sign402-gateway/tests/test_secure_state.py
git commit -m "feat: add private state primitives"
```

---

### Task 2: Encrypt User Purchase Tokens and Privatize JSON Stores

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`

**Interfaces:**
- Consumes: `SensitiveStateCipher` and `atomic_write_private_json()` from Task 1.
- Produces: `UserPurchaseStore(path, *, cipher=None)` with encrypted token
  persistence, legacy read compatibility, and fail-closed sensitive writes;
  private `UserSpendLimitStore` writes.

- [ ] **Step 1: Add failing user-store and spend-store tests**

Add imports:

```python
import stat

from cryptography.fernet import Fernet

from sign402_gateway.secure_state import (
    SensitiveStateCipher,
    SensitiveStateConfigurationError,
    SensitiveStateDecryptionError,
    SensitiveStateError,
)
```

Also add `UserPurchaseStore` to the existing import list from
`sign402_gateway.server`. Then add this helper and tests to
`GatewayServerTests`:

```python
    def state_cipher(self):
        return SensitiveStateCipher(Fernet.generate_key().decode("ascii"))

    def test_user_purchase_store_encrypts_token_at_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "user-purchases.json"
            store = UserPurchaseStore(path, cipher=self.state_cipher())
            event = {
                "ok": True,
                "quoteId": "q1",
                "fulfillmentToken": "reveal_secret_1",
            }

            observed_temp_documents: list[str] = []
            real_replace = os.replace

            def inspect_then_replace(source, target):
                observed_temp_documents.append(
                    Path(source).read_text(encoding="utf-8")
                )
                real_replace(source, target)

            with patch(
                "sign402_gateway.secure_state.os.replace",
                side_effect=inspect_then_replace,
            ):
                returned = store.write("1045618308", event)
            raw = path.read_text(encoding="utf-8")
            persisted = json.loads(raw)["1045618308"]

            self.assertIs(returned, event)
            self.assertNotIn("reveal_secret_1", raw)
            self.assertNotIn(
                "reveal_secret_1",
                "".join(observed_temp_documents),
            )
            self.assertNotIn("fulfillmentToken", persisted)
            self.assertIn("encryptedFulfillmentToken", persisted)
            self.assertEqual(store.read("1045618308"), event)
            self.assertNotIn(
                "encryptedFulfillmentToken",
                store.read("1045618308"),
            )
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_user_purchase_store_reads_legacy_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            legacy = (
                '{"1045618308":{"ok":true,"quoteId":"q1",'
                '"fulfillmentToken":"legacy_secret"}}\n'
            )
            path.write_text(legacy, encoding="utf-8")
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            loaded = store.read("1045618308")

            self.assertEqual(loaded["fulfillmentToken"], "legacy_secret")
            self.assertEqual(path.read_text(encoding="utf-8"), legacy)

    def test_user_purchase_store_refuses_to_copy_other_legacy_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            legacy = (
                '{"legacy-user":{"ok":true,'
                '"fulfillmentToken":"legacy_secret"}}\n'
            )
            path.write_text(legacy, encoding="utf-8")
            before = path.read_bytes()
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            with self.assertRaisesRegex(
                SensitiveStateError,
                "legacy plaintext fulfillment tokens must be migrated",
            ):
                store.write(
                    "new-user",
                    {"ok": True, "fulfillmentToken": "new_secret"},
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_user_purchase_store_invalid_envelope_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            path.write_text(
                '{"u":{"encryptedFulfillmentToken":"not-ciphertext"}}\n',
                encoding="utf-8",
            )
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            with self.assertRaises(SensitiveStateDecryptionError):
                store.read("u")

    def test_user_purchase_store_token_write_without_cipher_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            store = UserPurchaseStore(path)

            with self.assertRaises(
                SensitiveStateConfigurationError
            ):
                store.write(
                    "u",
                    {
                        "ok": True,
                        "fulfillmentToken": "reveal_secret",
                    },
                )

            self.assertFalse(path.exists())

    def test_user_purchase_store_encrypted_read_without_cipher_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            UserPurchaseStore(
                path,
                cipher=self.state_cipher(),
            ).write(
                "u",
                {
                    "ok": True,
                    "fulfillmentToken": "reveal_secret",
                },
            )

            with self.assertRaises(
                SensitiveStateConfigurationError
            ):
                UserPurchaseStore(path).read("u")

    def test_user_purchase_store_clear_removes_both_token_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            store = UserPurchaseStore(path, cipher=self.state_cipher())
            store.write(
                "u",
                {"ok": True, "fulfillmentToken": "reveal_secret"},
            )
            seeded = json.loads(path.read_text(encoding="utf-8"))
            seeded["u"]["fulfillmentToken"] = "legacy_secret"
            path.write_text(
                json.dumps(seeded) + "\n",
                encoding="utf-8",
            )

            store.clear_fulfillment_token("u")

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("reveal_secret", raw)
            self.assertNotIn("legacy_secret", raw)
            self.assertNotIn("encryptedFulfillmentToken", raw)
            self.assertNotIn("fulfillmentToken", store.read("u"))

    def test_user_purchase_store_clears_last_legacy_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            path.write_text(
                '{"u":{"ok":true,"fulfillmentToken":"legacy_secret"}}\n',
                encoding="utf-8",
            )
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            store.clear_fulfillment_token("u")

            self.assertNotIn(
                "legacy_secret",
                path.read_text(encoding="utf-8"),
            )
            self.assertNotIn("fulfillmentToken", store.read("u"))

    def test_user_purchase_store_clear_refuses_to_copy_other_legacy_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            original = (
                '{"u":{"encryptedFulfillmentToken":"not-read"},'
                '"other":{"fulfillmentToken":"other_legacy_secret"}}\n'
            )
            path.write_text(original, encoding="utf-8")
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            with self.assertRaises(SensitiveStateError):
                store.clear_fulfillment_token("u")

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_user_spend_limit_store_writes_private_state(self):
        previous_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "state" / "limits.json"
                store = UserSpendLimitStore(path)
                store.set_limit_settings(
                    "u",
                    max_per_tx_atomic=10,
                    daily_cap_atomic=100,
                    operator_max_per_tx_atomic=None,
                    operator_daily_cap_atomic=None,
                )
                self.assertEqual(
                    stat.S_IMODE(path.parent.stat().st_mode),
                    0o700,
                )
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        finally:
            os.umask(previous_umask)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_user_purchase_store_encrypts_token_at_rest \
  tests.test_gateway_server.GatewayServerTests.test_user_purchase_store_reads_legacy_without_rewrite \
  tests.test_gateway_server.GatewayServerTests.test_user_purchase_store_refuses_to_copy_other_legacy_token \
  tests.test_gateway_server.GatewayServerTests.test_user_purchase_store_invalid_envelope_fails_closed \
  tests.test_gateway_server.GatewayServerTests.test_user_purchase_store_token_write_without_cipher_fails_closed \
  tests.test_gateway_server.GatewayServerTests.test_user_purchase_store_encrypted_read_without_cipher_fails_closed \
  tests.test_gateway_server.GatewayServerTests.test_user_purchase_store_clear_removes_both_token_formats \
  tests.test_gateway_server.GatewayServerTests.test_user_purchase_store_clears_last_legacy_token \
  tests.test_gateway_server.GatewayServerTests.test_user_purchase_store_clear_refuses_to_copy_other_legacy_token \
  tests.test_gateway_server.GatewayServerTests.test_user_spend_limit_store_writes_private_state \
  -v
```

Expected: failures because `UserPurchaseStore` does not accept `cipher` and JSON stores still use `Path.write_text()`.

- [ ] **Step 3: Implement encrypted user-purchase persistence**

Import the Task 1 interfaces into `server.py`:

```python
from .secure_state import (
    SensitiveStateCipher,
    SensitiveStateConfigurationError,
    SensitiveStateError,
    atomic_write_private_json,
)
```

Replace `UserPurchaseStore` with:

```python
class UserPurchaseStore:
    def __init__(
        self,
        path: Path,
        *,
        cipher: SensitiveStateCipher | None = None,
    ) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.cipher = cipher

    def _read_all_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _assert_no_legacy_tokens(data: dict[str, Any]) -> None:
        if any(
            isinstance(item, dict) and "fulfillmentToken" in item
            for item in data.values()
        ):
            raise SensitiveStateError(
                "legacy plaintext fulfillment tokens must be migrated "
                "before updating user purchase state"
            )

    def _persisted_event(self, event: dict[str, Any]) -> dict[str, Any]:
        persisted = dict(event)
        persisted.pop("encryptedFulfillmentToken", None)
        if "fulfillmentToken" in persisted:
            if self.cipher is None:
                raise SensitiveStateConfigurationError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to persist fulfillment tokens"
                )
            token = str(persisted.pop("fulfillmentToken"))
            persisted["encryptedFulfillmentToken"] = self.cipher.encrypt_text(token)
        return persisted

    def write(
        self,
        telegram_user_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        key = str(telegram_user_id)
        with self.lock:
            data = self._read_all_unlocked()
            data[key] = self._persisted_event(event)
            self._assert_no_legacy_tokens(data)
            atomic_write_private_json(self.path, data)
            return event

    def read(self, telegram_user_id: str) -> dict[str, Any] | None:
        with self.lock:
            value = self._read_all_unlocked().get(str(telegram_user_id))
            if not isinstance(value, dict):
                return None
            event = dict(value)
            encrypted = event.pop("encryptedFulfillmentToken", None)
            if encrypted is not None:
                if self.cipher is None:
                    raise SensitiveStateConfigurationError(
                        "SIGN402_WALLET_MASTER_KEY is required "
                        "to read encrypted fulfillment tokens"
                    )
                event.pop("fulfillmentToken", None)
                event["fulfillmentToken"] = self.cipher.decrypt_text(str(encrypted))
            return event

    def clear_fulfillment_token(self, telegram_user_id: str) -> None:
        key = str(telegram_user_id)
        with self.lock:
            data = self._read_all_unlocked()
            event = data.get(key)
            if not isinstance(event, dict):
                return
            if (
                "fulfillmentToken" not in event
                and "encryptedFulfillmentToken" not in event
            ):
                return
            cleaned = dict(event)
            cleaned.pop("fulfillmentToken", None)
            cleaned.pop("encryptedFulfillmentToken", None)
            data[key] = cleaned
            self._assert_no_legacy_tokens(data)
            atomic_write_private_json(self.path, data)
```

Replace `UserSpendLimitStore._write_all_unlocked()` with:

```python
    def _write_all_unlocked(self, data: dict[str, Any]) -> None:
        atomic_write_private_json(self.path, data)
```

- [ ] **Step 4: Wire the cipher into user-purchase state**

Inside `build_server()`, before constructing the stores:

```python
    sensitive_state_key = os.environ.get(
        "SIGN402_WALLET_MASTER_KEY",
        "",
    ).strip()
    sensitive_state_cipher = (
        SensitiveStateCipher(sensitive_state_key)
        if sensitive_state_key
        else None
    )
    event_store = LatestEventStore(event_store_path)
    user_event_store = UserPurchaseStore(
        event_store_path.parent / "user-purchases.json",
        cipher=sensitive_state_cipher,
    )
    user_spend_limit_store = UserSpendLimitStore(user_spend_limit_store_path)
    agent_state_store = AgentStateStore(agent_state_path)
    bitrefill_commerce_store = BitrefillCommerceStore(
        bitrefill_commerce_store_path
    )
```

An invalid configured key fails full gateway startup. A missing key preserves
test/catalog-only startup, while `UserPurchaseStore` fails before any bearer
token write. Task 3 adds a managed-wallet purchase preflight that rejects a
missing cipher before approval or funding.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run the command from Step 2, then:

```bash
cd sign402-gateway
PYTHONPATH=. ../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server -v
```

Expected: all ten focused tests and the full gateway-server suite pass.

- [ ] **Step 6: Commit encrypted JSON stores**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "fix: encrypt gateway purchase state"
```

---

### Task 3: Secure the Bitrefill Commerce Persistence Boundary

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/sign402_gateway/commerce_store.py`
- Modify: `sign402-gateway/tests/test_commerce_store.py`
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py`

**Interfaces:**
- Consumes: `SensitiveStateCipher`, `SensitiveStateError`, `ensure_private_directory()`, and `ensure_private_file()`.
- Produces: `BitrefillCommerceStore(path, *, cipher=None)` with private SQLite
  creation, encrypted recipient metadata, compatible reads, and fail-closed
  updates for legacy plaintext recipient rows.

- [ ] **Step 1: Add failing persistence-boundary tests**

Add a generated test cipher and these tests to `test_commerce_store.py`:

```python
import json
import os
import sqlite3
import stat

from cryptography.fernet import Fernet

from sign402_gateway.secure_state import (
    SensitiveStateCipher,
    SensitiveStateDecryptionError,
    SensitiveStateError,
)


def test_cipher():
    return SensitiveStateCipher(Fernet.generate_key().decode("ascii"))


def raw_metadata(path: Path, quote_id: str) -> dict:
    with sqlite3.connect(path) as db:
        value = db.execute(
            "SELECT metadata_json FROM bitrefill_orders WHERE quote_id = ?",
            (quote_id,),
        ).fetchone()[0]
    return json.loads(value)
```

```python
    def test_new_recipient_is_encrypted_in_raw_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=test_cipher())
            store.save_quote(
                {
                    "quoteId": "q1",
                    "productId": "p1",
                    "packageId": "pkg1",
                    "packageValue": "10",
                    "expiresAtEpoch": 999,
                }
            )
            store.advance_state(
                "q1",
                "USER_APPROVED",
                {"recipient": {"email": "buyer@example.com"}},
            )

            raw = raw_metadata(path, "q1")
            self.assertNotIn("recipient", raw)
            self.assertNotIn("buyer@example.com", json.dumps(raw))
            self.assertIn("encryptedRecipient", raw)
            self.assertEqual(
                store.get_quote("q1")["metadata"]["recipient"],
                {"email": "buyer@example.com"},
            )
            self.assertNotIn(
                "encryptedRecipient",
                store.get_quote("q1")["metadata"],
            )
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_recipient_write_without_cipher_fails_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path)
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            before = store.get_quote("q1")

            with self.assertRaises(SensitiveStateError):
                store.advance_state(
                    "q1",
                    "USER_APPROVED",
                    {"recipient": {"email": "buyer@example.com"}},
                )

            self.assertEqual(store.get_quote("q1"), before)
            self.assertEqual(raw_metadata(path, "q1"), {})
            self.assertNotIn(
                "buyer@example.com",
                json.dumps(raw_metadata(path, "q1")),
            )

    def test_legacy_recipient_reads_but_row_update_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=test_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            legacy = {"recipient": {"email": "legacy@example.com"}}
            with sqlite3.connect(path) as db:
                db.execute(
                    "UPDATE bitrefill_orders SET metadata_json = ? "
                    "WHERE quote_id = ?",
                    (json.dumps(legacy), "q1"),
                )

            self.assertEqual(store.get_quote("q1")["metadata"], legacy)
            with self.assertRaisesRegex(
                SensitiveStateError,
                "legacy plaintext recipient must be migrated",
            ):
                store.advance_state("q1", "USER_APPROVED", {"paymentHash": "a" * 64})
            self.assertEqual(raw_metadata(path, "q1"), legacy)

```

Add:

```python
    def test_malformed_encrypted_recipient_never_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=test_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            seeded = {
                "encryptedRecipient": "not-ciphertext",
                "recipient": {"email": "legacy@example.com"},
            }
            with sqlite3.connect(path) as db:
                db.execute(
                    "UPDATE bitrefill_orders SET metadata_json = ? "
                    "WHERE quote_id = ?",
                    (json.dumps(seeded), "q1"),
                )

            with self.assertRaises(
                SensitiveStateDecryptionError
            ) as captured:
                store.get_quote("q1")
            self.assertNotIn(
                "legacy@example.com",
                str(captured.exception),
            )

    def test_sqlite_modes_are_private_under_umask_022(self):
        previous_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "private" / "orders.sqlite3"
                BitrefillCommerceStore(path, cipher=test_cipher())
                self.assertEqual(
                    stat.S_IMODE(path.parent.stat().st_mode),
                    0o700,
                )
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    0o600,
                )
        finally:
            os.umask(previous_umask)

    def test_existing_sqlite_modes_are_repaired_before_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            path = parent / "orders.sqlite3"
            BitrefillCommerceStore(path, cipher=test_cipher())
            os.chmod(parent, 0o755)
            os.chmod(path, 0o644)

            BitrefillCommerceStore(path, cipher=test_cipher())

            self.assertEqual(
                stat.S_IMODE(parent.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(path.stat().st_mode),
                0o600,
            )

    def test_dangling_sqlite_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir()
            path = parent / "orders.sqlite3"
            outside = Path(tmp) / "outside.sqlite3"
            path.symlink_to(outside)

            with self.assertRaises(SensitiveStateError):
                BitrefillCommerceStore(path, cipher=test_cipher())

            self.assertFalse(outside.exists())

    def test_try_mark_fulfilling_refuses_legacy_recipient_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            store = BitrefillCommerceStore(path, cipher=test_cipher())
            store.save_quote(
                {"quoteId": "q1", "productId": "p1", "expiresAtEpoch": 999}
            )
            legacy = {"recipient": {"email": "legacy@example.com"}}
            with sqlite3.connect(path) as db:
                db.execute(
                    "UPDATE bitrefill_orders SET metadata_json = ? "
                    "WHERE quote_id = ?",
                    (json.dumps(legacy), "q1"),
                )

            with self.assertRaises(SensitiveStateError):
                store.try_mark_fulfilling("q1")

            self.assertEqual(raw_metadata(path, "q1"), legacy)
            with sqlite3.connect(path) as db:
                state = db.execute(
                    "SELECT state FROM bitrefill_orders WHERE quote_id = ?",
                    ("q1",),
                ).fetchone()[0]
            self.assertEqual(state, "QUOTED")
```

Before constructing the store in
`test_initialization_closes_database_connection`, wrap the test in
`TemporaryDirectory()` and use `Path(tmp) / "orders.sqlite3"`. Never pass
`Path("orders.sqlite3")` after private-directory enforcement is introduced,
because its parent is the repository directory.

In `test_bitrefill_runner.py`, import
`SensitiveStateConfigurationError` and add:

```python
    def test_wallet_purchase_without_cipher_fails_before_approval_or_funding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BitrefillCommerceStore(
                Path(tmp) / "orders.sqlite3"
            )
            store.save_quote(
                {
                    "quoteId": "q1",
                    "productId": "p1",
                    "productName": "Product",
                    "expiresAtEpoch": 999,
                }
            )
            approval = Mock()
            funding = Mock()
            fulfillment = Mock()
            runner = WalletBitrefillPurchaseRunner(
                store=store,
                approval_client=approval,
                user_funding_runner=funding,
                fulfillment_runner=fulfillment,
                now_provider=lambda: 1,
            )

            with self.assertRaises(
                SensitiveStateConfigurationError
            ):
                runner(
                    {
                        "quoteId": "q1",
                        "telegramUserId": "u",
                        "recipient": {"email": "buyer@example.com"},
                    }
                )

            approval.assert_not_called()
            funding.assert_not_called()
            fulfillment.assert_not_called()
            self.assertEqual(store.get_quote("q1")["state"], "QUOTED")
```

- [ ] **Step 2: Run commerce tests and verify RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ../payment-executor/.venv/bin/python -m unittest \
  tests.test_commerce_store \
  tests.test_bitrefill_runner.BitrefillRunnerTests.test_wallet_purchase_without_cipher_fails_before_approval_or_funding \
  -v
```

Expected: constructor/signature failures, permissive SQLite modes, plaintext
recipient metadata, and unguarded legacy state transitions.

- [ ] **Step 3: Add private SQLite creation**

In `commerce_store.py`, add:

```python
from copy import deepcopy
import os

from .secure_state import (
    SensitiveStateCipher,
    SensitiveStateConfigurationError,
    SensitiveStateError,
    ensure_private_directory,
    ensure_private_file,
)
```

Keep the snapshot format unchanged in this task. Snapshot allowlisting moves
to Task 4 so the existing reveal behavior is not broken in an intermediate
commit.

- [ ] **Step 4: Encrypt recipient updates and protect legacy rows**

Change the constructor to:

```python
    def __init__(
        self,
        path: Path,
        *,
        cipher: SensitiveStateCipher | None = None,
    ):
        self.path = path
        self.cipher = cipher
        self.lock = threading.Lock()
        ensure_private_directory(self.path.parent)
        self._prepare_private_database_file()
        self._init_db()
        ensure_private_file(self.path)

    def require_sensitive_state_cipher(self) -> None:
        if self.cipher is None:
            raise SensitiveStateConfigurationError(
                "SIGN402_WALLET_MASTER_KEY is required "
                "for managed-wallet Bitrefill purchases"
            )
```

Now change the existing `build_server()` construction to:

```python
    bitrefill_commerce_store = BitrefillCommerceStore(
        bitrefill_commerce_store_path,
        cipher=sensitive_state_cipher,
    )
```

Add:

```python
    def _prepare_private_database_file(self) -> None:
        if self.path.exists():
            ensure_private_file(self.path)
            return
        try:
            fd = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
        except FileExistsError:
            ensure_private_file(self.path)
        else:
            os.close(fd)
            ensure_private_file(self.path)

    def _encoded_metadata_update(
        self,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        update = deepcopy(metadata)
        if "encryptedRecipient" in update:
            raise SensitiveStateError(
                "encryptedRecipient is reserved for the commerce store"
            )
        if "recipient" in update:
            if self.cipher is None:
                raise SensitiveStateError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to persist Bitrefill recipient state"
                )
            recipient = update.pop("recipient")
            if not isinstance(recipient, dict):
                raise ValueError("recipient must be an object")
            update["encryptedRecipient"] = self.cipher.encrypt_json(recipient)
        return update

    def _decoded_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        decoded = deepcopy(metadata)
        encrypted = decoded.pop("encryptedRecipient", None)
        if encrypted is not None:
            if self.cipher is None:
                raise SensitiveStateError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to read Bitrefill recipient state"
                )
            decoded.pop("recipient", None)
            decoded["recipient"] = self.cipher.decrypt_json(str(encrypted))
        return decoded
```

Update `get_quote()` to return `_decoded_metadata(raw_metadata)`.

Update `advance_state()` and `checkpoint()` before writing:

```python
            existing = json.loads(row["metadata_json"] or "{}")
            if "recipient" in existing:
                raise SensitiveStateError(
                    "legacy plaintext recipient must be migrated "
                    "before updating Bitrefill order state"
                )
            update = self._encoded_metadata_update(metadata or {})
            if "encryptedRecipient" in update:
                existing.pop("recipient", None)
            existing.update(update)
```

Persist only `existing`. Do not encode or decrypt unrelated existing metadata.

Keep `try_mark_fulfilling()` atomic across processes, but add this predicate
to its conditional `UPDATE`:

```sql
AND json_type(metadata_json, '$.recipient') IS NULL
```

When that update affects zero rows, select `state, metadata_json`. If the
metadata contains a top-level legacy `recipient`, raise the same
`SensitiveStateError`; otherwise preserve the existing `False` result. This
prevents the claim transition from journaling a legacy plaintext recipient
without weakening the single-winner SQL update.

In `WalletBitrefillPurchaseRunner.buy()`, call:

```python
        self.store.require_sensitive_state_cipher()
```

after reading and validating the quote but before spend enforcement, approval,
wallet decryption, user funding, or fulfillment. This makes the failing Step 1
test pass and is the boundary that lets catalog/test server startup work
without a key while preventing a configured managed-wallet purchase from
reaching a side effect.

- [ ] **Step 5: Update runner tests to use an explicit test cipher**

At the top of `test_bitrefill_runner.py` add:

```python
from cryptography.fernet import Fernet
from sign402_gateway.secure_state import (
    SensitiveStateCipher,
    SensitiveStateConfigurationError,
)


TEST_STATE_KEY = Fernet.generate_key().decode("ascii")


def test_store(path: Path) -> BitrefillCommerceStore:
    return BitrefillCommerceStore(
        path,
        cipher=SensitiveStateCipher(TEST_STATE_KEY),
    )
```

Mechanically replace `BitrefillCommerceStore(Path(tmp) / "orders.sqlite3")`
with `test_store(Path(tmp) / "orders.sqlite3")` throughout this test file.
Keep constructor-without-cipher coverage only in the focused commerce test
that proves sensitive writes fail closed.

- [ ] **Step 6: Run commerce and runner tests**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ../payment-executor/.venv/bin/python -m unittest \
  tests.test_commerce_store tests.test_bitrefill_runner -v
```

Expected: all tests pass; recipient round-trips in memory, raw SQLite contains
only `encryptedRecipient`, and every legacy-recipient mutation fails closed.

- [ ] **Step 7: Commit the commerce boundary**

```bash
git add \
  sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/sign402_gateway/commerce_store.py \
  sign402-gateway/tests/test_commerce_store.py \
  sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "fix: protect Bitrefill commerce state"
```

---

### Task 4: Refresh Redemption Only After Authorization

**Files:**
- Modify: `sign402-gateway/sign402_gateway/commerce_store.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_mcp.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/tests/test_commerce_store.py`
- Modify: `sign402-gateway/tests/test_bitrefill_mcp.py`
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`

**Interfaces:**
- Consumes: raw provider results and encrypted recipient state from Task 3.
- Produces: strict provider/checkpoint snapshots containing only `invoiceId`,
  `orderId`, normalized status, product/package identifiers, payment method,
  bounded timestamps, and safe treasury fields; authorized in-memory
  redemption; one-time reveal-token clearing only after non-empty redemption.

- [ ] **Step 1: Write failing snapshot and protected-refresh tests**

In `test_commerce_store.py`, add
`test_provider_and_checkpoint_snapshots_are_strict_allowlists()`. Write a
provider result and checkpoint containing unique markers in `redemption`,
`code`, `pin`, `activationUrl`, nested eSIM activation data, `paymentLink`,
`apiKey`, `stdout`, `stderr`, `command`, and `paymentInfo.address`. Assert
neither the forbidden keys nor their marker values occur in raw
`metadata_json` or any `orders.sqlite3*` sidecar. Assert the safe invoice ID,
order ID, normalized status, product/package IDs, payment method, timestamps,
and normalized treasury transaction fields remain.

In the same file add
`test_allowlisted_scalar_field_rejects_nested_secret_without_mutation()`.
Pass a mapping as `bitrefill.status`, expect a redacted `ValueError` naming
only the field, and assert raw metadata is unchanged. Add equivalent subtests
for a non-mapping `bitrefill` and `bitrefillCheckpoint`; these reserved fields
must fail closed rather than bypass sanitization.

In `test_bitrefill_runner.py`, add or rewrite these exact tests:

- `test_status_lookup_never_refreshes_provider`: seed
  `BITREFILL_PURCHASED` with a sanitized invoice snapshot, call lookup with
  `include_redemption=False`, and assert `refresh_purchase` was never called.
- `test_wrong_recipient_is_rejected_before_refresh`: seed an encrypted
  recipient, pass a different recipient, expect the existing authorization
  error, and assert zero provider calls.
- `test_wrong_token_is_rejected_before_refresh`: seed only a token hash, pass
  a wrong token, expect the token error, and assert zero provider calls.
- `test_authorized_refresh_returns_redemption_but_sqlite_stays_clean`: seed a
  valid invoice/token hash, return `READY-123` from the provider, assert the
  authorized response contains it, and scan raw SQLite plus sidecars to prove
  it was not persisted.
- `test_authorized_refresh_runs_for_delivered_order`: start from durable state
  `DELIVERED`, authorize with a token, and assert refresh still runs once so
  no persisted legacy redemption is needed.
- `test_refresh_without_redemption_does_not_claim_delivery`: return provider
  status `delivered` with empty redemption, assert the durable state and public
  `state` stay `BITREFILL_PURCHASED`, public `status` is not `delivered`, and assert
  `redemptionAvailable=False`.
- `test_refresh_failure_returns_redacted_retryable_result`: make refresh raise
  an exception containing `PROVIDER-SECRET`, assert the result has
  `redemptionUnavailable=True` without that marker, and assert SQLite remains
  marker-free.
- `test_test_client_regenerates_redemption_from_sanitized_snapshot`: pass the
  deterministic test client a snapshot with invoice/order IDs but no
  redemption and assert it deterministically recreates the test redemption
  from the quote.

Rewrite the existing
`test_order_lookup_can_reveal_redemption_when_recipient_matches`,
`test_order_lookup_requires_fulfillment_token_when_no_recipient_stored`,
`test_pending_order_refreshes_without_repurchase`, and
`test_order_lookup_rejects_redemption_reveal_for_wrong_recipient` fixtures so
they persist only sanitized invoice snapshots and obtain redemption from an
explicit fake provider. No test may seed persisted redemption as a supported
reveal source.

Add marker-based error tests for every persisted exception branch in
`BitrefillPurchaseRunner`, `WalletBitrefillPurchaseRunner`, and
`BitrefillFulfillmentRunner`: legacy Bankr payment, settlement/fulfillment,
managed-wallet funding, managed-wallet fulfillment, treasury funding, and
Bitrefill provider purchase. Give each fake exception a unique secret and
assert that marker appears in neither raw SQLite/sidecars nor the raised
public exception. Use the exact names
`test_bankr_payment_failure_is_redacted`,
`test_settlement_failure_is_redacted`,
`test_wallet_funding_failure_is_redacted`,
`test_wallet_fulfillment_failure_is_redacted`,
`test_treasury_funding_failure_is_redacted`, and
`test_provider_purchase_failure_is_redacted`.

Use this shape for the core persistence assertion:

```python
            result = lookup_bitrefill_order(
                store,
                "q1",
                include_redemption=True,
                fulfillment_token="reveal-token",
                bitrefill_client=provider,
            )

            self.assertEqual(
                result["redemption"]["value"]["code"],
                "READY-123",
            )
            for sidecar in path.parent.glob(f"{path.name}*"):
                self.assertNotIn("READY-123", sidecar.read_bytes().decode(
                    "utf-8",
                    errors="ignore",
                ))
```

Update the gateway happy-path mock to include:

```python
"redemption": {"value": {"code": "SECRET-CODE"}}
```

Pass the authenticated Telegram user ID into the helper and assert
`clear_fulfillment_token("1045618308")` is called exactly once on that happy
path.

Add three gateway tests asserting
`clear_fulfillment_token.assert_not_called()` for empty redemption,
`redemptionUnavailable=True`, and a ready-looking `telegramText` without an
actual redemption value. Add
`test_agent_last_purchase_after_reveal_hides_code()` to prove that an event
whose token was already cleared does not call the provider a second time.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ../payment-executor/.venv/bin/python -m unittest \
  tests.test_commerce_store \
  tests.test_bitrefill_mcp \
  tests.test_bitrefill_runner \
  tests.test_gateway_server.GatewayServerTests.test_agent_last_purchase_reveals_users_bitrefill_code \
  tests.test_gateway_server.GatewayServerTests.test_agent_last_purchase_does_not_clear_token_for_empty_redemption \
  tests.test_gateway_server.GatewayServerTests.test_agent_last_purchase_does_not_clear_token_when_refresh_is_unavailable \
  tests.test_gateway_server.GatewayServerTests.test_agent_last_purchase_does_not_clear_token_for_ready_text_without_redemption \
  tests.test_gateway_server.GatewayServerTests.test_agent_last_purchase_after_reveal_hides_code \
  -v
```

Expected: delivered snapshots cannot regenerate redemption, refresh occurs
before authorization or not at all for delivered rows, and token-clearing
logic trusts status text.

- [ ] **Step 3: Enforce strict provider snapshots at the store boundary**

In `commerce_store.py`, import `Mapping` and `Decimal`, then add a scalar-only
bounded value helper:

```python
from collections.abc import Mapping
from decimal import Decimal


def _bounded_scalar(value: Any, *, field: str, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(
        value,
        (str, int, float, Decimal),
    ):
        raise ValueError(f"{field} must be a scalar value")
    text = str(value).strip()
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return text
```

Build `sanitize_bitrefill_provider_snapshot(provider_result, quote)` from this
exact allowlist:

```text
provider
invoiceId
orderId
status
paymentMethod
createdAt
updatedAt
expiresAt
productId
packageId
packageValue
treasuryPayment.txId
treasuryPayment.network
treasuryPayment.asset
treasuryPayment.amount
treasuryPayment.amountAtomic
```

Normalize `status` to lowercase. Take product/package fields from the trusted
quote, not arbitrary provider copies. For treasury aliases, accept
`transactionHash`/`hash`, `chain`, `currency`/`token`, but store only the
canonical keys above. A present non-mapping treasury value fails with a
field-only error.

Build `sanitize_bitrefill_checkpoint(checkpoint, quote)` from this exact
allowlist:

```text
invoiceId
status
orderIds (at most 16 scalar values)
productId
packageId
packageValue
paymentInfo.amount
paymentInfo.asset
paymentInfo.network
treasuryPayment.txId
treasuryPayment.network
treasuryPayment.asset
treasuryPayment.amount
treasuryPayment.amountAtomic
```

Never retain `paymentInfo.address`. Present non-mapping `paymentInfo` or
`treasuryPayment`, a non-list `orderIds`, or a non-scalar allowlisted value
fails closed without including the value in the exception.

Change Task 3's `_encoded_metadata_update()` to also receive the trusted quote:

```python
    def _encoded_metadata_update(
        self,
        metadata: dict[str, Any],
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        update = deepcopy(metadata)
        if "encryptedRecipient" in update:
            raise SensitiveStateError(
                "encryptedRecipient is reserved for the commerce store"
            )
        if "recipient" in update:
            if self.cipher is None:
                raise SensitiveStateError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to persist Bitrefill recipient state"
                )
            recipient = update.pop("recipient")
            if not isinstance(recipient, dict):
                raise ValueError("recipient must be an object")
            update["encryptedRecipient"] = self.cipher.encrypt_json(recipient)
        for key, sanitizer in (
            ("bitrefill", sanitize_bitrefill_provider_snapshot),
            ("bitrefillCheckpoint", sanitize_bitrefill_checkpoint),
        ):
            if key not in update:
                continue
            value = update[key]
            if not isinstance(value, Mapping):
                raise ValueError(f"{key} must be an object")
            update[key] = sanitizer(value, quote)
        return update
```

Have `advance_state()` and `checkpoint()` select `quote_json`, decode it, and
pass it to `_encoded_metadata_update()`. Sanitize only the incoming update;
do not rewrite unrelated historical metadata. Run all sanitizers before the
SQL `UPDATE` so rejected values never enter a journal.

- [ ] **Step 4: Normalize MCP results before callbacks**

In `McpBitrefillClient._provider_result()` add:

```python
"paymentMethod": self.payment_method,
```

Replace the return value of `_pay_usdc_invoice()` with:

```python
        transfer = self.treasury_client.transfer_usdc(
            to_address=address,
            amount=format(payment_amount, "f"),
            chain="base",
        )
        if not isinstance(transfer, dict):
            raise ValueError("Bitrefill treasury transfer result is invalid")
        transaction_hash = str(
            transfer.get("txId")
            or transfer.get("transactionHash")
            or transfer.get("hash")
            or ""
        ).strip()
        if not transaction_hash:
            raise ValueError("Bitrefill treasury transfer hash is missing")
        return {
            "txId": transaction_hash,
            "network": "base",
            "asset": "USDC",
            "amount": format(payment_amount, "f"),
        }
```

Update MCP tests to assert that fake `command`, `stdout`, `stderr`, payment
link, and credential markers are absent from the callback and provider result.

- [ ] **Step 5: Make the deterministic test provider regenerate redemption**

Replace `TestBitrefillClient.refresh_purchase()` with:

```python
    def refresh_purchase(
        self,
        provider_result: dict[str, Any],
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        refreshed = deepcopy(provider_result)
        refreshed.update(
            {
                "ok": True,
                "provider": "bitrefill-test",
                "status": "delivered",
                "redemption": {
                    "type": "test",
                    "label": "Bitrefill test fulfillment",
                    "value": "TEST-REDEMPTION-NO-VALUE",
                },
            }
        )
        return refreshed
```

- [ ] **Step 6: Refactor order lookup to authorize before refresh**

In `lookup_bitrefill_order()`:

1. Build the status-only response from the persisted snapshot.
2. Return immediately when `include_redemption=False`.
3. Validate recipient/token before any provider call.
4. Require a client and non-empty `invoiceId`; otherwise return status plus
   `redemptionUnavailable=True`.
5. Refresh in a `try` block; on exception return the same redacted unavailable
   result without `str(exc)`.
6. Pass the raw refreshed result to
   `store.checkpoint(quote_id, {"bitrefill": refreshed})`; the Task 4 store
   boundary sanitizes it.
7. Use the raw in-memory redemption for the authorized response.
8. Advance to `DELIVERED` only when `_provider_is_delivered()` is true and the
   redemption detail is non-empty.

Use:

```python
    persisted_status = str(
        provider_result.get("status", record["state"].lower())
    )
    public_status = persisted_status
    if (
        persisted_status.strip().lower() == "delivered"
        and record["state"] != "DELIVERED"
    ):
        public_status = record["state"].lower()
    status_result = {
        "ok": True,
        "quoteId": record["quoteId"],
        "state": record["state"],
        "productId": quote.get("productId"),
        "productName": quote.get("productName"),
        "packageValue": quote.get("packageValue"),
        "orderId": provider_result.get("orderId"),
        "status": public_status,
    }
    if not include_redemption:
        return status_result

    stored_recipient = metadata.get("recipient")
    recipient_ok = (
        isinstance(stored_recipient, dict)
        and bool(stored_recipient)
        and recipient == stored_recipient
    )
    token_ok = _fulfillment_token_matches(metadata, fulfillment_token)
    if not (recipient_ok or token_ok):
        if stored_recipient:
            raise ValueError("recipient does not match order")
        raise ValueError("valid fulfillmentToken is required to reveal redemption")

    if (
        bitrefill_client is None
        or not str(provider_result.get("invoiceId") or "").strip()
    ):
        return {**status_result, "redemptionUnavailable": True}
    try:
        refreshed = bitrefill_client.refresh_purchase(provider_result, quote)
        if not isinstance(refreshed, dict):
            raise ValueError("Bitrefill refresh must return an object")
        store.checkpoint(quote_id, {"bitrefill": refreshed})
    except Exception:
        return {**status_result, "redemptionUnavailable": True}

    redemption = refreshed.get("redemption")
    provider_delivered = _provider_is_delivered(refreshed, quote)
    if not provider_delivered or not _redemption_detail_text(redemption):
        return {
            **status_result,
            "redemptionAvailable": False,
        }
    store.advance_state(quote_id, "DELIVERED", {})
    result = {
        **status_result,
        "state": "DELIVERED",
        "orderId": refreshed.get("orderId", status_result["orderId"]),
        "status": refreshed.get("status", "delivered"),
        "redemption": deepcopy(redemption),
    }
    result["telegramText"] = _bitrefill_delivery_telegram_text(
        quote,
        redemption=redemption,
    )
    return result
```

Do not use persisted legacy redemption as a reveal source. Legacy rows without
an invoice ID remain status-only until the controlled migration/reconciliation
step.

- [ ] **Step 7: Redact all Bitrefill-flow exception branches**

Replace every persisted `str(exc)` in the three Bitrefill purchase/fulfillment
runners with a fixed category, and re-raise a redacted `ValueError` with
`from None`:

```text
BitrefillPurchaseRunner Bankr payment:
  Bankr payment request failed

BitrefillPurchaseRunner settlement/fulfillment:
  Bitrefill settlement or fulfillment failed

WalletBitrefillPurchaseRunner user funding:
  Managed-wallet funding request failed

WalletBitrefillPurchaseRunner fulfillment:
  Bitrefill fulfillment request failed

BitrefillFulfillmentRunner treasury funding:
  Bitrefill funding request failed

BitrefillFulfillmentRunner provider purchase:
  Bitrefill provider request failed
```

For example, the final branch becomes:

```python
        except Exception:
            self.store.advance_state(
                quote_id,
                "FULFILLMENT_FAILED",
                {"fulfillmentError": "Bitrefill provider request failed"},
            )
            raise ValueError("Bitrefill provider request failed") from None
```

Keep the existing reconciliation states and non-exception metadata, but never
persist or return original exception text. Make every marker test from Step 1
pass.

- [ ] **Step 8: Implement single reveal and clear only for real redemption**

The clean base does not contain the user's unstaged single-reveal work.
Explicitly make these changes in `server.py`:

```python
def _has_nonempty_redemption_value(redemption: Any) -> bool:
    if not isinstance(redemption, dict) or "value" not in redemption:
        return False

    def has_value(value: Any) -> bool:
        if isinstance(value, dict):
            return any(has_value(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(has_value(item) for item in value)
        return bool(str(value or "").strip())

    return has_value(redemption["value"])
```

Pass `telegram_user_id` from `_handle_agent_last_purchase()` into
`_last_bitrefill_purchase_response()`, and add it as the helper's third
argument. For a Bitrefill event whose fulfillment token is already absent,
add this branch without calling the provider:

```python
    quote_id = str(event.get("quoteId", "") or "").strip()
    if not quote_id or "bitrefill" not in event:
        return None
    fulfillment_token = str(
        event.get("fulfillmentToken", "") or ""
    ).strip()
    if not fulfillment_token:
        product_name = str(
            event.get("productName") or "Your Bitrefill order"
        )
        return {
            "ok": True,
            "telegramText": (
                f"{product_name} was already delivered. For security its "
                "redemption code can only be revealed once — check where "
                "you saved it."
            ),
            "quoteId": quote_id,
        }
```

After the authorized lookup, compute:

```python
    redemption = order.get("redemption")
    has_redemption = _has_nonempty_redemption_value(redemption)
    telegram_text = order.get("telegramText")
    has_reveal_text = isinstance(telegram_text, str) and bool(
        telegram_text.strip()
    )
```

If either condition is false, return the processing/retry message and do not
clear. A ready-looking `telegramText` without actual redemption must never
count as a reveal. Only after both are true call:

```python
server.user_event_store.clear_fulfillment_token(telegram_user_id)
```

Keep the response contract unchanged and never include the raw redemption
object as a separate public field.

- [ ] **Step 9: Run focused tests and verify GREEN**

Run the command from Step 2, then:

```bash
cd sign402-gateway
PYTHONPATH=. ../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server -v
```

Expected: focused and full gateway suites pass and every raw SQLite/sidecar
scan remains free of the unique redemption markers.

- [ ] **Step 10: Commit protected redemption refresh**

```bash
git add \
  sign402-gateway/sign402_gateway/commerce_store.py \
  sign402-gateway/sign402_gateway/bitrefill.py \
  sign402-gateway/sign402_gateway/bitrefill_mcp.py \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/tests/test_commerce_store.py \
  sign402-gateway/tests/test_bitrefill_mcp.py \
  sign402-gateway/tests/test_bitrefill_runner.py \
  sign402-gateway/tests/test_gateway_server.py
git commit -m "fix: keep Bitrefill redemption out of state"
```

---

### Task 5: Centralize the Transaction Kill Switch

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`

**Interfaces:**
- Consumes: `SIGN402_PURCHASES_PAUSED`.
- Produces: `FUND_MOVING_POST_PATHS` and one pre-dispatch pause guard.

- [ ] **Step 1: Add the failing route-matrix test**

Import `FUND_MOVING_POST_PATHS` and `_purchases_paused`, then add:

```python
    def test_kill_switch_blocks_every_transaction_route_before_dispatch(self):
        routes = {
            "/approve-payment": "_handle_approve_payment",
            "/execute-payment": "_handle_execute_payment",
            "/agent/buy-probe": "_handle_agent_buy_probe",
            "/agent/buy-tool": "_handle_agent_buy_tool",
            "/agent/buy-x402": "_handle_agent_buy_x402",
            "/agent/top-up-llm-credits": "_handle_agent_top_up_llm_credits",
            "/agent/buy-bitrefill": "_handle_agent_buy_bitrefill",
            "/agent/buy-wallet-bitrefill": "_handle_agent_buy_wallet_bitrefill",
            "/agent/withdraw": "_handle_agent_withdraw",
            "/agent/llm-key/start": "_handle_agent_llm_key_start",
            "/agent/llm-key/verify": "_handle_agent_llm_key_verify",
            "/agent/llm-key/reconcile": "_handle_agent_llm_key_reconcile",
            "/internal/fulfill-bitrefill": (
                "_handle_internal_fulfill_bitrefill"
            ),
        }
        self.assertEqual(FUND_MOVING_POST_PATHS, frozenset(routes))

        for enabled in ("1", "true", "yes", "on"):
            with patch.dict(
                os.environ,
                {"SIGN402_PURCHASES_PAUSED": enabled},
            ):
                for path, handler_name in routes.items():
                    with self.subTest(enabled=enabled, path=path):
                        server = DummyServer()
                        server.firefly = Mock()
                        server.payment_executor = Mock()
                        server.agent_buy_probe = Mock()
                        server.x402_buyer = Mock()
                        server.user_x402_buyer = Mock()
                        server.bankr_llm_topup = Mock()
                        server.bitrefill_purchase_runner = Mock()
                        server.bitrefill_wallet_purchase_runner = Mock()
                        server.bitrefill_fulfillment_runner = Mock()
                        tripwires = (
                            server.firefly.approve_payment_hash,
                            server.payment_executor,
                            server.agent_buy_probe,
                            server.x402_buyer,
                            server.user_x402_buyer,
                            server.bankr_llm_topup,
                            server.bitrefill_purchase_runner,
                            server.bitrefill_wallet_purchase_runner,
                            server.bitrefill_fulfillment_runner,
                            server.user_wallet_service.decrypt_private_key_for_future_signing,
                            server.user_token_transfer_client.transfer_token,
                            server.user_token_transfer_client.transfer_native,
                            server.imessage_approval_service.request_purchase_approval,
                            server.bankr_llm_purchase_service.start,
                            server.bankr_llm_purchase_service.verify_otp,
                            server.bankr_llm_purchase_service.resume,
                            server.bankr_llm_purchase_service.reconcile,
                        )
                        with patch.object(
                            Sign402GatewayHandler,
                            "_read_json",
                            side_effect=AssertionError("body was read"),
                        ) as read_json:
                            with patch.object(
                                Sign402GatewayHandler,
                                handler_name,
                                side_effect=AssertionError(
                                    "handler was dispatched"
                                ),
                            ):
                                with patch("sys.stderr", io.StringIO()):
                                    handler = self.make_handler(
                                        path,
                                        {},
                                        server=server,
                                        headers=self.llm_auth_headers(),
                                    )
                        response = self.response_text(handler)
                        body = json.loads(
                            response.split("\r\n\r\n", 1)[1]
                        )
                        self.assertIn("HTTP/1.0 503", response)
                        self.assertEqual(
                            body,
                            {
                                "ok": False,
                                "paused": True,
                                "telegramText": (
                                    "⏸️ Purchases are temporarily paused for "
                                    "maintenance. Please try again later."
                                ),
                            },
                        )
                        read_json.assert_not_called()
                        for tripwire in tripwires:
                            tripwire.assert_not_called()

    def test_kill_switch_allows_representative_non_transaction_routes(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "u"
        server.user_event_store = Mock()
        server.user_event_store.read.return_value = {
            "ok": True,
            "telegramText": "Last purchase",
        }
        server.bitrefill_search_service = Mock(
            return_value={"ok": True, "products": []}
        )
        server.bitrefill_quote_service = Mock(
            return_value={"ok": True, "quoteId": "q1"}
        )
        server.bitrefill_settlement_preparation_runner = Mock(
            return_value={
                "ok": True,
                "quoteId": "q1",
                "status": "ready",
            }
        )
        server.user_spend_limit_store.limit_settings.return_value = {
            "maxPerTxAtomic": 10000,
            "dailyCapAtomic": 100000,
            "maxPerTxUsdc": "0.01",
            "dailyCapUsdc": "0.1",
            "operatorMaxPerTxAtomic": 10000,
            "operatorDailyCapAtomic": 100000,
            "userConfigured": False,
        }
        server.user_spend_limit_store.set_limit_settings.return_value = dict(
            server.user_spend_limit_store.limit_settings.return_value
        )
        cases = (
            (
                "/agent/spending-limits",
                {"telegramUserId": "u"},
                self.llm_auth_headers(),
            ),
            (
                "/agent/spending-limits",
                {
                    "telegramUserId": "u",
                    "maxPerTxAtomic": 10000,
                    "dailyCapAtomic": 100000,
                },
                self.llm_auth_headers(),
            ),
            (
                "/agent/last-purchase",
                {"telegramUserId": "u"},
                self.llm_auth_headers(),
            ),
            (
                "/agent/search-bitrefill",
                {"query": "gift"},
                None,
            ),
            (
                "/agent/quote-bitrefill",
                {"productId": "p1", "packageId": "pkg1"},
                None,
            ),
            (
                "/internal/prepare-bitrefill-settlement",
                {"quoteId": "q1", "fulfillmentToken": "test"},
                {"Authorization": "Bearer internal-test-secret"},
            ),
        )
        with patch.dict(
            os.environ,
            {
                "SIGN402_PURCHASES_PAUSED": "1",
                "SIGN402_BANKR_FULFILLMENT_SECRET": (
                    "internal-test-secret"
                ),
            },
        ):
            for path, payload, headers in cases:
                with self.subTest(path=path):
                    with patch("sys.stderr", io.StringIO()):
                        handler = self.make_handler(
                            path,
                            payload,
                            server=server,
                            headers=headers,
                        )
                    response = self.response_text(handler)
                    self.assertIn("HTTP/1.0 200 OK", response)
                    self.assertNotIn("HTTP/1.0 503", response)

    def test_purchase_pause_parser_rejects_all_other_values(self):
        for value in ("", "0", "false", "no", "off", "enabled"):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"SIGN402_PURCHASES_PAUSED": value},
                ):
                    self.assertFalse(_purchases_paused())
```

- [ ] **Step 2: Run the matrix and verify RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ../payment-executor/.venv/bin/python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_kill_switch_blocks_every_transaction_route_before_dispatch \
  tests.test_gateway_server.GatewayServerTests.test_kill_switch_allows_representative_non_transaction_routes \
  tests.test_gateway_server.GatewayServerTests.test_purchase_pause_parser_rejects_all_other_values \
  -v
```

Expected: missing constant and/or one of the previously unguarded handlers is dispatched.

- [ ] **Step 3: Add the authoritative route set**

Near the gateway route constants:

```python
FUND_MOVING_POST_PATHS = frozenset(
    {
        "/approve-payment",
        "/execute-payment",
        "/agent/buy-probe",
        "/agent/buy-tool",
        "/agent/buy-x402",
        "/agent/top-up-llm-credits",
        "/agent/buy-bitrefill",
        "/agent/buy-wallet-bitrefill",
        "/agent/withdraw",
        "/agent/llm-key/start",
        "/agent/llm-key/verify",
        "/agent/llm-key/reconcile",
        "/internal/fulfill-bitrefill",
    }
)
```

In `do_POST()`, immediately after resolving `path` and before route dispatch:

```python
        if path in FUND_MOVING_POST_PATHS and self._reject_if_purchases_paused():
            return
```

The clean base does not contain the user's unstaged pause helpers. Add them
explicitly:

```python
_PURCHASES_PAUSED_MESSAGE = (
    "⏸️ Purchases are temporarily paused for maintenance. "
    "Please try again later."
)


def _purchases_paused() -> bool:
    return os.getenv(
        "SIGN402_PURCHASES_PAUSED",
        "",
    ).strip().lower() in {"1", "true", "yes", "on"}
```

```python
    def _reject_if_purchases_paused(self) -> bool:
        if not _purchases_paused():
            return False
        self._send_json(
            {
                "ok": False,
                "paused": True,
                "telegramText": _PURCHASES_PAUSED_MESSAGE,
            },
            status=503,
        )
        return True
```

Do not add any handler-local pause guards. The route set and one pre-dispatch
guard are the single source of truth.

- [ ] **Step 4: Run the matrix and gateway suite**

Run the command from Step 2, then:

```bash
cd sign402-gateway
PYTHONPATH=. ../payment-executor/.venv/bin/python -m unittest discover -s tests -v
```

Expected: matrix tests pass and the full gateway suite is `OK`.

- [ ] **Step 5: Commit the centralized guard**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "fix: centralize transaction kill switch"
```

---

### Task 6: Security Documentation and Full Regression

**Files:**
- Modify: `sign402-gateway/.env.example`
- Modify: `sign402-gateway/SECURITY.md`

**Interfaces:**
- Consumes: completed Tasks 1–5.
- Produces: accurate operator-facing configuration and security invariants.

- [ ] **Step 1: Update configuration comments**

Update the `SIGN402_WALLET_MASTER_KEY` comment to state that the same key
encrypts managed wallets, Bankr API keys, Bitrefill recipients, and reveal
tokens. State that rotating it requires a separately reviewed migration.

Keep the current `SIGN402_PURCHASES_PAUSED` variable and document that every
route in `FUND_MOVING_POST_PATHS` returns `503` before reading the body or
dispatching a handler.

- [ ] **Step 2: Update the security model**

Document these exact properties in `SECURITY.md`:

- new sensitive JSON/SQLite files use `0700/0600`;
- new fulfillment tokens and recipients are Fernet-encrypted;
- provider snapshots are strict allowlists;
- redemption is fetched only after authorization and is never persisted;
- legacy plaintext state is read-compatible but updates fail closed until the
  controlled migration;
- setting the pause flag blocks all transaction-oriented routes, including LLM
  verify/reconcile and legacy routes;
- this code package does not migrate or rotate live state.

- [ ] **Step 3: Run formatting and focused security scans**

Run:

```bash
cd "$(git rev-parse --show-toplevel)"
git diff --check
if rg -n "TBD|TODO|FIXME|implement later" \
    sign402-gateway/sign402_gateway/secure_state.py \
    sign402-gateway/sign402_gateway/commerce_store.py \
    sign402-gateway/sign402_gateway/bitrefill_runner.py \
    sign402-gateway/sign402_gateway/server.py \
    sign402-gateway/tests; then
  exit 1
fi
```

Expected: both commands exit 0 and the changed code contains no placeholder
markers.

- [ ] **Step 4: Run every repository test suite**

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
```

Then:

```bash
npm --prefix cdp-x402-service test
npm --prefix singit-risk-check test
```

Expected: all suites exit 0. The final clean-worktree count must be greater
than the clean baseline recorded in Task 0 because this package adds regression
tests.

- [ ] **Step 5: Verify the original live-state targets were untouched**

Create a second metadata-only `stat` inventory and compare it with
`/tmp/sign402-p0-live-state-before.txt` for:

```text
cdp-x402-service/.env
payment-executor/.env
sign402-gateway/.env.wallet-bitrefill
demo-dashboard/bitrefill-orders.sqlite3
demo-dashboard/user-purchases.json
```

Only compare presence, modes, sizes, and mtimes. Do not hash or open contents,
invoke a database client, or print secret values. Expected: no difference.

Repeat the Task 0 `stat` loop with
`STATE_MANIFEST=/tmp/sign402-p0-live-state-after.txt`, then run:

```bash
cmp -s \
  /tmp/sign402-p0-live-state-before.txt \
  /tmp/sign402-p0-live-state-after.txt
```

Expected: exit 0. Do not display either manifest in normal output.

- [ ] **Step 6: Commit documentation**

```bash
git add sign402-gateway/.env.example sign402-gateway/SECURITY.md
git commit -m "docs: document P0 containment controls"
```

- [ ] **Step 7: Request final code review**

Use `superpowers:requesting-code-review` against the complete branch diff.
Critical and Important findings must be fixed and re-reviewed before branch
completion. Run `superpowers:verification-before-completion` after the final
review and before reporting success.
