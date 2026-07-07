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
_TELEGRAM_WITHDRAW_STARTED_MESSAGE = (
    "Withdrawal started. Approve it in iMessage; I'll post the result here."
)
_TELEGRAM_PUBLIC_COMMAND_MENU = (
    {"command": "start", "description": "Set up your Sign402 wallet"},
    {"command": "help", "description": "Show Sign402 commands"},
    {"command": "wallet", "description": "Show or create your Base wallet"},
    {"command": "balance", "description": "Show wallet balances"},
    {"command": "connect_imessage", "description": "Link iMessage approvals"},
    {"command": "limits", "description": "Show or set spending limits"},
    {"command": "withdraw", "description": "Withdraw ERC-20 tokens"},
    {"command": "bitrefill", "description": "Buy Bitrefill with SINGIT"},
    {"command": "last_purchase", "description": "Reveal latest purchase"},
    {"command": "llm_buy", "description": "Buy Bankr LLM credits"},
    {"command": "llm_credits", "description": "Show Bankr LLM credits"},
)
_TELEGRAM_MAIN_MENU_BUTTONS = (
    ("Wallet", "Balance"),
    ("Connect iMessage", "Limits"),
    ("Withdraw",),
    ("Buy Bitrefill", "Buy LLM Credits"),
    ("Last Purchase", "Help"),
)
_TELEGRAM_BUTTON_COMMANDS = {
    "wallet": "wallet",
    "balance": "balance",
    "connect imessage": "connect-imessage",
    "limits": "limits",
    "withdraw": "withdraw",
    "buy bitrefill": "bitrefill",
    "buy llm credits": "llm-buy",
    "last purchase": "last-purchase",
    "help": "help",
}
_BITREFILL_DEFAULT_COUNTRY = "CZ"
_BITREFILL_MAX_SEARCH_RESULTS = 5
_BITREFILL_MAX_PACKAGES = 8
_BITREFILL_CATALOG_PAGE_SIZE = 8
_BITREFILL_MENU_BUTTONS = (
    ("Browse Catalog", "Search Products"),
    ("Change Country",),
    ("Back",),
)
_BITREFILL_CATEGORY_BUTTONS = (
    ("All", "Shopping"),
    ("Food", "Games"),
    ("Mobile", "Travel"),
    ("Entertainment", "Back"),
)
_BITREFILL_CATEGORY_VALUES = {
    "all": "all",
    "shopping": "shopping",
    "food": "food",
    "games": "games",
    "mobile": "mobile",
    "travel": "travel",
    "entertainment": "entertainment",
}
_BITREFILL_COUNTRY_BUTTONS = (
    ("CZ", "US", "DE"),
    ("PL", "UA", "GB"),
    ("Other", "Back"),
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
_LLM_BUY_USAGE = "Usage: /llm_buy <usd> <email> [token]"
_LLM_TERMS_USAGE = "Usage: /llm_terms accept"
_LLM_CODE_USAGE = "Usage: /llm_code <six-digit code>"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_client_factory: Callable[[], GatewayClient] = GatewayClient.from_env
_telegram_api_opener: Callable[..., object] = urlopen
_background_runner: Callable[[Callable[[], None]], None]
_sleep: Callable[[float], None] = time.sleep
_BITREFILL_USER_COUNTRIES: dict[str, str] = {}
_BITREFILL_SESSIONS: dict[str, dict] = {}
_WITHDRAW_SESSIONS: dict[str, dict] = {}


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


def _help_text() -> str:
    return (
        "Sign402 commands\n\n"
        "/wallet - Create or show your Base wallet\n"
        "/balance - Show ETH, USDC, and SINGIT balances\n"
        "/connect_imessage - Link iMessage approvals\n"
        "/limits - View or set spending limits\n"
        "/bitrefill <product> <amount> <country> - Buy Bitrefill with SINGIT\n"
        "/last_purchase - Reveal your latest purchase\n"
        "/llm_buy <usd> <email> - Buy Bankr LLM credits\n"
        "/llm_credits - Show Bankr LLM credits"
    )


def _build_help_handler():
    async def handler(_raw_args: str) -> str:
        return _help_text()

    return handler


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

    withdraw_wizard_result = _handle_telegram_withdraw_wizard_message(
        event=event,
        source=source,
        gateway=gateway,
    )
    if withdraw_wizard_result:
        return withdraw_wizard_result

    bitrefill_wizard_result = _handle_telegram_bitrefill_wizard_message(
        event=event,
        source=source,
        gateway=gateway,
    )
    if bitrefill_wizard_result:
        return bitrefill_wizard_result

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
        if command == "start":
            client = _client_factory()
            wallet_text = client.execute("create-wallet", identity)
            text = _start_text(wallet_text)
        elif command == "help":
            text = _help_text()
        elif command == "wallet":
            client = _client_factory()
            text = client.execute("create-wallet", identity)
        elif command == "balance":
            client = _client_factory()
            text = client.execute(
                "balance",
                identity,
                user_access_token=_user_access_token(client, identity),
            )
        elif command == "last-purchase":
            client = _client_factory()
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
                client = _client_factory()
                max_per_tx_usdc, daily_cap_usdc = parsed_limits
                text = client.execute_spending_limits(
                    identity,
                    max_per_tx_usdc=max_per_tx_usdc,
                    daily_cap_usdc=daily_cap_usdc,
                )
        elif command == "connect-imessage":
            client = _client_factory()
            result = client.execute_imessage(
                "connect-imessage",
                {"telegramUserId": identity.user_id},
            )
            text = result.get("telegramText")
            if not isinstance(text, str) or not text.strip():
                text = _IMESSAGE_UNEXPECTED_ERROR_MESSAGE
        elif command in {"llm-buy", "llm-terms", "llm-credits"}:
            client = _client_factory()
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
        elif command == "withdraw":
            _open_withdraw_flow(identity=identity, source=source, gateway=gateway)
            return dict(_SKIP_RESULT)
        elif command == "bitrefill":
            parsed = _parse_bitrefill_args(args)
            if parsed is None:
                if str(args or "").strip():
                    _send_fixed_reply(gateway, source, _BITREFILL_USAGE)
                else:
                    _open_bitrefill_menu(identity=identity, source=source, gateway=gateway)
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
        _send_fixed_reply(
            gateway,
            source,
            text,
            reply_markup=_telegram_main_menu_reply_markup(),
        )
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


def _handle_telegram_bitrefill_wizard_message(*, event, source, gateway):
    identity = _identity_from_telegram_source(source)
    if identity is None:
        return None
    user_id = str(identity.user_id)
    session = _BITREFILL_SESSIONS.get(user_id)
    if not session:
        return None

    text = str(getattr(event, "text", "") or "").strip()
    if not text:
        return None
    normalized = _normalize_button_text(text)
    stage = str(session.get("stage") or "")
    if normalized == "back":
        if stage in {"select-category", "select-product"} and session.get("source") == "catalog":
            if stage == "select-product":
                _send_bitrefill_category_prompt(identity=identity, source=source, gateway=gateway)
            else:
                _open_bitrefill_menu(identity=identity, source=source, gateway=gateway)
        else:
            _BITREFILL_SESSIONS.pop(user_id, None)
            _send_fixed_reply(
                gateway,
                source,
                "Back to Sign402 main menu.",
                reply_markup=_telegram_main_menu_reply_markup(),
            )
        return dict(_SKIP_RESULT)
    if normalized == "change country":
        _BITREFILL_SESSIONS[user_id] = {"stage": "awaiting-country"}
        _send_bitrefill_country_prompt(gateway, source)
        return dict(_SKIP_RESULT)
    if normalized == "browse catalog":
        _send_bitrefill_category_prompt(identity=identity, source=source, gateway=gateway)
        return dict(_SKIP_RESULT)
    if normalized == "search products":
        country = _bitrefill_country(user_id)
        _BITREFILL_SESSIONS[user_id] = {"stage": "awaiting-search", "country": country}
        _send_fixed_reply(
            gateway,
            source,
            f"What do you want to buy in {country}?\n\nExample: amazon, playstation, mobile",
            reply_markup=_reply_keyboard((("Change Country", "Back"),)),
        )
        return dict(_SKIP_RESULT)

    try:
        if stage == "awaiting-country":
            return _handle_bitrefill_country_input(
                identity=identity,
                text=text,
                source=source,
                gateway=gateway,
            )
        if stage == "awaiting-search":
            return _handle_bitrefill_search_input(
                identity=identity,
                query=text,
                source=source,
                gateway=gateway,
            )
        if stage == "select-category":
            return _handle_bitrefill_category_input(
                identity=identity,
                text=text,
                source=source,
                gateway=gateway,
            )
        if stage == "select-product":
            if session.get("source") == "catalog":
                if normalized in {"next", "previous"}:
                    return _handle_bitrefill_catalog_pagination(
                        identity=identity,
                        direction=normalized,
                        source=source,
                        gateway=gateway,
                    )
            return _handle_bitrefill_product_choice(
                identity=identity,
                text=text,
                source=source,
                gateway=gateway,
            )
        if stage == "select-package":
            return _handle_bitrefill_package_choice(
                identity=identity,
                text=text,
                source=source,
                gateway=gateway,
            )
        if stage == "awaiting-recipient":
            return _handle_bitrefill_recipient_input(
                identity=identity,
                text=text,
                source=source,
                gateway=gateway,
            )
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
        return dict(_SKIP_RESULT)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 Bitrefill wizard failure stage=%s error=%s",
            stage,
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _UNEXPECTED_ERROR_MESSAGE)
        return dict(_SKIP_RESULT)
    return None


