import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from eth_account import Account

from sign402_gateway import web_api as wa
from sign402_gateway.agent_allowance import AllowanceStore
from sign402_gateway.solana_allowance import SolanaAllowanceStore
from sign402_gateway.wallet_reconciliation import Reconciliation, solana_category
from sign402_gateway.web_accounts import account_id_for
from tests.test_solana_allowance import ACCOUNT as SOL_ACCOUNT, OWNER as SOL_OWNER

NOW = 1_800_000_000  # 2027-01-15 08:00 UTC
DAY = 86400
TODAY = NOW // DAY * DAY
CREDIT = "https://ask.singitai.app/v1/credit"
SECRET_RESOURCE = "https://api.example.test/secret-path-12345"
SECRET_TX = "0x" + "ab" * 32
SECRET_PURPOSE = "Bitrefill Amazon 25 SECRET-GIFT-NOTE"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = AllowanceStore(Path(self.tmp.name) / "a.db")
        self.sol = SolanaAllowanceStore(Path(self.tmp.name) / "s.db")
        self.rec = Reconciliation(self.base, self.sol, now=lambda: NOW)
        self.user = Account.create().address
        self.account = account_id_for(self.user)
        self.n = 0

    def settle(self, resource, amount, at):
        self.n += 1
        self.base.count_settlement(SECRET_TX[:-2] + f"{self.n:02x}", "0",
                                   self.account, "0xpay", amount, resource, at)

    def limiter(self, expiry=NOW + 10 * DAY):
        self.base.record_limiter({
            "limiter_address": "0x" + "11" * 20, "user_id": self.account, "owner_address": self.user,
            "agent_address": "0x" + "22" * 20, "guardian_address": "0x" + "33" * 20, "daily_cap": 5_000_000,
            "per_purchase_cap": 2_000_000, "expiry": expiry, "deploy_tx": SECRET_TX, "status": "ACTIVE",
            "source": "pending", "created_at": NOW - DAY})


class BaseReportTests(Base):
    def test_known_account_by_category_day_and_topups(self):
        self.limiter()
        self.settle("https://ask.singitai.app/v1/chat/completions/metered", 300_000, TODAY + 10)
        self.settle(SECRET_RESOURCE, 1_000_000, TODAY - DAY + 5)
        self.settle("bitrefill:invoice:INV-SECRET-1", 2_000_000, TODAY - 3 * DAY)
        self.settle(CREDIT, 4_000_000, TODAY + 20)
        self.settle(SECRET_RESOURCE, 9_000_000, NOW - 40 * DAY)  # outside the window
        out = self.rec.report(self.user.lower())
        self.assertEqual(out["address"], self.user)
        self.assertEqual((out["chain"], out["known"]), ("base", True))
        self.assertEqual(out["window"], {"days": 30, "since": NOW - 30 * DAY, "until": NOW})
        self.assertEqual(out["spent"]["totalAtomic"], "3300000")
        self.assertEqual(out["spent"]["totalUsd"], "3.30")
        self.assertEqual(out["spent"]["count"], 3)
        self.assertEqual(out["spent"]["byCategory"], {
            "chat_answer": {"count": 1, "atomic": "300000", "usd": "0.30"},
            "data_tool": {"count": 1, "atomic": "1000000", "usd": "1.00"},
            "gift_card": {"count": 1, "atomic": "2000000", "usd": "2.00"}})
        self.assertEqual([d["date"] for d in out["spent"]["byDay"]], ["2027-01-12", "2027-01-14", "2027-01-15"])
        self.assertEqual(out["creditTopUps"], {"count": 1, "atomic": "4000000", "usd": "4.00"})
        self.assertEqual(out["allowance"], {
            "configured": True, "state": "active", "dailyCapAtomic": "5000000", "perPurchaseCapAtomic": "2000000",
            # today: the 0.30 answer and the 4.00 credit top-up, which is paid through the limiter too
            "spentTodayAtomic": "4300000", "remainingTodayAtomic": "700000", "expiry": NOW + 10 * DAY,
            "limiter": "0x" + "11" * 20})

    def test_days_narrows_the_window(self):
        self.settle(SECRET_RESOURCE, 1_000_000, NOW - 2 * DAY)
        self.settle(SECRET_RESOURCE, 2_000_000, NOW - 10 * DAY)
        self.assertEqual(self.rec.report(self.user, 3)["spent"]["totalAtomic"], "1000000")
        self.assertEqual(self.rec.report(self.user, 30)["spent"]["totalAtomic"], "3000000")

    def test_expired_and_paused_states(self):
        self.limiter(expiry=NOW - 1)
        self.assertEqual(self.rec.report(self.user)["allowance"]["state"], "expired")

    def test_unknown_address_has_the_same_shape(self):
        out = self.rec.report(Account.create().address)
        self.assertEqual((out["known"], out["allowance"], out["spent"]["totalAtomic"]), (False, None, "0"))
        self.assertEqual(out["spent"]["byCategory"], {})
        known = self.rec.report(self.user)
        self.assertEqual(set(out), set(known))

    def test_no_private_field_leaks(self):
        self.limiter()
        self.settle(SECRET_RESOURCE, 1_000_000, TODAY)
        self.settle("bitrefill:invoice:INV-SECRET-1", 2_000_000, TODAY)
        text = json.dumps(self.rec.report(self.user))
        for secret in ("secret", "SECRET", "INV-", "example.test", "0xab", SECRET_TX[:10], "deploy", "owner", "agent",
                       "guardian"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(self.rec.report(self.user)),
                         {"address", "chain", "known", "window", "spent", "creditTopUps", "allowance", "generatedAt"})


