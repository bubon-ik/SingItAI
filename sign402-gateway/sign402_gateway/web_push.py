"""Web Push to the installed web page (docs/allowance-web-v1.md, "Notifications").

The page subscribes through its service worker; the browser's push service (Apple,
Google, Mozilla, Microsoft) hands out an endpoint URL and two keys. We encrypt each
message to those keys (RFC 8291, aes128gcm) and sign the request with our VAPID key
(RFC 8292), so the push service carries text it cannot read, from a sender it can
name. Built on `cryptography`, which the gateway already depends on.

The server POSTs to a URL the browser chose, so only the known push services'
hosts are accepted: anything else would let a signed-in user make us call any host.

    SIGN402_WEB_PUSH_VAPID_KEY   our VAPID private key (base64url, 32 bytes); unset: no pushes
    SIGN402_WEB_PUSH_SUBJECT     how a push service contacts us (default: SIGN402_WEB_URI)

    python -m sign402_gateway.web_push generate   # prints a new key line for the env file
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

VAPID_KEY_ENV = "SIGN402_WEB_PUSH_VAPID_KEY"
SUBJECT_ENV = "SIGN402_WEB_PUSH_SUBJECT"
RECORD_SIZE = 4096
MAX_ENDPOINT = 1024
MAX_DEVICES = 10          # per account; the oldest subscription goes first
MAX_BODY_CHARS = 300      # what a lock screen shows anyway
USER_AGENT = "SingIt-WebPush/1"

# Push services the major browsers use. Exact hosts, or a suffix starting with a dot.
PUSH_HOSTS = (
    "fcm.googleapis.com",                   # Chrome, Edge on Android, Brave, Opera
    "updates.push.services.mozilla.com",    # Firefox
    "web.push.apple.com", ".push.apple.com",  # Safari and installed web apps on iPhone, iPad and Mac
    ".notify.windows.com",                  # Edge on Windows
)


class PushError(ValueError):
    """A subscription the page sent that we will not use."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    text = str(text).strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def private_key_from(text: str) -> ec.EllipticCurvePrivateKey:
    raw = unb64url(text)
    if len(raw) != 32:
        raise ValueError(f"{VAPID_KEY_ENV} must be 32 bytes, base64url.")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def generate_key() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return b64url(key.private_numbers().private_value.to_bytes(32, "big"))


def public_key_of(private: ec.EllipticCurvePrivateKey) -> str:
    """What the page passes to pushManager.subscribe as applicationServerKey."""
    return b64url(_public_bytes(private.public_key()))


# -- the subscription the page sends --

def allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)


def check_subscription(subscription: Any) -> tuple[str, str, str]:
    """(endpoint, p256dh, auth) from a PushSubscription's toJSON(), or PushError."""
    if not isinstance(subscription, Mapping):
        raise PushError("The browser sent no subscription.")
    endpoint = str(subscription.get("endpoint") or "")
    keys = subscription.get("keys") if isinstance(subscription.get("keys"), Mapping) else {}
    parts = urlsplit(endpoint)
    if (len(endpoint) > MAX_ENDPOINT or parts.scheme != "https" or parts.port not in (None, 443)
            or parts.username or parts.password or not allowed_host(parts.hostname or "")):
        raise PushError("This browser's push service is not supported.")
    try:
        p256dh, auth = unb64url(keys.get("p256dh", "")), unb64url(keys.get("auth", ""))
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    except (ValueError, TypeError):
        raise PushError("The browser sent an unreadable subscription.") from None
    if len(p256dh) != 65 or len(auth) != 16:
        raise PushError("The browser sent an unreadable subscription.")
    return endpoint, b64url(p256dh), b64url(auth)


# -- RFC 8291: the message, readable only by the subscribed browser --

def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *,
            as_private: ec.EllipticCurvePrivateKey | None = None, salt: bytes | None = None) -> bytes:
    """One aes128gcm record: salt, record size, our ephemeral public key, ciphertext."""
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt if salt is not None else os.urandom(16)
    as_public = _public_bytes(as_private.public_key())
    shared = as_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    prk_key = _hmac(auth_secret, shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(plaintext) + 1 + 16 > RECORD_SIZE:
        raise ValueError("The message is too long for one push.")
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # \x02: the last record, no padding
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public + ciphertext


# -- RFC 8292: who is sending --

def vapid_authorization(endpoint: str, private: ec.EllipticCurvePrivateKey, subject: str, now: float) -> str:
    parts = urlsplit(endpoint)
    header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64url(json.dumps({"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(now) + 12 * 3600,
                                "sub": subject}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    r, s = decode_dss_signature(private.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{claims}.{signature}, k={public_key_of(private)}"


def _post(url: str, headers: dict[str, str], body: bytes) -> int:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


class WebPush:
    """Sends a short notice to every device an account has subscribed."""

    def __init__(self, store: Any, private: ec.EllipticCurvePrivateKey, subject: str, *,
                 post: Callable[[str, dict[str, str], bytes], int] | None = None, now: Callable[[], float] = time.time):
        self.store = store
        self.private = private
        self.public_key = public_key_of(private)
        self.subject = subject
        self.post = post or _post
        self.now = now

    def subscribe(self, account: str, subscription: Any) -> int:
        endpoint, p256dh, auth = check_subscription(subscription)
        self.store.add_push(account, endpoint, p256dh, auth, int(self.now()), keep=MAX_DEVICES)
        return len(self.store.pushes_for(account))

    def send(self, account: str, title: str, body: str, *, url: str = "/app/", urgent: bool = False) -> int:
        """Delivered count. Never raises: a notice that cannot go out must not stop what sent it."""
        body = body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS - 1] + "…"
        message = json.dumps({"title": title, "body": body, "url": url}).encode()
        delivered = 0
        for row in self.store.pushes_for(account):
            host = urlsplit(row["endpoint"]).hostname or "?"  # the endpoint itself is a capability: never logged
            try:
                sealed = encrypt(message, unb64url(row["p256dh"]), unb64url(row["auth"]))
                status = self.post(row["endpoint"], {
                    "Authorization": vapid_authorization(row["endpoint"], self.private, self.subject, self.now()),
                    "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                    "TTL": str(24 * 3600), "Urgency": "high" if urgent else "normal", "User-Agent": USER_AGENT,
                }, sealed)
            except Exception as exc:
                logger.warning("web push: sending to %s failed (%s)", host, type(exc).__name__)
                continue
            if status in (404, 410):  # the user turned notifications off, or reinstalled the app
                self.store.drop_push(row["endpoint"])
            elif 200 <= status < 300:
                delivered += 1
            else:
                logger.warning("web push: %s answered HTTP %s", host, status)
        return delivered


def from_env(store: Any, env: Mapping[str, str] | None = None) -> WebPush | None:
    values = os.environ if env is None else env
    key = str(values.get(VAPID_KEY_ENV, "")).strip()
    if not key:
        return None
    subject = str(values.get(SUBJECT_ENV, "") or values.get("SIGN402_WEB_URI", "")).strip().rstrip("/")
    if not subject.startswith(("mailto:", "https://")):
        raise ValueError(f"{SUBJECT_ENV} (or SIGN402_WEB_URI) must be a mailto: or https:// address.")
    return WebPush(store, private_key_from(key), subject)


def main(argv: list[str]) -> int:
    if argv[1:] == ["generate"]:
        print(f"{VAPID_KEY_ENV}={generate_key()}")
        return 0
    print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