def _open_bitrefill_menu(*, identity: TelegramIdentity, source, gateway) -> None:
    user_id = str(identity.user_id)
    country = _bitrefill_country(user_id)
    _BITREFILL_SESSIONS[user_id] = {"stage": "menu", "country": country}
    _send_fixed_reply(
        gateway,
        source,
        _bitrefill_menu_text(country),
        reply_markup=_reply_keyboard(_BITREFILL_MENU_BUTTONS),
    )


def _bitrefill_menu_text(country: str) -> str:
    return (
        "Bitrefill\n\n"
        f"Country: {country}\n"
        "Browse Catalog to explore products, or Search Products if you know what you want."
    )


def _bitrefill_country(user_id: str) -> str:
    return _BITREFILL_USER_COUNTRIES.get(str(user_id), _BITREFILL_DEFAULT_COUNTRY)


def _send_bitrefill_country_prompt(gateway, source) -> None:
    _send_fixed_reply(
        gateway,
        source,
        "Send a two-letter country code, like CZ, US, DE, PL, UA.",
        reply_markup=_reply_keyboard(_BITREFILL_COUNTRY_BUTTONS),
    )


def _send_bitrefill_category_prompt(*, identity: TelegramIdentity, source, gateway) -> None:
    country = _bitrefill_country(str(identity.user_id))
    _BITREFILL_SESSIONS[str(identity.user_id)] = {
        "stage": "select-category",
        "source": "catalog",
        "country": country,
    }
    _send_fixed_reply(
        gateway,
        source,
        f"Choose a Bitrefill category for {country}.\n\nProducts from {country} and international catalog are included.",
        reply_markup=_reply_keyboard(_BITREFILL_CATEGORY_BUTTONS),
    )


