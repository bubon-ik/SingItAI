"""The web page's conversation, on Venice, paid from the account's own limiter.

The bot's private chat (venice_chat.py) meters messages against a Venice balance
held by a wallet, topped up $5 at a time over x402. A web account has no managed
wallet; it has its limiter's agent key. So here the agent key signs Venice in,
and a top-up is one more x402 purchase from the limiter: the same caps, spending
memory, rate limit and purchase history as crypto news or a gift card.

The owner's consent is the limits they set for their agent: they chose to let it
buy inside them without asking (docs/allowance-web-v1.md, "Chat"). A top-up that
does not fit those limits is refused with the reason, and nothing is paid.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from .agent_allowance import AllowanceError, AllowanceUnavailable
from .venice_chat import (
    VENICE_BASE_URL,
    ChatError,
    ChatState,
    VeniceChatClient,
    _urllib_transport,
    build_chat_policy,
)

logger = logging.getLogger(__name__)

TOPUP_URL = f"{VENICE_BASE_URL}/x402/top-up"
MAX_MESSAGES = 40
MAX_CHARS = 40_000
ROLES = {"system", "user", "assistant"}

# How a top-up appears in the purchase history and the spend ledger.
VENICE_CREDIT = {
    "id": "venice.credit", "name": "Venice AI credit", "kind": "x402_credit", "source": "Venice AI",
    "description": "Prepaid credit for your private AI chat. Each message is metered against it.",
    "resourceUrl": TOPUP_URL, "mcpStyleName": "venice_credit", "inputSchema": {"type": "object", "properties": {}},
    "command": "/chat",
}

# Transport is a seam for tests; nothing else changes it.
transport = _urllib_transport


def _usd(atomic: int) -> str:
    return f"{atomic / 1_000_000:.2f}"


def _messages(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("Send the conversation as a list of messages.")
    out = [{"role": str(m.get("role")), "content": str(m.get("content") or "")}
           for m in raw[-MAX_MESSAGES:] if isinstance(m, dict) and m.get("role") in ROLES and m.get("content")]
    if not out or out[-1]["role"] != "user":
        raise ValueError("The conversation must end with the user's message.")
    while sum(len(m["content"]) for m in out) > MAX_CHARS and len(out) > 2:
        out.pop(1 if out[0]["role"] == "system" else 0)  # drop the oldest turn, keep the instructions
    return out


def chat(server: Any, gw: Any, account: str, raw_messages: Any) -> tuple[int, dict[str, Any]]:
    base = getattr(server, "chat_service", None)
    if base is None:
        return 503, {"ok": False, "error": "chat_off", "text": "The private chat is off on this server."}
    messages = _messages(raw_messages)
    service = server.allowance
    row = service.lane_for(account)  # raises with the reason when paused, expired or not approved
    if row is None:
        raise AllowanceUnavailable("Set your limits and approve them first; then your chat is private, on Venice.")
    config = base.client.config
    store = base.store

    # The limiter is the standing approval; the chat's own policy mirrors it.
    policy = build_chat_policy(pay_to=config.bound_pay_to, network=config.network, asset=config.asset,
                               daily_cap_atomic=int(row["daily_cap"]), expires_at=int(row["expiry"]),
                               merchant_name="Venice AI")
    session = store.get_session(account)
    if session.paused and session.pause_reason in (ChatState.RECONCILIATION_REQUIRED, ChatState.MERCHANT_CHANGED):
        return 409, {"ok": False, "error": "chat_paused", "text": (
            "Your private chat is paused: a Venice top-up could not be confirmed or Venice changed where it is paid. "
            "Nothing more will be charged. Write to support to resume it.")}
    if session.policy_hash != policy.policy_hash:
        store.approve_policy(account, policy)

    agent, agent_key = service.agent_key(account)
    refusal: dict[str, str] = {}
    paid: dict[str, int] = {}

    def sign(address: str, message: str) -> str:
        if address.lower() != agent.lower():
            raise AllowanceError("That address is not this account's agent.")
        return Account.sign_message(encode_defunct(text=message), private_key=agent_key).signature.to_0x_hex()

    def settle(requirement: dict[str, Any], *, user_id: str) -> dict[str, Any]:
        resource = requirement.get("resource")
        resource_url = resource if isinstance(resource, str) and resource.startswith("https://") else TOPUP_URL
        requirements = gw.normalize_x402_payment_required({"accepts": [requirement]}, resource_url=resource_url)
        gw._validate_base_usdc_x402_requirement(requirements)
        amount = int(requirements["amountAtomic"])
        if amount > int(row["per_purchase_cap"]):
            refusal["text"] = (f"Venice sells chat credit in {_usd(amount)} USDC top-ups, above your "
                               f"{_usd(int(row['per_purchase_cap']))} USDC per-purchase limit. Set a per-purchase "
                               f"limit of at least {_usd(amount)} to chat. Nothing was paid.")
            raise AllowanceError(refusal["text"])
        gw._enforce_user_purchase_rate(account)
        server.user_event_store.preflight_write()
        try:
            event = pay_from_allowance(
                server, gw, account, dict(VENICE_CREDIT), resource_url, requirements, request_body={},
                payment_context={"title": "VENICE AI CREDIT", "subject": "private chat"},
                approval={"ok": True, "status": "approved", "source": "web_allowance",
                          "approvalId": "web-chat-" + secrets.token_hex(6)},
                claim_scope="venice-" + secrets.token_hex(6))
        except Exception as exc:
            decision = getattr(exc, "decision", None)  # spending memory's refusal carries its reason
            refusal["text"] = str(getattr(decision, "reason", None) or getattr(exc, "message", None) or exc)
            raise
        if event.get("ok"):
            paid["amount"] = amount
        return event

    from .web_internal import _limits_from_limiter, pay_from_allowance  # noqa: PLC0415 - avoids an import cycle

    _limits_from_limiter(server, gw, account)
    client = VeniceChatClient(store=store, transport=transport, signer=sign, settle=settle, config=config,
                              purchases_paused=getattr(base.client, "purchases_paused", None))
    try:
        result = client.send(account, messages, wallet_address=agent)
    except ChatError as exc:
        text = refusal.get("text") if exc.__class__.__name__ == "PrefundFailed" and refusal else str(exc)
        return 400, {"ok": False, "error": "chat_refused", "text": text}
    reply: dict[str, Any] = {"ok": True, "text": result.text, "costAtomic": result.cost_atomic,
                             "creditAtomic": result.outstanding_atomic}
    if result.prefunded and paid:
        reply["topUpUsd"] = _usd(paid["amount"])
    return 200, reply


def models(server: Any, account: str) -> tuple[int, dict[str, Any]]:
    """Every Venice chat model, cheapest first, with the one this account talks to.

    The page filters by category and name itself: one request, however much they browse.
    """
    base = getattr(server, "chat_service", None)
    if base is None:
        return 503, {"ok": False, "error": "chat_off", "text": "The private chat is off on this server."}
    catalogue = base._catalogue()
    chosen = base.store.get_session(account).model or base.default_model
    listed = catalogue.models()
    categories = [c for c in catalogue.categories() if c.key != "all"]
    return 200, {
        "ok": True, "chosen": chosen,
        "chosenLabel": next((m.label for m in listed if m.model_id == chosen), chosen),
        "categories": [{"key": c.key, "label": c.label} for c in categories],
        "models": [{"id": m.model_id, "label": m.label, "blurb": m.blurb,
                    "inputUsdPerMTok": m.input_usd_per_mtok, "outputUsdPerMTok": m.output_usd_per_mtok,
                    "tags": [c.key for c in categories if catalogue._matches(m.model_id, c.key)]}
                   for m in listed[:300]],
    }


def choose_model(server: Any, account: str, model_id: Any) -> tuple[int, dict[str, Any]]:
    """Talk to another model from the next message on. Moves no money."""
    base = getattr(server, "chat_service", None)
    if base is None:
        return 503, {"ok": False, "error": "chat_off", "text": "The private chat is off on this server."}
    return 200, base.set_model(account, str(model_id or ""))  # UnknownModel is a ValueError: refused with its reason
