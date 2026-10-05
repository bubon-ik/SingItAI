"""Trusted Telegram identity capture for the separate remote Trezor plugin."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


_COMMANDS = frozenset(
    {"trezor-status", "trezor-test", "trezor-prepare", "trezor-confirm", "trezor-cancel"}
)


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: str


_current_identity: ContextVar[TelegramIdentity | None] = ContextVar(
    "sign402_trezor_remote_identity",
    default=None,
)


def _command(event: Any) -> str:
    getter = getattr(event, "get_command", None)
    value = getter() if callable(getter) else None
    if not value:
        text = str(getattr(event, "text", "") or "").strip()
        if text.startswith("/"):
            value = text[1:].split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    return str(value or "").strip().lower().replace("_", "-")


def capture_identity(*, event: Any, **_kwargs: Any) -> None:
    _current_identity.set(None)
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform_name = str(getattr(platform, "value", platform) or "").strip().lower()
    if platform_name != "telegram" or _command(event) not in _COMMANDS:
        return
    user_id = str(getattr(source, "user_id", "") or "").strip()
    if user_id.isascii() and user_id.isdecimal() and len(user_id) <= 32:
        _current_identity.set(TelegramIdentity(user_id=user_id))


def consume_identity() -> TelegramIdentity | None:
    identity = _current_identity.get()
    _current_identity.set(None)
    return identity