def _handle_bitrefill_country_input(*, identity: TelegramIdentity, text: str, source, gateway):
    raw_country = str(text or "").strip().upper()
    if raw_country == "OTHER":
        _send_bitrefill_country_prompt(gateway, source)
        return dict(_SKIP_RESULT)
    if not re.fullmatch(r"[A-Z]{2}", raw_country):
        _send_fixed_reply(
            gateway,
            source,
            "Please send a two-letter country code, for example CZ or US.",
            reply_markup=_reply_keyboard(_BITREFILL_COUNTRY_BUTTONS),
        )
        return dict(_SKIP_RESULT)
    _BITREFILL_USER_COUNTRIES[str(identity.user_id)] = raw_country
    _open_bitrefill_menu(identity=identity, source=source, gateway=gateway)
    return dict(_SKIP_RESULT)


def _handle_bitrefill_search_input(*, identity: TelegramIdentity, query: str, source, gateway):
    country = _bitrefill_country(str(identity.user_id))
    clean_query = str(query or "").strip()
    if len(clean_query) < 2:
        _send_fixed_reply(
            gateway,
            source,
            f"What do you want to buy in {country}?\n\nType at least 2 characters.",
            reply_markup=_reply_keyboard((("Change Country", "Back"),)),
        )
        return dict(_SKIP_RESULT)
    client = _client_factory()
    result = client.search_bitrefill_products(
        query=clean_query,
        country=country,
        include_test_products=False,
    )
    products = _normalize_bitrefill_products(result.get("products"))
    if not products:
        _BITREFILL_SESSIONS[str(identity.user_id)] = {
            "stage": "awaiting-search",
            "country": country,
        }
        _send_fixed_reply(
            gateway,
            source,
            f"No Bitrefill products found for \"{clean_query}\" in {country}.\n\nTry another search.",
            reply_markup=_reply_keyboard((("Change Country", "Back"),)),
        )
        return dict(_SKIP_RESULT)
    limited = products[:_BITREFILL_MAX_SEARCH_RESULTS]
    _BITREFILL_SESSIONS[str(identity.user_id)] = {
        "stage": "select-product",
        "country": country,
        "products": limited,
    }
    _send_fixed_reply(
        gateway,
        source,
        _format_bitrefill_search_results(clean_query, country, limited),
        reply_markup=_numbered_reply_keyboard(len(limited)),
    )
    return dict(_SKIP_RESULT)


