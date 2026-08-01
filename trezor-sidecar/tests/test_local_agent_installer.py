import os
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase


SIDECAR_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = SIDECAR_ROOT / "scripts" / "install-local-hermes-plugin.sh"
PLUGIN_DIR = SIDECAR_ROOT / "hermes-local-plugin"


class LocalAgentInstallerTests(TestCase):
    def make_environment(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        real_home = root / "real-home"
        local_home = real_home / ".sign402-trezor-agent"
        binaries = root / "bin"
        real_home.mkdir()
        binaries.mkdir()
        calls = root / "calls.log"
        hermes = binaries / "hermes"
        hermes.write_text(
            '#!/bin/sh\nprintf "%s|%s\\n" "$HOME" "$*" >> "$HERMES_CALL_LOG"\n',
            encoding="utf-8",
        )
        hermes.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(real_home),
                "PATH": f"{binaries}{os.pathsep}{env.get('PATH', '')}",
                "HERMES_CALL_LOG": str(calls),
                "SIGN402_TREZOR_LOCAL_AGENT_HOME": str(local_home),
                "SIGN402_TREZOR_LOCAL_PLUGIN_SOURCE": str(PLUGIN_DIR),
            }
        )
        return real_home, local_home, calls, env

    def test_installs_and_enables_only_under_dedicated_home(self):
        real_home, local_home, calls, env = self.make_environment()

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        destination = local_home / ".hermes" / "plugins" / "sign402-trezor-local"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(destination.resolve(), PLUGIN_DIR.resolve())
        self.assertFalse((real_home / ".hermes").exists())
        self.assertFalse(calls.exists())
        self.assertIn("dedicated Hermes", result.stdout)

    def test_refuses_missing_or_production_home_and_existing_path(self):
        real_home, local_home, calls, env = self.make_environment()
        for configured in (None, str(real_home), str(real_home / ".hermes" / "local")):
            case_env = dict(env)
            if configured is None:
                case_env.pop("SIGN402_TREZOR_LOCAL_AGENT_HOME")
            else:
                case_env["SIGN402_TREZOR_LOCAL_AGENT_HOME"] = configured
            result = subprocess.run(
                ["sh", str(INSTALLER)],
                env=case_env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)

        destination = local_home / ".hermes" / "plugins" / "sign402-trezor-local"
        destination.mkdir(parents=True)
        (destination / "keep.txt").write_text("keep", encoding="utf-8")
        result = subprocess.run(
            ["sh", str(INSTALLER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((destination / "keep.txt").read_text(), "keep")
        self.assertFalse(calls.exists())
