# Discard Legacy Fulfillment Tokens Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add and safely run an operator-only command that removes obsolete
fulfillment-token fields from the production purchase store without changing
the live purchase flow or deleting non-secret purchase history.

**Architecture:** A standalone gateway module validates the complete JSON store,
reports secret-field counts in dry-run mode, and performs an explicit atomic
rewrite only with `--apply`. It reuses `atomic_write_private_json`; no gateway
handler, pricing, wallet, approval, or Bitrefill code changes.

**Tech Stack:** Python 3.14, standard-library `argparse`, `json`, `pathlib`, and
`unittest`; existing `sign402_gateway.secure_state.atomic_write_private_json`;
Git worktrees; systemd on the Sign402 VPS.

## Global Constraints

- Do not modify gateway handlers, the Telegram/Hermes flow, pricing, approval,
  payment, wallet, or Bitrefill purchase code.
- Preserve every purchase record and every field except top-level
  `fulfillmentToken` and `encryptedFulfillmentToken`.
- Never print token values, complete purchase records, wallet secrets, or the
  master key.
- Dry run is the default; filesystem mutation requires explicit `--apply`.
- Reject missing, malformed, non-object, non-record-object, and symlink inputs
  without mutation.
- Use the existing atomic private JSON writer; successful rewrites must leave
  the parent directory mode `0700` and file mode `0600`.
- Observe every focused test fail for the intended reason before adding the
  production behavior that makes it pass.
- Do not initiate, approve, or simulate a live purchase.
- Run the production rewrite only while `sign402-gateway` is stopped.
- Delete the temporary root-only production backup only after the cleaned file,
  restarted service, listening socket, and health endpoint are verified.

---

## File Structure

- Create
  `sign402-gateway/sign402_gateway/discard_legacy_fulfillment_tokens.py`:
  isolated validation, inspection, discard operation, safe report, and CLI.
- Create
  `sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py`:
  real-temporary-file behavior and failure-path coverage.
- Modify
  `docs/superpowers/specs/2026-07-26-discard-legacy-fulfillment-tokens-design.md`:
  record written-spec approval.
- Create this implementation plan only; no runtime code imports the new module.

---

### Task 1: Dry-Run Inspection

**Files:**
- Create:
  `sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py`
- Create:
  `sign402-gateway/sign402_gateway/discard_legacy_fulfillment_tokens.py`

**Interfaces:**
- Produces:
  `cleanup_legacy_fulfillment_tokens(path: pathlib.Path, *, apply: bool = False) -> CleanupReport`
- Produces:
  `LegacyFulfillmentTokenCleanupError`, whose messages contain no store values.
- `CleanupReport` contains exactly `mode`, `records`,
  `plaintext_token_records`, `encrypted_token_records`,
  `token_fields_removed`, and `changed`.

- [ ] **Step 1: Write the failing dry-run test**

Create the test module with a real JSON file and a dynamic import that turns the
initial missing module into an assertion failure:

```python
from __future__ import annotations

import importlib
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


MODULE_NAME = "sign402_gateway.discard_legacy_fulfillment_tokens"
PLAINTEXT_MARKER = "PLAINTEXT-TOKEN-MARKER"
ENCRYPTED_MARKER = "ENCRYPTED-TOKEN-MARKER"


def cleanup_module():
    try:
        return importlib.import_module(MODULE_NAME)
    except ModuleNotFoundError:
        return None


class DiscardLegacyFulfillmentTokensTests(unittest.TestCase):
    def write_store(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_dry_run_reports_counts_without_mutating_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "user-purchases.json"
            self.write_store(
                path,
                {
                    "u1": {
                        "ok": True,
                        "fulfillmentToken": PLAINTEXT_MARKER,
                    },
                    "u2": {
                        "ok": True,
                        "encryptedFulfillmentToken": ENCRYPTED_MARKER,
                    },
                    "u3": {"ok": True},
                },
            )
            before_bytes = path.read_bytes()
            before_mtime_ns = path.stat().st_mtime_ns
            module = cleanup_module()
            self.assertIsNotNone(module, "cleanup module must exist")

            report = module.cleanup_legacy_fulfillment_tokens(path)

            self.assertEqual(
                report,
                {
                    "mode": "dry-run",
                    "records": 3,
                    "plaintext_token_records": 1,
                    "encrypted_token_records": 1,
                    "token_fields_removed": 2,
                    "changed": False,
                },
            )
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime_ns)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
env -u BITREFILL_API_KEY -u SIGN402_BITREFILL_MODE \
  PYTHONPATH='/Users/mp/Documents/Berlin Hack/.worktrees/clear-legacy-fulfillment-tokens/sign402-gateway' \
  '/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest \
  '/Users/mp/Documents/Berlin Hack/.worktrees/clear-legacy-fulfillment-tokens/sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py' \
  -v
```

