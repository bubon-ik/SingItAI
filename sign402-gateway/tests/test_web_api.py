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

