import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "scripts" / "install-hermes-wallet-plugin.sh"


class InstallerTests(unittest.TestCase):
    def make_environment(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        home = root / "home"
        fake_bin = root / "bin"
        home.mkdir()
        fake_bin.mkdir()
        call_log = root / "hermes-calls.log"
        hermes = fake_bin / "hermes"
        hermes.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HERMES_CALL_LOG"\n',
            encoding="utf-8",
        )
        hermes.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
                "HERMES_CALL_LOG": str(call_log),
                "SIGN402_PLUGIN_SOURCE": str(PLUGIN_DIR),
            }
        )
        return home, call_log, env

    def test_installer_links_plugin_and_enables_it(self):
        home, call_log, env = self.make_environment()

        result = subprocess.run(
            ["bash", str(INSTALLER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        destination = home / ".hermes" / "plugins" / "sign402-wallet"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(destination.resolve(), PLUGIN_DIR.resolve())
        self.assertEqual(
            call_log.read_text(encoding="utf-8").splitlines(),
            ["plugins enable sign402-wallet"],
        )
        self.assertNotIn("TOKEN", result.stdout.upper())

    def test_installer_is_idempotent_for_correct_symlink(self):
        home, call_log, env = self.make_environment()

        subprocess.run(
            ["bash", str(INSTALLER)],
            env=env,
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (home / ".hermes" / "plugins" / "sign402-wallet").resolve(),
            PLUGIN_DIR.resolve(),
        )
        self.assertEqual(
            call_log.read_text(encoding="utf-8").splitlines(),
            [
                "plugins enable sign402-wallet",
                "plugins enable sign402-wallet",
            ],
        )

    def test_installer_refuses_to_replace_existing_path(self):
        home, call_log, env = self.make_environment()
        destination = home / ".hermes" / "plugins" / "sign402-wallet"
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_text("keep", encoding="utf-8")

        result = subprocess.run(
            ["bash", str(INSTALLER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to replace", result.stderr.lower())
        self.assertEqual(
            (destination / "keep.txt").read_text(encoding="utf-8"),
            "keep",
        )
        self.assertFalse(call_log.exists())


if __name__ == "__main__":
    unittest.main()
