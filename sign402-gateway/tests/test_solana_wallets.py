import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sign402_gateway.solana_keys import b58decode, b58encode, keypair_address
from sign402_gateway.solana_wallets import ManagedWalletService
from sign402_gateway.user_wallets import UserWalletStore, WalletEncryptionError, build_wallet_service_from_env


class SolanaWalletTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'wallets.db'
        self.key = Fernet.generate_key().decode()
        self.store = UserWalletStore(self.path)
        self.service = ManagedWalletService(store=self.store, master_key=self.key,
                                           solana_balance_provider=lambda _: {'SOL': '0.000000000', 'USDC': '1.250000'})

    def test_base_and_solana_are_independent_for_each_user(self):
        base = self.service.create_wallet('alice')
        sol = self.service.create_wallet('alice', chain='solana')
        bob = self.service.create_wallet('bob', chain='solana')
        self.assertEqual(self.service.wallet_status('alice')['wallet'], base['wallet'])
        self.assertEqual(self.service.wallet_status('alice', chain='solana')['wallet'], sol['wallet'])
        self.assertNotEqual(sol['wallet']['address'], bob['wallet']['address'])
        self.assertFalse(sol['wallet']['spendingEnabled'])
        self.assertEqual(self.service.resolve_telegram_user_id(sol['accessToken']), 'alice')
        self.assertEqual(self.service.resolve_telegram_user_id(base['accessToken']), 'alice')
        self.assertEqual(len(b58decode(sol['wallet']['address'])), 32)

    def test_solana_only_user_has_no_base_wallet(self):
        self.service.create_wallet('alice', chain='solana')
        self.assertIsNone(self.store.get_wallet_by_telegram_user_id('alice'))
        self.assertFalse(self.service.wallet_status('alice')['ok'])
        with self.assertRaisesRegex(ValueError, 'wallet not found'):
            self.service.decrypt_private_key_for_future_signing('alice')

    def test_key_is_encrypted_bound_and_not_returned_or_logged(self):
        created = self.service.create_wallet('alice', chain='solana')
        key = self.service.decrypt_private_key_for_future_signing('alice', chain='solana')
        self.assertEqual(keypair_address(key), created['wallet']['address'])
        raw = b58decode(key)
        self.assertEqual(len(raw), 64)
        signer = Ed25519PrivateKey.from_private_bytes(raw[:32])
        signer.public_key().verify(signer.sign(b'test-only'), b'test-only')
        self.assertNotIn(key, json.dumps(created))
        self.assertNotIn(key.encode(), self.path.read_bytes())
        with sqlite3.connect(self.path) as db:
            events = db.execute('SELECT * FROM user_wallet_audit').fetchall()
        self.assertNotIn(key, str(events))
        stored = self.store.get_wallet_by_telegram_user_id('alice', chain='solana')
        plaintext = json.loads(Fernet(self.key.encode()).decrypt(stored['encrypted_private_key'].encode()))
        self.assertEqual(plaintext['telegramUserId'], 'alice')
        self.assertEqual(plaintext['chain'], 'solana')

    def test_ciphertext_cannot_be_reassigned_to_another_user(self):
        self.service.create_wallet('alice', chain='solana')
        self.service.create_wallet('bob', chain='solana')
        alice = self.store.get_wallet_by_telegram_user_id('alice', chain='solana')
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE solana_user_wallets SET encrypted_private_key=? WHERE telegram_user_id=?',
                       (alice['encrypted_private_key'], 'bob'))
        with self.assertRaisesRegex(WalletEncryptionError, 'could not be decrypted'):
            self.service.decrypt_private_key_for_future_signing('bob', chain='solana')

    def test_missing_or_wrong_encryption_key_fails_without_disclosing_secrets(self):
        other = ManagedWalletService(store=self.store, master_key='')
        with self.assertRaises(WalletEncryptionError):
            other.create_wallet('alice', chain='solana')
        self.assertIsNone(self.store.get_wallet_by_telegram_user_id('alice', chain='solana'))
        self.service.create_wallet('alice', chain='solana')
        other.master_key = Fernet.generate_key().decode()
        with self.assertRaisesRegex(WalletEncryptionError, '^Solana wallet private key could not be decrypted$'):
            other.decrypt_private_key_for_future_signing('alice', chain='solana')

    def test_reopen_preserves_key_and_concurrent_creation_has_one_winner(self):
        other = ManagedWalletService(store=UserWalletStore(self.path), master_key=self.key)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: (self.service if i % 2 else other).create_wallet('alice', chain='solana'), range(8)))
        self.assertEqual(sum(r['created'] for r in results), 1)
        self.assertEqual(len({r['wallet']['address'] for r in results}), 1)
        self.assertEqual(other.decrypt_private_key_for_future_signing('alice', chain='solana'),
                         self.service.decrypt_private_key_for_future_signing('alice', chain='solana'))

    def test_additive_migration_preserves_legacy_base_row_and_access_token(self):
        base = self.service.create_wallet('alice')
        before = self.store.get_wallet_by_telegram_user_id('alice')
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE solana_user_wallets')
        reopened = UserWalletStore(self.path)
        self.assertEqual(before, reopened.get_wallet_by_telegram_user_id('alice'))
        self.assertEqual(reopened.resolve_telegram_user_id(base['accessToken']), 'alice')
        other = ManagedWalletService(store=reopened, master_key=self.key)
        other.create_wallet('alice', chain='solana')
        self.assertEqual(before, reopened.get_wallet_by_telegram_user_id('alice'))

    def test_unknown_network_is_rejected_before_creating_any_wallet(self):
        for chain in ['devnet', 'SOLANA', '', None, [], 'base; DROP TABLE user_wallets']:
            for operation in [self.service.create_wallet, self.service.wallet_status,
                              self.service.wallet_balance, self.service.decrypt_private_key_for_future_signing]:
                with self.subTest(chain=chain, operation=operation.__name__), self.assertRaises(ValueError):
                    operation('alice', chain=chain)
        self.assertIsNone(self.store.get_wallet_by_telegram_user_id('alice'))

    def test_balance_reads_correct_wallet_without_base_fallback(self):
        self.service.create_wallet('alice')
        self.assertFalse(self.service.wallet_balance('alice', chain='solana')['ok'])
        created = self.service.create_wallet('alice', chain='solana')
        seen = []
        self.service.solana_balance_provider = lambda address: seen.append(address) or {'SOL': '0', 'USDC': '7'}
        result = self.service.wallet_balance('alice', chain='solana')
        self.assertEqual(seen, [created['wallet']['address']])
        self.assertEqual(result['balances'], {'SOL': '0', 'USDC': '7'})
        self.assertNotIn('ETH', result['telegramText'])

    def test_rpc_error_is_not_reported_as_zero_or_leaked(self):
        self.service.create_wallet('alice', chain='solana')
        def fail(_):
            raise RuntimeError('provider-secret-token')
        self.service.solana_balance_provider = fail
        result = self.service.wallet_balance('alice', chain='solana')
        self.assertTrue(result['balanceUnavailable'])
        self.assertNotIn('balances', result)
        self.assertNotIn('provider-secret-token', json.dumps(result))

    def test_factory_builds_multi_chain_service(self):
        service = build_wallet_service_from_env(env={'SIGN402_WALLET_MASTER_KEY': self.key}, store_path=self.path)
        self.assertEqual(service.create_wallet('alice', chain='solana')['wallet']['chain'], 'solana')

    def test_base58_vectors_preserve_leading_zero_bytes(self):
        for raw, encoded in [(b'\0' * 32, '1' * 32), (b'\0\0\1', '112'), (b'hello world', 'StV1DL6CwTryKyV')]:
            self.assertEqual(b58encode(raw), encoded)
            self.assertEqual(b58decode(encoded), raw)
        with self.assertRaises(ValueError):
            b58decode('0OIl')