def _handle_bitrefill_category_input(*, identity: TelegramIdentity, text: str, source, gateway):
    normalized = _normalize_button_text(text)
    category = _BITREFILL_CATEGORY_VALUES.get(normalized)
    if category is None:
        _send_fixed_reply(
            gateway,
            source,
            "Choose a category from the buttons.",
            reply_markup=_reply_keyboard(_BITREFILL_CATEGORY_BUTTONS),
        )
        return dict(_SKIP_RESULT)
    return _send_bitrefill_catalog_page(
        identity=identity,
        category=category,
        start=0,
        source=source,
        gateway=gateway,
    )


def _handle_bitrefill_catalog_pagination(
    *,
    identity: TelegramIdentity,
    direction: str,
    source,
    gateway,
):
    session = _BITREFILL_SESSIONS.get(str(identity.user_id), {})
    category = str(session.get("category") or "all")
    start = int(session.get("start") or 0)
    if direction == "next":
        if not session.get("hasNext"):
            _send_fixed_reply(gateway, source, "There is no next page.")
            return dict(_SKIP_RESULT)
        start += _BITREFILL_CATALOG_PAGE_SIZE
    else:
        if not session.get("hasPrevious"):
            _send_fixed_reply(gateway, source, "There is no previous page.")
            return dict(_SKIP_RESULT)
        start = max(0, start - _BITREFILL_CATALOG_PAGE_SIZE)
    return _send_bitrefill_catalog_page(
        identity=identity,
        category=category,
        start=start,
        source=source,
        gateway=gateway,
    )


def _send_bitrefill_catalog_page(
    *,
    identity: TelegramIdentity,
    category: str,
    start: int,
    source,
    gateway,
):
    country = _bitrefill_country(str(identity.user_id))
    client = _client_factory()
    result = client.list_bitrefill_products(
        country=country,
        category=category,
        start=start,
        limit=_BITREFILL_CATALOG_PAGE_SIZE,
        include_international=True,
        include_test_products=False,
    )
    products = _normalize_bitrefill_products(result.get("products"))
    has_previous = bool(result.get("hasPrevious"))
    has_next = bool(result.get("hasNext"))
    if not products:
        _BITREFILL_SESSIONS[str(identity.user_id)] = {
            "stage": "select-category",
            "source": "catalog",
            "country": country,
        }
        _send_fixed_reply(
            gateway,
            source,
            "No products found in this category. Choose another category.",
            reply_markup=_reply_keyboard(_BITREFILL_CATEGORY_BUTTONS),
        )
        return dict(_SKIP_RESULT)
    _BITREFILL_SESSIONS[str(identity.user_id)] = {
        "stage": "select-product",
        "source": "catalog",
        "country": country,
        "category": category,
        "start": start,
        "hasPrevious": has_previous,
        "hasNext": has_next,
        "products": products,
    }
    _send_fixed_reply(
        gateway,
        source,
        _format_bitrefill_catalog_page(country, category, start, products),
        reply_markup=_bitrefill_catalog_reply_keyboard(
            len(products),
            has_previous=has_previous,
            has_next=has_next,
        ),
    )
    return dict(_SKIP_RESULT)


def _handle_bitrefill_product_choice(*, identity: TelegramIdentity, text: str, source, gateway):
    session = _BITREFILL_SESSIONS.get(str(identity.user_id), {})
    products = session.get("products") if isinstance(session.get("products"), list) else []
    index = _parse_choice_index(text, len(products))
    if index is None:
        _send_fixed_reply(
            gateway,
            source,
            "Reply with a product number from the list.",
            reply_markup=_numbered_reply_keyboard(len(products)),
        )
        return dict(_SKIP_RESULT)
    country = str(session.get("country") or _bitrefill_country(str(identity.user_id)))
    product = products[index]
    product_id = str(product.get("productId") or product.get("id") or "").strip()
    client = _client_factory()
    details = client.get_bitrefill_product(product_id=product_id, country=country)
    packages = _normalize_bitrefill_packages(details.get("packages"))
    if not packages:
        _send_fixed_reply(
            gateway,
            source,
            "This Bitrefill product has no available packages right now. Try another product.",
            reply_markup=_reply_keyboard((("Search Products", "Back"),)),
        )
        return dict(_SKIP_RESULT)
    limited_packages = packages[:_BITREFILL_MAX_PACKAGES]
    _BITREFILL_SESSIONS[str(identity.user_id)] = {
        "stage": "select-package",
        "country": country,
        "product": details,
        "packages": limited_packages,
    }
    _send_fixed_reply(
        gateway,
        source,
        _format_bitrefill_packages(details, limited_packages),
        reply_markup=_numbered_reply_keyboard(len(limited_packages)),
    )
    return dict(_SKIP_RESULT)