Expected: `FAIL` with `cleanup module must exist`; no network call occurs.

- [ ] **Step 3: Implement the minimal validated dry run**

Create the module with these definitions:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict, cast


class LegacyFulfillmentTokenCleanupError(RuntimeError):
    pass


class CleanupReport(TypedDict):
    mode: str
    records: int
    plaintext_token_records: int
    encrypted_token_records: int
    token_fields_removed: int
    changed: bool


def _load_purchase_store(path: Path) -> dict[str, dict[str, Any]]:
    if path.is_symlink():
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must not be a symlink"
        )
    if not path.exists():
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store does not exist"
        )
    if not path.is_file():
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must be a regular file"
        )
    try:
        serialized = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must be UTF-8 JSON"
        ) from None
    except OSError:
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store could not be read"
        ) from None
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError:
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must contain valid JSON"
        ) from None
    if not isinstance(payload, dict):
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store root must be an object"
        )
    if any(not isinstance(record, dict) for record in payload.values()):
        raise LegacyFulfillmentTokenCleanupError(
            "every purchase store record must be an object"
        )
    return cast(dict[str, dict[str, Any]], payload)


def cleanup_legacy_fulfillment_tokens(
    path: Path,
    *,
    apply: bool = False,
) -> CleanupReport:
    data = _load_purchase_store(Path(path))
    plaintext_records = sum(
        "fulfillmentToken" in record for record in data.values()
    )
    encrypted_records = sum(
        "encryptedFulfillmentToken" in record for record in data.values()
    )
    token_fields = plaintext_records + encrypted_records
    return {
        "mode": "apply" if apply else "dry-run",
        "records": len(data),
        "plaintext_token_records": plaintext_records,
        "encrypted_token_records": encrypted_records,
        "token_fields_removed": token_fields,
        "changed": False,
    }
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command again.

Expected: `Ran 1 test` and `OK`.

- [ ] **Step 5: Commit the dry-run slice**

```bash
git add \
  sign402-gateway/sign402_gateway/discard_legacy_fulfillment_tokens.py \
  sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py
git commit -m "feat: inspect legacy fulfillment tokens safely"
```

---

### Task 2: Explicit Atomic Discard

**Files:**
- Modify:
  `sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py`
- Modify:
  `sign402-gateway/sign402_gateway/discard_legacy_fulfillment_tokens.py`

**Interfaces:**
- Consumes:
  `cleanup_legacy_fulfillment_tokens(path, apply=False)` from Task 1.
- Consumes:
  `atomic_write_private_json(path, payload)` and `SensitiveStateError` from
  `sign402_gateway.secure_state`.
- Produces:
  `apply=True` removal of both token-field formats from all records in one
  atomic rewrite.

- [ ] **Step 1: Write failing apply-mode tests**

Add four tests:

