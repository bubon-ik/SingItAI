import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from sign402_gateway.secure_state import SensitiveStateCipher, SensitiveStateError
from sign402_gateway.user_emails import BuyerEmailStore, mask_email


def make_cipher() -> SensitiveStateCipher:
    return SensitiveStateCipher(Fernet.generate_key().decode("ascii"))


def raw_rows(path: Path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute("SELECT * FROM buyer_emails").fetchall()
    finally:
        connection.close()
    return json.dumps(rows)


class BuyerEmailStoreTests(unittest.TestCase):
    """Guest checkout needs an address per buyer, so we hold personal data."""

    def _store(self, root, **kwargs):
        kwargs.setdefault("cipher", make_cipher())
        return BuyerEmailStore(Path(root) / "private" / "emails.sqlite3", **kwargs)

    def test_an_address_round_trips(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)

            store.set_email("u1", "Buyer@Example.com")

            self.assertEqual(store.get_email("u1"), "buyer@example.com")

    def test_the_address_is_never_stored_in_the_clear(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "private" / "emails.sqlite3"
            store = BuyerEmailStore(path, cipher=make_cipher())

            store.set_email("u1", "buyer@example.com")

            self.assertNotIn("buyer@example.com", raw_rows(path))

    def test_an_unset_buyer_reads_as_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)

            self.assertIsNone(store.get_email("nobody"))

    def test_setting_an_address_again_replaces_it(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            store.set_email("u1", "first@example.com")

            store.set_email("u1", "second@example.com")

            self.assertEqual(store.get_email("u1"), "second@example.com")

    def test_forgetting_removes_the_address(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            store.set_email("u1", "buyer@example.com")

            self.assertTrue(store.forget_email("u1"))

            self.assertIsNone(store.get_email("u1"))
            self.assertFalse(store.forget_email("u1"))

    def test_an_invalid_address_is_refused_before_storage(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)

            for value in ("", "   ", "no-at-sign", "who@nodot", "a b@example.com"):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        store.set_email("u1", value)

            self.assertIsNone(store.get_email("u1"))

    def test_a_rejected_address_is_not_repeated_in_the_error(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)

            with self.assertRaises(ValueError) as captured:
                store.set_email("u1", "bad-address")

            self.assertNotIn("bad-address", str(captured.exception))

    def test_storing_without_a_cipher_fails_rather_than_writing_plaintext(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "emails.sqlite3"
            store = BuyerEmailStore(path, cipher=None)

            with self.assertRaises(SensitiveStateError):
                store.set_email("u1", "buyer@example.com")

            self.assertNotIn("buyer@example.com", raw_rows(path))


class MaskEmailTests(unittest.TestCase):
    """Responses confirm which address is on file without disclosing it."""

    def test_it_keeps_only_the_first_character_and_the_domain(self):
        self.assertEqual(mask_email("buyer@example.com"), "b***@example.com")

    def test_a_single_character_local_part_still_masks(self):
        self.assertEqual(mask_email("a@example.com"), "***@example.com")

    def test_nothing_masks_to_nothing(self):
        self.assertEqual(mask_email(""), "")


if __name__ == "__main__":
    unittest.main()
