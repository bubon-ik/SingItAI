"""Phone calls to numbers StablePhone cannot reach (Europe first), through our own Bland account.

Step A of the SingIt Call plan, which ends in a separate x402 service selling AI phone calls for USDC
on Base or Solana. This step is a closed pilot inside SingIt: only numbers on an allowlist the owner
sets are called, a few a day, and the user is not charged: the minutes come out of our Bland credit
while the real price per minute is measured. Charging and the separate service come after that.

Bland places the call from its own number pool once the account holds purchased credit (international
calls need at least $5 bought). With our own Twilio connected (Bland "BYOT": an `encrypted_key` made in
Bland's dashboard from the Twilio SID and token, numbers imported there), calls go out from our Twilio
number instead, and the destination country must be enabled in Twilio's Geo Permissions.
docs.bland.ai/tutorials/custom-twilio, docs.bland.ai/api-v1/post/calls.

The same rules as every call here: the number is one the user typed and is on the list, the AI says it
is an AI calling for a customer, nothing is recorded, at most three minutes, voicemail hangs up, and a
call is never retried. The order is written before Bland is asked, so a lost answer is never sent twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .agent_allowance import AllowanceError

ENABLED_ENV = "SIGN402_CALLS_BLAND_ENABLED"
KEY_ENV = "SIGN402_BLAND_API_KEY"
ALLOWED_ENV = "SIGN402_CALLS_ALLOWED_NUMBERS"     # the pilot's numbers, E.164, comma separated
TWILIO_KEY_ENV = "SIGN402_BLAND_ENCRYPTED_KEY"     # optional: our Twilio through Bland (BYOT)
FROM_ENV = "SIGN402_BLAND_FROM"                    # optional: the imported Twilio number to call from
DB_ENV = "SIGN402_CALLS_DB"
API = "https://api.bland.ai/v1/calls"
PER_DAY = 3
MAX_MINUTES = 3
ACTIVE_SECONDS = 600  # one call at a time per account; an unfinished one older than this no longer blocks
PHONE = re.compile(r"\+[1-9]\d{7,14}")
# Bland's language codes (docs.bland.ai/api-v1/post/calls): the ones the pilot speaks.
LANGUAGES = {"english": "en", "czech": "cs", "german": "de", "spanish": "babel-es", "french": "babel-fr"}
OPENING = {
    "en": "Hello, this is an AI assistant calling on behalf of a customer.",
    "cs": "Dobrý den, tady je asistent s umělou inteligencí, volám jménem zákazníka.",
    "de": "Guten Tag, hier ist ein KI-Assistent, ich rufe im Auftrag eines Kunden an.",
}
PILOT_PREFIX = "bland:"


def _numbers(raw: str) -> set[str]:
    return {n for n in (re.sub(r"[\s().-]", "", part) for part in str(raw or "").split(",")) if PHONE.fullmatch(n)}


def pilot_accepts(phone: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether the pilot would call this number: on, and the number on its list. Needs no key."""
    values = os.environ if env is None else env
    if str(values.get(ENABLED_ENV, "")).strip() != "1":
        return False
    return re.sub(r"[\s().-]", "", str(phone or "")) in _numbers(values.get(ALLOWED_ENV, ""))


