"""Hermes plugin for trusted Sign402 Telegram wallet and iMessage approval commands."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .client import GatewayClient, GatewayClientError
from .identity import capture_gateway_identity, consume_gateway_identity


logger = logging.getLogger(__name__)

_TELEGRAM_ONLY_MESSAGE = (
    "Wallet commands are available only from an authenticated Telegram message."
)
_UNEXPECTED_ERROR_MESSAGE = (
    "Wallet service is temporarily unavailable. Please try again."
)
_IMESSAGE_UNEXPECTED_ERROR_MESSAGE = (
    "iMessage approval service is temporarily unavailable. Please try again."
)
_SKIP_RESULT = {"action": "skip", "reason": "sign402-imessage-handled"}
_PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_COMMANDS = {
    "wallet": ("wallet", "Show your managed Base wallet"),
    "create-wallet": ("create-wallet", "Create your managed Base wallet"),
    "balance": ("balance", "Show your managed Base wallet balance"),
}
_IMESSAGE_COMMANDS = {
    "connect_imessage": (
        "connect-imessage",
        "Link your iMessage number for Sign402 approvals",
    ),
    "test_approval": (
        "test-imessage-approval",
        "Send a no-funds Sign402 approval test to iMessage",
    ),
}

_client_factory: Callable[[], GatewayClient] = GatewayClient.from_env


def _build_handler(operation: str):
    async def handler(_raw_args: str) -> str:
        identity = consume_gateway_identity()
        if identity is None:
            return _TELEGRAM_ONLY_MESSAGE
        try:
            client = _client_factory()
            return await asyncio.to_thread(client.execute, operation, identity)
        except GatewayClientError as exc:
            return exc.user_message
        except Exception as exc:
            logger.warning(
                "Unexpected Sign402 wallet plugin failure operation=%s error=%s",
                operation,
                type(exc).__name__,
            )
            return _UNEXPECTED_ERROR_MESSAGE

    return handler


def _build_imessage_handler(operation: str):
    async def handler(_raw_args: str) -> str:
        identity = consume_gateway_identity()
        if identity is None:
            return _TELEGRAM_ONLY_MESSAGE
        try:
            client = _client_factory()
            result = await asyncio.to_thread(
                client.execute_imessage,
                operation,
                {"telegramUserId": identity.user_id},
            )
            telegram_text = result.get("telegramText")
            if isinstance(telegram_text, str) and telegram_text.strip():
                return telegram_text.strip()
            return _IMESSAGE_UNEXPECTED_ERROR_MESSAGE
        except GatewayClientError as exc:
            return exc.user_message
        except Exception as exc:
            logger.warning(
                "Unexpected Sign402 iMessage plugin failure operation=%s error=%s",
                operation,
                type(exc).__name__,
            )
            return _IMESSAGE_UNEXPECTED_ERROR_MESSAGE

    return handler


def handle_pre_gateway_dispatch(*, event, gateway=None, **kwargs):
    """Capture trusted identities and consume Photon approval messages."""

    capture_gateway_identity(event=event, **kwargs)
    source = getattr(event, "source", None)
    if _platform_name(source) != "photon":
        return None

    text = str(getattr(event, "text", "") or "").strip()
    photon_user_id = str(getattr(source, "user_id", "") or "").strip()
    if not photon_user_id:
        return None

    if _looks_like_pairing_code(text):
        return _handle_photon_pairing_code(
            code=text.upper(),
            photon_user_id=photon_user_id,
            source=source,
            gateway=gateway,
        )

    decision = text.upper()
    if decision in {"YES", "NO"}:
        return _handle_photon_decision(
            decision=decision,
            photon_user_id=photon_user_id,
            source=source,
            gateway=gateway,
        )

    return None


def _handle_photon_pairing_code(*, code: str, photon_user_id: str, source, gateway):
    try:
        result = _client_factory().execute_imessage(
            "link",
            {"code": code, "photonUserId": photon_user_id},
        )
        text = _imessage_text(result)
        if result.get("ok"):
            _approve_photon_source(gateway, source)
        _send_fixed_reply(gateway, source, text)
        return dict(_SKIP_RESULT)
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
        return dict(_SKIP_RESULT)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 Photon pairing failure error=%s",
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _IMESSAGE_UNEXPECTED_ERROR_MESSAGE)
        return dict(_SKIP_RESULT)


def _handle_photon_decision(*, decision: str, photon_user_id: str, source, gateway):
    try:
        client = _client_factory()
        pending = client.execute_imessage(
            "pending",
            {"photonUserId": photon_user_id},
        )
        if not pending.get("pending"):
            return None
        result = client.execute_imessage(
            "decision",
            {"photonUserId": photon_user_id, "decision": decision},
        )
        _send_fixed_reply(gateway, source, _imessage_text(result))
        return dict(_SKIP_RESULT)
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
        return dict(_SKIP_RESULT)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 Photon decision failure error=%s",
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _IMESSAGE_UNEXPECTED_ERROR_MESSAGE)
        return dict(_SKIP_RESULT)


def _imessage_text(result: dict) -> str:
    text = result.get("imessageText")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return _IMESSAGE_UNEXPECTED_ERROR_MESSAGE


def _send_fixed_reply(gateway, source, text: str) -> None:
    if gateway is None:
        return
    adapters = getattr(gateway, "adapters", {}) or {}
    adapter = adapters.get(_platform_name(source))
    send = getattr(adapter, "send", None)
    if not callable(send):
        return
    chat_id = str(getattr(source, "chat_id", "") or getattr(source, "user_id", "") or "")
    if not chat_id:
        return
    coroutine = send(chat_id, text)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coroutine)
    else:
        loop.create_task(coroutine)


def _approve_photon_source(gateway, source) -> None:
    pairing_store = getattr(gateway, "pairing_store", None)
    if pairing_store is None:
        return
    generate_code = getattr(pairing_store, "generate_code", None)
    approve_code = getattr(pairing_store, "approve_code", None)
    if not callable(generate_code) or not callable(approve_code):
        return
    user_id = str(getattr(source, "user_id", "") or "").strip()
    user_name = str(getattr(source, "user_name", "") or "").strip()
    code = generate_code("photon", user_id, user_name)
    if code:
        approve_code("photon", code)


def _platform_name(source) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _looks_like_pairing_code(value: str) -> bool:
    code = str(value or "").strip().upper()
    return (
        len(code) == 8
        and all(character in _PAIRING_CODE_ALPHABET for character in code)
    )


def register(ctx) -> None:
    """Register trusted Telegram identity capture and Sign402 commands."""

    ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
    for command, (operation, description) in _COMMANDS.items():
        ctx.register_command(
            command,
            handler=_build_handler(operation),
            description=description,
        )
    for command, (operation, description) in _IMESSAGE_COMMANDS.items():
        ctx.register_command(
            command,
            handler=_build_imessage_handler(operation),
            description=description,
        )
