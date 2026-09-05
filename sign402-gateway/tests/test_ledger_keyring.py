"""The master key through the Ledger Key Ring.

No hardware, and no `WALLET_CLI_MOCK=1` either — Phase 0 established that the
flag swaps wallet-cli's trustchain backend and leaves the device transport
alone, so `ring init` still demands a device and CI still cannot run it. What
these tests do instead is point `SIGN402_LEDGER_WALLET_CLI` at a stand-in
binary and assert on the command line the gateway builds and what it does with
each kind of answer.

That is the right seam anyway. The thing worth testing is not whether Ledger's
AES works; it is whether *we* refuse to boot when the decrypt fails, and
whether the plaintext ever touches disk.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from sign402_gateway import keyring
from sign402_gateway.keyring import (
    LedgerKeyringError,
    install_master_key,
    keyring_enabled,
    load_master_key,
)

VALID_KEY = Fernet.generate_key().decode("ascii")


def fake_cli(script_body: str) -> str:
    """A stand-in for `wallet-cli` that records its argv and answers as told."""
    directory = Path(tempfile.mkdtemp())
    path = directory / "wallet-cli"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os, pathlib\n"
        "pathlib.Path(os.environ['FAKE_CLI_ARGV']).write_text('\\n'.join(sys.argv[1:]))\n"
        + script_body
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def env_for(cli: str, enc: Path, **overrides) -> dict:
    values = {
        "SIGN402_LEDGER_KEYRING_ENABLED": "1",
        "SIGN402_LEDGER_WALLET_CLI": cli,
        "SIGN402_LEDGER_KEYRING_FILE": str(enc),
        "SIGN402_LEDGER_KEYRING_KEY": "sign402-master",
        "WALLET_PASS": "test-pass",
        "FAKE_CLI_ARGV": str(enc.parent / "argv.txt"),
    }
    values.update(overrides)
    return values


class SwitchedOffTests(unittest.TestCase):
    """A box that was never provisioned has to behave exactly as it did."""

    def test_the_ring_is_off_unless_it_is_turned_on(self):
        self.assertFalse(keyring_enabled({}))
        for value in ("0", "false", "no", "off", "", "  "):
            with self.subTest(value=value):
                self.assertFalse(
                    keyring_enabled({"SIGN402_LEDGER_KEYRING_ENABLED": value})
                )
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(
                    keyring_enabled({"SIGN402_LEDGER_KEYRING_ENABLED": value})
                )

    def test_with_the_ring_off_the_environment_variable_is_the_key(self):
        self.assertEqual(
            load_master_key({"SIGN402_WALLET_MASTER_KEY": VALID_KEY}), VALID_KEY
        )

    def test_with_the_ring_off_an_absent_key_is_still_absent(self):
        """Not an error here. The call sites that need it already say so."""
        self.assertEqual(load_master_key({}), "")


class DecryptTests(unittest.TestCase):
    def setUp(self):
        self.enc = Path(tempfile.mkdtemp()) / "master-key.enc"
        self.enc.write_bytes(b"ciphertext")
        self.argv = self.enc.parent / "argv.txt"

    def cli_that_prints(self, value: str) -> str:
        return fake_cli(f"sys.stdout.write({value!r})\n")

    def test_a_good_decrypt_returns_the_key(self):
        cli = self.cli_that_prints(VALID_KEY + "\n")

        self.assertEqual(
            load_master_key(env_for(cli, self.enc)), VALID_KEY
        )

    def test_the_plaintext_is_read_from_stdout_and_never_written_to_disk(self):
        """`-o` would put a decrypted master key on the filesystem.

        That is the same vulnerability this change exists to remove, with extra
        steps. The command is asserted argument by argument rather than by
        grepping the source, so a later refactor cannot reintroduce it quietly.
        """
        cli = self.cli_that_prints(VALID_KEY)

        load_master_key(env_for(cli, self.enc))

        argv = self.argv.read_text().splitlines()
        self.assertEqual(
            argv,
            ["ring", "decrypt", "--key", "sign402-master", "-i", str(self.enc)],
        )
        self.assertNotIn("-o", argv)
        self.assertNotIn("--out", argv)

    def test_the_key_name_is_configurable(self):
        cli = self.cli_that_prints(VALID_KEY)

        load_master_key(
            env_for(cli, self.enc, SIGN402_LEDGER_KEYRING_KEY="other-name")
        )

        self.assertIn("other-name", self.argv.read_text().splitlines())

    def test_wallet_pass_reaches_the_cli(self):
        """The documented way to run wallet-cli unattended on a server."""
        cli = fake_cli(
            "sys.stdout.write(os.environ.get('WALLET_PASS', 'MISSING'))\n"
        )

        self.assertEqual(
            load_master_key(
                env_for(cli, self.enc, WALLET_PASS=VALID_KEY)
            ),
            VALID_KEY,
        )


class RefusalTests(unittest.TestCase):
    """Every one of these is a service that does not start.

    There is no fallback to the plaintext environment variable while the ring
    is on, and that is the security property rather than an oversight: a silent
    fallback is how the wrong key gets deployed, and the way you find out is a
    customer whose wallet will not open.
    """

    def setUp(self):
        self.enc = Path(tempfile.mkdtemp()) / "master-key.enc"
        self.enc.write_bytes(b"ciphertext")

    def test_a_failed_decrypt_refuses_to_boot_and_names_the_file_and_key(self):
        cli = fake_cli(
            "sys.stderr.write('Ledger Key Ring not initialized.')\n"
            "sys.exit(3)\n"
        )

        with self.assertRaises(LedgerKeyringError) as raised:
            load_master_key(
                env_for(cli, self.enc, SIGN402_WALLET_MASTER_KEY=VALID_KEY)
            )

        message = str(raised.exception)
        self.assertIn(str(self.enc), message)
        self.assertIn("sign402-master", message)
        self.assertIn("Ledger Key Ring not initialized", message)

    def test_a_corrupt_ciphertext_does_not_fall_back_to_the_environment(self):
        """The one that matters most, so it is asserted rather than assumed."""
        cli = fake_cli("sys.stderr.write('bad ciphertext')\nsys.exit(1)\n")

        with self.assertRaises(LedgerKeyringError):
            load_master_key(
                env_for(cli, self.enc, SIGN402_WALLET_MASTER_KEY=VALID_KEY)
            )

    def test_an_empty_decrypt_refuses_to_boot(self):
        cli = fake_cli("pass\n")

        with self.assertRaises(LedgerKeyringError) as raised:
            load_master_key(env_for(cli, self.enc))

        self.assertIn("returned nothing", str(raised.exception))

    def test_a_decrypted_value_that_is_not_a_fernet_key_refuses_to_boot(self):
        """Caught here rather than at the first wallet a customer opens."""
        cli = fake_cli("sys.stdout.write('not-a-fernet-key')\n")

        with self.assertRaises(LedgerKeyringError) as raised:
            load_master_key(env_for(cli, self.enc))

        message = str(raised.exception)
        self.assertIn("not a valid Fernet key", message)
        self.assertNotIn("not-a-fernet-key", message)

    def test_the_failure_message_never_echoes_the_decrypted_value(self):
        cli = fake_cli("sys.stdout.write('SECRET-PLAINTEXT-VALUE')\n")

        with self.assertRaises(LedgerKeyringError) as raised:
            load_master_key(env_for(cli, self.enc))

        self.assertNotIn("SECRET-PLAINTEXT-VALUE", str(raised.exception))

    def test_a_missing_file_setting_refuses_to_boot(self):
        with self.assertRaises(LedgerKeyringError) as raised:
            load_master_key(
                {
                    "SIGN402_LEDGER_KEYRING_ENABLED": "1",
                    "SIGN402_WALLET_MASTER_KEY": VALID_KEY,
                }
            )

        self.assertIn("SIGN402_LEDGER_KEYRING_FILE", str(raised.exception))

    def test_a_missing_wallet_cli_refuses_to_boot_and_says_how_to_fix_it(self):
        with self.assertRaises(LedgerKeyringError) as raised:
            load_master_key(
                env_for("/nonexistent/wallet-cli", self.enc)
            )

        message = str(raised.exception)
        self.assertIn("/nonexistent/wallet-cli", message)
        self.assertIn("SIGN402_LEDGER_WALLET_CLI", message)

    def test_a_hung_cli_refuses_to_boot_rather_than_hanging_forever(self):
        cli = fake_cli("import time\ntime.sleep(30)\n")

        with self.assertRaises(LedgerKeyringError) as raised:
            load_master_key(
                env_for(
                    cli, self.enc, SIGN402_LEDGER_KEYRING_TIMEOUT_SECONDS="1"
                )
            )

        self.assertIn("did not finish", str(raised.exception))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.enc = Path(tempfile.mkdtemp()) / "master-key.enc"
        self.enc.write_bytes(b"ciphertext")

    def test_the_key_is_put_where_the_rest_of_the_gateway_looks_for_it(self):
        """Eight call sites keep reading the environment variable unchanged."""
        cli = fake_cli(f"sys.stdout.write({VALID_KEY!r})\n")
        env = env_for(cli, self.enc)

        install_master_key(env)

        self.assertEqual(env["SIGN402_WALLET_MASTER_KEY"], VALID_KEY)

    def test_installing_with_the_ring_off_leaves_the_environment_alone(self):
        env = {"SIGN402_WALLET_MASTER_KEY": VALID_KEY}

        self.assertEqual(install_master_key(env), VALID_KEY)
        self.assertEqual(env, {"SIGN402_WALLET_MASTER_KEY": VALID_KEY})

    def test_installing_on_an_unconfigured_box_writes_nothing(self):
        env: dict[str, str] = {}

        self.assertEqual(install_master_key(env), "")
        self.assertEqual(env, {})


class SourceTests(unittest.TestCase):
    def test_the_module_never_names_the_output_flag(self):
        """A belt-and-braces reading of the acceptance criterion in the spec.

        `test_the_plaintext_is_read_from_stdout_and_never_written_to_disk` is
        the real check — it asserts the argv that is actually executed. This
        one catches an `-o` added to some other command in the same module,
        which that test would not see.
        """
        source = Path(keyring.__file__).read_text()
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        # Docstrings talk about `-o` on purpose; the executable lines must not.
        for forbidden in ('"-o"', "'-o'", '"--out"', "'--out'"):
            self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main()