class CallStore:
    """The pilot's calls: who asked, the provider's id and where it stands. The number is kept as a hash
    and its last digits; the task is not kept."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                order_id TEXT PRIMARY KEY, account TEXT NOT NULL, phone_hash TEXT NOT NULL, phone_last TEXT NOT NULL,
                provider_call_id TEXT, state TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

    def open(self, account: str, phone: str, now: int, per_day: int) -> str:
        """A new order, written before the provider is asked, or a refusal: today's calls used, one still going."""
        with self._db() as db:
            today = db.execute("SELECT COUNT(*) FROM calls WHERE account = ? AND created_at >= ?",
                               (account, now // 86_400 * 86_400)).fetchone()[0]
            if today >= per_day:
                raise AllowanceError(f"That is today's {per_day} calls. More tomorrow.")
            busy = db.execute("SELECT 1 FROM calls WHERE account = ? "
                              "AND state IN ('dispatching', 'accepted', 'dispatch_unknown') "  # a lost answer may be ringing
                              "AND created_at >= ?", (account, now - ACTIVE_SECONDS)).fetchone()
            if busy:
                raise AllowanceError("A call is still going. Check its result first.")
            order_id = "call_" + secrets.token_urlsafe(10)
            db.execute("INSERT INTO calls VALUES (?, ?, ?, ?, NULL, 'dispatching', ?, ?)",
                       (order_id, account, hashlib.sha256(phone.encode()).hexdigest(), phone[-4:], now, now))
        return order_id

    def update(self, order_id: str, state: str, now: int, provider_call_id: str | None = None) -> None:
        with self._db() as db:
            db.execute("UPDATE calls SET state = ?, provider_call_id = COALESCE(?, provider_call_id), updated_at = ? "
                       "WHERE order_id = ?", (state, provider_call_id, now, order_id))

    def get(self, account: str, order_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM calls WHERE account = ? AND order_id = ?", (account, order_id)).fetchone()
        return dict(row) if row else None


def _http(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read(2_000_000) or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read(200_000) or b"{}")
        except ValueError:
            return error.code, {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ConnectionError("Bland did not answer") from None


class BlandCalls:
    def __init__(self, *, api_key: str, allowed: set[str], store: CallStore, twilio_key: str = "",
                 from_number: str = "", http: Callable[..., tuple[int, Any]] = _http, now: Callable[[], float] = time.time):
        self.api_key, self.allowed, self.store = api_key, allowed, store
        self.twilio_key, self.from_number, self.http, self.now = twilio_key, from_number, http, now

    def _headers(self) -> dict[str, str]:
        headers = {"authorization": self.api_key}
        if self.twilio_key:
            headers["encrypted_key"] = self.twilio_key  # our Twilio, through Bland
        return headers

    def accepts(self, phone: str) -> bool:
        return phone in self.allowed

    def start(self, account: str, phone: str, task: str, language: str, instructions: str) -> dict[str, Any]:
        """One call to a listed number: written down first, asked once, never retried."""
        if not self.accepts(phone):
            raise AllowanceError("This number is not in the calling pilot. Nothing was called.")
        code = LANGUAGES.get(language.strip().lower(), "en")
        order_id = self.store.open(account, phone, int(self.now()), PER_DAY)
        body = {"phone_number": phone, "task": instructions, "first_sentence": OPENING.get(code, OPENING["en"]),
                "language": code, "max_duration": MAX_MINUTES, "record": False, "wait_for_greeting": True,
                "voicemail": {"action": "hangup"}, "metadata": {"order_id": order_id}}
        if self.twilio_key and self.from_number:
            body["from"] = self.from_number
        try:
            status, reply = self.http("POST", API, self._headers(), body)
        except ConnectionError:
            self.store.update(order_id, "dispatch_unknown", int(self.now()))
            raise AllowanceError("The call request got no answer, so it may or may not ring. It was not repeated.") from None
        call_id = str((reply or {}).get("call_id") or "") if isinstance(reply, dict) else ""
        if status != 200 or (reply or {}).get("status") != "success" or not re.fullmatch(r"[\w-]{6,80}", call_id):
            self.store.update(order_id, "refused", int(self.now()))
            message = str((reply or {}).get("message") or "") if isinstance(reply, dict) else ""
            raise AllowanceError(f"Bland did not place the call{': ' + message[:160] if message else ''}. Nothing rang.")
        self.store.update(order_id, "accepted", int(self.now()), call_id)
        return {"ok": True, "callId": PILOT_PREFIX + order_id, "phone": phone, "costUsd": "0", "pilot": True}

    def status(self, account: str, call_id: str) -> dict[str, Any]:
        """How the call went: only the account that asked for it may read it."""
        order = self.store.get(account, call_id[len(PILOT_PREFIX):]) if call_id.startswith(PILOT_PREFIX) else None
        if order is None or not order["provider_call_id"]:
            raise AllowanceError("Unknown call.")
        status, body = self.http("GET", f"{API}/{order['provider_call_id']}", self._headers())
        if status != 200 or not isinstance(body, dict):
            raise AllowanceError("Bland would not show this call right now. Try again in a moment.")
        completed = bool(body.get("completed"))
        if completed and order["state"] == "accepted":
            self.store.update(order["order_id"], "completed", int(self.now()))
        lines = []
        for turn in body.get("transcripts") or []:
            if isinstance(turn, dict) and turn.get("text") and turn.get("user") in ("assistant", "user"):
                lines.append(f"{'Assistant' if turn['user'] == 'assistant' else 'Them'}: {str(turn['text']).strip()}")
        return {"ok": True, "completed": completed, "status": str(body.get("status") or body.get("queue_status") or ""),
                "answeredBy": str(body.get("answered_by") or ""), "seconds": body.get("call_length"),
                "summary": str(body.get("summary") or "")[:2000],
                "transcript": ("\n".join(lines) or str(body.get("concatenated_transcript") or ""))[:6000],
                "error": str(body.get("error_message") or "")[:300]}


_built: dict[str, Any] = {}


def from_env(env: Mapping[str, str] | None = None) -> BlandCalls | None:
    """The pilot, when it is switched on with a key and at least one number; None otherwise."""
    values = os.environ if env is None else env
    key = str(values.get(KEY_ENV, "")).strip()
    allowed = _numbers(values.get(ALLOWED_ENV, ""))
    if str(values.get(ENABLED_ENV, "")).strip() != "1" or not key or not allowed:
        return None
    if env is None and "calls" in _built:
        return _built["calls"]
    store = CallStore(Path(values.get(DB_ENV) or Path.home() / ".sign402" / "calls.db"))
    calls = BlandCalls(api_key=key, allowed=allowed, store=store,
                       twilio_key=str(values.get(TWILIO_KEY_ENV, "")).strip(),
                       from_number=re.sub(r"[\s().-]", "", str(values.get(FROM_ENV, ""))))
    if env is None:
        _built["calls"] = calls
    return calls