def _handle_bitrefill_package_choice(*, identity: TelegramIdentity, text: str, source, gateway):
    session = _BITREFILL_SESSIONS.get(str(identity.user_id), {})
    packages = session.get("packages") if isinstance(session.get("packages"), list) else []
    index = _parse_choice_index(text, len(packages))
    if index is None:
        _send_fixed_reply(
            gateway,
            source,
            "Reply with an amount number from the list.",
            reply_markup=_numbered_reply_keyboard(len(packages)),
        )
        return dict(_SKIP_RESULT)
    product = session.get("product") if isinstance(session.get("product"), dict) else {}
    package = packages[index]
    country = str(session.get("country") or _bitrefill_country(str(identity.user_id)))
    required_fields = [
        str(field).strip()
        for field in product.get("requiredRecipientFields", [])
        if str(field).strip()
    ]
    if required_fields:
        _BITREFILL_SESSIONS[str(identity.user_id)] = {
            "stage": "awaiting-recipient",
            "country": country,
            "product": product,
            "package": package,
            "recipientFields": required_fields,
            "recipient": {},
        }
        _send_fixed_reply(
            gateway,
            source,
            f"Send {required_fields[0]} for {product.get('name') or 'this product'}.",
            reply_markup=_reply_keyboard((("Back",),)),
        )
        return dict(_SKIP_RESULT)
    _start_bitrefill_purchase_from_wizard(
        identity=identity,
        product=product,
        package=package,
        country=country,
        recipient={},
        source=source,
        gateway=gateway,
    )
    return dict(_SKIP_RESULT)


def _handle_bitrefill_recipient_input(*, identity: TelegramIdentity, text: str, source, gateway):
    user_id = str(identity.user_id)
    session = _BITREFILL_SESSIONS.get(user_id, {})
    fields = session.get("recipientFields") if isinstance(session.get("recipientFields"), list) else []
    recipient = dict(session.get("recipient") or {})
    missing = [field for field in fields if field not in recipient]
    if not missing:
        _send_fixed_reply(gateway, source, "Recipient is already complete.")
        return dict(_SKIP_RESULT)
    field = missing[0]
    value = str(text or "").strip()
    if not value:
        _send_fixed_reply(gateway, source, f"Send {field} to continue.")
        return dict(_SKIP_RESULT)
    recipient[field] = value
    remaining = [candidate for candidate in fields if candidate not in recipient]
    if remaining:
        session["recipient"] = recipient
        _BITREFILL_SESSIONS[user_id] = session
        _send_fixed_reply(gateway, source, f"Send {remaining[0]} to continue.")
        return dict(_SKIP_RESULT)
    product = session.get("product") if isinstance(session.get("product"), dict) else {}
    package = session.get("package") if isinstance(session.get("package"), dict) else {}
    country = str(session.get("country") or _bitrefill_country(user_id))
    _start_bitrefill_purchase_from_wizard(
        identity=identity,
        product=product,
        package=package,
        country=country,
        recipient=recipient,
        source=source,
        gateway=gateway,
    )
    return dict(_SKIP_RESULT)


def _start_bitrefill_purchase_from_wizard(
    *,
    identity: TelegramIdentity,
    product: dict,
    package: dict,
    country: str,
    recipient: dict,
    source,
    gateway,
) -> None:
    product_id = str(product.get("productId") or product.get("id") or "").strip()
    package_id = str(package.get("packageId") or package.get("id") or "").strip()
    _BITREFILL_SESSIONS.pop(str(identity.user_id), None)
    _send_fixed_reply(gateway, source, _TELEGRAM_BITREFILL_STARTED_MESSAGE)
    _run_in_background(
        lambda: _execute_telegram_bitrefill_request(
            product_id=product_id,
            package_id=package_id,
            country=country,
            recipient=recipient,
            identity=identity,
            source=source,
            gateway=gateway,
        )
    )


def _normalize_bitrefill_products(raw_products) -> list[dict]:
    if not isinstance(raw_products, list):
        return []
    products: list[dict] = []
    for product in raw_products:
        if not isinstance(product, dict):
            continue
        product_id = str(product.get("productId") or product.get("id") or "").strip()
        name = str(product.get("name") or "").strip()
        if product_id and name:
            products.append(product)
    return products


