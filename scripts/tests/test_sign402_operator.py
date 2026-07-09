import hmac
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sign402-operator.py"


def load_operator():
    spec = importlib.util.spec_from_file_location("sign402_operator", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def hmac_digest(master_key: str, value: str) -> str:
    return hmac.new(
        master_key.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, size: int = -1) -> bytes:
        return self.body

    def close(self):
        pass


class RecordingOpener:
    def __init__(self, body: bytes = b'{"ok":true,"removed":true}'):
        self.body = body
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.body)


class Sign402OperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.master_key = Fernet.generate_key().decode("ascii")
        self.fernet = Fernet(self.master_key.encode("ascii"))

    def make_config(self):
        operator = load_operator()
        return operator.OperatorConfig(
            user_wallet_db=self.root / "user-wallets.db",
            imessage_db=self.root / "imessage-approvals.db",
            bankr_llm_db=self.root / "bankr-llm.db",
            bitrefill_db=self.root / "bitrefill-orders.sqlite3",
            spend_limits_json=self.root / "user-spend-limits.json",
            master_key=self.master_key,
            gateway_url="http://127.0.0.1:8099",
            photon_api_token="photon-token",
        )

    def seed_user_wallet(self):
        db = sqlite3.connect(self.root / "user-wallets.db")
        with db:
            db.execute(
                """
                CREATE TABLE user_wallets (
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
                CREATE TABLE user_access_tokens (
                    telegram_user_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO user_wallets (
                    telegram_user_id, telegram_username, chain, wallet_address,
                    encrypted_private_key, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "123",
                    "alice",
                    "base",
                    "0x1111111111111111111111111111111111111111",
                    "super-secret-private-key",
                    "created",
                    1_800_000_000,
                    1_800_000_010,
                ),
            )
            db.execute(
                "INSERT INTO user_access_tokens VALUES (?, ?, ?)",
                ("123", "secret-token-hash", 1_800_000_011),
            )
        db.close()

    def seed_imessage(self):
        phone = "+15551234567"
        encrypted_phone = self.fernet.encrypt(phone.encode("utf-8")).decode("ascii")
        db = sqlite3.connect(self.root / "imessage-approvals.db")
        with db:
            db.execute(
                """
                CREATE TABLE imessage_links (
                    telegram_user_id TEXT PRIMARY KEY,
                    photon_digest TEXT NOT NULL UNIQUE,
                    encrypted_photon_user_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE imessage_approvals (
                    approval_id TEXT PRIMARY KEY,
                    telegram_user_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    commitment_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    decision_at INTEGER
                )
                """
            )
            db.execute(
                "INSERT INTO imessage_links VALUES (?, ?, ?, ?, ?)",
                (
                    "123",
                    hmac_digest(self.master_key, f"photon:{phone}"),
                    encrypted_phone,
                    1_800_000_020,
                    1_800_000_020,
                ),
            )
            db.execute(
                "INSERT INTO imessage_approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "approval_1",
                    "123",
                    "sign402_bitrefill",
                    "a" * 64,
                    "pending",
                    "{}",
                    "{}",
                    1_800_000_030,
                    1_900_000_000,
                    None,
                ),
            )
        db.close()

    def seed_limits(self):
        (self.root / "user-spend-limits.json").write_text(
            json.dumps(
                {
                    "limits": {
                        "123": {
                            "maxPerTxAtomic": 500000,
                            "dailyCapAtomic": 1000000,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def seed_bankr(self):
        db = sqlite3.connect(self.root / "bankr-llm.db")
        with db:
            db.execute(
                """
                CREATE TABLE bankr_llm_purchases (
                    purchase_id TEXT PRIMARY KEY,
                    telegram_user_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    state TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    bankr_wallet_address TEXT NOT NULL DEFAULT '',
                    encrypted_api_key TEXT NOT NULL DEFAULT '',
                    api_key_fingerprint TEXT NOT NULL DEFAULT '',
                    approval_request_id TEXT NOT NULL DEFAULT '',
                    source_wallet_address TEXT NOT NULL DEFAULT '',
                    singit_amount_atomic TEXT NOT NULL DEFAULT '',
                    commitment_hash TEXT NOT NULL DEFAULT '',
                    transfer_hash TEXT NOT NULL DEFAULT '',
                    topup_result_json TEXT NOT NULL DEFAULT '',
                    credits_json TEXT NOT NULL DEFAULT '',
                    baseline_credits_usd TEXT NOT NULL DEFAULT '',
                    payment_token_address TEXT NOT NULL DEFAULT '',
                    payment_token_symbol TEXT NOT NULL DEFAULT '',
                    payment_token_decimals TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    otp_attempts TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO bankr_llm_purchases (
                    purchase_id, telegram_user_id, email, amount_usd, state,
                    expires_at, api_key_fingerprint, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "llm_1",
                    "123",
                    "alice@example.com",
                    "1",
                    "BANKR_KEY_CREATED",
                    1_900_000_000,
                    "fingerprint",
                    1_800_000_040,
                    1_800_000_050,
                ),
            )
        db.close()

    def seed_bitrefill(self):
        db = sqlite3.connect(self.root / "bitrefill-orders.sqlite3")
        with db:
            db.execute(
                """
                CREATE TABLE bitrefill_orders (
                    quote_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    quote_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                "INSERT INTO bitrefill_orders VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "quote_1",
                    "DELIVERED",
                    json.dumps(
                        {
                            "quoteId": "quote_1",
                            "telegramUserId": "123",
                            "productName": "Bitrefill Gift Card",
                            "priceUsd": "0.10",
                        }
                    ),
                    "{}",
                    1_800_000_060,
                    1_800_000_070,
                ),
            )
        db.close()

    def test_user_report_includes_status_without_secret_material(self):
        operator = load_operator()
        self.seed_user_wallet()
        self.seed_imessage()
        self.seed_limits()
        self.seed_bankr()
        self.seed_bitrefill()

        report = operator.build_user_report(self.make_config(), "123")

        self.assertIn("Telegram: 123 (@alice)", report)
        self.assertIn("Wallet: 0x1111111111111111111111111111111111111111", report)
        self.assertIn("iMessage: linked +15551234567", report)
        self.assertIn("Pending approval: approval_1 sign402_bitrefill", report)
        self.assertIn("Limits: max 0.5 USDC / day 1 USDC", report)
        self.assertIn("Bankr LLM: BANKR_KEY_CREATED $1", report)
        self.assertIn("Bitrefill: DELIVERED Bitrefill Gift Card $0.10", report)
        self.assertNotIn("super-secret-private-key", report)
        self.assertNotIn("secret-token-hash", report)

    def test_find_imessage_locates_telegram_user_without_digest(self):
        operator = load_operator()
        self.seed_imessage()

        report = operator.find_imessage_report(self.make_config(), "+1 (555) 123-4567")

        self.assertIn("iMessage: +15551234567", report)
        self.assertIn("Telegram: 123", report)
        self.assertNotIn(hmac_digest(self.master_key, "photon:+15551234567"), report)

    def test_unlink_imessage_calls_gateway_with_photon_token(self):
        operator = load_operator()
        opener = RecordingOpener(
            b'{"ok":true,"removed":true,"telegramText":"iMessage approval link removed."}'
        )

        result = operator.unlink_imessage(
            self.make_config(),
            telegram_id="123",
            opener=opener,
        )

        self.assertEqual(result["telegramText"], "iMessage approval link removed.")
        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8099/agent/imessage/unlink",
        )
        self.assertEqual(timeout, 10.0)
        self.assertEqual(request.get_header("Authorization"), "Bearer photon-token")
        self.assertEqual(json.loads(request.data), {"telegramUserId": "123"})

    def test_unlink_imessage_requires_identifier(self):
        operator = load_operator()

        with self.assertRaises(SystemExit) as caught:
            operator.unlink_imessage(self.make_config())

        self.assertIn("telegram id or phone", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
