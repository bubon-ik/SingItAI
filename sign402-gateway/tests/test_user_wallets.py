import base64
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.fernet import Fernet

from sign402_gateway.base_balances import AlchemyBaseBalanceProvider
from sign402_gateway.user_wallets import (
    ManagedBaseWalletService,
    UserWalletStore,
    WalletEncryptionError,
    build_wallet_service_from_env,
)


def test_master_key() -> str:
    return Fernet.generate_key().decode("ascii")


class UserWalletTests(unittest.TestCase):
    def make_service(
        self,
        master_key: str | None = None,
        balance_provider=None,
    ):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = UserWalletStore(Path(tmp.name) / "wallets.db")
        service = ManagedBaseWalletService(
            store=store,
            master_key=master_key or test_master_key(),
            balance_provider=balance_provider,
        )
        return service, store

    def test_issue_and_resolve_per_user_access_token(self):
        _service, store = self.make_service()

        token = store.issue_access_token("1045618308")

        self.assertTrue(token)
        self.assertEqual(store.resolve_telegram_user_id(token), "1045618308")
        # Only the hash is stored, never the plaintext token.
        self.assertNotIn(token, store.path.read_bytes().decode("utf-8", "replace"))
        # Bogus / empty tokens resolve to nothing.
        self.assertIsNone(store.resolve_telegram_user_id("bogus-token"))
        self.assertIsNone(store.resolve_telegram_user_id(""))

    def test_reissuing_access_token_revokes_the_previous_one(self):
        _service, store = self.make_service()

        first = store.issue_access_token("1045618308")
        second = store.issue_access_token("1045618308")

        self.assertNotEqual(first, second)
        self.assertIsNone(store.resolve_telegram_user_id(first))
        self.assertEqual(store.resolve_telegram_user_id(second), "1045618308")

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

    def test_create_wallet_is_idempotent_for_concurrent_calls(self):
        service, _store = self.make_service()

        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(
                executor.map(lambda _index: service.create_wallet("1045618308"), range(16))
            )

        addresses = {result["wallet"]["address"] for result in results}
        created_count = sum(1 for result in results if result["created"])
        self.assertEqual(created_count, 1)
        self.assertEqual(len(addresses), 1)

    def test_store_uses_private_directory_and_database_permissions(self):
        if os.name != "posix":
            self.skipTest("POSIX mode bits are not available on this platform")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store_path = Path(tmp.name) / "wallets" / "wallets.db"

        UserWalletStore(store_path)

        self.assertEqual(store_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(store_path.stat().st_mode & 0o777, 0o600)

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
        self.assertIsNone(service.balance_provider)

    def test_build_wallet_service_from_env_configures_alchemy_balance_provider(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        singit_address = "0x" + "44" * 20

        service = build_wallet_service_from_env(
            env={
                "SIGN402_WALLET_MASTER_KEY": test_master_key(),
                "SIGN402_BASE_RPC_URL": (
                    "https://base-mainnet.g.alchemy.com/v2/private-key"
                ),
                "SIGN402_SINGIT_TOKEN_ADDRESS": singit_address,
            },
            store_path=Path(tmp.name) / "wallets.db",
        )

        self.assertIsInstance(
            service.balance_provider,
            AlchemyBaseBalanceProvider,
        )
        self.assertEqual(
            service.balance_provider.singit_token_address,
            singit_address,
        )

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

    def test_balance_degrades_when_provider_raises(self):
        def failing_balance_provider(_address: str) -> dict[str, str]:
            raise RuntimeError("rpc down")

        service, _store = self.make_service()
        service.balance_provider = failing_balance_provider
        created = service.create_wallet("1045618308")

        result = service.wallet_balance("1045618308")

        self.assertTrue(result["ok"])
        self.assertEqual(result["wallet"]["address"], created["wallet"]["address"])
        self.assertTrue(result["balanceUnavailable"])
        self.assertIn("Balance lookup is unavailable", result["telegramText"])
        self.assertNotIn("private", str(result).lower())

    def test_balance_accepts_structured_alchemy_provider_response(self):
        token_address = "0x" + "11" * 20

        def balance_provider(_address: str):
            return {
                "balances": {
                    "SINGIT": "250",
                    "ETH": "0.001",
                    "USDC": "12.5",
                },
                "unverifiedTokens": [
                    {
                        "symbol": "OTHER",
                        "contractAddress": token_address,
                        "balance": "3",
                    }
                ],
            }

        service, _store = self.make_service(balance_provider=balance_provider)
        service.create_wallet("1045618308")

        result = service.wallet_balance("1045618308")

        self.assertFalse(result["balanceUnavailable"])
        self.assertEqual(
            result["balances"],
            {"SINGIT": "250", "ETH": "0.001", "USDC": "12.5"},
        )
        self.assertEqual(result["unverifiedTokens"][0]["symbol"], "OTHER")
        text = result["telegramText"]
        self.assertLess(text.index("- ETH:"), text.index("- USDC:"))
        self.assertLess(text.index("- USDC:"), text.index("- SINGIT:"))
        self.assertIn("Unverified tokens (not enabled for spending)", text)
        self.assertIn("OTHER: 3 (0x111111...1111)", text)

    def test_balance_keeps_legacy_dictionary_provider_compatible(self):
        service, _store = self.make_service(
            balance_provider=lambda _address: {
                "USDC": "2",
                "ETH": "0.5",
            }
        )
        service.create_wallet("1045618308")

        result = service.wallet_balance("1045618308")

        self.assertFalse(result["balanceUnavailable"])
        self.assertEqual(result["balances"], {"USDC": "2", "ETH": "0.5"})
        self.assertNotIn("unverifiedTokens", result)
        self.assertLess(
            result["telegramText"].index("- ETH:"),
            result["telegramText"].index("- USDC:"),
        )

    def test_withdrawable_tokens_includes_trusted_and_discovered_erc20(self):
        other_token = "0x" + "22" * 20

        def balance_provider(_address: str):
            return {
                "balances": {
                    "ETH": "0.001",
                    "USDC": "12.5",
                    "SINGIT": "250",
                },
                "unverifiedTokens": [
                    {
                        "symbol": "OTHER",
                        "contractAddress": other_token,
                        "balance": "3",
                        "decimals": 8,
                    }
                ],
            }

        service, _store = self.make_service(balance_provider=balance_provider)
        service.create_wallet("1045618308")

        result = service.withdrawable_tokens("1045618308")

        self.assertTrue(result["ok"])
        self.assertFalse(result["balanceUnavailable"])
        symbols = [token["symbol"] for token in result["tokens"]]
        self.assertEqual(symbols, ["USDC", "SINGIT", "OTHER"])
        self.assertEqual(result["tokens"][0]["decimals"], 6)
        self.assertEqual(result["tokens"][0]["balance"], "12.5")
        self.assertEqual(result["tokens"][1]["decimals"], 18)
        self.assertEqual(result["tokens"][2]["contractAddress"], other_token)
        self.assertEqual(result["tokens"][2]["decimals"], 8)
        self.assertNotIn("ETH", symbols)
        self.assertIn("Choose a token to withdraw", result["telegramText"])


if __name__ == "__main__":
    unittest.main()
