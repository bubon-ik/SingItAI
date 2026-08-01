import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from trezor_sidecar.broker_store import BrokerStore


ADDRESS = "0xB80b5Ca13583fB7E0236db4bD8834B9035654558"


class BrokerStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        tokens = iter(["enrollment-" + "e" * 32, "companion-" + "t" * 32])
        identifiers = iter(["c" * 24, "j" * 24, "k" * 24])
        self.store = BrokerStore(
            Path(self.temp.name) / "state.db",
            token_factory=lambda: next(tokens),
            id_factory=lambda: next(identifiers),
        )

    def enroll(self):
        code = self.store.create_enrollment("12345", now=1_700_000_000)
        companion = self.store.enroll(code, ADDRESS, now=1_700_000_001)
        return code, companion

    def test_enrollment_is_single_use_and_raw_credentials_are_not_stored(self):
        code, companion = self.enroll()
        self.assertEqual(companion["userId"], "12345")
        self.assertEqual(companion["walletAddress"], ADDRESS)
        with self.assertRaisesRegex(ValueError, "invalid or expired"):
            self.store.enroll(code, ADDRESS, now=1_700_000_002)

        raw = Path(self.temp.name, "state.db").read_bytes()
        self.assertNotIn(code.encode(), raw)
        self.assertNotIn(companion["token"].encode(), raw)
        if os.name == "posix":
            self.assertEqual(Path(self.temp.name, "state.db").stat().st_mode & 0o777, 0o600)

    def test_job_is_idempotent_claimed_once_and_completion_is_idempotent(self):
        _, companion = self.enroll()
        arguments = dict(
            user_id="12345",
            kind="purchase_intent",
            idempotency_key="approve:12345678",
            payload={"intentId": "x"},
            expires_at=1_700_000_100,
            now=1_700_000_002,
        )
        created = self.store.create_job(**arguments)
        replay = self.store.create_job(**arguments)
        self.assertEqual(created["jobId"], replay["jobId"])
        claimed = self.store.claim(companion["token"], now=1_700_000_003)
        claimed_again = self.store.claim(companion["token"], now=1_700_000_004)
        self.assertEqual(claimed["jobId"], claimed_again["jobId"])
        self.assertEqual(claimed["state"], "LEASED")

        finished = self.store.finish(
            companion["token"],
            created["jobId"],
            result={"ok": True},
            error_code=None,
            now=1_700_000_005,
        )
        replayed = self.store.finish(
            companion["token"],
            created["jobId"],
            result={"ok": True},
            error_code=None,
            now=1_700_000_006,
        )
        self.assertEqual(finished["state"], "SUCCEEDED")
        self.assertEqual(replayed["result"], {"ok": True})
        self.assertIsNone(self.store.claim(companion["token"], now=1_700_000_007))

    def test_conflicts_wrong_token_and_expiration_fail_closed(self):
        _, companion = self.enroll()
        created = self.store.create_job(
            user_id="12345",
            kind="usdc_payment",
            idempotency_key="bitrefill-pay:invoice-1",
            payload={"invoiceId": "invoice-1"},
            expires_at=1_700_000_010,
            now=1_700_000_002,
        )
        with self.assertRaises(PermissionError):
            self.store.claim("wrong-" + "x" * 32, now=1_700_000_003)
        with self.assertRaisesRegex(ValueError, "idempotency conflict"):
            self.store.create_job(
                user_id="12345",
                kind="usdc_payment",
                idempotency_key="bitrefill-pay:invoice-1",
                payload={"invoiceId": "changed"},
                expires_at=1_700_000_010,
                now=1_700_000_003,
            )
        self.assertIsNone(self.store.claim(companion["token"], now=1_700_000_010))
        self.assertEqual(self.store.job(created["jobId"], now=1_700_000_011)["state"], "EXPIRED")

    def test_only_narrow_job_types_are_accepted(self):
        self.enroll()
        with self.assertRaisesRegex(ValueError, "kind"):
            self.store.create_job(
                user_id="12345",
                kind="generic_mcp",
                idempotency_key="generic:12345678",
                payload={"tool": "trezor_push_transaction"},
                expires_at=1_700_000_100,
                now=1_700_000_002,
            )

    def test_schema_is_separate_and_contains_no_wallet_key_column(self):
        database = sqlite3.connect(Path(self.temp.name, "state.db"))
        try:
            columns = {
                row[1]
                for table in ("enrollments", "companions", "jobs")
                for row in database.execute(f"PRAGMA table_info({table})")
            }
        finally:
            database.close()
        self.assertFalse({"private_key", "mcp_token", "seed", "signed_transaction"} & columns)