```python
    def test_apply_removes_both_formats_and_preserves_other_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "user-purchases.json"
            self.write_store(
                path,
                {
                    "u1": {
                        "ok": True,
                        "fulfillmentToken": PLAINTEXT_MARKER,
                        "nested": {"fulfillmentToken": "keep-nested"},
                    },
                    "u2": {
                        "ok": False,
                        "encryptedFulfillmentToken": ENCRYPTED_MARKER,
                        "quoteId": "q2",
                    },
                    "u3": {"ok": True, "quoteId": "q3"},
                },
            )
            module = cleanup_module()

            report = module.cleanup_legacy_fulfillment_tokens(
                path,
                apply=True,
            )

            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                report,
                {
                    "mode": "apply",
                    "records": 3,
                    "plaintext_token_records": 1,
                    "encrypted_token_records": 1,
                    "token_fields_removed": 2,
                    "changed": True,
                },
            )
            self.assertNotIn("fulfillmentToken", persisted["u1"])
            self.assertNotIn("encryptedFulfillmentToken", persisted["u2"])
            self.assertEqual(
                persisted["u1"]["nested"],
                {"fulfillmentToken": "keep-nested"},
            )
            self.assertEqual(persisted["u2"]["quoteId"], "q2")
            self.assertEqual(persisted["u3"]["quoteId"], "q3")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(PLAINTEXT_MARKER, raw)
            self.assertNotIn(ENCRYPTED_MARKER, raw)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_apply_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "user-purchases.json"
            self.write_store(
                path,
                {"u": {"fulfillmentToken": PLAINTEXT_MARKER}},
            )
            before = path.read_bytes()
            module = cleanup_module()

            with patch(
                "sign402_gateway.secure_state.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaises(
                    module.LegacyFulfillmentTokenCleanupError
                ):
                    module.cleanup_legacy_fulfillment_tokens(
                        path,
                        apply=True,
                    )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                list(path.parent.glob(f".{path.name}.*.tmp")),
                [],
            )

    def test_apply_clean_and_empty_stores_are_noops(self):
        module = cleanup_module()
        for payload in ({}, {"u": {"ok": True}}):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "state" / "user-purchases.json"
                    self.write_store(path, payload)
                    before_bytes = path.read_bytes()
                    before_mtime_ns = path.stat().st_mtime_ns

                    report = module.cleanup_legacy_fulfillment_tokens(
                        path,
                        apply=True,
                    )

                    self.assertFalse(report["changed"])
                    self.assertEqual(report["token_fields_removed"], 0)
                    self.assertEqual(path.read_bytes(), before_bytes)
                    self.assertEqual(
                        path.stat().st_mtime_ns,
                        before_mtime_ns,
                    )
```

The first test proves the production change that removes both top-level token
formats. The failure test proves the atomic writer cannot leave a partial
document or change the original JSON bytes. Safe permission tightening by the
existing private-state helper is allowed. The no-op test prevents needless
rewrites of already-clean state.

- [ ] **Step 2: Run focused tests and verify RED**

Run the Task 1 Step 2 command.

Expected: the three new test methods fail because `apply=True` does not yet
write or report `changed=True`; the Task 1 dry-run test remains green.

- [ ] **Step 3: Implement explicit apply mode**

Import the existing secure-state primitives:

```python
from .secure_state import SensitiveStateError, atomic_write_private_json
```

Before returning the report, add:

```python
    changed = False
    if apply and token_fields:
        cleaned = {
            key: {
                field: value
                for field, value in record.items()
                if field not in {
                    "fulfillmentToken",
                    "encryptedFulfillmentToken",
                }
            }
            for key, record in data.items()
        }
        try:
            atomic_write_private_json(Path(path), cleaned)
        except (OSError, SensitiveStateError):
            raise LegacyFulfillmentTokenCleanupError(
                "purchase store could not be atomically updated"
            ) from None
        changed = True
```

Return `"changed": changed`; keep the pre-operation counts in the report so the
operator sees exactly how many fields were discarded.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Task 1 Step 2 command.

Expected: `Ran 4 tests` and `OK`.

- [ ] **Step 5: Commit the atomic-discard slice**

```bash
git add \
  sign402-gateway/sign402_gateway/discard_legacy_fulfillment_tokens.py \
  sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py
git commit -m "feat: discard legacy fulfillment tokens atomically"
```

---

### Task 3: Safe CLI and Complete Validation

**Files:**
- Modify:
  `sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py`
- Modify:
  `sign402-gateway/sign402_gateway/discard_legacy_fulfillment_tokens.py`

