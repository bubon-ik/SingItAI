import json
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.fernet import Fernet
from eth_account import Account
from eth_account.messages import encode_defunct

from sign402_gateway import agent_allowance as aa
from sign402_gateway import web_api as wa
from sign402_gateway.web_accounts import WebAccountStore, WebAuth, WebAuthError, account_id_for
from tests.test_agent_allowance import FakeEvm, funded

NOW = 1_800_000_000
DOMAIN, URI = "app.singit.test", "https://app.singit.test"


def sign(account, message):
    return Account.sign_message(encode_defunct(text=message), account.key).signature.to_0x_hex()


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Account.create()
        self.clock = [NOW]
        self.store = WebAccountStore(Path(self.tmp.name) / "web.db")
        self.auth = WebAuth(self.store, domain=DOMAIN, uri=URI, allowed=[self.user.address],
                            now=lambda: self.clock[0])

    def sign_in(self, signer=None, edit=lambda m: m):
        issued = self.auth.nonce(self.user.address.lower())
        message = edit(issued["message"])
        return self.auth.verify(message, sign(signer or self.user, message))

    def test_the_message_is_eip4361_for_this_site_and_base(self):
        message = self.auth.nonce(self.user.address)["message"]
        lines = message.splitlines()
        self.assertEqual(lines[0], f"{DOMAIN} wants you to sign in with your Ethereum account:")
        self.assertEqual(lines[1], self.user.address)
        self.assertIn("does not move funds", message)
        for expected in (f"URI: {URI}", "Version: 1", "Chain ID: 8453"):
            self.assertIn(expected, lines)
        self.assertTrue(any(line.startswith("Expiration Time: ") for line in lines))

    def test_signing_in_opens_a_session_and_an_account_owned_by_that_address(self):
        signed = self.sign_in()
        self.assertEqual(signed["account"], account_id_for(self.user.address))
        session = self.auth.session(signed["token"], signed["csrfToken"])
        self.assertEqual(session["address"], self.user.address)
        self.assertEqual(self.store.owner_for(signed["account"]), self.user.address)
        self.assertIsNone(self.store.owner_for("1045618308"))

    def expired(self):
        issued = self.auth.nonce(self.user.address)
        self.clock[0] = NOW + 301
        return self.auth.verify(issued["message"], sign(self.user, issued["message"]))

    def test_what_must_not_sign_in(self):
        cases = {
            "another key": lambda: self.sign_in(signer=Account.create()),
            "an edited message": lambda: self.sign_in(edit=lambda m: m.replace(DOMAIN, "evil.test")),
            "an expired request": self.expired,
            "garbage": lambda: self.auth.verify("Nonce: nope", "0x00"),
        }
        for name, attempt in cases.items():
            with self.subTest(name):
                self.clock[0] = NOW
                with self.assertRaises(WebAuthError):
                    attempt()

    def test_a_signed_message_signs_in_once(self):
        issued = self.auth.nonce(self.user.address)
        signature = sign(self.user, issued["message"])
        self.auth.verify(issued["message"], signature)
        with self.assertRaises(WebAuthError):
            self.auth.verify(issued["message"], signature)

    def test_only_the_beta_allowlist_gets_in_and_stays_in(self):
        with self.assertRaises(WebAuthError):
            self.auth.nonce(Account.create().address)
        signed = self.sign_in()
        self.auth.allowed = set()
        with self.assertRaises(WebAuthError):
            self.auth.session(signed["token"])

    def test_sessions_need_their_csrf_token_expire_and_log_out(self):
        signed = self.sign_in()
        with self.assertRaises(WebAuthError):
            self.auth.session(signed["token"], "wrong")
        self.auth.logout(signed["token"])
        with self.assertRaises(WebAuthError):
            self.auth.session(signed["token"])
        again = self.sign_in()
        self.clock[0] += 12 * 3600 + 1
        with self.assertRaises(WebAuthError):
            self.auth.session(again["token"])

    def test_tokens_are_stored_only_as_hashes(self):
        signed = self.sign_in()
        raw = (Path(self.tmp.name) / "web.db").read_bytes()
        self.assertNotIn(signed["token"].encode(), raw)
        self.assertNotIn(signed["csrfToken"].encode(), raw)


