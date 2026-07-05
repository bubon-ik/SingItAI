"""Hermes plugin for trusted Sign402 Telegram wallet and iMessage approval commands."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
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
_TELEGRAM_COMMAND_MENU_TIMEOUT_SECONDS = 10
_TELEGRAM_COMMAND_MENU_REFRESH_DELAYS_SECONDS = (0, 2, 8)
_TELEGRAM_MESSAGE_CHUNK_SIZE = 3900
_TELEGRAM_PAID_TOOL_STARTED_MESSAGE = (
    "Sign402 purchase started. Approve it in iMessage; I'll post the result here."
)
_TELEGRAM_BITREFILL_STARTED_MESSAGE = (
    "Bitrefill purchase started. Approve it in iMessage; I'll post the result here."
)
_TELEGRAM_LLM_STARTED_MESSAGE = (
    "Bankr LLM purchase started. Approve it in iMessage; I'll post the result here."
)
_TELEGRAM_PUBLIC_COMMAND_MENU = (
    {"command": "start", "description": "Set up your Sign402 wallet"},
    {"command": "wallet", "description": "Show your Base wallet"},
    {"command": "balance", "description": "Show wallet balances"},
    {"command": "last_purchase", "description": "Show your latest purchase"},
    {"command": "limits", "description": "Show or set spending limits"},
    {"command": "connect_imessage", "description": "Link iMessage approvals"},
    {"command": "bitrefill", "description": "Buy Bitrefill with SINGIT"},
    {"command": "llm_buy", "description": "Buy Bankr LLM credits"},
    {"command": "llm_terms", "description": "Accept Bankr LLM terms"},
    {"command": "llm_code", "description": "Verify Bankr email code"},
    {"command": "llm_credits", "description": "Show Bankr LLM credits"},
)
_COMMANDS = {
    "wallet": ("create-wallet", "Show your Base agent wallet"),
    "balance": ("balance", "Show your managed Base wallet balance"),
    "last-purchase": ("last-purchase", "Show your latest Sign402 purchase"),
}
_IMESSAGE_COMMANDS = {
    "connect-imessage": (
        "connect-imessage",
        "Link your iMessage number for Sign402 approvals",
    ),
}
_LIMITS_USAGE = "Usage: /limits 0.005 0.05 or /set_limits 0.005 0.05"
_BITREFILL_USAGE = "Usage: /bitrefill <productId> <packageId> [country]"
_LLM_BUY_USAGE = "Usage: /llm_buy <usd> <email>"
_LLM_TERMS_USAGE = "Usage: /llm_terms accept"
_LLM_CODE_USAGE = "Usage: /llm_code <six-digit code>"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_client_factory: Callable[[], GatewayClient] = GatewayClient.from_env
_telegram_api_opener: Callable[..., object] = urlopen
_background_runner: Callable[[Callable[[], None]], None]
_sleep: Callable[[float], None] = time.sleep


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
            # create-wallet is the bootstrap that issues the token; every other
            # op authenticates as the specific user via their per-user token.
            token = None if operation == "create-wallet" else _user_access_token(client, identity)
            return await asyncio.to_thread(
                client.execute, operation, identity, user_access_token=token
            )
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


def _build_limits_handler(command: str):
    async def handler(raw_args: str) -> str:
        identity = consume_gateway_identity()
        if identity is None:
            return _TELEGRAM_ONLY_MESSAGE
        parsed_limits = _parse_limit_args(command, raw_args)
        if parsed_limits is None:
            return _LIMITS_USAGE
        try:
            max_per_tx_usdc, daily_cap_usdc = parsed_limits
            client = _client_factory()
            return await asyncio.to_thread(
                client.execute_spending_limits,
                identity,
                max_per_tx_usdc=max_per_tx_usdc,
                daily_cap_usdc=daily_cap_usdc,
            )
        except GatewayClientError as exc:
            return exc.user_message
        except Exception as exc:
            logger.warning(
                "Unexpected Sign402 limits plugin failure error=%s",
                type(exc).__name__,
            )
            return _UNEXPECTED_ERROR_MESSAGE

    return handler


def _build_bitrefill_handler():
    async def handler(raw_args: str) -> str:
        identity = consume_gateway_identity()
        if identity is None:
            return _TELEGRAM_ONLY_MESSAGE
        parsed = _parse_bitrefill_args(raw_args)
        if parsed is None:
            return _BITREFILL_USAGE
        product_id, package_id, country = parsed
        try:
            client = _client_factory()
            token = _user_access_token(client, identity)
            return await asyncio.to_thread(
                client.execute_bitrefill_purchase,
                identity,
                product_id=product_id,
                package_id=package_id,
                country=country,
                recipient={},
                user_access_token=token,
            )
        except GatewayClientError as exc:
            return exc.user_message
        except Exception as exc:
            logger.warning(
                "Unexpected Sign402 Bitrefill plugin failure error=%s",
                type(exc).__name__,
            )
            return _UNEXPECTED_ERROR_MESSAGE

    return handler


def _build_llm_handler(operation: str):
    async def handler(raw_args: str) -> str:
        identity = consume_gateway_identity()
        if identity is None:
            return _TELEGRAM_ONLY_MESSAGE
        parsed = _llm_operation_payload(operation, raw_args)
        if parsed is None:
            return _llm_usage(operation)
        try:
            client = _client_factory()
            result = await asyncio.to_thread(
                client.execute_llm,
                operation,
                identity,
                payload=parsed,
                user_access_token=_user_access_token(client, identity),
            )
            return _llm_result_text(result, reveal_api_key=operation == "verify")
        except GatewayClientError as exc:
            return exc.user_message
        except Exception as exc:
            logger.warning(
                "Unexpected Sign402 Bankr LLM plugin failure operation=%s error=%s",
                operation,
                type(exc).__name__,
            )
            return _UNEXPECTED_ERROR_MESSAGE

    return handler


def _build_llm_code_handler():
    return _build_llm_handler("verify")


def _start_text(wallet_text: str) -> str:
    return (
        "Welcome to Sign402.\n\n"
        f"{wallet_text.strip()}\n\n"
        "Next steps:\n"
        "1. Fund this Base wallet with ETH for gas and USDC for payments.\n"
        "2. Run /balance to check funds.\n"
        "3. Run /limits to review spending limits.\n"
        "4. Run /connect_imessage to link iMessage approvals.\n\n"
        "After that, try: buy crypto news"
    )


def handle_pre_gateway_dispatch(*, event, gateway=None, **kwargs):
    """Capture trusted identities and consume Photon approval messages."""

    capture_gateway_identity(event=event, **kwargs)
    source = getattr(event, "source", None)
    telegram_command = _telegram_public_command(event, source)
    if telegram_command:
        return _handle_telegram_public_command_request(
            command=telegram_command,
            args=_telegram_command_args(event),
            source=source,
            gateway=gateway,
        )

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


def _handle_telegram_public_command_request(*, command: str, args: str = "", source, gateway):
    identity = consume_gateway_identity() or _identity_from_telegram_source(source)
    if identity is None:
        _send_fixed_reply(gateway, source, _TELEGRAM_ONLY_MESSAGE)
        return dict(_SKIP_RESULT)
    try:
        client = _client_factory()
        if command == "start":
            wallet_text = client.execute("create-wallet", identity)
            text = _start_text(wallet_text)
        elif command == "wallet":
            text = client.execute("create-wallet", identity)
        elif command == "balance":
            text = client.execute(
                "balance",
                identity,
                user_access_token=_user_access_token(client, identity),
            )
        elif command == "last-purchase":
            text = client.execute(
                "last-purchase",
                identity,
                user_access_token=_user_access_token(client, identity),
            )
        elif command in {"limits", "set-limits"}:
            parsed_limits = _parse_limit_args(command, args)
            if parsed_limits is None:
                text = _LIMITS_USAGE
            else:
                max_per_tx_usdc, daily_cap_usdc = parsed_limits
                text = client.execute_spending_limits(
                    identity,
                    max_per_tx_usdc=max_per_tx_usdc,
                    daily_cap_usdc=daily_cap_usdc,
                )
        elif command == "connect-imessage":
            result = client.execute_imessage(
                "connect-imessage",
                {"telegramUserId": identity.user_id},
            )
            text = result.get("telegramText")
            if not isinstance(text, str) or not text.strip():
                text = _IMESSAGE_UNEXPECTED_ERROR_MESSAGE
        elif command in {"llm-buy", "llm-terms", "llm-credits"}:
            operation = {
                "llm-buy": "start",
                "llm-terms": "accept-terms",
                "llm-credits": "credits",
            }[command]
            payload = _llm_operation_payload(operation, args)
            if payload is None:
                _send_fixed_reply(gateway, source, _llm_usage(operation))
                return dict(_SKIP_RESULT)
            result = client.execute_llm(
                operation,
                identity,
                payload=payload,
                user_access_token=_user_access_token(client, identity),
            )
            text = _llm_result_text(result)
        elif command == "llm-code":
            payload = _llm_operation_payload("verify", args)
            if payload is None:
                _send_fixed_reply(gateway, source, _LLM_CODE_USAGE)
                return dict(_SKIP_RESULT)
            _send_fixed_reply(gateway, source, _TELEGRAM_LLM_STARTED_MESSAGE)
            _run_in_background(
                lambda: _execute_telegram_llm_request(
                    operation="verify",
                    payload=payload,
                    identity=identity,
                    source=source,
                    gateway=gateway,
                )
            )
            return dict(_SKIP_RESULT)
        elif command == "bitrefill":
            parsed = _parse_bitrefill_args(args)
            if parsed is None:
                _send_fixed_reply(gateway, source, _BITREFILL_USAGE)
                return dict(_SKIP_RESULT)
            product_id, package_id, country = parsed
            _send_fixed_reply(gateway, source, _TELEGRAM_BITREFILL_STARTED_MESSAGE)
            _run_in_background(
                lambda: _execute_telegram_bitrefill_request(
                    product_id=product_id,
                    package_id=package_id,
                    country=country,
                    identity=identity,
                    source=source,
                    gateway=gateway,
                )
            )
            return dict(_SKIP_RESULT)
        else:
            return None
        _send_fixed_reply(gateway, source, text)
        return dict(_SKIP_RESULT)
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
        return dict(_SKIP_RESULT)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 Telegram public command failure command=%s error=%s",
            command,
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _UNEXPECTED_ERROR_MESSAGE)
        return dict(_SKIP_RESULT)


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


_USER_ACCESS_TOKENS: dict[str, str] = {}


def _user_access_token(client, identity: TelegramIdentity) -> str | None:
    """Return the caller's per-user gateway token, minting one if unseen.

    Cached in-process across requests. create-wallet is idempotent and returns
    a fresh token, so a cold cache (e.g. after restart) is refilled without a
    separate bootstrap step. Failures degrade to None (gateway then falls back
    to the shared-token path) rather than blocking the purchase.
    """
    user_id = str(identity.user_id)
    cached = _USER_ACCESS_TOKENS.get(user_id)
    if cached:
        return cached
    try:
        result = client.create_wallet(identity)
    except Exception:
        return None
    token = str(result.get("accessToken") or "") if isinstance(result, dict) else ""
    if token:
        _USER_ACCESS_TOKENS[user_id] = token
        return token
    return None


def _execute_telegram_paid_tool_request(
    *,
    tool: str,
    identity: TelegramIdentity,
    source,
    gateway,
) -> None:
    try:
        client = _client_factory()
        token = _user_access_token(client, identity)
        text = client.execute_paid_tool(tool, identity, user_access_token=token)
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


def _execute_telegram_bitrefill_request(
    *,
    product_id: str,
    package_id: str,
    country: str,
    identity: TelegramIdentity,
    source,
    gateway,
) -> None:
    try:
        client = _client_factory()
        token = _user_access_token(client, identity)
        text = client.execute_bitrefill_purchase(
            identity,
            product_id=product_id,
            package_id=package_id,
            country=country,
            recipient={},
            user_access_token=token,
        )
        _send_fixed_reply(gateway, source, text)
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 Telegram Bitrefill failure error=%s",
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _UNEXPECTED_ERROR_MESSAGE)


def _execute_telegram_llm_request(
    *,
    operation: str,
    payload: dict[str, str],
    identity: TelegramIdentity,
    source,
    gateway,
) -> None:
    try:
        client = _client_factory()
        result = client.execute_llm(
            operation,
            identity,
            payload=payload,
            user_access_token=_user_access_token(client, identity),
        )
        _send_fixed_reply(
            gateway,
            source,
            _llm_result_text(result, reveal_api_key=operation == "verify"),
        )
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 Bankr LLM background failure operation=%s error=%s",
            operation,
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


def _schedule_telegram_public_command_menu_refresh() -> None:
    _run_in_background(_refresh_telegram_public_command_menu)


def _refresh_telegram_public_command_menu() -> None:
    if not _telegram_bot_token():
        return
    for delay in _TELEGRAM_COMMAND_MENU_REFRESH_DELAYS_SECONDS:
        if delay > 0:
            _sleep(delay)
        _configure_telegram_public_command_menu()


def _configure_telegram_public_command_menu() -> None:
    token = _telegram_bot_token()
    if not token:
        return

    scopes: tuple[dict[str, str] | None, ...] = (
        None,
        {"type": "all_private_chats"},
    )
    for scope in scopes:
        payload_fields = {
            "commands": json.dumps(
                list(_TELEGRAM_PUBLIC_COMMAND_MENU),
                separators=(",", ":"),
            )
        }
        if scope is not None:
            payload_fields["scope"] = json.dumps(scope, separators=(",", ":"))
        payload = urlencode(payload_fields).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{token}/setMyCommands",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            response = _telegram_api_opener(
                request,
                timeout=_TELEGRAM_COMMAND_MENU_TIMEOUT_SECONDS,
            )
            try:
                read = getattr(response, "read", None)
                if callable(read):
                    read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except (HTTPError, TimeoutError, URLError, OSError) as exc:
            logger.warning(
                "Could not configure Telegram command menu scope=%s error=%s",
                scope or "default",
                type(exc).__name__,
            )


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


def _telegram_public_command(event, source) -> str | None:
    if not _is_telegram_source(source):
        return None
    text = str(getattr(event, "text", "") or "").strip()
    if not text.startswith("/"):
        return None
    command = text[1:].split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    normalized = command.strip().lower().replace("_", "-")
    if normalized in {
        "start",
        "wallet",
        "balance",
        "last-purchase",
        "limits",
        "set-limits",
        "connect-imessage",
        "bitrefill",
        "llm-buy",
        "llm-terms",
        "llm-code",
        "llm-credits",
    }:
        return normalized
    return None


def _telegram_command_args(event) -> str:
    text = str(getattr(event, "text", "") or "").strip()
    if not text.startswith("/"):
        return ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _parse_limit_args(command: str, raw_args: str) -> tuple[str | None, str | None] | None:
    args = str(raw_args or "").strip().split()
    if command == "limits" and not args:
        return (None, None)
    if command == "limits" and len(args) == 1 and "=" in args[0]:
        return (None, None)
    if len(args) < 2:
        return None
    return (args[0], args[1])


def _parse_bitrefill_args(raw_args: str) -> tuple[str, str, str] | None:
    args = str(raw_args or "").strip().split()
    if len(args) < 2:
        return None
    country = args[2].upper() if len(args) >= 3 else "US"
    return (args[0], args[1], country)


def _parse_llm_buy_args(raw_args: str) -> tuple[str, str] | None:
    args = str(raw_args or "").strip().split()
    if len(args) != 2:
        return None
    amount_text, email = args
    try:
        amount = Decimal(amount_text)
    except (InvalidOperation, ValueError):
        return None
    if (
        not amount.is_finite()
        or amount < Decimal("1")
        or amount > Decimal("1000")
        or amount != amount.quantize(Decimal("0.01"))
        or _EMAIL_RE.fullmatch(email) is None
    ):
        return None
    return amount_text, email


def _llm_operation_payload(
    operation: str,
    raw_args: str,
) -> dict[str, str] | None:
    if operation == "start":
        parsed = _parse_llm_buy_args(raw_args)
        if parsed is None:
            return None
        amount, email = parsed
        return {"amountUsd": amount, "email": email}
    if operation == "accept-terms":
        return {} if str(raw_args or "").strip().lower() == "accept" else None
    if operation == "verify":
        code = str(raw_args or "").strip()
        return {"code": code} if re.fullmatch(r"\d{6}", code) else None
    if operation == "credits":
        return {} if not str(raw_args or "").strip() else None
    return None


def _llm_usage(operation: str) -> str:
    return {
        "start": _LLM_BUY_USAGE,
        "accept-terms": _LLM_TERMS_USAGE,
        "verify": _LLM_CODE_USAGE,
        "credits": "Usage: /llm_credits",
    }.get(operation, _UNEXPECTED_ERROR_MESSAGE)


def _llm_result_text(
    result: dict,
    *,
    reveal_api_key: bool = False,
) -> str:
    text = result.get("telegramText")
    if not isinstance(text, str) or not text.strip():
        raise GatewayClientError(_UNEXPECTED_ERROR_MESSAGE)
    rendered = text.strip()
    if reveal_api_key:
        api_key = result.get("apiKey")
        if isinstance(api_key, str) and api_key.startswith("bk_"):
            rendered = f"{rendered}\n\nAPI key:\n{api_key}"
    return rendered


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

    _schedule_telegram_public_command_menu_refresh()
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
    ctx.register_command(
        "limits",
        handler=_build_limits_handler("limits"),
        description="Show or set Sign402 spending limits",
    )
    ctx.register_command(
        "set-limits",
        handler=_build_limits_handler("set-limits"),
        description="Set Sign402 spending limits",
    )
    ctx.register_command(
        "bitrefill",
        handler=_build_bitrefill_handler(),
        description="Buy Bitrefill with SINGIT",
    )
    ctx.register_command(
        "llm-buy",
        handler=_build_llm_handler("start"),
        description="Buy Bankr LLM credits with SINGIT",
    )
    ctx.register_command(
        "llm-terms",
        handler=_build_llm_handler("accept-terms"),
        description="Accept Bankr LLM purchase terms",
    )
    ctx.register_command(
        "llm-code",
        handler=_build_llm_code_handler(),
        description="Verify the Bankr email code",
    )
    ctx.register_command(
        "llm-credits",
        handler=_build_llm_handler("credits"),
        description="Show Bankr LLM credit balance",
    )
    for command, (operation, description) in _IMESSAGE_COMMANDS.items():
        ctx.register_command(
            command,
            handler=_build_imessage_handler(operation),
            description=description,
        )