**Interfaces:**
- Consumes:
  `cleanup_legacy_fulfillment_tokens(path, apply=False)` from Tasks 1–2.
- Produces:
  `main(argv: Sequence[str] | None = None) -> int`.
- Produces module command:
  `python -m sign402_gateway.discard_legacy_fulfillment_tokens --path PATH [--apply]`.
- Success writes one safe JSON report to stdout and returns `0`.
- Validation failure writes one static safe message to stderr and returns `2`.

- [ ] **Step 1: Write failing validation and CLI tests**

Add four test methods:

```python
    def test_invalid_document_shapes_fail_without_mutation(self):
        module = cleanup_module()
        cases = (
            ("malformed", b'{"u":{"fulfillmentToken":"SECRET"'),
            ("non-object-root", b"[]\n"),
            ("non-object-record", b'{"u":"SECRET"}\n'),
        )
        for name, before in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "user-purchases.json"
                    path.write_bytes(before)
                    with self.assertRaises(
                        module.LegacyFulfillmentTokenCleanupError
                    ) as captured:
                        module.cleanup_legacy_fulfillment_tokens(
                            path,
                            apply=True,
                        )
                    self.assertNotIn("SECRET", str(captured.exception))
                    self.assertEqual(path.read_bytes(), before)

    def test_missing_and_symlink_paths_fail_without_following_link(self):
        module = cleanup_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            link = root / "link.json"
            self.write_store(
                target,
                {"u": {"fulfillmentToken": PLAINTEXT_MARKER}},
            )
            link.symlink_to(target)
            before = target.read_bytes()
            for path in (root / "missing.json", link):
                with self.subTest(path=path.name):
                    with self.assertRaises(
                        module.LegacyFulfillmentTokenCleanupError
                    ):
                        module.cleanup_legacy_fulfillment_tokens(
                            path,
                            apply=True,
                        )
            self.assertEqual(target.read_bytes(), before)

    def test_cli_outputs_only_machine_readable_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            self.write_store(
                path,
                {"u": {"fulfillmentToken": PLAINTEXT_MARKER}},
            )
            module = cleanup_module()
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = module.main(["--path", str(path)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "mode": "dry-run",
                    "records": 1,
                    "plaintext_token_records": 1,
                    "encrypted_token_records": 0,
                    "token_fields_removed": 1,
                    "changed": False,
                },
            )
            self.assertNotIn(PLAINTEXT_MARKER, stdout.getvalue())

    def test_cli_error_never_echoes_token_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            path.write_text(
                '{"u":{"fulfillmentToken":"CLI-SECRET-MARKER"',
                encoding="utf-8",
            )
            module = cleanup_module()
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = module.main(
                    ["--path", str(path), "--apply"]
                )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("valid JSON", stderr.getvalue())
            self.assertNotIn("CLI-SECRET-MARKER", stderr.getvalue())
```

- [ ] **Step 2: Run focused tests and verify RED**

Run the Task 1 Step 2 command.

Expected: validation tests pass against the core loader; the two CLI tests fail
because `main` does not exist. Earlier tests remain green.

- [ ] **Step 3: Implement the safe CLI**

Add imports and `main`:

```python
import argparse
import sys
from collections.abc import Sequence


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or discard fulfillment-token fields from a "
            "Sign402 user purchase store."
        )
    )
    parser.add_argument(
        "--path",
        required=True,
        type=Path,
        help="absolute path to user-purchases.json",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically remove token fields; default is dry run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        report = cleanup_legacy_fulfillment_tokens(
            args.path,
            apply=args.apply,
        )
    except LegacyFulfillmentTokenCleanupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Do not include exception causes or serialized store data in output.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Task 1 Step 2 command.

Expected: `Ran 8 tests` and `OK`, with empty stderr.

- [ ] **Step 5: Commit the safe CLI slice**

```bash
git add \
  sign402-gateway/sign402_gateway/discard_legacy_fulfillment_tokens.py \
  sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py
