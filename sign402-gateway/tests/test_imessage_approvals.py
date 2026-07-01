import base64
import os
import tempfile
import time
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from sign402_gateway.imessage_approvals import (
    HermesCliNotifier,
    ImessageApprovalService,
    ImessageApprovalStore,
    normalize_e164,
)
from sign402_gateway.user_wallets import ManagedBaseWalletService, UserWalletStore


def test_master_key() -> str:
    return Fernet.generate_key().decode("ascii")


class RecordingNotifier:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.messages = []

    def send(self, *, photon_user_id: str, message: str) -> dict[str, object]:
        self.messages.append({"photonUserId": photon_user_id, "message": message})
        return {"ok": self.ok, "stdout": "", "stderr": ""}


class ImessageApprovalTests(unittest.TestCase):
    def make_service(self, *, notifier: RecordingNotifier | None = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        master_key = test_master_key()
        wallet_store = UserWalletStore(Path(tmp.name) / "wallets.db")
        wallet_service = ManagedBaseWalletService(
            store=wallet_store,
            master_key=master_key,
        )
        approval_store = ImessageApprovalStore(Path(tmp.name) / "approvals.db")
        service = ImessageApprovalService(
            store=approval_store,
            wallet_service=wallet_service,
            master_key=master_key,
            notifier=notifier or RecordingNotifier(),
            now=lambda: 1_800_000_000,
        )
        return service, wallet_service, approval_store

    def make_linked_service(self):
        notifier = RecordingNotifier()
        service, wallet_service, store = self.make_service(notifier=notifier)
        wallet_service.create_wallet("1045618308")
        pairing = service.create_pairing("1045618308")
        service.link_photon_sender(pairing["code"], "+1 (555) 123-4567")
        return service, wallet_service, store, notifier

    def test_normalize_e164_accepts_common_formatting(self):
        self.assertEqual(normalize_e164("+1 (555) 123-4567"), "+15551234567")

    def test_normalize_e164_rejects_non_e164_values(self):
        for value in ("5551234567", "+012345", "+123", "+15551234567 ext 9"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_e164(value)

    def test_pairing_code_links_encrypted_phone_to_existing_wallet(self):
        service, wallet_service, store = self.make_service()
        wallet_service.create_wallet("1045618308", "AlpskyKnedlik")

        pairing = service.create_pairing("1045618308")
        result = service.link_photon_sender(pairing["code"], "+1 (555) 123-4567")

        self.assertTrue(result["ok"])
        self.assertEqual(result["telegramUserId"], "1045618308")
        self.assertIn("linked", result["imessageText"].lower())
        raw_database = Path(store.path).read_bytes()
        self.assertNotIn(b"+15551234567", raw_database)
        self.assertNotIn(pairing["code"].encode("ascii"), raw_database)

    def test_pairing_requires_existing_wallet(self):
        service, _wallet_service, _store = self.make_service()

        result = service.create_pairing("1045618308")

        self.assertFalse(result["ok"])
        self.assertIn("No Base agent wallet", result["telegramText"])

    def test_pairing_code_is_single_use(self):
        service, wallet_service, _store = self.make_service()
        wallet_service.create_wallet("1045618308")
        pairing = service.create_pairing("1045618308")

        first = service.link_photon_sender(pairing["code"], "+15551234567")
        second = service.link_photon_sender(pairing["code"], "+15551234567")

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("invalid", second["imessageText"].lower())

    def test_one_photon_sender_cannot_link_two_telegram_users(self):
        service, wallet_service, _store = self.make_service()
        wallet_service.create_wallet("1045618308")
        wallet_service.create_wallet("2045618308")
        first = service.create_pairing("1045618308")
        second = service.create_pairing("2045618308")

        service.link_photon_sender(first["code"], "+15551234567")
        conflict = service.link_photon_sender(second["code"], "+15551234567")

        self.assertFalse(conflict["ok"])
        self.assertIn("could not link", conflict["imessageText"].lower())

    def test_test_approval_sends_canonical_message_and_accepts_yes_once(self):
        service, _wallet_service, _store, notifier = self.make_linked_service()

        created = service.create_test_approval("1045618308")
        pending = service.pending_for_photon_sender("+15551234567")
        decided = service.record_decision("+15551234567", "YES")
        replay = service.record_decision("+15551234567", "YES")

        self.assertTrue(created["ok"])
        self.assertIn("Reply YES or NO.", notifier.messages[0]["message"])
        self.assertEqual(notifier.messages[0]["photonUserId"], "+15551234567")
        self.assertTrue(pending["pending"])
        self.assertEqual(decided["status"], "approved")
        self.assertFalse(replay["ok"])
        self.assertIn("No pending approval", replay["imessageText"])

    def test_no_pending_approval_leaves_yes_for_normal_chat(self):
        service, _wallet_service, _store, _notifier = self.make_linked_service()

        result = service.pending_for_photon_sender("+15551234567")

        self.assertFalse(result["pending"])

    def test_duplicate_pending_approval_is_rejected(self):
        service, _wallet_service, _store, _notifier = self.make_linked_service()

        first = service.create_test_approval("1045618308")
        second = service.create_test_approval("1045618308")

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("already pending", second["telegramText"])

    def test_delivery_failure_closes_approval(self):
        service, wallet_service, _store = self.make_service(notifier=RecordingNotifier(False))
        wallet_service.create_wallet("1045618308")
        pairing = service.create_pairing("1045618308")
        service.link_photon_sender(pairing["code"], "+15551234567")

        result = service.create_test_approval("1045618308")
        decision = service.record_decision("+15551234567", "YES")

        self.assertFalse(result["ok"])
        self.assertIn("could not deliver", result["telegramText"])
        self.assertFalse(decision["ok"])

    def test_hermes_cli_notifier_uses_argument_array_without_shell(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "sent", "stderr": ""},
            )()

        notifier = HermesCliNotifier(
            hermes_cli="/home/hermes/.local/bin/hermes",
            hermes_home="/home/hermes/.hermes",
            runner=runner,
        )

        result = notifier.send(
            photon_user_id="+15551234567",
            message="Sign402 approval request",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            calls[0][0],
            [
                "/home/hermes/.local/bin/hermes",
                "send",
                "--to",
                "photon:+15551234567",
                "Sign402 approval request",
            ],
        )
        self.assertFalse(calls[0][1]["shell"])
        self.assertEqual(calls[0][1]["env"]["HOME"], "/home/hermes")
        self.assertEqual(calls[0][1]["env"]["HERMES_HOME"], "/home/hermes/.hermes")

    def test_hermes_cli_notifier_passes_photon_sidecar_environment(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "sent", "stderr": ""},
            )()

        notifier = HermesCliNotifier(
            hermes_cli="/home/hermes/.local/bin/hermes",
            hermes_home="/home/hermes/.hermes",
            runner=runner,
        )
        old_project_id = os.environ.get("PHOTON_PROJECT_ID")
        old_project_secret = os.environ.get("PHOTON_PROJECT_SECRET")
        old_allowed_users = os.environ.get("PHOTON_ALLOWED_USERS")
        old_home_channel = os.environ.get("PHOTON_HOME_CHANNEL")
        old_logname = os.environ.get("LOGNAME")
        old_path = os.environ.get("PATH")
        old_token = os.environ.get("PHOTON_SIDECAR_TOKEN")
        old_port = os.environ.get("PHOTON_SIDECAR_PORT")
        old_user = os.environ.get("USER")
        try:
            os.environ["PHOTON_PROJECT_ID"] = "project-id"
            os.environ["PHOTON_PROJECT_SECRET"] = "project-secret"
            os.environ["PHOTON_ALLOWED_USERS"] = "+15551234567"
            os.environ["PHOTON_HOME_CHANNEL"] = "+15551234567"
            os.environ["LOGNAME"] = "hermes"
            os.environ["PATH"] = "/home/hermes/.local/bin:/usr/bin"
            os.environ["PHOTON_SIDECAR_TOKEN"] = "sidecar-token"
            os.environ["PHOTON_SIDECAR_PORT"] = "8789"
            os.environ["USER"] = "hermes"

            result = notifier.send(
                photon_user_id="+15551234567",
                message="Sign402 approval request",
            )
        finally:
            if old_project_id is None:
                os.environ.pop("PHOTON_PROJECT_ID", None)
            else:
                os.environ["PHOTON_PROJECT_ID"] = old_project_id
            if old_project_secret is None:
                os.environ.pop("PHOTON_PROJECT_SECRET", None)
            else:
                os.environ["PHOTON_PROJECT_SECRET"] = old_project_secret
            if old_allowed_users is None:
                os.environ.pop("PHOTON_ALLOWED_USERS", None)
            else:
                os.environ["PHOTON_ALLOWED_USERS"] = old_allowed_users
            if old_home_channel is None:
                os.environ.pop("PHOTON_HOME_CHANNEL", None)
            else:
                os.environ["PHOTON_HOME_CHANNEL"] = old_home_channel
            if old_logname is None:
                os.environ.pop("LOGNAME", None)
            else:
                os.environ["LOGNAME"] = old_logname
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path
            if old_token is None:
                os.environ.pop("PHOTON_SIDECAR_TOKEN", None)
            else:
                os.environ["PHOTON_SIDECAR_TOKEN"] = old_token
            if old_port is None:
                os.environ.pop("PHOTON_SIDECAR_PORT", None)
            else:
                os.environ["PHOTON_SIDECAR_PORT"] = old_port
            if old_user is None:
                os.environ.pop("USER", None)
            else:
                os.environ["USER"] = old_user

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][1]["env"]["LOGNAME"], "hermes")
        self.assertEqual(calls[0][1]["env"]["PATH"], "/home/hermes/.local/bin:/usr/bin")
        self.assertEqual(calls[0][1]["env"]["USER"], "hermes")
        self.assertEqual(calls[0][1]["env"]["PHOTON_PROJECT_ID"], "project-id")
        self.assertEqual(
            calls[0][1]["env"]["PHOTON_PROJECT_SECRET"], "project-secret"
        )
        self.assertEqual(calls[0][1]["env"]["PHOTON_ALLOWED_USERS"], "+15551234567")
        self.assertEqual(calls[0][1]["env"]["PHOTON_HOME_CHANNEL"], "+15551234567")
        self.assertEqual(calls[0][1]["env"]["PHOTON_SIDECAR_TOKEN"], "sidecar-token")
        self.assertEqual(calls[0][1]["env"]["PHOTON_SIDECAR_PORT"], "8789")

    def test_invalid_master_key_fails_clearly(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bad_key = base64.urlsafe_b64encode(b"short").decode("ascii")

        with self.assertRaisesRegex(ValueError, "valid Fernet key"):
            ImessageApprovalService(
                store=ImessageApprovalStore(Path(tmp.name) / "approvals.db"),
                wallet_service=ManagedBaseWalletService(
                    store=UserWalletStore(Path(tmp.name) / "wallets.db"),
                    master_key=test_master_key(),
                ),
                master_key=bad_key,
                notifier=RecordingNotifier(),
            )

    def test_expired_approval_cannot_be_decided(self):
        current_time = [1_800_000_000]
        service, wallet_service, _store = self.make_service()
        service.now = lambda: current_time[0]
        wallet_service.create_wallet("1045618308")
        pairing = service.create_pairing("1045618308")
        service.link_photon_sender(pairing["code"], "+15551234567")

        service.create_test_approval("1045618308")
        current_time[0] += 121
        decision = service.record_decision("+15551234567", "YES")

        self.assertFalse(decision["ok"])
        self.assertIn("No pending approval", decision["imessageText"])


if __name__ == "__main__":
    unittest.main()
