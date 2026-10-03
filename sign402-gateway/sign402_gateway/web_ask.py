"""SingIt Ask: one x402 payment per answer, signed by the account's own network agent.

Opt-in through the existing model picker. No prepaid chat credit, manual wallet
signature or new wallet is involved. Solana pays the measured cost directly from the owner account with its delegate.
"""
import hashlib
from pathlib import Path
from typing import Any

from .agent_allowance import AllowanceError, AllowanceUnavailable
from . import ask_metered, ask_solana

MODEL = "singit-ask"
LABEL = "DeepSeek V4.1 Flash · SingIt Ask"
URL = ask_metered.URL
PRICE = ask_metered.CAP


def selected(server: Any, account: str) -> bool:
    base = getattr(server, "chat_service", None)
    return base is not None and base.store.get_session("ask-web:" + account).model == MODEL


def choose(server: Any, account: str) -> tuple[int, dict[str, Any]]:
    if not account.startswith(("wallet:", "solana:")):
        raise AllowanceUnavailable("A supported Base or Solana account is required.")
    base = getattr(server, "chat_service", None)
    if base is None:
        raise AllowanceUnavailable("Chat is not configured on this server.")
    # Keep the linked Telegram/Venice model intact: this adapter belongs to the web chat.
    base.store.set_model("ask-web:" + account, MODEL)
    return 200, {"ok": True, "chosen": MODEL, "label": LABEL, "provider": "SingIt Ask"}


def listing(result: dict[str, Any], account: str) -> dict[str, Any]:
    if account.startswith(("wallet:", "solana:")):
        fee = "0.001"
        result["models"].insert(0, {
            "id": MODEL, "label": LABEL, "provider": "SingIt Ask",
            "blurb": f"Actual token cost + 30% + {fee} USDC settlement fee. Up to 0.003 per answer; Solana charges your wallet directly, with no agent SOL top-up.",
            "billingMode": "actual_usage", "markupPercent": 30, "settlementFeeUsd": fee, "maxChargeUsd": "0.003", "tags": ["x402"],
        })
        result["categories"].insert(0, {"key": "x402", "label": "Pay per answer"})
    result["provider"] = "SingIt Ask" if result["chosen"] == MODEL else "Venice"
    if result["chosen"] == MODEL:
        result["chosenLabel"] = LABEL
    return result


def require_payment_ready(account: str) -> None:
    """Do not buy more search data while the selected answer model is blocked."""
    root = Path.home() / ".sign402" / "ask-metered"
    name = hashlib.sha256(account.encode()).hexdigest() + ("-solana" if account.startswith("solana:") else "")
    if any((root / (name + suffix)).exists() for suffix in (".json", ".submitted")):
        raise AllowanceError("A previous Ask payment needs settlement review. No new search or chat payment was sent.")


def usage(account: str = "") -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "model": MODEL, "modelLabel": LABEL, "billingMode": "actual_usage",
                 "maxChargeAtomic": PRICE, "markupPercent": 30, "settlementFeeAtomic": 1000, "creditAtomic": None, "topUps": []}


def _payment_context(messages: list[dict[str, str]], account: str) -> list[dict[str, str]]:
    """Authoritative provider/network context, kept even when old turns are trimmed."""
    common = (
        "Current selected chat service: DeepSeek V4.1 Flash via SingIt Ask. "
        "Each answer uses x402 automatically within the approved limits: actual model cost + 30% + "
        "0.001 USDC, with a 0.003 USDC maximum. This is not Venice credit or a prepaid chat balance. "
        "Do not infer payment failure or request an agent top-up from old assistant messages or an "
        "internal float balance. Only actual operation results can establish a payment problem. "
        "A greeting needs a brief greeting, not an unsolicited wallet status or funding checklist. "
    )
    if account.startswith("solana:"):
        payment = (
            "The connected network is Solana. For SingIt Ask the existing SPL delegate pays USDC "
            "directly from the user's wallet to the merchant; Coinbase CDP pays transaction fees. "
            "Zero USDC or SOL on the agent is normal and does not block Ask. No agent SOL top-up, "
            "agent token account or Agent funds step is needed for this chat. SOL in the user's own "
            "wallet is needed only for wallet actions they sign, such as approving/revoking the grant. "
            "Do not extend Ask's payment rules to every other purchase; those use their own quotes/checks."
        )
    else:
        payment = (
            "The connected network is Base. The existing limiter funds the agent automatically "
            "when necessary; CDP settles the actual chat invoice. The legacy funding leg can use "
            "operator-funded ETH, so do not claim every Base network cost is recovered from the user. "
            "Do not tell a Base user to add SOL."
        )
    out = [dict(m) for m in messages]
    instruction = "\n\nCurrent payment configuration (supersedes older funding advice): " + common + payment
    if out and out[0]["role"] == "system":
        out[0]["content"] += instruction
    else:
        out.insert(0, {"role": "system", "content": instruction.strip()})
    while len(out) > 40:
        out.pop(1)
    return out


def chat(server: Any, gw: Any, account: str, raw_messages: Any, context: Any = None) -> tuple[int, dict[str, Any]]:
    from .web_internal import _limits_from_limiter
    from .web_venice import _messages, _with_data

    if not account.startswith(("wallet:", "solana:")):
        raise AllowanceUnavailable("Unsupported account network. No payment was attempted.")
    if getattr(gw, "_purchases_paused", lambda: False)():
        raise AllowanceUnavailable("Payments are paused. No payment was attempted.")
    messages = _payment_context(_with_data(_messages(raw_messages), context), account)
    # Match the merchant's 24,000 UTF-16 code-unit limit before any payment.
    def size() -> int:
        return sum(len(m["content"].encode("utf-16-le")) // 2 for m in messages)
    while size() > 24000 and len(messages) > 2:
        messages.pop(1 if messages[0]["role"] == "system" else 0)
    if size() > 24000:
        raise ValueError("This question is too long for SingIt Ask. Shorten it and try again; nothing was paid.")
    if not account.startswith("solana:"):
        _limits_from_limiter(server, gw, account)
    gw._enforce_user_purchase_rate(account)
    server.user_event_store.preflight_write()
    payer = ask_solana if account.startswith("solana:") else ask_metered
    amount, data, tx = payer.pay(server, gw, account,
                                {"model": MODEL, "messages": messages, "max_tokens": 1200})
    # Never retry a paid request on a missing or malformed answer.
    choices = data.get("choices") if isinstance(data, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise AllowanceError("The paid response contained no answer. Do not pay again until its settlement is checked.")
    used = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    def tokens(key: str) -> int:
        value = used.get(key)
        return value if type(value) is int and value >= 0 else 0
    return 200, {"ok": True, "text": text, "model": MODEL, "modelLabel": LABEL,
                 "costAtomic": amount, "billingMode": "actual_usage", "billing": data.get("billing"), "txId": tx,
                 "promptTokens": tokens("prompt_tokens"), "completionTokens": tokens("completion_tokens")}