git commit -m "feat: add safe legacy token cleanup command"
```

---

### Task 4: Verification and Independent Review

**Files:**
- Verify:
  `sign402-gateway/sign402_gateway/discard_legacy_fulfillment_tokens.py`
- Verify:
  `sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py`
- Verify:
  `docs/superpowers/specs/2026-07-26-discard-legacy-fulfillment-tokens-design.md`
- Verify:
  `docs/superpowers/plans/2026-07-26-discard-legacy-fulfillment-tokens.md`

**Interfaces:**
- Consumes all Task 1–3 commits.
- Produces a reviewed commit whose diff contains no purchase-flow change.

- [ ] **Step 1: Run the focused suite**

```bash
env -u BITREFILL_API_KEY -u SIGN402_BITREFILL_MODE \
  PYTHONPATH='/Users/mp/Documents/Berlin Hack/.worktrees/clear-legacy-fulfillment-tokens/sign402-gateway' \
  '/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest \
  '/Users/mp/Documents/Berlin Hack/.worktrees/clear-legacy-fulfillment-tokens/sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py' \
  -v
```

Expected: `Ran 8 tests` and `OK`.

- [ ] **Step 2: Run the complete gateway suite**

```bash
env -u BITREFILL_API_KEY -u SIGN402_BITREFILL_MODE \
  PYTHONWARNINGS='ignore::ResourceWarning' \
  PYTHONPATH='/Users/mp/Documents/Berlin Hack/.worktrees/clear-legacy-fulfillment-tokens/sign402-gateway' \
  '/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover \
  -s '/Users/mp/Documents/Berlin Hack/.worktrees/clear-legacy-fulfillment-tokens/sign402-gateway/tests' \
  -q
```

Expected: `Ran 572 tests` and `OK`; no network purchase route is invoked.

- [ ] **Step 3: Verify scope, secrets, and repository consistency**

```bash
git diff --check 073964d526191d1d3ad8e9972668b21e204f70ba..HEAD
git diff --name-only 073964d526191d1d3ad8e9972668b21e204f70ba..HEAD
git status --short --branch
git diff 073964d526191d1d3ad8e9972668b21e204f70ba..HEAD -- \
  sign402-gateway/sign402_gateway/server.py \
  sign402-gateway/sign402_gateway/bitrefill_runner.py \
  hermes-plugins/sign402-wallet
```

Expected: the final command emits no diff; only the new operator module, its
tests, the approved spec status, and this plan are in scope. Test markers are
synthetic and no live token value appears anywhere in the diff.

- [ ] **Step 4: Request independent specification and code-quality reviews**

Give reviewers the approved spec, base commit
`073964d526191d1d3ad8e9972668b21e204f70ba`, final commit, exact focused/full
test output, and the Global Constraints. Require findings to identify exact
files and lines. Fix every Critical or Important finding with a new failing
test first, then rerun Steps 1–3.

- [ ] **Step 5: Commit any review fixes**

```bash
git add \
  sign402-gateway/sign402_gateway/discard_legacy_fulfillment_tokens.py \
  sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py \
  docs/superpowers/specs/2026-07-26-discard-legacy-fulfillment-tokens-design.md \
  docs/superpowers/plans/2026-07-26-discard-legacy-fulfillment-tokens.md
