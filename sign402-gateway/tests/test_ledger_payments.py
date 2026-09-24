"""HTTP requests, real policy/database/budget and real EIP-712 signatures.

Only the external quote, wallet key access and network payment are doubles.
No device or money is needed. Reopening both stores models process restart;
concurrent requests share only durable state, not a Python lock.
"""
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from eth_account import Account
from eth_account.messages import encode_defunct
from spending_memory import Payment, SpendingMemory, SpendingPolicy

from sign402_gateway import server as gateway
from sign402_gateway.ledger_approval import APPROVERS_ENV, ENABLED_ENV, OWNER_ENV
from sign402_gateway.ledger_payments import LedgerConfig, LedgerOperationStore
from tests.test_gateway_server import DummyServer, FakeSocket

OWNER = "1045618308"
KEY = Account.from_key("0x" + "11" * 32)
OTHER_KEY = Account.from_key("0x" + "22" * 32)
REQ = {
    "scheme": "exact", "network": "base-mainnet", "x402Network": "eip155:8453",
    "amountAtomic": "1000", "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
    "paymentIntent": "crypto-news-1", "purpose": "x402_api_access",
    "extra": {"name": "USD Coin", "version": "2"},
}


class LedgerPaymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            ENABLED_ENV: "1", OWNER_ENV: OWNER, APPROVERS_ENV: KEY.address,
            "SIGN402_WALLET_MASTER_KEY": Fernet.generate_key().decode(),
            "SIGN402_USER_PURCHASES_PER_HOUR": "0", "SIGN402_USER_REQUESTS_PER_MINUTE": "0",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.requirements = copy.deepcopy(REQ)
        for name, kwargs in (
            ("fetch_x402_payment_required", {"return_value": {"accepts": [{}]}}),
            ("normalize_x402_payment_required", {"side_effect": lambda *a, **k: copy.deepcopy(self.requirements)}),
        ):
            p = patch.object(gateway, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.server = self.make_server()

    def make_server(self):
        s = DummyServer()
        s.spending_policy = SpendingPolicy(SpendingMemory.local(str(self.root / "memory.db")), daily_cap_usd=Decimal("5"))
        s.user_spend_limit_store = gateway.UserSpendLimitStore(self.root / "limits.json")
        s.user_wallet_service.resolve_telegram_user_id.side_effect = lambda token: OWNER if token == "owner-token" else "other-owner"
        s.user_wallet_service.decrypt_private_key_for_future_signing.return_value = "TEST_KEY_NOT_A_WALLET"
        s.user_event_store = Mock()
        s.user_x402_buyer.return_value = {
            "ok": True, "txId": "0xTEST_TRANSACTION", "approvalId": "test",
            "resourceResult": {"status": 200, "body": {"headline": "Private result"}},
            "amountAtomic": "1000", "asset": REQ["asset"], "network": REQ["network"],
        }
        s.ledger_payments = gateway.build_ledger_payments(s, LedgerConfig.from_env(), path=self.root / "ledger" / "operations.db")
        return s

    def http(self, path, data=None, *, server=None, token="owner-token", auth=True):
        payload = {"telegramUserId": OWNER, **(data or {})}
        raw = json.dumps(payload).encode()
        headers = ""
        if auth:
            headers = f"X-Sign402-Wallet-Token: test-wallet-token\r\nX-Sign402-User-Token: {token}\r\n"
        sock = FakeSocket((f"POST {path} HTTP/1.1\r\nHost: localhost\r\n{headers}Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n").encode() + raw)
        with patch("sys.stderr", io.StringIO()):
            gateway.Sign402GatewayHandler(sock, ("127.0.0.1", 1), server or self.server)
        head, body = sock.wfile.getvalue().split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(body)

    def start(self, request_id="request-0001", **kwargs):
        return self.http("/agent/buy-tool", {"tool": "news", "requestId": request_id}, **kwargs)

    def signed(self, response, *, key=KEY, **message_changes):
        from sign402_gateway.ledger_approval import display_text
        typed = copy.deepcopy(response["approval"])
        typed["message"].update(message_changes)
        return {"signingMethod": "personal_sign",
                "signature": key.sign_message(encode_defunct(text=display_text(typed["message"], chain=typed["domain"]["chainId"]))).signature.hex(),
                "expiresAt": typed["message"]["expiresAt"], "journalId": typed["message"]["journalId"]}

    def approve(self, pending, **kwargs):
        return self.http("/agent/ledger-approve", {"requestId": pending["requestId"], "ledgerApproval": self.signed(pending)}, **kwargs)

    def test_real_escalation_sign_pay_replay_and_account_once(self):
        status, pending = self.start()
        self.assertEqual(status, 202, pending)
        self.assertEqual(pending["approval"]["message"]["rule"], "unknown_merchant")
        self.server.user_x402_buyer.assert_not_called()
        self.assertFalse(self.server.user_spend_limit_store.path.exists())
        status, paid = self.approve(pending)
        self.assertEqual(status, 200, paid)
        self.assertEqual(paid["status"], "succeeded")
        self.assertEqual(self.approve(pending), (200, paid))
        self.assertEqual(self.start(), (200, paid))
        self.server.user_x402_buyer.assert_called_once()
        self.server.imessage_approval_service.request_purchase_approval.assert_not_called()
        self.assertEqual(self.server.spending_policy.memory.spent_today(OWNER), Decimal("0.001"))
        data = json.loads((self.root / "limits.json").read_text())
        self.assertEqual(len(data["records"]), 1)
        self.assertEqual(data["reservations"], [])

    def test_initial_retry_returns_the_same_challenge(self):
        first = self.start()
        self.assertEqual(self.start(), first)

    def test_success_and_pending_survive_restart(self):
        _, pending = self.start()
        self.server = self.make_server()
        self.assertEqual(self.start(), (202, pending))
        status, paid = self.approve(pending)
        self.assertEqual(status, 200, paid)
        self.server = self.make_server()
        self.assertEqual(self.approve(pending), (200, paid))
        self.server.user_x402_buyer.assert_not_called()

    def test_changed_request_id_payload_cannot_reuse_approval(self):
        self.start()
        status, _ = self.http("/agent/buy-tool", {"tool": "weather", "requestId": "request-0001"})
        self.assertEqual(status, 409)

    def test_signatures_bind_amount_address_owner_and_decision(self):
        _, pending = self.start()
        mutations = [{"amountUsd": "99"}, {"payTo": "0x" + "ab" * 20}, {"owner": "someone-else"},
                     {"journalId": "another"}, {"purchase": "Different product"}, {"resource": "https://other.invalid/news"}]
        for fields in mutations:
            with self.subTest(fields=fields):
                code, _ = self.http("/agent/ledger-approve", {"requestId": pending["requestId"], "ledgerApproval": self.signed(pending, **fields)})
                self.assertEqual(code, 400)
        code, _ = self.http("/agent/ledger-approve", {"requestId": pending["requestId"], "ledgerApproval": self.signed(pending, key=OTHER_KEY)})
        self.assertEqual(code, 400)
        self.server.user_x402_buyer.assert_not_called()

    def test_old_pending_approval_requires_new_request_but_finished_results_remain_readable(self):
        _, pending = self.start()
        old = copy.deepcopy(pending["approval"])
        old.pop("signingMethod")
        old.pop("displayText")
        old["domain"]["version"] = "1"
        store = self.server.ledger_payments.store
        store.transition(OWNER, pending["requestId"], "pending", "pending", approval=old)
        code, result = self.approve(pending)
        self.assertEqual(code, 400)
        self.server.user_x402_buyer.assert_not_called()
        saved = {"ok": True, "status": "succeeded", "requestId": pending["requestId"], "historical": True}
        store.transition(OWNER, pending["requestId"], "pending", "succeeded", result=saved)
        code, result = self.http("/agent/ledger-status", {"requestId": pending["requestId"]})
        self.assertEqual(code, 200)
        self.assertTrue(result["historical"])
        self.server.user_x402_buyer.assert_not_called()

    def test_another_operation_cannot_use_the_first_signature(self):
        _, first = self.start()
        _, second = self.start("request-0002")
        status, _ = self.http("/agent/ledger-approve", {"requestId": second["requestId"], "ledgerApproval": self.signed(first)})
        self.assertEqual(status, 400)

    def test_no_auth_or_another_user_cannot_read_or_approve(self):
        _, pending = self.start()
        for path in ("/agent/ledger-status", "/agent/ledger-approve", "/agent/ledger-cancel"):
            self.assertEqual(self.http(path, {"requestId": pending["requestId"]}, auth=False)[0], 401)
            self.assertEqual(self.http(path, {"requestId": pending["requestId"], "telegramUserId": "other-owner"}, token="other-token")[0], 403)

    def test_cancel_and_expiry_never_pay_or_hold_budget(self):
        _, pending = self.start()
        self.assertEqual(self.http("/agent/ledger-cancel", {"requestId": pending["requestId"]})[0], 410)
        self.assertEqual(self.approve(pending)[0], 410)
        _, next_pending = self.start("request-0002")
        with patch("sign402_gateway.ledger_payments.time.time", return_value=next_pending["expiresAt"] + 1):
            self.assertEqual(self.approve(next_pending)[0], 410)
        self.server.user_x402_buyer.assert_not_called()
        self.assertFalse((self.root / "limits.json").exists())

    def test_quote_changed_since_signing_never_pays(self):
        _, pending = self.start()
        self.requirements["amountAtomic"] = "2000"
        code, body = self.approve(pending)
        self.assertEqual((code, body["status"]), (409, "failed"))
        self.server.user_x402_buyer.assert_not_called()

    def test_new_hard_block_overrides_a_previous_approval(self):
        _, pending = self.start()
        payment = self.server.ledger_payments._payment(self.server.ledger_payments.store.get(OWNER, pending["requestId"]))
        self.server.spending_policy.memory.remember_settlement(
            Payment(merchant=payment.merchant, pay_to="0x" + "ab" * 20,
                    amount_usd=payment.amount_usd, owner=OWNER), tx_id="0xNEW_HISTORY")
        code, body = self.approve(pending)
        self.assertEqual((code, body["status"]), (409, "failed"))
        self.server.user_x402_buyer.assert_not_called()

    def test_current_wallet_cap_is_enforced_before_send(self):
        _, pending = self.start()
        with patch.dict(os.environ, {"SIGN402_USER_WALLET_MAX_ATOMIC_PER_TX": "100"}):
            code, body = self.approve(pending)
        self.assertEqual((code, body["status"]), (409, "failed"))
        self.server.user_x402_buyer.assert_not_called()

    def test_expiry_during_preflight_releases_budget_without_paying(self):
        _, pending = self.start()
        original = self.server.ledger_payments.reserve
        def expire_after_reservation(*args):
            reservation = original(*args)
            clock = patch("sign402_gateway.ledger_payments.time.time", return_value=pending["expiresAt"] + 1)
            clock.start()
            self.addCleanup(clock.stop)
            return reservation
        self.server.ledger_payments.reserve = expire_after_reservation
        code, body = self.approve(pending)
        self.assertEqual((code, body["status"]), (409, "failed"))
        self.server.user_x402_buyer.assert_not_called()
        self.assertEqual(json.loads((self.root / "limits.json").read_text())["reservations"], [])

    def test_pause_during_preflight_releases_budget_without_paying(self):
        _, pending = self.start()
        self.server.ledger_payments.paused = Mock(side_effect=[False, True])
        code, body = self.approve(pending)
        self.assertEqual((code, body["status"]), (409, "failed"))
        self.server.user_x402_buyer.assert_not_called()
        self.assertEqual(json.loads((self.root / "limits.json").read_text())["reservations"], [])

    def test_familiar_payment_uses_policy_without_device_and_retries_once(self):
        _, pending = self.start()
        self.assertEqual(self.approve(pending)[0], 200)
        self.server.user_x402_buyer.reset_mock()
        code, paid = self.start("request-0002")
        self.assertEqual((code, paid["status"]), (200, "succeeded"))
        self.assertEqual(self.start("request-0002"), (200, paid))
        self.server.user_x402_buyer.assert_called_once()
        self.assertEqual(self.server.user_x402_buyer.call_args.kwargs["approval"]["source"], "spending_memory")

    def test_disabled_memory_cannot_fall_back_to_chat(self):
        _, pending = self.start()
        self.server.spending_policy = None
        self.assertEqual(self.approve(pending)[0], 503)
        self.assertEqual(self.start("request-0002")[0], 503)
        self.server.imessage_approval_service.request_purchase_approval.assert_not_called()

    def test_pause_blocks_payment_but_status_and_cancel_still_work(self):
        _, pending = self.start()
        with patch.dict(os.environ, {"SIGN402_PURCHASES_PAUSED": "1"}):
            self.assertEqual(self.approve(pending)[0], 503)
            self.assertEqual(self.http("/agent/ledger-status", {"requestId": pending["requestId"]})[0], 202)
            self.assertEqual(self.http("/agent/ledger-cancel", {"requestId": pending["requestId"]})[0], 410)

    def test_concurrent_submissions_call_payer_once(self):
        _, pending = self.start()
        entered, release = threading.Event(), threading.Event()
        original = self.server.user_x402_buyer.return_value
        def slow_pay(*a, **k):
            entered.set()
            self.assertTrue(release.wait(5))
            return original
        self.server.user_x402_buyer.side_effect = slow_pay
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.approve, pending)
            self.assertTrue(entered.wait(5))
            try:
                code, body = self.approve(pending)
                self.assertEqual((code, body["status"]), (409, "executing"))
            finally:
                release.set()
            self.assertEqual(first.result()[0], 200)
        self.server.user_x402_buyer.assert_called_once()

    def test_ambiguous_payment_blocks_retries_and_new_operations_after_restart(self):
        _, pending = self.start()
        self.server.user_x402_buyer.side_effect = TimeoutError("may already be settled")
        code, body = self.approve(pending)
        self.assertEqual((code, body["status"]), (409, "uncertain"))
        self.server = self.make_server()
        self.assertEqual(self.approve(pending)[1]["status"], "uncertain")
        self.assertEqual(self.start("request-0002")[0], 409)
        self.server.user_x402_buyer.assert_not_called()

    def test_process_loss_after_consumption_never_resubmits(self):
        _, pending = self.start()
        self.server.ledger_payments.store.transition(OWNER, pending["requestId"], "pending", "executing")
        self.server = self.make_server()
        self.assertEqual(self.approve(pending)[1]["status"], "executing")
        self.server.user_x402_buyer.assert_not_called()

    def test_response_and_intent_are_encrypted_signature_is_not_stored(self):
        _, pending = self.start()
        signature = self.signed(pending)["signature"]
        self.assertEqual(self.approve(pending)[0], 200)
        raw = (self.root / "ledger" / "operations.db").read_bytes()
        for secret in (b"Private result", b"TEST_KEY_NOT_A_WALLET", signature.encode(), REQ["receiver"].encode()):
            self.assertNotIn(secret, raw)

    def test_database_metadata_cannot_relabel_a_signed_operation(self):
        _, pending = self.start()
        with self.server.ledger_payments.store.transaction() as db:
            db.execute("UPDATE ledger_operations SET request_id=? WHERE request_id=?",
                       ("relabeled-request", pending["requestId"]))
        code, _ = self.http("/agent/ledger-approve", {
            "requestId": "relabeled-request", "ledgerApproval": self.signed(pending)})
        self.assertEqual(code, 503)
        self.server.user_x402_buyer.assert_not_called()

    def test_valid_config_required_and_disabled_build_has_no_store(self):
        self.assertIsNone(LedgerConfig.from_env({}))
        for env in ({ENABLED_ENV: "1"}, {ENABLED_ENV: "1", OWNER_ENV: OWNER, APPROVERS_ENV: "invalid"}):
            with self.assertRaises(ValueError):
                LedgerConfig.from_env(env)
        self.assertIsNone(gateway.build_ledger_payments(self.server, None))


if __name__ == "__main__":
    unittest.main()