def _normalize_bitrefill_packages(raw_packages) -> list[dict]:
    if not isinstance(raw_packages, list):
        return []
    packages: list[dict] = []
    for package in raw_packages:
        if not isinstance(package, dict):
            continue
        package_id = str(package.get("packageId") or package.get("id") or "").strip()
        if package_id:
            packages.append(package)
    return packages


def _format_bitrefill_search_results(query: str, country: str, products: list[dict]) -> str:
    lines = [f"Found products for \"{query}\" in {country}:"]
    for index, product in enumerate(products, start=1):
        name = str(product.get("name") or "Unknown product").strip()
        category = str(product.get("category") or product.get("productType") or "").strip()
        suffix = f" - {category}" if category else ""
        lines.append(f"{index}. {name}{suffix}")
    lines.append("")
    lines.append("Reply with a number.")
    return "\n".join(lines)


def _format_bitrefill_catalog_page(
    country: str,
    category: str,
    start: int,
    products: list[dict],
) -> str:
    category_title = category.replace("-", " ").title()
    page = (start // _BITREFILL_CATALOG_PAGE_SIZE) + 1
    lines = [f"{category_title} products in {country} + international:", f"Page {page}"]
    for index, product in enumerate(products, start=1):
        name = str(product.get("name") or "Unknown product").strip()
        product_country = str(product.get("country") or "").strip().upper()
        suffix = _bitrefill_product_country_suffix(name, product_country, country)
        lines.append(f"{index}. {name}{suffix}")
    lines.append("")
    lines.append("Reply with a number.")
    return "\n".join(lines)


def _bitrefill_product_country_suffix(name: str, product_country: str, user_country: str) -> str:
    if not product_country or product_country == user_country:
        return ""
    if product_country == "XI":
        if "international" in str(name or "").casefold():
            return ""
        return " (Global)"
    return f" ({product_country})"


def _format_bitrefill_packages(product: dict, packages: list[dict]) -> str:
    name = str(product.get("name") or "this product").strip()
    currency = str(product.get("currency") or "").strip().upper()
    lines = [f"Choose amount for {name}:"]
    for index, package in enumerate(packages, start=1):
        value = str(package.get("value") or package.get("packageId") or "").strip()
        price_usd = str(package.get("priceUsd") or "").strip()
        if currency and currency != "USD":
            amount = f"{value} {currency}"
        else:
            suffix = f" (${price_usd})" if price_usd else ""
            amount = f"{value}{suffix}"
        lines.append(f"{index}. {amount}")
    lines.append("")
    lines.append("Reply with a number.")
    return "\n".join(lines)


def _parse_choice_index(text: str, max_count: int) -> int | None:
    value = str(text or "").strip()
    if not value.isdecimal():
        return None
    index = int(value) - 1
    if index < 0 or index >= max_count:
        return None
    return index


def _numbered_reply_keyboard(count: int) -> dict:
    rows: list[tuple[str, ...]] = []
    numbers = [str(index) for index in range(1, count + 1)]
    for offset in range(0, len(numbers), 3):
        rows.append(tuple(numbers[offset : offset + 3]))
    rows.append(("Search Products", "Back"))
    return _reply_keyboard(tuple(rows))


def _bitrefill_catalog_reply_keyboard(
    count: int,
    *,
    has_previous: bool,
    has_next: bool,
) -> dict:
    rows: list[tuple[str, ...]] = []
    numbers = [str(index) for index in range(1, count + 1)]
    for offset in range(0, len(numbers), 4):
        rows.append(tuple(numbers[offset : offset + 4]))
    navigation = []
    if has_previous:
        navigation.append("Previous")
    if has_next:
        navigation.append("Next")
    if navigation:
        rows.append(tuple(navigation))
    rows.append(("Back",))
    return _reply_keyboard(tuple(rows))


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


def _open_withdraw_flow(*, identity: TelegramIdentity, source, gateway) -> None:
    user_id = str(identity.user_id)
    try:
        client = _client_factory()
        token = _user_access_token(client, identity)
        result = client.withdraw_tokens(identity, user_access_token=token)
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
        return
    except Exception as exc:
        logger.warning("Unexpected Sign402 withdraw token lookup error=%s", type(exc).__name__)
        _send_fixed_reply(gateway, source, _UNEXPECTED_ERROR_MESSAGE)
        return

    tokens = _normalize_withdraw_tokens(result.get("tokens") if isinstance(result, dict) else [])
    if not tokens:
        text = (
            str(result.get("telegramText") or "").strip()
            if isinstance(result, dict)
            else ""
        )
        _send_fixed_reply(
            gateway,
            source,
            text or "No ERC-20 token balances are available to withdraw yet.",
            reply_markup=_telegram_main_menu_reply_markup(),
        )
        return

    _WITHDRAW_SESSIONS[user_id] = {"step": "token", "tokens": tokens}
    _send_fixed_reply(
        gateway,
        source,
        _format_withdraw_tokens(tokens),
        reply_markup=_withdraw_reply_keyboard(tokens),
    )


def _handle_telegram_withdraw_wizard_message(*, event, source, gateway):
    identity = consume_gateway_identity() or _identity_from_telegram_source(source)
    if identity is None:
        return None
    user_id = str(identity.user_id)
    session = _WITHDRAW_SESSIONS.get(user_id)
    if not session:
        return None

    text = str(getattr(event, "text", "") or "").strip()
    if _normalize_button_text(text) == "back":
        _WITHDRAW_SESSIONS.pop(user_id, None)
        _send_fixed_reply(
            gateway,
            source,
            "Withdrawal cancelled.",
            reply_markup=_telegram_main_menu_reply_markup(),
        )
        return dict(_SKIP_RESULT)

    step = str(session.get("step") or "")
    if step == "token":
        tokens = _normalize_withdraw_tokens(session.get("tokens"))
        try:
            index = int(text)
        except ValueError:
            _send_fixed_reply(gateway, source, "Reply with a token number.")
            return dict(_SKIP_RESULT)
        if index < 1 or index > len(tokens):
            _send_fixed_reply(gateway, source, "Reply with a valid token number.")
            return dict(_SKIP_RESULT)
        token = tokens[index - 1]
        session.update({"step": "amount", "token": token})
        _send_fixed_reply(
            gateway,
            source,
            f"How much {token['symbol']} do you want to withdraw?\n"
            f"Available: {token['balance']} {token['symbol']}",
        )
        return dict(_SKIP_RESULT)

    if step == "amount":
        token = session.get("token") if isinstance(session.get("token"), dict) else {}
        amount = _parse_positive_decimal_text(text)
        if amount is None:
            _send_fixed_reply(gateway, source, "Send a positive amount.")
            return dict(_SKIP_RESULT)
        if Decimal(amount) > Decimal(str(token.get("balance") or "0")):
            _send_fixed_reply(gateway, source, "Amount exceeds your token balance.")
            return dict(_SKIP_RESULT)
        session.update({"step": "address", "amount": amount})
        _send_fixed_reply(gateway, source, "Send the Base address to receive the tokens.")
        return dict(_SKIP_RESULT)

    if step == "address":
        if re.fullmatch(r"0x[a-fA-F0-9]{40}", text) is None:
            _send_fixed_reply(gateway, source, "Send a valid Base address.")
            return dict(_SKIP_RESULT)
        token = session.get("token") if isinstance(session.get("token"), dict) else {}
        amount = str(session.get("amount") or "")
        _WITHDRAW_SESSIONS.pop(user_id, None)
        _send_fixed_reply(gateway, source, _TELEGRAM_WITHDRAW_STARTED_MESSAGE)
        _run_in_background(
            lambda: _execute_telegram_withdraw_request(
                token_address=str(token.get("contractAddress") or ""),
                amount=amount,
                to_address=text,
                identity=identity,
                source=source,
                gateway=gateway,
            )
        )
        return dict(_SKIP_RESULT)

    _WITHDRAW_SESSIONS.pop(user_id, None)
    return None


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


def _normalize_withdraw_tokens(raw_tokens) -> list[dict]:
    tokens: list[dict] = []
    if not isinstance(raw_tokens, list):
        return tokens
    for raw in raw_tokens:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "ERC20").strip()[:16] or "ERC20"
        contract = str(raw.get("contractAddress") or "").strip()
        balance = str(raw.get("balance") or "0").strip()
        decimals = raw.get("decimals")
        if re.fullmatch(r"0x[a-fA-F0-9]{40}", contract) is None:
            continue
        if isinstance(decimals, bool) or not isinstance(decimals, int):
            continue
        if decimals < 0 or decimals > 36:
            continue
        if _parse_positive_decimal_text(balance) is None:
            continue
        tokens.append(
            {
                "symbol": symbol,
                "contractAddress": contract,
                "balance": balance,
                "decimals": decimals,
                "verified": bool(raw.get("verified")),
            }
        )
    return tokens


