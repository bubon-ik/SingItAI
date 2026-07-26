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
