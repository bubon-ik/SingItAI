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
        service, _store = self.make_service(
            master_key=base64.urlsafe_b64encode(b"short").decode("ascii")
        )

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