class WebApiTests(unittest.TestCase):
    """The routes, with the real allowance service on a fake chain."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Account.create()
        self.store = WebAccountStore(Path(self.tmp.name) / "web.db")
        self.evm = FakeEvm(aa.Artifact.load())
        self.service = aa.AllowanceService(
            store=aa.AllowanceStore(Path(self.tmp.name) / "allowance.db"), evm=self.evm,
            fernet=Fernet(Fernet.generate_key()), owners={}, owner_lookup=self.store.owner_for,
            guardian_key=lambda: Account.create().key.to_0x_hex(), gas_funder_key=funded(self.evm),
            artifact=self.evm.artifact, max_daily=100_000_000, max_per_purchase=25_000_000, max_days=90,
            now=lambda: NOW,
        )
        self.api = wa.WebApi(WebAuth(self.store, domain=DOMAIN, uri=URI, allowed=None, now=lambda: NOW),
                             self.service, now=lambda: NOW)

    def call(self, method, path, body=None, *, token="", csrf=None, client="198.51.100.7"):
        return self.api.handle(method, path, token=token, csrf=csrf, body=body or {}, client=client)

    def sign_in(self):
        _, issued, _ = self.call("POST", "/auth/nonce", {"address": self.user.address})
        status, body, headers = self.call("POST", "/auth/verify", {
            "message": issued["message"], "signature": sign(self.user, issued["message"])})
        self.assertEqual(status, 200)
        token = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        return token, body["csrfToken"], body

    def test_sign_in_sets_a_strict_http_only_cookie(self):
        _, _, body = self.sign_in()
        _, issued, _ = self.call("POST", "/auth/nonce", {"address": self.user.address})
        _, _, headers = self.call("POST", "/auth/verify", {"message": issued["message"],
                                                           "signature": sign(self.user, issued["message"])})
        for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/web/v1"):
            self.assertIn(flag, headers["Set-Cookie"])
        self.assertEqual(body["address"], self.user.address)

    def test_a_signed_in_wallet_creates_its_own_limiter(self):
        token, csrf, body = self.sign_in()
        status, allowance, _ = self.call("GET", "/allowance", token=token)
        self.assertEqual((status, allowance["configured"]), (200, False))
        self.assertNotIn("telegramText", allowance)

        with self.assertRaises(WebAuthError):
            self.call("POST", "/allowance/setup", {"dailyCap": "5", "perPurchaseCap": "1", "days": "30"}, token=token)
        status, created, _ = self.call("POST", "/allowance/setup",
                                       {"dailyCap": "5", "perPurchaseCap": "1", "days": "30"}, token=token, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertTrue(created["created"])
        self.assertEqual(created["state"], "waiting_for_grant")
        self.assertEqual(created["owner"], self.user.address)
        deployed_owner = self.evm._constructor()[1]
        self.assertEqual(deployed_owner, int(self.user.address, 16))

        _, allowance, _ = self.call("GET", "/allowance", token=token)
        self.assertEqual((allowance["limiter"], allowance["state"]), (created["limiter"], "waiting_for_grant"))
        self.assertEqual(allowance["alerts"], [])
        self.assertEqual(allowance["staleLimiters"], [])
        self.assertIn("ownerEthWei", allowance)
        with self.assertRaises(aa.AllowanceError):  # on the lane, nothing granted yet: a refusal
            self.service.lane_for(body["account"])
        self.assertIsNone(self.service.lane_for("wallet:0x" + "12" * 20))  # no such account: not on the lane

    def test_setup_is_refused_before_anything_is_deployed(self):
        token, csrf, _ = self.sign_in()
        self.evm.usdc_balance = lambda address: 999_999
        with self.assertRaises(wa.WebError) as raised:
            self.call("POST", "/allowance/setup", {"dailyCap": "5", "perPurchaseCap": "1", "days": "30"},
                      token=token, csrf=csrf)
        self.assertEqual(raised.exception.code, "owner_needs_usdc")
        self.assertEqual(self.evm.sent, [])

    def test_a_wallet_gets_three_limiters_a_month(self):
        token, csrf, _ = self.sign_in()
        for daily in ("5", "6", "7"):
            self.call("POST", "/allowance/setup", {"dailyCap": daily, "perPurchaseCap": "1", "days": "30"},
                      token=token, csrf=csrf)
        with self.assertRaises(wa.WebError) as raised:
            self.call("POST", "/allowance/setup", {"dailyCap": "8", "perPurchaseCap": "1", "days": "30"},
                      token=token, csrf=csrf)
        self.assertEqual(raised.exception.code, "too_many_limiters")

    def test_sign_in_is_rate_limited_per_client(self):
        for _ in range(30):
            self.call("POST", "/auth/nonce", {"address": self.user.address})
        with self.assertRaises(wa.WebError) as raised:
            self.call("POST", "/auth/nonce", {"address": self.user.address})
        self.assertEqual(raised.exception.status, 429)
        self.call("POST", "/auth/nonce", {"address": self.user.address}, client="203.0.113.9")


class WebHttpTests(unittest.TestCase):
    """The HTTP layer: JSON only, CORS for one origin, errors as JSON."""

    def setUp(self):
        self.api = unittest.mock.Mock()
        self.server = wa.WebServer(("127.0.0.1", 0), self.api, URI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, method, path, body=None, headers=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_the_page_origin_may_call_and_others_may_not(self):
        status, headers, _ = self.request("OPTIONS", "/web/v1/auth/nonce", headers={"Origin": URI})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], URI)
        self.assertIn(wa.CSRF_HEADER, headers["Access-Control-Allow-Headers"])
        status, _, _ = self.request("OPTIONS", "/web/v1/auth/nonce", headers={"Origin": "https://evil.test"})
        self.assertEqual(status, 403)

    def test_json_only_cookie_and_csrf_are_passed_through(self):
        self.api.handle.return_value = (200, {"ok": True}, {})
        status, _, body = self.request("POST", "/web/v1/auth/nonce", b"address=0x1",
                                       {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((status, json.loads(body)["error"]), (415, "json_only"))
        self.api.handle.assert_not_called()

        status, headers, _ = self.request("POST", "/web/v1/allowance/setup", {"dailyCap": "5"}, {
            "Content-Type": "application/json", "Cookie": f"{wa.COOKIE}=tok", wa.CSRF_HEADER: "c",
            "Origin": URI})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        args, kwargs = self.api.handle.call_args
        self.assertEqual(args, ("POST", "/allowance/setup"))
        self.assertEqual((kwargs["token"], kwargs["csrf"], kwargs["body"]), ("tok", "c", {"dailyCap": "5"}))

    def test_failures_are_json_with_the_right_status_and_no_detail_from_crashes(self):
        for error, status, code in ((WebAuthError("Sign in first."), 401, "auth"),
                                    (aa.AllowanceError("Caps are limited"), 400, "refused"),
                                    (aa.AllowanceUnavailable("not enabled"), 403, "not_enabled"),
                                    (RuntimeError("secret detail"), 500, "internal")):
            with self.subTest(code=code):
                self.api.handle.side_effect = error
                got, _, body = self.request("GET", "/web/v1/allowance")
                payload = json.loads(body)
                self.assertEqual((got, payload["error"]), (status, code))
                self.assertNotIn("secret detail", body.decode())
        got, _, _ = self.request("GET", "/health")
        self.assertEqual(got, 404)



from tests.test_agent_allowance import DeviceLaneEvm  # noqa: E402


class WalletChain(DeviceLaneEvm):
    """DeviceLaneEvm plus what the wallet path reads: transactions by hash and block times."""

    def __init__(self, artifact, owner):
        super().__init__(artifact, owner)
        self.txs = {}
        self.receipts = {}
        self.block_time = NOW
        self.nonce = 4

    def call_word(self, to, data):
        if to == aa.USDC and data.startswith(aa.selector("nonces(address)")):
            return self.nonce
        return super().call_word(to, data)

    def call(self, method, params):
        if method == "eth_getTransactionByHash":
            return self.txs.get(params[0])
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(self.block_time)}
        return super().call(method, params)

    def put(self, tx_hash, sender, data, to=aa.USDC, chain_id="0x2105"):
        self.txs[tx_hash] = {"hash": tx_hash, "from": sender.lower(), "to": to.lower(), "input": data,
                             "value": "0x0", "chainId": chain_id}

    def mine(self, n, spender, allowance):
        self.receipts[tx_hash(n)] = {"status": "0x1", "blockNumber": "0x10"}
        self.allowances[spender] = allowance


def tx_hash(n):
    return "0x" + format(n, "064x")


class WalletLaneTests(unittest.TestCase):
    """Grant and revoke from the owner's wallet: the page sends, the chain decides."""

    USER = "wallet:owner"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.owner = Account.create()
        self.clock = [NOW]
        self.evm = WalletChain(aa.Artifact.load(), self.owner.address)
        self.service = aa.AllowanceService(
            store=aa.AllowanceStore(Path(self.tmp.name) / "allowance.db"), evm=self.evm,
            fernet=Fernet(Fernet.generate_key()), owners={self.USER: self.owner.address},
            guardian_key=lambda: Account.create().key.to_0x_hex(), gas_funder_key=funded(self.evm),
            artifact=self.evm.artifact, max_daily=100_000_000, max_per_purchase=25_000_000, max_days=90,
            max_grant=300_000_000, now=lambda: self.clock[0],
        )
        self.limiter = self.service.setup(self.USER, "10", "2", "30")["limiter"]

    def approve(self, spender, amount):
        return aa.encode_call("approve(address,uint256)", spender, amount)

    def grant(self, amount="5", n=1, data=None, sender=None):
        prepared = self.service.prepare_wallet(self.USER, "GRANT", amount=amount)
        self.evm.put(tx_hash(n), sender or self.owner.address, data or prepared["tx"]["data"])
        self.service.submit_wallet(self.USER, prepared["operation"], tx_hash(n))
        return prepared

    def test_the_page_gets_exactly_the_approve_to_send(self):
        prepared = self.service.prepare_wallet(self.USER, "GRANT", amount="5")
        self.assertEqual(prepared["tx"], {"from": self.owner.address, "to": aa.USDC,
                                          "data": self.approve(self.limiter, 5_000_000),
                                          "value": "0x0", "chainId": "0x2105"})
        self.assertEqual(prepared["walletShows"],
                         f"Your wallet will ask you to approve up to 5 USDC for {self.limiter[:6]}…{self.limiter[-4:]}.")
        self.assertEqual(prepared["expiresAt"], NOW + aa.WALLET_PREPARE_SECONDS)

    def test_a_grant_goes_from_submitted_to_done_by_reading_the_chain(self):
        prepared = self.service.prepare_wallet(self.USER, "GRANT", amount="5")
        op = self.service.submit_wallet(self.USER, prepared["operation"], tx_hash(1))
        self.assertEqual(op["state"], "SUBMITTED")  # Base has not seen it yet

        self.evm.put(tx_hash(1), self.owner.address, prepared["tx"]["data"])
        self.assertEqual(self.service.operation(self.USER, prepared["operation"])["state"], "BROADCAST")

        self.evm.mine(1, self.limiter, 5_000_000)
        op = self.service.operation(self.USER, prepared["operation"])
        self.assertEqual((op["state"], op["detail"]), ("DONE", "Allowance now 5 USDC."))
        self.assertEqual(self.service.status(self.USER)["state"], "granted")
        self.assertEqual(self.evm.broadcasts, [])  # the wallet sent it; the server never does

    def test_an_amount_the_user_lowered_in_the_wallet_is_what_counts(self):
        prepared = self.grant("5", data=self.approve(self.limiter, 2_000_000))
        self.evm.mine(1, self.limiter, 2_000_000)
        op = self.service.operation(self.USER, prepared["operation"])
        self.assertEqual((op["state"], op["amountAtomic"]), ("DONE", "2000000"))

    def test_a_transaction_that_is_not_the_prepared_approve_is_never_counted(self):
        stranger = Account.create().address
        cases = {
            "from another wallet": dict(sender=stranger),
            "to another spender": dict(data=self.approve(stranger, 5_000_000)),
            "above the ceiling": dict(data=self.approve(self.limiter, 300_000_001)),
            "not an approve": dict(data=aa.encode_call("transfer(address,uint256)", self.limiter, 5_000_000)),
        }
        for n, (name, spoil) in enumerate(cases.items(), start=10):
            with self.subTest(name):
                prepared = self.grant(n=n, **spoil)
                op = self.service.operation(self.USER, prepared["operation"])
                self.assertEqual(op["state"], "FAILED")
                self.assertIn("Nothing counted", op["detail"])

    def test_another_token_or_chain_is_refused(self):
        prepared = self.service.prepare_wallet(self.USER, "GRANT", amount="5")
        self.evm.put(tx_hash(1), self.owner.address, prepared["tx"]["data"], chain_id="0x1")
        self.service.submit_wallet(self.USER, prepared["operation"], tx_hash(1))
        self.assertIn("not on Base", self.service.operation(self.USER, prepared["operation"])["detail"])

    def test_an_older_transaction_with_the_same_calldata_is_not_this_request(self):
        prepared = self.grant("5")
        self.evm.block_time = NOW - 3600
        self.evm.mine(1, self.limiter, 5_000_000)
        op = self.service.operation(self.USER, prepared["operation"])
        self.assertEqual(op["state"], "FAILED")
        self.assertIn("older than this request", op["detail"])

    def test_one_transaction_counts_for_one_request(self):
        self.grant("5", n=1)
        self.evm.mine(1, self.limiter, 5_000_000)
        self.service.status(self.USER)
        again = self.service.prepare_wallet(self.USER, "GRANT", amount="5")
        with self.assertRaises(aa.AllowanceError):
            self.service.submit_wallet(self.USER, again["operation"], tx_hash(1))

    def test_what_is_refused_before_the_wallet_is_asked(self):
        with self.assertRaises(aa.AllowanceError):
            self.service.prepare_wallet(self.USER, "GRANT", amount="300.01")
        self.evm.getter_override["paused()"] = 1
        with self.assertRaises(aa.AllowanceError) as raised:
            self.service.prepare_wallet(self.USER, "GRANT", amount="5")
        self.assertIn("paused or expired", str(raised.exception))
        self.evm.getter_override = {"owner()": int(Account.create().address, 16)}
        with self.assertRaises(aa.AllowanceError) as raised:
            self.service.prepare_wallet(self.USER, "GRANT", amount="5")
        self.assertIn("failed its check", str(raised.exception))

    def test_requests_expire_are_replaced_and_do_not_overlap(self):
        first = self.service.prepare_wallet(self.USER, "GRANT", amount="5")
        second = self.service.prepare_wallet(self.USER, "GRANT", amount="6")
        self.assertEqual(self.service.operation(self.USER, first["operation"])["state"], "EXPIRED")
        self.service.submit_wallet(self.USER, second["operation"], tx_hash(2))
        with self.assertRaises(aa.AllowanceError):
            self.service.prepare_wallet(self.USER, "GRANT", amount="7")
        self.clock[0] += aa.WALLET_PREPARE_SECONDS + 1
        op = self.service.operation(self.USER, second["operation"])
        self.assertEqual(op["state"], "FAILED")
        self.assertIn("never saw", op["detail"])
        third = self.service.prepare_wallet(self.USER, "GRANT", amount="7")
        self.clock[0] += aa.WALLET_PREPARE_SECONDS + 1
        self.assertEqual(self.service.operation(self.USER, third["operation"])["state"], "EXPIRED")

    def test_a_wallet_revoke_closes_the_lane_and_returns_the_float(self):
        self.grant("5", n=1)
        self.evm.mine(1, self.limiter, 5_000_000)
        self.service.status(self.USER)
        prepared = self.service.prepare_wallet(self.USER, "REVOKE")
        self.assertEqual(prepared["tx"]["data"], self.approve(self.limiter, 0))
        self.assertIn("0 USDC (a revoke)", prepared["walletShows"])
        self.evm.put(tx_hash(2), self.owner.address, prepared["tx"]["data"])
        self.service.submit_wallet(self.USER, prepared["operation"], tx_hash(2))
        self.assertEqual(self.service.operation(self.USER, prepared["operation"])["state"], "BROADCAST")
        self.evm.mine(2, self.limiter, 0)
        op = self.service.operation(self.USER, prepared["operation"])
        self.assertEqual(op["state"], "DONE")
        self.assertIn("went back to your wallet", op["detail"])
        agent = self.service.store.agent(self.USER)["agent_address"]
        self.assertEqual(self.evm.sent[-1]["from"], agent)

    def test_old_limiters_the_owner_still_allows_are_listed_for_revoking(self):
        old = self.limiter
        new = self.service.setup(self.USER, "20", "5", "30")["limiter"]
        self.evm.allowances[old] = 5_000_000
        self.evm.allowances[new] = 1_000_000
        self.assertEqual(self.service.stale_allowances(self.USER),
                         [{"limiter": old, "allowanceAtomic": 5_000_000, "status": "SUPERSEDED"}])
        self.evm.allowances[old] = 0
        self.assertEqual(self.service.stale_allowances(self.USER), [])

    def test_a_revoke_must_approve_zero(self):
        prepared = self.service.prepare_wallet(self.USER, "REVOKE")
        self.evm.put(tx_hash(3), self.owner.address, self.approve(self.limiter, 1))
        self.service.submit_wallet(self.USER, prepared["operation"], tx_hash(3))
        self.assertIn("must approve 0", self.service.operation(self.USER, prepared["operation"])["detail"])


