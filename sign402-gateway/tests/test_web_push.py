import hashlib
import hmac
import json
import struct
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sign402_gateway import allowance_watcher as aw
from sign402_gateway import web_api as wa
from sign402_gateway import web_push as wp
from sign402_gateway.web_accounts import WebAccountStore
from tests.test_web_api import NOW, URI, WebApiTests

ACCOUNT = "wallet:0x1111111111111111111111111111111111111111"


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


class Browser:
    """The subscribing side: its keys, the subscription it hands the page, and decryption."""

    def __init__(self, host="fcm.googleapis.com", name="a"):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = bytes(range(16))
        self.public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                         serialization.PublicFormat.UncompressedPoint)
        self.endpoint = f"https://{host}/fcm/send/{name}"

    def subscription(self):
        return {"endpoint": self.endpoint, "keys": {"p256dh": wp.b64url(self.public), "auth": wp.b64url(self.auth)}}

    def decrypt(self, body):
        salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
        as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
        ikm = _hmac(_hmac(self.auth, shared), b"WebPush: info\x00" + self.public + as_public + b"\x01")
        prk = _hmac(salt, ikm)
        cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
        nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
        plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
        assert rs == 4096 and plain.endswith(b"\x02")
        return json.loads(plain[:-1])


class Sent:
    """A push service: records what we POST and answers with the next status."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.requests = []

    def __call__(self, url, headers, body):
        self.requests.append((url, headers, body))
        status = self.statuses.pop(0) if self.statuses else 201
        if isinstance(status, Exception):
            raise status
        return status


class EncryptionTests(unittest.TestCase):
    def test_rfc8291_appendix_a(self):
        as_private = ec.derive_private_key(
            int.from_bytes(wp.unb64url("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        sealed = wp.encrypt(
            b"When I grow up, I want to be a watermelon",
            wp.unb64url("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
            wp.unb64url("BTBZMqHH6r4Tts7J_aSIgg"), as_private=as_private, salt=wp.unb64url("DGv6ra1nlYgDCS1FRnbzlw"))
        self.assertEqual(wp.b64url(sealed), (
            "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6Tlz"
            "AC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"))

    def test_only_the_subscribed_browser_reads_it(self):
        browser = Browser()
        sealed = wp.encrypt(b'{"body": "hi"}', browser.public, browser.auth)
        self.assertEqual(browser.decrypt(sealed), {"body": "hi"})
        self.assertNotIn(b"hi", sealed)
        with self.assertRaises(Exception):
            Browser().decrypt(sealed)

    def test_vapid_names_the_push_service_and_verifies_with_our_public_key(self):
        key = wp.private_key_from(wp.generate_key())
        header = wp.vapid_authorization("https://fcm.googleapis.com/fcm/send/abc", key, "mailto:ops@singit.test", NOW)
        token, public = header.removeprefix("vapid t=").split(", k=")
        self.assertEqual(public, wp.public_key_of(key))
        head, claims, signature = token.split(".")
        self.assertEqual(json.loads(wp.unb64url(claims)),
                         {"aud": "https://fcm.googleapis.com", "exp": NOW + 12 * 3600, "sub": "mailto:ops@singit.test"})
        raw = wp.unb64url(signature)
        key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                                f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))

    def test_a_generated_key_loads_and_a_bad_one_does_not(self):
        self.assertEqual(len(wp.unb64url(wp.public_key_of(wp.private_key_from(wp.generate_key())))), 65)
        with self.assertRaises(ValueError):
            wp.private_key_from("short")


class SubscriptionTests(unittest.TestCase):
    def test_the_major_push_services_are_accepted(self):
        for host in ("fcm.googleapis.com", "web.push.apple.com", "api.push.apple.com",
                     "updates.push.services.mozilla.com", "wns2-par02p.notify.windows.com"):
            with self.subTest(host):
                endpoint, _, _ = wp.check_subscription(Browser(host).subscription())
                self.assertTrue(endpoint.startswith(f"https://{host}/"))

    def test_we_never_post_anywhere_else(self):
        good = Browser().subscription()
        cases = {
            "another host": "https://evil.test/push",
            "a lookalike suffix": "https://evilpush.apple.com/x",
            "a lookalike prefix": "https://fcm.googleapis.com.evil.test/x",
            "plain http": "http://fcm.googleapis.com/fcm/send/a",
            "another port": "https://fcm.googleapis.com:8443/fcm/send/a",
            "credentials": "https://user:pw@fcm.googleapis.com/fcm/send/a",
            "loopback": "https://127.0.0.1/push",
            "too long": "https://fcm.googleapis.com/" + "a" * 2000,
        }
        for name, endpoint in cases.items():
            with self.subTest(name), self.assertRaises(wp.PushError):
                wp.check_subscription({**good, "endpoint": endpoint})

    def test_unreadable_keys_are_refused(self):
        good = Browser().subscription()
        for keys in ({}, {"p256dh": "AAAA", "auth": good["keys"]["auth"]},
                     {"p256dh": wp.b64url(b"\x04" + b"\x01" * 64), "auth": good["keys"]["auth"]},
                     {"p256dh": good["keys"]["p256dh"], "auth": "AAAA"}):
            with self.subTest(keys), self.assertRaises(wp.PushError):
                wp.check_subscription({**good, "keys": keys})
        with self.assertRaises(wp.PushError):
            wp.check_subscription("nope")


class SendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WebAccountStore(Path(self.tmp.name) / "web.db")
        self.store.ensure_account(ACCOUNT.split(":")[1], NOW)
        self.sent = Sent()
        self.push = wp.WebPush(self.store, wp.private_key_from(wp.generate_key()), "https://app.singit.test",
                               post=self.sent, now=lambda: NOW)

    def test_each_device_gets_the_message_sealed_to_it(self):
        phone, laptop = Browser(name="phone"), Browser("web.push.apple.com", "laptop")
        self.push.subscribe(ACCOUNT, phone.subscription())
        self.assertEqual(self.push.subscribe(ACCOUNT, laptop.subscription()), 2)
        self.assertEqual(self.push.send(ACCOUNT, "SingIt", "Your limiter was paused.", urgent=True), 2)
        by_url = {url: (headers, body) for url, headers, body in self.sent.requests}
        for browser in (phone, laptop):
            headers, body = by_url[browser.endpoint]
            self.assertEqual(browser.decrypt(body), {"title": "SingIt", "body": "Your limiter was paused.", "url": "/app/"})
            self.assertEqual((headers["Content-Encoding"], headers["Urgency"], headers["TTL"]), ("aes128gcm", "high", "86400"))
            self.assertTrue(headers["Authorization"].startswith("vapid t="))

    def test_gone_devices_are_forgotten_and_failures_never_raise(self):
        for name in ("gone", "broken", "ok"):
            self.push.subscribe(ACCOUNT, Browser(name=name).subscription())
        self.sent.statuses = [410, OSError("down"), 201]
        self.assertEqual(self.push.send(ACCOUNT, "SingIt", "hello"), 1)
        left = [row["endpoint"].rsplit("/", 1)[1] for row in self.store.pushes_for(ACCOUNT)]
        self.assertEqual(left, ["broken", "ok"])

    def test_long_text_is_cut_to_what_a_lock_screen_shows(self):
        browser = Browser()
        self.push.subscribe(ACCOUNT, browser.subscription())
        self.push.send(ACCOUNT, "SingIt", "x" * 1000)
        body = browser.decrypt(self.sent.requests[0][2])["body"]
        self.assertEqual((len(body), body[-1]), (wp.MAX_BODY_CHARS, "…"))

    def test_an_account_keeps_its_newest_devices_and_a_device_moves_with_its_sign_in(self):
        clock = [NOW]
        self.push.now = lambda: clock[0]
        browsers = [Browser(name=str(i)) for i in range(wp.MAX_DEVICES + 2)]
        for browser in browsers:
            clock[0] += 1
            self.push.subscribe(ACCOUNT, browser.subscription())
        kept = [row["endpoint"] for row in self.store.pushes_for(ACCOUNT)]
        self.assertEqual(kept, [b.endpoint for b in browsers[2:]])
        other = "wallet:0x2222222222222222222222222222222222222222"
        self.push.subscribe(other, browsers[-1].subscription())
        self.assertEqual([r["endpoint"] for r in self.store.pushes_for(other)], [browsers[-1].endpoint])
        self.assertNotIn(browsers[-1].endpoint, [r["endpoint"] for r in self.store.pushes_for(ACCOUNT)])

    def test_a_lane_user_is_found_by_account_or_linked_telegram(self):
        self.assertEqual(self.store.account_for_user(ACCOUNT), ACCOUNT)
        self.assertIsNone(self.store.account_for_user("wallet:0x3333333333333333333333333333333333333333"))
        self.assertIsNone(self.store.account_for_user("1045618308"))
        code = self.store.new_link_code(ACCOUNT, NOW)
        self.store.link_telegram(code, "1045618308", NOW)
        self.assertEqual(self.store.account_for_user("1045618308"), ACCOUNT)

    def test_from_env(self):
        self.assertIsNone(wp.from_env(self.store, {}))
        key = wp.generate_key()
        push = wp.from_env(self.store, {wp.VAPID_KEY_ENV: key, "SIGN402_WEB_URI": URI + "/"})
        self.assertEqual((push.subject, push.public_key), (URI, wp.public_key_of(wp.private_key_from(key))))
        with self.assertRaises(ValueError):
            wp.from_env(self.store, {wp.VAPID_KEY_ENV: key, wp.SUBJECT_ENV: "ops@singit.test"})


class PushRoutesTests(WebApiTests):
    def setUp(self):
        super().setUp()
        self.sent = Sent()
        self.api.push = wp.WebPush(self.store, wp.private_key_from(wp.generate_key()), URI, post=self.sent,
                                   now=lambda: NOW)

    def test_subscribe_test_and_unsubscribe(self):
        token, csrf, body = self.sign_in()
        _, key, _ = self.call("GET", "/push/key", token=token)
        self.assertEqual((key["enabled"], key["publicKey"], key["devices"]), (True, self.api.push.public_key, 0))
        browser = Browser()
        _, reply, _ = self.call("POST", "/push/subscribe", {"subscription": browser.subscription()}, token=token, csrf=csrf)
        self.assertEqual(reply, {"subscribed": True, "devices": 1})
        _, reply, _ = self.call("POST", "/push/test", token=token, csrf=csrf)
        self.assertEqual(reply, {"sent": 1})
        self.assertIn("Notifications are on", browser.decrypt(self.sent.requests[0][2])["body"])
        self.call("POST", "/push/unsubscribe", {"endpoint": browser.endpoint}, token=token, csrf=csrf)
        self.assertEqual(self.store.pushes_for(body["account"]), [])
        with self.assertRaises(wa.WebError) as caught:
            self.call("POST", "/push/test", token=token, csrf=csrf)
        self.assertEqual(caught.exception.code, "push_failed")

    def test_a_foreign_endpoint_or_a_missing_csrf_is_refused(self):
        token, csrf, _ = self.sign_in()
        with self.assertRaises(wa.WebError) as caught:
            self.call("POST", "/push/subscribe", {"subscription": {**Browser().subscription(), "endpoint": "https://evil.test/x"}},
                      token=token, csrf=csrf)
        self.assertEqual(caught.exception.code, "push_unsupported")
        with self.assertRaises(Exception):
            self.call("POST", "/push/subscribe", {"subscription": Browser().subscription()}, token=token)
        self.assertEqual(self.sent.requests, [])

    def test_one_account_cannot_remove_anothers_device(self):
        token, csrf, first = self.sign_in()
        browser = Browser()
        self.call("POST", "/push/subscribe", {"subscription": browser.subscription()}, token=token, csrf=csrf)
        from eth_account import Account
        self.user = Account.create()
        other_token, other_csrf, _ = self.sign_in()
        self.call("POST", "/push/unsubscribe", {"endpoint": browser.endpoint}, token=other_token, csrf=other_csrf)
        self.assertEqual(len(self.store.pushes_for(first["account"])), 1)

    def test_off_on_this_server(self):
        self.api.push = None
        token, csrf, _ = self.sign_in()
        self.assertEqual(self.call("GET", "/push/key", token=token)[1], {"enabled": False})
        with self.assertRaises(wa.WebError) as caught:
            self.call("POST", "/push/subscribe", {"subscription": Browser().subscription()}, token=token, csrf=csrf)
        self.assertEqual(caught.exception.code, "push_off")


class WatcherNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "web.db"
        self.store = WebAccountStore(self.db)
        self.store.ensure_account(ACCOUNT.split(":")[1], NOW)
        self.browser = Browser()
        wp.WebPush(self.store, wp.private_key_from(wp.generate_key()), URI).subscribe(ACCOUNT, self.browser.subscription())
        self.env = {"SIGN402_WEB_ENABLED": "1", "SIGN402_WEB_DB": str(self.db), wp.VAPID_KEY_ENV: wp.generate_key(),
                    "SIGN402_WEB_URI": URI}

    def notify(self, env, user, text, telegram_fails=False):
        sent, telegram = Sent(), []
        def fake_telegram(token):
            def send(user_id, message):
                if telegram_fails:
                    raise OSError("telegram down")
                telegram.append((user_id, message))
            return send
        with unittest.mock.patch.object(aw, "TelegramNotifier", fake_telegram), \
                unittest.mock.patch.object(wp, "_post", sent):
            notify = aw.build_notifier(env)
            if notify:
                notify(user, text)
        return notify, telegram, sent

    def test_an_alarm_reaches_telegram_and_the_device_even_when_telegram_fails(self):
        _, telegram, sent = self.notify({**self.env, aw.BOT_TOKEN_ENV: "t"}, ACCOUNT, "⚠️ paused")
        self.assertEqual(telegram, [(ACCOUNT, "⚠️ paused")])
        self.assertEqual(self.browser.decrypt(sent.requests[0][2])["body"], "⚠️ paused")
        self.assertEqual(sent.requests[0][1]["Urgency"], "high")
        _, _, sent = self.notify({**self.env, aw.BOT_TOKEN_ENV: "t"}, ACCOUNT, "info", telegram_fails=True)
        self.assertEqual((len(sent.requests), sent.requests[0][1]["Urgency"]), (1, "normal"))

    def test_without_a_key_it_is_telegram_as_before_and_without_either_nothing(self):
        env = {k: v for k, v in self.env.items() if k != wp.VAPID_KEY_ENV}
        notify, telegram, sent = self.notify({**env, aw.BOT_TOKEN_ENV: "t"}, ACCOUNT, "info")
        self.assertEqual((telegram, sent.requests), ([(ACCOUNT, "info")], []))
        self.assertIsNone(self.notify(env, ACCOUNT, "info")[0])
        self.assertIsNone(self.notify({}, ACCOUNT, "info")[0])


if __name__ == "__main__":
    unittest.main()