git commit -m "fix: harden legacy token cleanup"
```

Skip this commit only when reviewers report no findings and the worktree is
already clean.

---

### Task 5: Integrate, Push, and Deploy the Reviewed Commit

**Files:**
- Integrate into:
  `/Users/mp/Documents/Berlin Hack` on `x402Bnkr`
- Deploy to:
  `/home/hermes/apps/sign402` on the Sign402 VPS

**Interfaces:**
- Consumes the reviewed Task 4 commit.
- Produces the same exact Git object locally, on `singitai/x402Bnkr`, and on the
  VPS before production state is touched.

- [ ] **Step 1: Capture and verify exact commits**

```bash
cleanup_base_commit='073964d526191d1d3ad8e9972668b21e204f70ba'
cleanup_reviewed_commit="$(
  git -C '/Users/mp/Documents/Berlin Hack/.worktrees/clear-legacy-fulfillment-tokens' \
    rev-parse HEAD
)"
test "$(
  git -C '/Users/mp/Documents/Berlin Hack/.worktrees/clear-legacy-fulfillment-tokens' \
    merge-base "$cleanup_base_commit" "$cleanup_reviewed_commit"
)" = "$cleanup_base_commit"
```

- [ ] **Step 2: Fast-forward the main branch without touching user files**

```bash
cd '/Users/mp/Documents/Berlin Hack'
test "$(git branch --show-current)" = 'x402Bnkr'
git diff --quiet
git diff --cached --quiet
git merge --ff-only codex/clear-legacy-fulfillment-tokens
test "$(git rev-parse HEAD)" = "$cleanup_reviewed_commit"
```

Untracked user files remain untouched.

- [ ] **Step 3: Rerun focused and full tests after integration**

Repeat Task 4 Steps 1–2 with
`PYTHONPATH='/Users/mp/Documents/Berlin Hack/sign402-gateway'` and the main
checkout test path.

Expected: `8/8` focused and `572/572` full.

- [ ] **Step 4: Push and verify the remote ref**

```bash
git push singitai x402Bnkr
test "$(
  git ls-remote singitai refs/heads/x402Bnkr | awk '{print $1}'
)" = "$cleanup_reviewed_commit"
```

- [ ] **Step 5: Transfer a checksummed bundle because the VPS has no GitHub key**

```bash
cleanup_bundle='/private/tmp/sign402-clear-legacy-tokens.bundle'
git bundle create "$cleanup_bundle" x402Bnkr
cleanup_bundle_sha256="$(
  shasum -a 256 "$cleanup_bundle" | awk '{print $1}'
)"
scp "$cleanup_bundle" hermes@164.68.104.44:/tmp/sign402-clear-legacy-tokens.bundle
```

The checksum and reviewed commit remain in the same local shell for Step 6.

- [ ] **Step 6: Fast-forward the VPS checkout and verify exact HEAD**

Run from the same local shell. The literal values are passed as positional
arguments to the remote shell, so the VPS cannot silently select a different
commit:

```bash
ssh hermes@164.68.104.44 bash -s -- \
  "$cleanup_base_commit" \
  "$cleanup_reviewed_commit" \
  "$cleanup_bundle_sha256" <<'REMOTE'
set -euo pipefail
cleanup_base_commit="$1"
cleanup_reviewed_commit="$2"
cleanup_bundle_sha256="$3"
cleanup_bundle='/tmp/sign402-clear-legacy-tokens.bundle'
cd /home/hermes/apps/sign402
test "$(git rev-parse HEAD)" = "$cleanup_base_commit"
git diff --quiet
git diff --cached --quiet
test "$(
  sha256sum "$cleanup_bundle" | awk '{print $1}'
)" = "$cleanup_bundle_sha256"
git bundle verify "$cleanup_bundle"
test "$(
  git bundle list-heads "$cleanup_bundle" refs/heads/x402Bnkr |
    awk '{print $1}'
)" = "$cleanup_reviewed_commit"
git fetch "$cleanup_bundle" x402Bnkr
test "$(git rev-parse FETCH_HEAD)" = "$cleanup_reviewed_commit"
git merge --ff-only FETCH_HEAD
test "$(git rev-parse HEAD)" = "$cleanup_reviewed_commit"
REMOTE
```

Do not derive the deployment target from an unreviewed remote state.

- [ ] **Step 7: Test the deployed command without changing production state**

On the VPS:

```bash
cd /home/hermes/apps/sign402
PYTHONPATH='/home/hermes/apps/sign402/sign402-gateway' \
  sign402-gateway/.venv/bin/python \
  -m unittest \
  sign402-gateway/tests/test_discard_legacy_fulfillment_tokens.py \
  -v
