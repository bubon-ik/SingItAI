"""Hermes plugin for trusted Sign402 Telegram wallet and iMessage approval commands."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .client import GatewayClient, GatewayClientError
from .identity import (
    TelegramIdentity,
    capture_gateway_identity,
    consume_gateway_identity,
)


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
_TELEGRAM_TOKEN_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "HERMES_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_TOKEN",
)
_TELEGRAM_SEND_TIMEOUT_SECONDS = 15
_TELEGRAM_MESSAGE_CHUNK_SIZE = 3900
_TELEGRAM_PAID_TOOL_STARTED_MESSAGE = (
    "Sign402 purchase started. Approve it in iMessage; I'll post the result here."
)
_COMMANDS = {
    "wallet": ("create-wallet", "Show your Base agent wallet"),
    "balance": ("balance", "Show your managed Base wallet balance"),
}
_IMESSAGE_COMMANDS = {
    "connect-imessage": (
        "connect-imessage",
        "Link your iMessage number for Sign402 approvals",
    ),
}

_client_factory: Callable[[], GatewayClient] = GatewayClient.from_env
_telegram_api_opener: Callable[..., object] = urlopen
_background_runner: Callable[[Callable[[], None]], None]


def _default_background_runner(callback: Callable[[], None]) -> None:
    thread = threading.Thread(target=callback, name="sign402-paid-tool", daemon=True)
    thread.start()


_background_runner = _default_background_runner


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


def _build_start_handler():
    async def handler(_raw_args: str) -> str:
        identity = consume_gateway_identity()
        if identity is None:
            return _TELEGRAM_ONLY_MESSAGE
        try:
            client = _client_factory()
            wallet_text = await asyncio.to_thread(
                client.execute,
                "create-wallet",
                identity,
            )
            return _start_text(wallet_text)
        except GatewayClientError as exc:
            return exc.user_message
        except Exception as exc:
            logger.warning(
                "Unexpected Sign402 start plugin failure error=%s",
                type(exc).__name__,
            )
            return _UNEXPECTED_ERROR_MESSAGE

    return handler


def _start_text(wallet_text: str) -> str:
    return (
        "Welcome to Sign402.\n\n"
        f"{wallet_text.strip()}\n\n"
        "Next steps:\n"
        "1. Fund this Base wallet with ETH for gas and USDC for payments.\n"
        "2. Run /balance to check funds.\n"
        "3. Run /connect_imessage to link iMessage approvals.\n\n"
        "After that, try: buy crypto news"
    )


def handle_pre_gateway_dispatch(*, event, gateway=None, **kwargs):
    """Capture trusted identities and consume Photon approval messages."""

    capture_gateway_identity(event=event, **kwargs)
    source = getattr(event, "source", None)
    telegram_tool = _telegram_paid_tool_intent(event, source)
    if telegram_tool:
        return _handle_telegram_paid_tool_request(
            tool=telegram_tool,
            source=source,
            gateway=gateway,
        )

    if not _is_photon_source(event, source):
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


def _handle_telegram_paid_tool_request(*, tool: str, source, gateway):
    identity = consume_gateway_identity() or _identity_from_telegram_source(source)
    if identity is None:
        _send_fixed_reply(gateway, source, _TELEGRAM_ONLY_MESSAGE)
        return dict(_SKIP_RESULT)
    _send_fixed_reply(gateway, source, _TELEGRAM_PAID_TOOL_STARTED_MESSAGE)
    _run_in_background(
        lambda: _execute_telegram_paid_tool_request(
            tool=tool,
            identity=identity,
            source=source,
            gateway=gateway,
        )
    )
    return dict(_SKIP_RESULT)


def _execute_telegram_paid_tool_request(
    *,
    tool: str,
    identity: TelegramIdentity,
    source,
    gateway,
) -> None:
    try:
        text = _client_factory().execute_paid_tool(tool, identity)
        _send_fixed_reply(gateway, source, text)
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 Telegram paid tool failure tool=%s error=%s",
            tool,
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _UNEXPECTED_ERROR_MESSAGE)


def _run_in_background(callback: Callable[[], None]) -> None:
    _background_runner(callback)


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
    if _is_telegram_source(source) and _send_telegram_reply_direct(source, text):
        return
    if gateway is None:
        return
    adapters = getattr(gateway, "adapters", {}) or {}
    adapter = adapters.get(_platform_name(source))
    if adapter is None:
        source_platform = getattr(source, "platform", None)
        adapter = adapters.get(source_platform)
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
        task = loop.create_task(coroutine)
        task.add_done_callback(_log_send_task_failure)


def _send_telegram_reply_direct(source, text: str) -> bool:
    token = _telegram_bot_token()
    chat_id = str(getattr(source, "chat_id", "") or getattr(source, "user_id", "") or "")
    if not token or not chat_id:
        return False

    try:
        for chunk in _telegram_message_chunks(text):
            payload = urlencode(
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": "true",
                }
            ).encode("utf-8")
            request = Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            response = _telegram_api_opener(
                request,
                timeout=_TELEGRAM_SEND_TIMEOUT_SECONDS,
            )
            try:
                read = getattr(response, "read", None)
                if callable(read):
                    read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        return True
    except (HTTPError, TimeoutError, URLError, OSError) as exc:
        logger.warning(
            "Direct Telegram reply failed error=%s; falling back to Hermes adapter",
            type(exc).__name__,
        )
        return False


def _telegram_bot_token() -> str:
    for name in _TELEGRAM_TOKEN_ENV_NAMES:
        value = str(os.environ.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _telegram_message_chunks(text: str) -> list[str]:
    value = str(text or "")
    if not value:
        return [""]
    return [
        value[index : index + _TELEGRAM_MESSAGE_CHUNK_SIZE]
        for index in range(0, len(value), _TELEGRAM_MESSAGE_CHUNK_SIZE)
    ]


def _log_send_task_failure(task) -> None:
    try:
        task.result()
    except Exception as exc:
        logger.warning("Hermes adapter reply failed error=%s", type(exc).__name__)


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


def _is_photon_source(event, source) -> bool:
    platform_name = _platform_name(source)
    if platform_name in {"photon", "imessage", "imessage via photon", "platforms/photon"}:
        return True
    raw_message = getattr(event, "raw_message", None)
    if isinstance(raw_message, dict):
        raw_platform = str(raw_message.get("platform", "") or "").strip().lower()
        return raw_platform in {"imessage", "photon"}
    return False


def _is_telegram_source(source) -> bool:
    return _platform_name(source) == "telegram"


def _identity_from_telegram_source(source) -> TelegramIdentity | None:
    if not _is_telegram_source(source):
        return None
    user_id = str(getattr(source, "user_id", "") or "").strip()
    if not user_id.isdecimal():
        return None
    raw_username = getattr(source, "user_name", None)
    username = str(raw_username).strip() if raw_username else None
    raw_chat_id = getattr(source, "chat_id", None)
    chat_id = str(raw_chat_id).strip() if raw_chat_id is not None else None
    return TelegramIdentity(
        user_id=user_id,
        username=username or None,
        chat_id=chat_id or None,
    )


def _telegram_paid_tool_intent(event, source) -> str | None:
    if not _is_telegram_source(source):
        return None
    text = str(getattr(event, "text", "") or "").strip().lower()
    if text.startswith("/"):
        return None
    normalized = (
        text.replace("_", " ")
        .replace("-", " ")
        .replace("crypto news", "cryptonews")
    )
    # Do not initiate a real spend flow from a message that merely mentions
    # buying (a question, a negation, or a cancellation) rather than requesting
    # one. This prevents casual chatter like "why did you buy crypto news?" from
    # popping an approval prompt / starting a purchase.
    if any(marker in normalized for marker in _NON_PURCHASE_MARKERS):
        return None
    if "buy" in normalized and "cryptonews" in normalized:
        return "news"
    return None


_NON_PURCHASE_MARKERS = (
    "don't",
    "do not",
    "dont",
    "didn't",
    "did not",
    "didnt",
    "won't",
    "will not",
    "wont",
    "shouldn't",
    "should not",
    "why",
    "cancel",
    "stop",
    "never",
    "already bought",
)


def _looks_like_pairing_code(value: str) -> bool:
    code = str(value or "").strip().upper()
    return (
        len(code) == 8
        and all(character in _PAIRING_CODE_ALPHABET for character in code)
    )


def register(ctx) -> None:
    """Register trusted Telegram identity capture and Sign402 commands."""

    ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
    ctx.register_command(
        "start",
        handler=_build_start_handler(),
        description="Start Sign402 wallet onboarding",
    )
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
