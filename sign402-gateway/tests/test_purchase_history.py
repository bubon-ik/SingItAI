import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from sign402_gateway.purchase_history import UserPurchaseStore, purchase_id, purchase_summary
from sign402_gateway.secure_state import SensitiveStateCipher, SensitiveStateError
import test_gateway_server as fixtures


def order(number):
    return {"ok": True, "quoteId": f"quote-{number}", "fulfillmentToken": f"reveal-secret-{number}",
            "bitrefill": {"status": "delivered", "orderId": f"invoice-{number}"},
            "receipt": {"name": "Alza CZ", "denomination": "200 CZK", "paid": "9.44 USDC",
                        "network": "Base", "txId": "0x" + "a" * 64},
            "telegramText": "Your order is ready."}


class PurchaseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private" / "purchases.json"
        self.cipher = SensitiveStateCipher(Fernet.generate_key().decode())
        self.store = UserPurchaseStore(self.path, cipher=self.cipher)

    def test_retains_multiple_encrypted_orders_across_restart(self):
        for number in range(3):
            self.store.write("alice", order(number))
        restarted = UserPurchaseStore(self.path, cipher=self.cipher)
        self.assertEqual(restarted.read("alice"), order(2))
        self.assertEqual(restarted.read("alice", purchase_id(order(0))), order(0))
        self.assertEqual(len(restarted.summaries("alice")), 3)
        raw = self.path.read_text()
        self.assertNotIn("reveal-secret", raw)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertIsNone(restarted.read("bob", purchase_id(order(0))))
        self.assertEqual(restarted.summaries("bob"), [])

    def test_upgrades_legacy_latest_without_rewriting_it_on_read(self):
        self.store.write("alice", order(0))
        data = json.loads(self.path.read_text())
        data["alice"].pop("_recordedAt")
        self.path.write_text(json.dumps(data))
        before = self.path.read_bytes()
        self.assertEqual(self.store.read("alice"), order(0))
        self.assertEqual(self.path.read_bytes(), before)
        self.store.write("alice", order(1))
        self.assertEqual(self.store.read("alice", purchase_id(order(0))), order(0))

    def test_clearing_one_order_never_clears_the_newest_order(self):
        for number in range(2):
            self.store.write("alice", order(number))
        self.store.clear_fulfillment_token("alice", purchase_id(order(0)))
        self.assertNotIn("fulfillmentToken", self.store.read("alice", purchase_id(order(0))))
        self.assertEqual(self.store.read("alice")["fulfillmentToken"], "reveal-secret-1")
        self.assertFalse(self.store.summaries("alice")[1]["canReveal"])
        self.assertTrue(self.store.summaries("alice")[0]["canReveal"])

    def test_summaries_never_expose_provider_data_or_reveal_credentials(self):
        item = order(0)
        item.update(telegramText="CODE-MARKER", recipient={"email": "EMAIL-MARKER"}, resourceUrl="https://merchant.invalid/TOKEN-MARKER")
        item["bitrefill"]["redemption"] = {"value": "CODE-MARKER"}
        self.store.write("alice", item)
        summary = json.dumps(self.store.summaries("alice"))
        for marker in ("CODE-MARKER", "EMAIL-MARKER", "TOKEN-MARKER", "reveal-secret", "fulfillmentToken", "redemption"):
            self.assertNotIn(marker, summary)
        self.assertEqual(self.store.summaries("alice")[0]["transactionUrl"], "https://basescan.org/tx/0x" + "a" * 64)
        item["receipt"]["txId"] = "https://merchant.invalid/bearer"
        self.assertEqual(purchase_summary(item)["transactionUrl"], "")

    def test_duplicates_replace_instead_of_creating_another_receipt(self):
        for number in (0, 1, 0):
            self.store.write("alice", order(number))
        self.assertEqual(len(self.store.summaries("alice")), 2)

    def test_history_is_bounded(self):
        for number in range(105):
            self.store.write("alice", order(number))
        self.assertEqual(len(self.store.summaries("alice")), 100)
        self.assertIsNone(self.store.read("alice", purchase_id(order(0))))

    def test_nested_legacy_plaintext_prevents_any_rewrite(self):
        self.store.write("alice", order(0))
        data = json.loads(self.path.read_text())
        data["alice"]["_history"] = [{"ok": True, "fulfillmentToken": "legacy-secret"}]
        self.path.write_text(json.dumps(data))
        before = self.path.read_bytes()
        with self.assertRaises(SensitiveStateError):
            self.store.write("alice", order(1))
        self.assertEqual(self.path.read_bytes(), before)


class PurchaseHistoryEndpointTests(unittest.TestCase):
    make_handler = fixtures.GatewayServerTests.make_handler
    response_text = fixtures.GatewayServerTests.response_text
    response_json = fixtures.GatewayServerTests.response_json

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = UserPurchaseStore(Path(self.tmp.name) / "history.json", cipher=SensitiveStateCipher(Fernet.generate_key().decode()))
        self.server = fixtures.DummyServer()
        self.server.user_event_store = self.store
        self.server.user_wallet_service.resolve_telegram_user_id.return_value = "alice"
        self.server.bitrefill_order_lookup = Mock(return_value={"redemption": {"value": "fixture-code"}, "telegramText": "Code: fixture-code", "status": "delivered"})
        self.headers = {"Authorization": "Bearer test-wallet-token", "X-Sign402-User-Token": "user-token"}
        for number in range(8):
            self.store.write("alice", order(number))
        self.store.write("bob", order(99))

    def request(self, payload, headers=None):
        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/purchases", {"telegramUserId": "alice", **payload}, server=self.server, headers=self.headers if headers is None else headers)
        return self.response_text(handler), self.response_json(handler)

    def test_list_is_paginated_and_never_reveals(self):
        _, page = self.request({})
        self.assertEqual(len(page["purchases"]), 6)
        self.assertTrue(page["hasNext"])
        _, last = self.request({"offset": 6})
        self.assertEqual(len(last["purchases"]), 2)
        self.assertFalse(last["hasNext"])
        self.server.bitrefill_order_lookup.assert_not_called()

    def test_requires_gateway_and_user_auth(self):
        for headers in ({}, {"Authorization": "Bearer wrong", "X-Sign402-User-Token": "user-token"}, {"Authorization": "Bearer test-wallet-token"}):
            response, _ = self.request({}, headers=headers)
            self.assertIn("401", response)
        self.server.bitrefill_order_lookup.assert_not_called()

    def test_other_users_purchase_is_not_visible_or_revealable(self):
        for reveal in (False, True):
            response, result = self.request({"purchaseId": purchase_id(order(99)), "reveal": reveal})
            self.assertIn("404", response)
            self.assertFalse(result["ok"])
        self.server.bitrefill_order_lookup.assert_not_called()

    def test_selected_reveal_fetches_exact_order_and_preserves_latest_capability(self):
        _, result = self.request({"purchaseId": purchase_id(order(0)), "reveal": True})
        self.assertIn("fixture-code", result["telegramText"])
        self.server.bitrefill_order_lookup.assert_called_once_with("quote-0", include_redemption=True, fulfillment_token="reveal-secret-0")
        self.assertNotIn("fulfillmentToken", self.store.read("alice", purchase_id(order(0))))
        self.assertEqual(self.store.read("alice")["fulfillmentToken"], "reveal-secret-7")
        _, result = self.request({"purchaseId": purchase_id(order(0)), "reveal": True})
        self.assertNotIn("fixture-code", result["telegramText"])
        self.server.bitrefill_order_lookup.assert_called_once()
        self.assertNotIn("fixture-code", self.store.path.read_text())
