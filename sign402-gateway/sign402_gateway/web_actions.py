"""Actions the web agent takes for the user, one button press each: an email to themselves, a phone call.

Unlike data, these reach other people or inboxes, so they never happen inside the limits on their own:
the agent drafts, the page shows the exact recipient, text and price, and only the user's press on that
card sends it. The seller is bound to its address per network with a price ceiling, like data
(web_data.py), and paid from the account's own limits.

  * Email (StableEmail, $0.02, Base or Solana): only to the address the account saved, from
    relay@stableemail.dev with replies going to the user. Plain text made from the chat, never a code.
  * Call (StablePhone, $0.54, Base): an AI voice calls the number the user wrote, says it is an AI
    assistant calling for a customer, is not recorded, lasts at most three minutes, and never agrees to
    pay or shares personal data. Its result and transcript are read back with the agent's
    Sign-In-With-X, as only the paying wallet may. Not on Solana yet: which wallet StablePhone counts as
    the payer of a delegated payment is unverified, and without it the result could not be read.

A few a day per account; the counters live in memory and start again on a restart, while the limits
themselves are durable.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import bland_calls
from .agent_allowance import AllowanceError, AllowanceUnavailable
from .web_data import BASE, SOLANA, DataTool, network_of, pay_once

EMAIL = DataTool("email", "Email to you", "StableEmail",
                 {BASE: "0xdb5aa553feeb2c3e3d03e8360b36fb0f7e480671", SOLANA: "HvBMG7ezcwDssxXP7DPJJsveyDnvUm2wNySBR5WF2XEY"},
                 25_000, lambda p: ("POST", "https://stableemail.dev/api/send", None))
CALL = DataTool("call", "Phone call", "StablePhone", {BASE: "0xD219dB8179Bb9C1899eF87f39eebA9D1070c6801"},
                600_000, lambda p: ("POST", "https://stablephone.dev/api/call", None), networks=(BASE,))
CALL_STATUS_URL = "https://stablephone.dev/api/call/"
PER_DAY = {"email": 10, "call": 3}
PHONE = re.compile(r"\+[1-9]\d{7,14}")
# StablePhone's /api/call schema (2026-09-29), not general E.164 support.
CALL_PHONE = re.compile(r"\+1[0-9]{10}")
CALL_REGION_MESSAGE = ("StablePhone currently accepts only +1 numbers with 10 following digits. "
                       "Calls to +420 and other country codes are not available here. Nothing was paid.")
EMAIL_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
FOOTER = ("\n\n—\nSent by your SingIt agent because you asked for it in your chat on app.singitai.app. "
          "Replies go to you.")

_counts: dict[tuple[str, str, int], int] = {}
_lock = threading.Lock()


def _count(account: str, kind: str) -> None:
    """One more of this kind today, or a refusal when today's are used."""
    key = (account, kind, int(time.time()) // 86_400)
    with _lock:
        if _counts.get(key, 0) >= PER_DAY[kind]:
            raise AllowanceError(f"That is today's {PER_DAY[kind]} {'emails' if kind == 'email' else 'calls'}. "
                                 "More tomorrow. Nothing was paid.")
        _counts[key] = _counts.get(key, 0) + 1


def _uncount(account: str, kind: str) -> None:
    key = (account, kind, int(time.time()) // 86_400)
    with _lock:
        _counts[key] = max(0, _counts.get(key, 0) - 1)


def saved_email(server: Any, account: str) -> str:
    store = getattr(server, "buyer_email_store", None)
    return (store.get_email(account) if store is not None else None) or ""


def save_email(server: Any, account: str, address: Any) -> str:
    store = getattr(server, "buyer_email_store", None)
    if store is None:
        raise AllowanceUnavailable("Emails cannot be saved on this server.")
    try:
        return store.set_email(account, str(address or ""))
    except ValueError as exc:
        raise AllowanceError(f"That email does not look right: {exc}") from None


def _record(server: Any, account: str, name: str, amount: int, tx: str) -> None:
    """A Solana action in Purchases, as a Base one is recorded by the limiter's payment."""
    events = getattr(server, "user_event_store", None)
    if events is not None and network_of(account) == SOLANA:
        events.write(account, {"ok": True, "toolId": f"action.{name}", "toolName": name, "network": "solana",
                               "txId": tx, "amountAtomic": str(amount),
                               "receipt": {"name": name, "paid": f"{amount / 1_000_000:g} USDC", "network": "Solana",
                                           "txId": tx, "status": "Completed"}})


def send_email(server: Any, gw: Any, account: str, subject: Any, text: Any) -> dict[str, Any]:
    """The email the user pressed Send on, to the address they saved and nowhere else."""
    to = saved_email(server, account)
    if not to:
        raise AllowanceError("Save your email first; I only send to your own address.")
    subject = " ".join(str(subject or "").split())[:150] or "From your SingIt chat"
    body = str(text or "").strip()[:8000]
    if not body:
        raise AllowanceError("There is nothing to send.")
    _count(account, "email")
    try:
        amount, reply, tx = pay_once(server, gw, account, EMAIL, "POST", "https://stableemail.dev/api/send",
                                     {"to": [to], "subject": subject, "text": body + FOOTER, "replyTo": to}, record=True)
    except Exception:
        _uncount(account, "email")
        raise
    if not (isinstance(reply, dict) and reply.get("success")):
        raise AllowanceError("StableEmail took the payment but did not confirm sending. It was not repeated.")
    _record(server, account, "Email to you", amount, tx)
    return {"ok": True, "to": to, "costUsd": f"{amount / 1_000_000:.3f}"}


def call_task(task: str, language: str) -> str:
    """What the calling AI is told, around what the user asked: who it is, and what it must never do."""
    return (f"You are an AI assistant making a phone call on behalf of a customer. Speak {language or 'English'}. "
            "Start by saying that you are an AI assistant calling for a customer. Then do this:\n"
            f"{task.strip()[:1200]}\n"
            "Be brief and polite. Never agree to pay anything, never give card or bank details, and share no "
            "personal data beyond a name given above. If they cannot help, thank them and end the call. "
            "Do not pretend to be a human.")


def start_call(server: Any, gw: Any, account: str, phone: Any, task: Any, language: Any = "") -> dict[str, Any]:
    """The call the user pressed Call on: that number, that task, disclosed as an AI, not recorded."""
    phone = re.sub(r"[\s().-]", "", str(phone or ""))
    pilot = None if CALL_PHONE.fullmatch(phone) else bland_calls.from_env()
    if pilot is not None and pilot.accepts(phone):  # Europe, through our Bland: the pilot's numbers, no charge
        task = str(task or "").strip()
        if not task:
            raise AllowanceError("What should the call be about?")
        language = re.sub(r"[^A-Za-z -]", "", str(language or ""))[:30]
        return pilot.start(account, phone, task, language, call_task(task, language))
    if network_of(account) != BASE:
        raise AllowanceError("Phone calls work from a Base wallet for now.")
    if not PHONE.fullmatch(phone):
        raise AllowanceError("That is not a full phone number with its country code, like +1 202 555 0123.")
    if not CALL_PHONE.fullmatch(phone):
        raise AllowanceError(CALL_REGION_MESSAGE)
    task = str(task or "").strip()
    if not task:
        raise AllowanceError("What should the call be about?")
    language = re.sub(r"[^A-Za-z -]", "", str(language or ""))[:30]
    body = {"phone_number": phone, "task": call_task(task, language), "max_duration": 3, "record": False,
            "wait_for_greeting": True, "voicemail_action": "hangup"}
    _count(account, "call")
    try:
        amount, reply, _ = pay_once(server, gw, account, CALL, "POST", "https://stablephone.dev/api/call", body, record=True)
    except Exception:
        _uncount(account, "call")
        raise
    call_id = str((reply or {}).get("call_id") or "") if isinstance(reply, dict) else ""
    if not re.fullmatch(r"[\w-]{6,80}", call_id):
        raise AllowanceError("StablePhone took the payment but did not start the call. It was not repeated.")
    return {"ok": True, "callId": call_id, "phone": phone, "costUsd": f"{amount / 1_000_000:.2f}"}


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read(2_000_000) or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read(200_000) or b"{}")
        except ValueError:
            return error.code, {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise AllowanceError("StablePhone is not answering right now. Try again in a moment.") from None


def call_status(server: Any, account: str, call_id: Any, http: Any = None) -> dict[str, Any]:
    """How the call went, signed in as the agent that paid for it (only it may read it)."""
    from .allowance_bitrefill import siwx_header  # noqa: PLC0415
    call_id = str(call_id or "")
    if call_id.startswith(bland_calls.PILOT_PREFIX):
        pilot = bland_calls.from_env()
        if pilot is None:
            raise AllowanceError("The calling pilot is off on this server.")
        return pilot.status(account, call_id)
    if not re.fullmatch(r"[\w-]{6,80}", call_id) or network_of(account) != BASE:
        raise AllowanceError("Unknown call.")
    http = http or _get
    url = CALL_STATUS_URL + call_id
    status, challenge = http(url)
    extension = (challenge.get("extensions") or {}).get("sign-in-with-x") if isinstance(challenge, dict) else None
    if status != 402 or not extension:
        raise AllowanceError("StablePhone did not offer a sign-in to read the call.")
    _, key = server.allowance.agent_key(account)
    status, body = http(url, {"SIGN-IN-WITH-X": siwx_header(extension, key)})
    if status != 200 or not isinstance(body, dict):
        raise AllowanceError("StablePhone would not show this call to your agent.")
    lines = []
    for turn in body.get("transcripts") or []:
        if isinstance(turn, dict) and turn.get("text"):
            who = "Assistant" if str(turn.get("user") or "").lower() in ("assistant", "agent") else "Them"
            lines.append(f"{who}: {str(turn['text']).strip()}")
    transcript = "\n".join(lines) or str(body.get("transcript") or "")
    return {"ok": True, "completed": bool(body.get("completed")), "status": str(body.get("status") or ""),
            "answeredBy": str(body.get("answered_by") or ""), "seconds": body.get("call_length"),
            "summary": str(body.get("summary") or "")[:2000], "transcript": transcript[:6000],
            "error": str(body.get("error_message") or "")[:300]}
