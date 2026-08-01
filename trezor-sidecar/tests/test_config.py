from decimal import Decimal
from pathlib import Path
from unittest import TestCase

from trezor_sidecar.config import RunnerSettings, SidecarSettings


class SidecarSettingsTests(TestCase):
    def test_live_mode_requires_every_secret_and_positive_cap(self):
        with self.assertRaisesRegex(ValueError, "SIGN402_TREZOR_MCP_TOKEN"):
            SidecarSettings.from_env({"SIGN402_TREZOR_POC_ENABLED": "1"})

    def test_sidecar_is_fixed_to_loopback_and_base(self):
        settings = SidecarSettings.from_env({
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_MCP_TOKEN": "mcp-secret",
            "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-secret",
            "SIGN402_TREZOR_POC_MAX_USD": "2.50",
            "SIGN402_TREZOR_BASE_RPC_URL": "https://base.example.invalid",
        })
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8111)
        self.assertEqual(settings.chain_id, 8453)
        self.assertEqual(settings.max_usd, Decimal("2.50"))
        self.assertEqual(
            settings.state_path,
            Path("~/.sign402-trezor-poc/state.db").expanduser(),
        )

    def test_live_sidecar_rejects_every_noncanonical_state_path(self):
        env = {
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_MCP_TOKEN": "mcp-secret",
            "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-secret",
            "SIGN402_TREZOR_POC_MAX_USD": "2.50",
            "SIGN402_TREZOR_BASE_RPC_URL": "https://base.example.invalid",
        }
        expected = Path("~/.sign402-trezor-poc/state.db").expanduser()
        accepted = SidecarSettings.from_env({
            **env,
            "SIGN402_TREZOR_STATE_PATH": "~/.sign402-trezor-poc/state.db",
        })
        self.assertEqual(accepted.state_path, expected)
        for path in (
            "/tmp/trezor-poc-test.db",
            "~/.sign402-trezor-poc/states.db",
            "../sign402-gateway/state.db",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(
                ValueError,
                "SIGN402_TREZOR_STATE_PATH",
            ):
                SidecarSettings.from_env({**env, "SIGN402_TREZOR_STATE_PATH": path})

    def test_runner_does_not_accept_trezor_mcp_token_as_configuration(self):
        settings = RunnerSettings.from_env({
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-secret",
            "SIGN402_TREZOR_POC_MAX_USD": "2.50",
            "BITREFILL_API_KEY": "bitrefill-secret",
            "SIGN402_TREZOR_MCP_TOKEN": "must-not-be-copied",
        })
        self.assertFalse(hasattr(settings, "mcp_token"))

    def test_non_literal_enabled_value_is_disabled_and_needs_no_secrets(self):
        settings = SidecarSettings.from_env({"SIGN402_TREZOR_POC_ENABLED": "true"})
        self.assertFalse(settings.enabled)

    def test_live_sidecar_rejects_non_https_or_decorated_rpc_urls(self):
        env = {
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_MCP_TOKEN": "mcp-secret",
            "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-secret",
            "SIGN402_TREZOR_POC_MAX_USD": "2.50",
        }
        with self.assertRaisesRegex(ValueError, "SIGN402_TREZOR_BASE_RPC_URL"):
            SidecarSettings.from_env({**env, "SIGN402_TREZOR_BASE_RPC_URL": "http://base.example.invalid"})
        with self.assertRaisesRegex(ValueError, "SIGN402_TREZOR_BASE_RPC_URL"):
            SidecarSettings.from_env({**env, "SIGN402_TREZOR_BASE_RPC_URL": "https://base.example.invalid/?key=value"})

    def test_settings_repr_redacts_all_credentials_and_credentialed_rpc_url(self):
        canaries = (
            "mcp-secret-canary",
            "sidecar-secret-canary",
            "bitrefill-secret-canary",
            "rpc-user-canary",
            "rpc-password-canary",
        )
        sidecar = SidecarSettings.from_env({
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_MCP_TOKEN": canaries[0],
            "SIGN402_TREZOR_SIDECAR_TOKEN": canaries[1],
            "SIGN402_TREZOR_POC_MAX_USD": "2.50",
            "SIGN402_TREZOR_BASE_RPC_URL": (
                f"https://{canaries[3]}:{canaries[4]}@base.example.invalid"
            ),
        })
        runner = RunnerSettings.from_env({
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_SIDECAR_TOKEN": canaries[1],
            "SIGN402_TREZOR_POC_MAX_USD": "2.50",
            "BITREFILL_API_KEY": canaries[2],
        })

        rendered = repr(sidecar) + repr(runner)

        for canary in canaries:
            self.assertNotIn(canary, rendered)