def _format_withdraw_tokens(tokens: list[dict]) -> str:
    lines = ["Choose a token to withdraw:"]
    for index, token in enumerate(tokens, start=1):
        symbol = str(token.get("symbol") or "ERC20")
        balance = str(token.get("balance") or "0")
        line = f"{index}. {symbol}: {balance}"
        if not token.get("verified"):
            line += f" ({_short_address(str(token.get('contractAddress') or ''))})"
        lines.append(line)
    lines.append("")
    lines.append("Reply with a number.")
    return "\n".join(lines)


def _withdraw_reply_keyboard(tokens: list[dict]) -> dict:
    labels = [str(index) for index in range(1, min(len(tokens), 8) + 1)]
    rows = [tuple(labels[index : index + 4]) for index in range(0, len(labels), 4)]
    rows.append(("Back",))
    return _reply_keyboard(rows, placeholder="Choose token")


def _parse_positive_decimal_text(text: str) -> str | None:
    value = str(text or "").strip().replace(",", ".")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return format(parsed, "f").rstrip("0").rstrip(".") if "." in format(parsed, "f") else format(parsed, "f")


def _short_address(address: str) -> str:
    text = str(address or "")
    if len(text) < 12:
        return text
    return f"{text[:8]}...{text[-4:]}"


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


