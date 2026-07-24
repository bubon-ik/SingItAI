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