class SolanaReportTests(Base):
    def setUp(self):
        super().setUp()
        self.sol.set_limits(SOL_ACCOUNT, SOL_OWNER, 6_000_000, 3_000_000, NOW + 5 * DAY, NOW - DAY)
        self.sol.add_agent(SOL_ACCOUNT, "AgentAddr1111111111111111111111111111111111", "enc", NOW - DAY)

    def spend(self, amount, purpose, at, tx="PullTxSecret"):
        self.sol.add_spend(SOL_ACCOUNT, amount, purpose, tx, at)

    def test_known_account_categories_and_unsettled_excluded(self):
        self.spend(1_000_000, "Weather", TODAY + 5)
        self.spend(2_000_000, SECRET_PURPOSE, TODAY - DAY)
        self.spend(500_000, "SingIt Ask · actual usage", TODAY - 2 * DAY)
        self.spend(3_000_000, "SingIt credit", TODAY + 6)
        self.spend(700_000, "some free text I wrote", TODAY + 7)
        self.spend(9_000_000, "Weather", TODAY + 8, tx=None)  # reserved, never confirmed
        out = self.rec.report(SOL_OWNER)
        self.assertEqual((out["chain"], out["known"], out["address"]), ("solana", True, SOL_OWNER))
        self.assertEqual(out["spent"]["totalAtomic"], "4200000")
        self.assertEqual(sorted(out["spent"]["byCategory"]), ["chat_answer", "data_tool", "gift_card", "other"])
        self.assertEqual(out["creditTopUps"]["atomic"], "3000000")
        al = out["allowance"]
        self.assertEqual((al["state"], al["dailyCapAtomic"], al["perPurchaseCapAtomic"]), ("waiting_for_grant", "6000000", "3000000"))
        self.assertEqual(al["limiter"], "AgentAddr1111111111111111111111111111111111")
        # today: Weather 1.0 + credit 3.0 + other 0.7 settled, plus the 9.0 unsettled one as held room
        self.assertEqual(al["spentTodayAtomic"], str(1_000_000 + 3_000_000 + 700_000 + 9_000_000))
        self.assertEqual(al["remainingTodayAtomic"], "0")

    def test_no_private_field_leaks(self):
        self.spend(2_000_000, SECRET_PURPOSE, TODAY)
        self.spend(700_000, "some free text I wrote", TODAY)
        text = json.dumps(self.rec.report(SOL_OWNER))
        for secret in ("SECRET", "free text", "PullTx", "Amazon"):
            self.assertNotIn(secret, text)

    def test_expired(self):
        self.sol.set_limits(SOL_ACCOUNT, SOL_OWNER, 6_000_000, 3_000_000, NOW - 1, NOW - DAY)
        self.assertEqual(self.rec.report(SOL_OWNER)["allowance"]["state"], "expired")

    def test_purpose_mapping_is_conservative(self):
        self.assertEqual(solana_category("Bitrefill X 10"), "gift_card")
        self.assertEqual(solana_category("Venice AI credit"), "other")
        self.assertEqual(solana_category(""), "other")


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.limiter()
        self.settle(SECRET_RESOURCE, 1_000_000, TODAY)
        self.service = SimpleNamespace(store=self.base)
        self.api = wa.WebApi(SimpleNamespace(), self.service, now=lambda: NOW)
        self.api.solana = SimpleNamespace(store=self.sol)
        self.api.reconciliation_by_ip = wa.RateLimit(50, 60)
        self.server = wa.WebServer(("127.0.0.1", 0), self.api, "https://app.test")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.url + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_served_without_an_api_key_with_public_headers(self):
        status, headers, body = self.get(f"/v1/wallet/{self.user}/reconciliation?days=7")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(headers["Cache-Control"], "public, max-age=60")
        self.assertNotIn("Access-Control-Allow-Credentials", headers)
        out = json.loads(body)
        self.assertEqual((out["known"], out["window"]["days"], out["spent"]["count"]), (True, 7, 1))

    def test_head_has_headers_and_no_body(self):
        status, headers, body = self.get(f"/v1/wallet/{self.user}/reconciliation", "HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["Cache-Control"], "public, max-age=60")

    def test_bad_input_is_400_json(self):
        for path in ("/v1/wallet/0x123/reconciliation", "/v1/wallet/not-an-address!/reconciliation",
                     f"/v1/wallet/{self.user}/reconciliation?days=0", f"/v1/wallet/{self.user}/reconciliation?days=91",
                     f"/v1/wallet/{self.user}/reconciliation?days=x", "/v1/wallet/" + "1" * 50 + "/reconciliation"):
            with self.subTest(path):
                status, _, body = self.get(path)
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(body)["ok"])

    def test_other_v1_paths_still_need_a_key(self):
        status, _, _ = self.get("/v1/wallet/" + self.user + "/other")
        self.assertIn(status, (401, 404))
        self.assertNotEqual(status, 200)

    def test_rate_limit_is_429_with_retry_after(self):
        self.api.reconciliation_by_ip = wa.RateLimit(3, 60)
        for _ in range(3):
            self.assertEqual(self.get(f"/v1/wallet/{self.user}/reconciliation")[0], 200)
        status, headers, body = self.get(f"/v1/wallet/{self.user}/reconciliation")
        self.assertEqual((status, headers["Retry-After"]), (429, "60"))
        self.assertEqual(json.loads(body)["error"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
