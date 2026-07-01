"""Trusted Telegram identity binding for Sign402 wallet commands."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


_TELEGRAM_COMMANDS = frozenset(
    {"wallet", "create-wallet", "balance", "connect-imessage", "test-approval"}
)


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: str
    username: str | None = None
    chat_id: str | None = None


_current_identity: ContextVar[TelegramIdentity | None] = ContextVar(
    "sign402_telegram_identity",
    default=None,
)


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _event_command(event: Any) -> str:
    get_command = getattr(event, "get_command", None)
    command = get_command() if callable(get_command) else None
    if not command:
        text = str(getattr(event, "text", "") or "").strip()
        if text.startswith("/"):
            command = text[1:].split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    return str(command or "").strip().lower().replace("_", "-")


def capture_gateway_identity(*, event: Any, **_kwargs: Any) -> None:
    """Bind a trusted Telegram source for a recognized wallet command."""

    _current_identity.set(None)
    source = getattr(event, "source", None)
    if _platform_name(source) != "telegram":
        return
    if _event_command(event) not in _TELEGRAM_COMMANDS:
        return

    user_id = str(getattr(source, "user_id", "") or "").strip()
    if not user_id.isdecimal():
        return

    raw_username = getattr(source, "user_name", None)
    username = str(raw_username).strip() if raw_username else None
    raw_chat_id = getattr(source, "chat_id", None)
    chat_id = str(raw_chat_id).strip() if raw_chat_id is not None else None
    _current_identity.set(
        TelegramIdentity(
            user_id=user_id,
            username=username or None,
            chat_id=chat_id or None,
        )
    )


def consume_gateway_identity() -> TelegramIdentity | None:
    """Return the current trusted identity once, then clear it."""

    identity = _current_identity.get()
    _current_identity.set(None)
    return identity
