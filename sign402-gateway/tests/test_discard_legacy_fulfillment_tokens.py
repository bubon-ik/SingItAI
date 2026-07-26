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
