import os
from pathlib import Path
from unittest import TestCase

from trezor_sidecar.remote_agent import RemoteAgentSettings


class RemoteAgentSettingsTests(TestCase):
    def valid(self):
        return {
            "SIGN402_TREZOR_REMOTE_AGENT_ENABLED": "1",
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_REMOTE_PURCHASES_ENABLED": "0",
            "SIGN402_TREZOR_REMOTE_USER_ID": "12345",
            "SIGN402_TREZOR_BROKER_URL": "http://127.0.0.1:8122",
            "SIGN402_TREZOR_BROKER_INTERNAL_TOKEN": "x" * 32,
            "SIGN402_TREZOR_POC_MAX_USD": "1.00",
        }

    def test_all_gates_default_off_and_disabled_needs_no_credentials(self):
        settings = RemoteAgentSettings.from_env({})
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.purchases_enabled)

    def test_status_only_mode_does_not_require_bitrefill_key(self):
        settings = RemoteAgentSettings.from_env(self.valid())
        self.assertTrue(settings.enabled)
        self.assertFalse(settings.purchases_enabled)
        self.assertEqual(settings.bitrefill_api_key, "")

    def test_live_gate_requires_bitrefill_key_and_one_user(self):
        env = self.valid() | {"SIGN402_TREZOR_REMOTE_PURCHASES_ENABLED": "1"}
        with self.assertRaisesRegex(ValueError, "BITREFILL_API_KEY"):
            RemoteAgentSettings.from_env(env)
        settings = RemoteAgentSettings.from_env(env | {"BITREFILL_API_KEY": "secret"})
        self.assertTrue(settings.purchases_enabled)
        self.assertEqual(settings.user_id, "12345")

    def test_remote_state_cannot_use_production_paths(self):
        env = self.valid() | {
            "SIGN402_TREZOR_REMOTE_STATE_PATH": str(Path.home() / ".sign402" / "wallets.db")
        }
        with self.assertRaisesRegex(ValueError, "state"):
            RemoteAgentSettings.from_env(env)