```

Expected: `Ran 8 tests` and `OK`. Do not restart the service yet; the deployed
files are not imported by the running gateway.

---

### Task 6: Production Cleanup and Health Verification

**Files:**
- Mutate only:
  `/home/hermes/apps/sign402/demo-dashboard/user-purchases.json`
- Temporary root-only backup:
  `/var/backups/sign402/user-purchases.pre-discard-20260726.json`

**Interfaces:**
- Consumes the exact deployed Task 5 command.
- Produces a purchase store with unchanged records except that both top-level
  token-field formats are absent.
- Produces a restarted healthy `sign402-gateway`.

- [ ] **Step 1: Run read-only production preflight**

On the VPS:

```bash
sudo runuser -u hermes -- env \
  PYTHONPATH='/home/hermes/apps/sign402/sign402-gateway' \
  /home/hermes/apps/sign402/sign402-gateway/.venv/bin/python \
  -m sign402_gateway.discard_legacy_fulfillment_tokens \
  --path /home/hermes/apps/sign402/demo-dashboard/user-purchases.json
```

Expected JSON has `records: 3`, `plaintext_token_records: 3`,
`encrypted_token_records: 0`, `token_fields_removed: 3`, and `changed: false`.
Stop if any value differs.

- [ ] **Step 2: Stop the gateway and create the exact private backup**

```bash
sudo systemctl stop sign402-gateway
test "$(systemctl is-active sign402-gateway)" = 'inactive'
sudo test ! -e /var/backups/sign402/user-purchases.pre-discard-20260726.json
sudo install -d -m 0700 -o root -g root /var/backups/sign402
sudo install -m 0600 -o root -g root \
  /home/hermes/apps/sign402/demo-dashboard/user-purchases.json \
  /var/backups/sign402/user-purchases.pre-discard-20260726.json
sudo test "$(
  sudo stat -c '%a:%U:%G' \
    /var/backups/sign402/user-purchases.pre-discard-20260726.json
)" = '600:root:root'
```

Do not display the backup contents.

- [ ] **Step 3: Apply the cleanup as the service user**

```bash
sudo runuser -u hermes -- env \
  PYTHONPATH='/home/hermes/apps/sign402/sign402-gateway' \
  /home/hermes/apps/sign402/sign402-gateway/.venv/bin/python \
  -m sign402_gateway.discard_legacy_fulfillment_tokens \
  --path /home/hermes/apps/sign402/demo-dashboard/user-purchases.json \
  --apply
```

Expected JSON has `mode: apply`, `records: 3`,
`plaintext_token_records: 3`, `encrypted_token_records: 0`,
`token_fields_removed: 3`, and `changed: true`.

- [ ] **Step 4: Verify cleaned state before restarting**

Repeat Step 1.

Expected JSON has `records: 3`, both token-record counts `0`,
`token_fields_removed: 0`, and `changed: false`.

Then verify ownership and mode:

```bash
test "$(
  sudo stat -c '%a:%U:%G' \
    /home/hermes/apps/sign402/demo-dashboard/user-purchases.json
)" = '600:hermes:hermes'
```

If any check fails, leave the gateway stopped and retain the backup.

- [ ] **Step 5: Start and verify the gateway**

```bash
sudo systemctl start sign402-gateway
systemctl show sign402-gateway \
  -p ActiveState -p SubState -p MainPID -p NRestarts
sudo ss -ltnp '( sport = :8099 )'
curl -fsS http://127.0.0.1:8099/health |
python3 -c \
  'import json,sys; assert json.load(sys.stdin)["ok"]; print("health ok")'
```

Expected: `ActiveState=active`, `SubState=running`, a nonzero `MainPID`,
`NRestarts=0`, `127.0.0.1:8099` listening, and `health ok`. If startup or health
fails, stop the gateway and retain the backup for explicit rollback.

- [ ] **Step 6: Delete the obsolete-token backup after all checks pass**

```bash
sudo unlink \
  /var/backups/sign402/user-purchases.pre-discard-20260726.json
sudo test ! -e \
  /var/backups/sign402/user-purchases.pre-discard-20260726.json
```

This is the only intentional backup deletion. It is authorized by the user's
confirmation that the three old purchases are redeemed and no longer needed.

- [ ] **Step 7: Hand off the purchase retry**

Report the exact deployed commit, before/after field counts, file mode, service
state, restart count, listening socket, and health result. Tell the user to
retry the purchase manually. Do not start or approve the purchase on their
behalf.