class WalletRoutesTests(WebApiTests):
    def test_grant_through_the_routes_and_only_for_its_own_account(self):
        token, csrf, body = self.sign_in()
        self.call("POST", "/allowance/setup", {"dailyCap": "5", "perPurchaseCap": "1", "days": "30"},
                  token=token, csrf=csrf)
        with self.assertRaises(aa.AllowanceError):
            self.call("POST", "/allowance/grant/prepare", {"amount": "5", "method": "blind"}, token=token, csrf=csrf)
        with self.assertRaises(WebAuthError):
            self.call("POST", "/allowance/grant/prepare", {"amount": "5"}, token=token)
        _, prepared, _ = self.call("POST", "/allowance/grant/prepare", {"amount": "5"}, token=token, csrf=csrf)
        self.assertEqual(prepared["tx"]["from"], self.user.address)

        with self.assertRaises(wa.WebError):  # a grant id on the revoke route
            self.call("POST", "/allowance/revoke/submit", {"operation": prepared["operation"], "txHash": tx_hash(1)},
                      token=token, csrf=csrf)
        self.evm.rpc = self.evm  # FakeEvm has no receipts: the submitted hash stays SUBMITTED
        self.evm.call = lambda method, params: None
        _, op, _ = self.call("POST", "/allowance/grant/submit", {"operation": prepared["operation"], "txHash": tx_hash(1)},
                             token=token, csrf=csrf)
        self.assertEqual(op["state"], "SUBMITTED")
        _, op, _ = self.call("GET", f"/allowance/operations/{prepared['operation']}", token=token)
        self.assertEqual(op["txHash"], tx_hash(1))

        self.user = Account.create()
        other_token, _, _ = self.sign_in()
        with self.assertRaises(aa.AllowanceError):
            self.call("GET", f"/allowance/operations/{prepared['operation']}", token=other_token)


