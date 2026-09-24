"""Client trust boundaries; HTTP + real device is covered by the rehearsal."""
import copy
import importlib.util
import io
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location("ledger_purchase", Path(__file__).resolve().parents[2] / "tools/ledger-approve/purchase.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class LedgerClientTests(unittest.TestCase):
    def setUp(self):
        self.gateway = client.Gateway("http://127.0.0.1:8099", "owner", "gateway-token", "user-token")
        expiry = int(time.time()) + 300
        self.pending = {
            "status": "pending", "decision": "needs_ledger_approval", "requestId": "request-0001", "expiresAt": expiry,
            "approval": {"domain": {"name": "SingIt Spending Approval", "version": "2", "chainId": 8453},
                         "signingMethod": "personal_sign", "displayText": "test display",
                         "message": {"purchase": "Test news", "owner": "owner", "expiresAt": expiry, "merchant": "test.invalid",
                                     "payTo": "0x" + "11" * 20, "amountUsd": "0.001", "journalId": "decision-0001"}},
        }

    def test_transport_requires_credentials_and_https_or_loopback(self):
        for origin in ("http://example.com", "https://name:password@example.com", "https://example.com/path",
                       "https://example.com?token=secret", "file:///tmp/a"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                client.Gateway(origin, "owner", "gateway-token", "user-token")
        with self.assertRaises(ValueError):
            client.Gateway("https://example.com", "owner", "", "user-token")

    def test_post_authenticates_and_never_follows_redirects(self):
        self.gateway.opener = Mock()
        self.gateway.opener.open.side_effect = HTTPError(self.gateway.url, 307, "redirect", {}, io.BytesIO())
        with self.assertRaisesRegex(ValueError, "redirects"):
            self.gateway.post("/agent/ledger-approve", {"requestId": "request-0001"})
        request = self.gateway.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer gateway-token")
        self.assertEqual(request.get_header("X-sign402-user-token"), "user-token")
        self.assertIsNone(client.NoRedirects().redirect_request(request, None, 307, "", {}, "https://other.invalid"))

    def test_cli_resumes_pending_then_reuses_success_without_signing_again(self):
        paid = {"ok": True, "status": "succeeded", "requestId": "request-0001"}
        self.gateway.post = Mock(side_effect=[self.pending, paid, paid])
        with patch.object(client, "sign_pending", return_value={"signature": "test"}) as sign:
            self.assertEqual(client.run(self.gateway, "approve", "request-0001"), paid)
            self.assertEqual(client.run(self.gateway, "approve", "request-0001"), paid)
        sign.assert_called_once()
        self.assertEqual(self.gateway.post.call_args_list[1].args[0], "/agent/ledger-approve")

    def test_response_for_another_request_never_reaches_device(self):
        self.gateway.post = Mock(return_value={**self.pending, "requestId": "another-request"})
        with patch.object(client, "sign_pending") as sign, self.assertRaises(ValueError):
            client.run(self.gateway, "buy", "request-0001")
        sign.assert_not_called()

    def test_bad_envelope_or_expiry_never_reaches_device(self):
        for change in ("owner", "chain", "expiry", "old_format", "missing_text"):
            pending = copy.deepcopy(self.pending)
            if change == "owner":
                pending["approval"]["message"]["owner"] = "another-owner"
            elif change == "chain":
                pending["approval"]["domain"]["chainId"] = 1
            elif change == "old_format":
                pending["approval"]["signingMethod"] = "eth_signTypedData_v4"
                pending["approval"]["domain"]["version"] = "1"
            elif change == "missing_text":
                pending["approval"].pop("displayText")
            else:
                pending["approval"]["message"]["expiresAt"] = pending["expiresAt"] = int(time.time()) - 1
            with self.subTest(change=change), patch.object(client.subprocess, "run") as device, self.assertRaises(ValueError):
                client.sign_pending(pending, "owner")
            device.assert_not_called()

    def test_device_rejection_never_submits_an_approval(self):
        self.gateway.post = Mock(return_value=self.pending)
        with patch.object(client.subprocess, "run", return_value=Mock(returncode=1, stdout="")), patch("sys.stderr", io.StringIO()), self.assertRaises(ValueError):
            client.run(self.gateway, "buy", "request-0001")
        self.gateway.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