def _execute_telegram_withdraw_request(
    *,
    token_address: str,
    amount: str,
    to_address: str,
    identity: TelegramIdentity,
    source,
    gateway,
) -> None:
    try:
        client = _client_factory()
        token = _user_access_token(client, identity)
        text = client.execute_withdrawal(
            identity,
            token_address=token_address,
            amount=amount,
            to_address=to_address,
            user_access_token=token,
        )
        _send_fixed_reply(gateway, source, text)
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 Telegram withdraw failure error=%s",
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _UNEXPECTED_ERROR_MESSAGE)


def _execute_telegram_bitrefill_request(
    *,
    product_id: str,
    package_id: str,
    country: str,
    recipient: dict | None = None,
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
            recipient=dict(recipient or {}),
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


def _send_fixed_reply(gateway, source, text: str, *, reply_markup: dict | None = None) -> None:
    if _is_telegram_source(source) and _send_telegram_reply_direct(
        source,
        text,
        reply_markup=reply_markup,
    ):
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


def _send_telegram_reply_direct(
    source,
    text: str,
    *,
    reply_markup: dict | None = None,
) -> bool:
    token = _telegram_bot_token()
    chat_id = str(getattr(source, "chat_id", "") or getattr(source, "user_id", "") or "")
    if not token or not chat_id:
        return False

    try:
        for chunk in _telegram_message_chunks(text):
            payload_fields = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            }
            if reply_markup is not None:
                payload_fields["reply_markup"] = json.dumps(
                    reply_markup,
                    separators=(",", ":"),
                )
            payload = urlencode(payload_fields).encode("utf-8")
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


def _telegram_main_menu_reply_markup() -> dict:
    return _reply_keyboard(_TELEGRAM_MAIN_MENU_BUTTONS, placeholder="Choose a Sign402 action")


def _reply_keyboard(
    rows: tuple[tuple[str, ...], ...],
    *,
    placeholder: str = "Choose an action",
) -> dict:
    return {
        "keyboard": [
            [{"text": label} for label in row]
            for row in rows
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": placeholder,
    }


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
    if text.startswith("/"):
        command = text[1:].split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    else:
        button_command = _TELEGRAM_BUTTON_COMMANDS.get(_normalize_button_text(text))
        if button_command is None:
            return None
        command = button_command
    normalized = command.strip().lower().replace("_", "-")
    if normalized in {
        "start",
        "help",
        "wallet",
        "balance",
        "last-purchase",
        "limits",
        "set-limits",
        "withdraw",
        "connect-imessage",
        "bitrefill",
        "llm-buy",
        "llm-terms",
        "llm-code",
        "llm-credits",
    }:
        return normalized
    return None


def _normalize_button_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower().replace("_", " "))


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


def _parse_llm_buy_args(raw_args: str) -> tuple[str, str, str] | None:
    args = str(raw_args or "").strip().split()
    if len(args) not in {2, 3}:
        return None
    amount_text, email = args[0], args[1]
    token = args[2] if len(args) == 3 else ""
    if token and not re.fullmatch(r"[A-Za-z0-9]{2,12}|0x[a-fA-F0-9]{40}", token):
        return None
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
    return amount_text, email, token


def _llm_operation_payload(
    operation: str,
    raw_args: str,
) -> dict[str, str] | None:
    if operation == "start":
        parsed = _parse_llm_buy_args(raw_args)
        if parsed is None:
            return None
        amount, email, token = parsed
        payload = {"amountUsd": amount, "email": email}
        if token:
            payload["paymentToken"] = token
        return payload
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
    ctx.register_command(
        "help",
        handler=_build_help_handler(),
        description="Show Sign402 commands",
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