class OperationsMigrationTests(unittest.TestCase):
    def test_a_v1_table_keeps_every_row_and_accepts_the_wallet_states(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowance.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE operations (
                    op_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('GRANT', 'REVOKE', 'PAUSE')),
                    limiter_address TEXT NOT NULL, amount INTEGER NOT NULL, job_id TEXT, tx_hash TEXT,
                    state TEXT NOT NULL CHECK(state IN ('WAITING_DEVICE', 'BROADCAST', 'DONE', 'FAILED')),
                    detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
                CREATE INDEX operations_by_user ON operations(user_id, created_at);
                INSERT INTO operations VALUES ('op_1', '1045618308', 'GRANT', '0xL', 1000000, 'job_1', '0xT',
                                               'DONE', 'Allowance now 0.8 USDC.', 1, 2);""")
            db.commit()
            db.close()
            store = aa.AllowanceStore(path)
            row = store.op("1045618308", "op_1")
            self.assertEqual((row["state"], row["method"], row["detail"]), ("DONE", "device", "Allowance now 0.8 USDC."))
            store.insert_op({"op_id": "op_2", "user_id": "u", "kind": "GRANT", "limiter_address": "0xL", "amount": 1,
                             "job_id": None, "tx_hash": None, "state": "PREPARED", "detail": "", "method": "approve",
                             "created_at": 3, "updated_at": 3})
            aa.AllowanceStore(path)  # a second start changes nothing
            self.assertEqual(len(store.recent_ops("1045618308", 10)), 1)
            indexes = sqlite3.connect(path).execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'operations'").fetchall()
            self.assertIn(("operations_by_user",), indexes)


def sign_permit(account, typed):
    from eth_account.messages import encode_typed_data

    message = {k: (int(v) if k in ("value", "nonce", "deadline") else v) for k, v in typed["message"].items()}
    signable = encode_typed_data(full_message={**typed, "message": message})
    return Account.sign_message(signable, account.key).signature.to_0x_hex()


class PermitTests(WalletLaneTests):
    """Grant and revoke without gas: the wallet signs EIP-2612, we send it."""

    def permit(self, kind="GRANT", amount="5", signer=None):
        prepared = self.service.prepare_wallet(self.USER, kind, amount=amount, method="permit")
        signature = sign_permit(signer or self.owner, prepared["typedData"])
        return prepared, signature

    def test_the_wallet_gets_usdc_permit_typed_data_with_the_current_nonce(self):
        prepared, _ = self.permit()
        typed = prepared["typedData"]
        self.assertEqual(typed["domain"], {"name": "USD Coin", "version": "2", "chainId": 8453,
                                           "verifyingContract": aa.USDC})
        self.assertEqual(typed["message"], {"owner": self.owner.address, "spender": self.limiter, "value": "5000000",
                                            "nonce": "4", "deadline": str(NOW + aa.WALLET_PREPARE_SECONDS)})
        self.assertNotIn("tx", prepared)
        self.assertIn("costs you no gas", prepared["walletShows"])

    def test_a_signed_permit_is_sent_by_us_and_read_back(self):
        prepared, signature = self.permit()
        sent = len(self.evm.sent)
        op = self.service.submit_permit(self.USER, prepared["operation"], signature)
        tx = self.evm.sent[sent]
        self.assertEqual((tx["to"], tx["value"]), (aa.USDC, 0))
        raw = bytes.fromhex(signature[2:])
        v = raw[64] if raw[64] >= 27 else raw[64] + 27
        self.assertEqual(tx["data"], aa.encode_call(
            "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)", self.owner.address, self.limiter,
            5_000_000, NOW + aa.WALLET_PREPARE_SECONDS, v, int.from_bytes(raw[:32], "big"),
            int.from_bytes(raw[32:64], "big")))
        self.assertEqual(op["state"], "BROADCAST")
        self.evm.receipts[op["txHash"]] = {"status": "0x1", "blockNumber": "0x10"}
        self.evm.allowances[self.limiter] = 5_000_000
        self.assertEqual(self.service.operation(self.USER, prepared["operation"])["state"], "DONE")

    def test_a_permit_someone_else_sent_first_still_counts(self):
        prepared, signature = self.permit()
        op = self.service.submit_permit(self.USER, prepared["operation"], signature)
        self.evm.receipts[op["txHash"]] = {"status": "0x0", "blockNumber": "0x10"}
        self.evm.allowances[self.limiter] = 5_000_000
        self.assertEqual(self.service.operation(self.USER, prepared["operation"])["state"], "DONE")

    def test_what_is_never_sent(self):
        sent = len(self.evm.sent)
        prepared, signature = self.permit(signer=Account.create())
        with self.assertRaises(aa.AllowanceError) as raised:
            self.service.submit_permit(self.USER, prepared["operation"], signature)
        self.assertIn("not signed by your wallet", str(raised.exception))

        prepared, signature = self.permit()
        self.evm.nonce = 5  # the wallet used this nonce for another permit meanwhile
        with self.assertRaises(aa.AllowanceError) as raised:
            self.service.submit_permit(self.USER, prepared["operation"], signature)
        self.assertIn("nonce", str(raised.exception))

        self.evm.nonce = 4
        prepared, signature = self.permit()
        self.clock[0] += aa.WALLET_PREPARE_SECONDS
        with self.assertRaises(aa.AllowanceError):
            self.service.submit_permit(self.USER, prepared["operation"], signature)
        self.assertEqual(self.service.operation(self.USER, prepared["operation"])["state"], "EXPIRED")

        with self.assertRaises(aa.AllowanceError):
            self.service.submit_permit(self.USER, prepared["operation"], "0x1234")
        self.assertEqual(len(self.evm.sent), sent)

    def test_approve_and_permit_requests_take_only_their_own_answer(self):
        prepared, signature = self.permit()
        with self.assertRaises(aa.AllowanceError):
            self.service.submit_wallet(self.USER, prepared["operation"], tx_hash(1))
        approve = self.service.prepare_wallet(self.USER, "GRANT", amount="5")
        with self.assertRaises(aa.AllowanceError):
            self.service.submit_permit(self.USER, approve["operation"], signature)

    def test_a_gasless_revoke(self):
        prepared, signature = self.permit(kind="REVOKE", amount=None)
        self.assertEqual(prepared["typedData"]["message"]["value"], "0")
        op = self.service.submit_permit(self.USER, prepared["operation"], signature)
        self.evm.receipts[op["txHash"]] = {"status": "0x1", "blockNumber": "0x10"}
        self.evm.allowances[self.limiter] = 0
        self.assertEqual(self.service.operation(self.USER, prepared["operation"])["state"], "DONE")


class StaticPageTests(unittest.TestCase):
    """The web API serves website/app and website/assets, and nothing else of the disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "website"
        (root / "app").mkdir(parents=True)
        (root / "assets").mkdir()
        (root / "app" / "index.html").write_text("<title>app</title>")
        (root / "app" / "main.js").write_text("console.log(1)")
        (root / "assets" / "favicon.svg").write_text("<svg/>")
        (root / "index.html").write_text("landing")
        (Path(self.tmp.name) / "secret.txt").write_text("SECRET")
        self.api = unittest.mock.Mock()
        self.api.handle.return_value = (200, {"ok": True}, {})
        self.server = wa.WebServer(("127.0.0.1", 0), self.api, URI, root)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path, headers=None):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), body

    def test_the_page_and_its_assets_are_served_with_safe_headers(self):
        status, headers, body = self.get("/app/")
        self.assertEqual((status, body), (200, b"<title>app</title>"))
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.get("/app/main.js")[1]["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(self.get("/assets/favicon.svg")[0], 200)
        status, headers, _ = self.get("/")
        self.assertEqual((status, headers["Location"]), (302, "/app/"))

    def test_nothing_outside_app_and_assets_is_reachable(self):
        for path in ("/index.html", "/../secret.txt", "/app/../../secret.txt", "/assets/../index.html",
                     "/app/%2e%2e/%2e%2e/secret.txt", "/nope"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"SECRET", body)
                self.assertNotIn(b"landing", body)

    def test_api_paths_still_reach_the_api_and_the_client_is_cloudflares_view(self):
        self.get("/web/v1/session", {"Cf-Connecting-Ip": "203.0.113.7", "X-Forwarded-For": "198.51.100.1"})
        self.assertEqual(self.api.handle.call_args.kwargs["client"], "203.0.113.7")
        self.get("/web/v1/session", {"X-Forwarded-For": "1.1.1.1, 198.51.100.2"})
        self.assertEqual(self.api.handle.call_args.kwargs["client"], "198.51.100.2")
