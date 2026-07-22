"""Hermes plugin for trusted Sign402 Telegram wallet and iMessage approval commands."""

from __future__ import annotations

import asyncio
import base64
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
_SIGN402_TELEGRAM_ALLOWED_USERS_ENV = "SIGN402_TELEGRAM_ALLOWED_USERS"
_TELEGRAM_SEND_TIMEOUT_SECONDS = 15
_TELEGRAM_COMMAND_MENU_TIMEOUT_SECONDS = 10
_TELEGRAM_COMMAND_MENU_REFRESH_DELAYS_SECONDS = (0, 2, 8)
_TELEGRAM_MESSAGE_CHUNK_SIZE = 3900
_PHOTON_MAX_RESPONSE_BYTES = 256 * 1024
_USER_ACCESS_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
_USER_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 5 * 60
_USER_ACCESS_TOKEN_CACHE_MAX_USERS = 4096
_TELEGRAM_OPERATION_MAX_USERS = 4096
_TELEGRAM_PAID_TOOL_STARTED_MESSAGE = (
    "Sign402 purchase started. Approve it in your selected approval channel; "
    "I'll post the result here."
)
_TELEGRAM_BITREFILL_STARTED_MESSAGE = (
    "Bitrefill purchase started. Approve it in your selected approval channel; "
    "I'll post the result here."
)
_TELEGRAM_LLM_STARTED_MESSAGE = (
    "Bankr LLM purchase started. Approve it in your selected approval channel; "
    "I'll post the result here."
)
_TELEGRAM_WITHDRAW_STARTED_MESSAGE = (
    "Withdrawal started. Approve it in your selected approval channel; "
    "I'll post the result here."
)
_TELEGRAM_PUBLIC_COMMAND_STARTED_MESSAGES = {
    "start": "Loading wallet…",
    "wallet": "Loading wallet…",
    "balance": "Checking balance…",
    "last-purchase": "Loading last purchase…",
    "limits": "Loading spending limits…",
    "set-limits": "Updating spending limits…",
    "connect-imessage": "Loading approval settings…",
    "connect-whatsapp": "Loading approval settings…",
    "llm-buy": "Preparing LLM credits…",
    "llm-terms": "Updating LLM terms…",
    "llm-credits": "Checking LLM credits…",
}
_TELEGRAM_PUBLIC_COMMAND_MENU = (
    {"command": "start", "description": "Set up your Sign402 wallet"},
    {"command": "help", "description": "Show Sign402 commands"},
    {"command": "wallet", "description": "Show or create your Base wallet"},
    {"command": "balance", "description": "Show wallet balances"},
    {"command": "connect_imessage", "description": "Select or link iMessage approvals"},
    {"command": "connect_whatsapp", "description": "Select or link WhatsApp approvals"},
    {"command": "limits", "description": "Show or set spending limits"},
    {"command": "withdraw", "description": "Withdraw Base assets"},
    {"command": "bitrefill", "description": "Buy Bitrefill with a wallet token"},
    {"command": "last_purchase", "description": "Reveal latest purchase"},
    {"command": "llm_buy", "description": "Buy Bankr LLM credits"},
    {"command": "llm_credits", "description": "Show Bankr LLM credits"},
)
_TELEGRAM_MAIN_MENU_BUTTONS = (
    ("Wallet", "Balance"),
    ("Connect iMessage", "Connect WhatsApp"),
    ("Limits",),
    ("Withdraw",),
    ("Buy Bitrefill", "Buy LLM Credits"),
    ("Last Purchase", "Help"),
)
_TELEGRAM_BUTTON_COMMANDS = {
    "wallet": "wallet",
    "balance": "balance",
    "connect imessage": "connect-imessage",
    "connect whatsapp": "connect-whatsapp",
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
    "connect-whatsapp": (
        "connect-whatsapp",
        "Link your WhatsApp number for Sign402 approvals",
    ),
}
_IMESSAGE_PUBLIC_LINE_ENV_NAMES = (
    "SIGN402_IMESSAGE_PUBLIC_LINE",
    "SIGN402_IMESSAGE_PUBLIC_NUMBER",
    "PHOTON_PUBLIC_IMESSAGE_LINE",
)
_WHATSAPP_PUBLIC_LINE_ENV_NAMES = (
    "SIGN402_WHATSAPP_PUBLIC_LINE",
    "WHATSAPP_CLOUD_PUBLIC_LINE",
)
_PHOTON_PROJECT_ID_ENV_NAMES = ("PHOTON_PROJECT_ID", "SPECTRUM_PROJECT_ID")
_PHOTON_PROJECT_SECRET_ENV_NAMES = ("PHOTON_PROJECT_SECRET", "SPECTRUM_PROJECT_SECRET")
_PHOTON_AUTO_REGISTER_ENV_NAMES = (
    "SIGN402_PHOTON_AUTO_REGISTER_USERS",
    "PHOTON_AUTO_REGISTER_USERS",
)
_PHOTON_API_BASE_URL_ENV_NAMES = ("PHOTON_API_BASE_URL", "SPECTRUM_API_BASE_URL")
_PHOTON_API_TIMEOUT_SECONDS = 15
_PHOTON_REGISTRATION_WINDOW_SECONDS = 60 * 60
_PHOTON_REGISTRATION_MAX_ATTEMPTS_PER_USER = 3
_PHOTON_REGISTRATION_MAX_ATTEMPTS_GLOBAL = 120
_LIMITS_USAGE = "Usage: /limits 0.005 0.05 or /set_limits 0.005 0.05"
_BITREFILL_USAGE = "Usage: /bitrefill <productId> <packageId> <country> <token>"
_LLM_BUY_USAGE = "Usage: /llm_buy <usd> <email> [token]"
_LLM_TERMS_USAGE = "Usage: /llm_terms accept"
_LLM_CODE_USAGE = "Usage: /llm_code <six-digit code>"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_client_factory: Callable[[], GatewayClient] = GatewayClient.from_env
_telegram_api_opener: Callable[..., object] = urlopen
_photon_api_opener: Callable[..., object] = urlopen
_background_runner: Callable[[Callable[[], None]], None]
_sleep: Callable[[float], None] = time.sleep
_BITREFILL_USER_COUNTRIES: dict[str, str] = {}
_BITREFILL_SESSIONS: dict[str, dict] = {}
_WITHDRAW_SESSIONS: dict[str, dict] = {}
_IMESSAGE_CONNECT_SESSIONS: dict[str, dict] = {}
_PHOTON_USER_PHONE_CACHE: dict[str, str] = {}
_PHOTON_REGISTRATION_ATTEMPTS_BY_USER: dict[str, list[float]] = {}
_PHOTON_REGISTRATION_ATTEMPTS_GLOBAL: list[float] = []
_PHOTON_REGISTRATION_ATTEMPTS_LOCK = threading.RLock()
_TELEGRAM_OPERATION_GENERATIONS: dict[str, int] = {}
_TELEGRAM_ACTIVE_OPERATIONS: dict[str, tuple[int, str]] = {}
_TELEGRAM_OPERATION_LOCK = threading.RLock()


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
            if operation == "create-wallet":
                return await asyncio.to_thread(_create_wallet_text, client, identity)
            # The bootstrap token is never used as a substitute for a user token.
            token = _user_access_token(client, identity)
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


def _select_existing_approval_channel(
    client: GatewayClient,
    identity: TelegramIdentity,
    channel: str,
) -> str | None:
    result = client.execute_approval(
        "select-existing",
        {"telegramUserId": identity.user_id, "channel": channel},
    )
    if result.get("selected") is True:
        text = result.get("telegramText")
        if not isinstance(text, str) or not text.strip():
            raise GatewayClientError(_IMESSAGE_UNEXPECTED_ERROR_MESSAGE)
        return text.strip()
    if result.get("requiresPairing") is True:
        return None
    raise GatewayClientError(_IMESSAGE_UNEXPECTED_ERROR_MESSAGE)


def _build_imessage_handler(operation: str):
    async def handler(_raw_args: str) -> str:
        identity = consume_gateway_identity()
        if identity is None:
            return _TELEGRAM_ONLY_MESSAGE
        try:
            client = _client_factory()
            channel = "whatsapp" if operation == "connect-whatsapp" else "imessage"
            selected_text = await asyncio.to_thread(
                _select_existing_approval_channel,
                client,
                identity,
                channel,
            )
            if selected_text is not None:
                return selected_text
            payload = {"telegramUserId": identity.user_id}
            if channel == "whatsapp":
                payload["channel"] = "whatsapp"
            result = await asyncio.to_thread(
                client.execute_approval,
                "connect-imessage",
                payload,
            )
            telegram_text = result.get("telegramText")
            if isinstance(telegram_text, str) and telegram_text.strip():
                return _telegram_imessage_pairing_text(
                    telegram_text,
                    public_line=_approval_public_line(channel),
                    channel=channel,
                )
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
            wallet_text = await asyncio.to_thread(_create_wallet_text, client, identity)
            return _start_text(wallet_text, support_id=identity.user_id)
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
                user_access_token=_user_access_token(client, identity),
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
        product_id, package_id, country, token_selector = parsed
        try:
            client = _client_factory()
            token = _user_access_token(client, identity)
            payment_token = _load_bitrefill_payment_token(
                client,
                identity,
                token_selector,
                user_access_token=token,
            )
            return await asyncio.to_thread(
                client.execute_bitrefill_purchase,
                identity,
                product_id=product_id,
                package_id=package_id,
                country=country,
                recipient={},
                payment_token=payment_token,
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


def _start_text(wallet_text: str, *, support_id: str = "") -> str:
    support = str(support_id or "").strip()
    support_line = f"Support ID: {support}\n\n" if support else ""
    return (
        "Welcome to Sign402.\n\n"
        f"{wallet_text.strip()}\n\n"
        f"{support_line}"
        "Next steps:\n"
        "1. Wallet - fund this Base wallet with ETH for gas and USDC/SINGIT for payments.\n"
        "2. Balance - check ETH, USDC, and SINGIT.\n"
        "3. Connect iMessage or Connect WhatsApp - link approvals for your phone number.\n"
        "4. Limits - review or set spending limits.\n"
        "5. Buy - use the buttons or send a request like: buy crypto news"
    )


def _help_text() -> str:
    return (
        "Sign402 commands\n\n"
        "/wallet - Create or show your Base wallet\n"
        "/balance - Show ETH, USDC, and SINGIT balances\n"
        "/connect_imessage - Select or link iMessage approvals\n"
        "/connect_whatsapp - Select or link WhatsApp approvals\n"
        "/limits - View or set spending limits\n"
        "/bitrefill <product> <amount> <country> <token> - Buy with a wallet token\n"
        "/last_purchase - Reveal your latest purchase\n"
        "/llm_buy <usd> <email> - Buy Bankr LLM credits\n"
        "/llm_credits - Show Bankr LLM credits"
    )


def _telegram_imessage_pairing_text(
    raw_text: str,
    *,
    public_line: str | None = None,
    channel: str = "imessage",
) -> str:
    text = str(raw_text or "").strip()
    line = str(public_line or "").strip() or _imessage_public_line()
    if not line or not text:
        return text
    if line in text:
        return text
    channel_label = _approval_channel_label(channel)
    return (
        f"To link {channel_label} approvals, send the code below to the Sign402 {channel_label} line:\n"
        f"{line}\n\n"
        f"{text}"
    )


def _imessage_public_line() -> str:
    for name in _IMESSAGE_PUBLIC_LINE_ENV_NAMES:
        value = str(os.environ.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _approval_public_line(channel: str) -> str:
    if str(channel or "").strip().lower() == "whatsapp":
        return _env_first(_WHATSAPP_PUBLIC_LINE_ENV_NAMES)
    return _imessage_public_line()


def _approval_channel_label(channel: str) -> str:
    return "WhatsApp" if str(channel or "").strip().lower() == "whatsapp" else "iMessage"


def _photon_auto_register_users_enabled() -> bool:
    return any(_truthy_env(name) for name in _PHOTON_AUTO_REGISTER_ENV_NAMES)


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_first(names: tuple[str, ...]) -> str:
    for name in names:
        value = str(os.environ.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _photon_api_base_url() -> str:
    return _env_first(_PHOTON_API_BASE_URL_ENV_NAMES) or "https://spectrum.photon.codes"


def _photon_project_credentials() -> tuple[str, str]:
    project_id = _env_first(_PHOTON_PROJECT_ID_ENV_NAMES)
    project_secret = _env_first(_PHOTON_PROJECT_SECRET_ENV_NAMES)
    if not project_id or not project_secret:
        raise GatewayClientError(
            "iMessage registration is not configured. Please contact the operator."
        )
    return project_id, project_secret


def _photon_auth_headers(project_id: str, project_secret: str) -> dict[str, str]:
    basic = base64.b64encode(f"{project_id}:{project_secret}".encode("utf-8")).decode(
        "ascii"
    )
    return {
        "Authorization": f"Basic {basic}",
        "Accept": "application/json",
        "User-Agent": "Sign402-Hermes/0.1",
    }


def _read_photon_json_response(response) -> dict:
    try:
        body = response.read(_PHOTON_MAX_RESPONSE_BYTES + 1)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if len(body) > _PHOTON_MAX_RESPONSE_BYTES:
        raise GatewayClientError(
            "Could not reach the iMessage registration service. Please try again."
        )
    try:
        parsed = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
        raise GatewayClientError(
            "Could not reach the iMessage registration service. Please try again."
        ) from exc
    if not isinstance(parsed, dict):
        raise GatewayClientError(
            "Could not reach the iMessage registration service. Please try again."
        )
    return parsed


def _is_e164_phone_number(value: str) -> bool:
    # Keep this aligned with sign402_gateway.imessage_approvals.normalize_e164:
    # E.164 numbers have 8 to 15 digits in the approval flow.
    return re.fullmatch(r"\+[1-9]\d{7,14}", str(value or "").strip()) is not None


def _reserve_photon_registration_attempt(identity: TelegramIdentity) -> None:
    """Bound shared-number provisioning in the public Telegram beta.

    Photon registration creates an external Project User. This in-memory guard
    is intentionally applied before that API call, counts failed attempts too,
    and does not restrict ordinary wallet or approval operations.
    """
    now = time.monotonic()
    cutoff = now - _PHOTON_REGISTRATION_WINDOW_SECONDS
    user_id = str(identity.user_id)
    with _PHOTON_REGISTRATION_ATTEMPTS_LOCK:
        _PHOTON_REGISTRATION_ATTEMPTS_GLOBAL[:] = [
            timestamp
            for timestamp in _PHOTON_REGISTRATION_ATTEMPTS_GLOBAL
            if timestamp > cutoff
        ]
        for tracked_user_id, timestamps in tuple(
            _PHOTON_REGISTRATION_ATTEMPTS_BY_USER.items()
        ):
            recent = [timestamp for timestamp in timestamps if timestamp > cutoff]
            if recent:
                _PHOTON_REGISTRATION_ATTEMPTS_BY_USER[tracked_user_id] = recent
            else:
                _PHOTON_REGISTRATION_ATTEMPTS_BY_USER.pop(tracked_user_id, None)

        user_attempts = _PHOTON_REGISTRATION_ATTEMPTS_BY_USER.get(user_id, [])
        if len(user_attempts) >= _PHOTON_REGISTRATION_MAX_ATTEMPTS_PER_USER:
            raise GatewayClientError(
                "Too many iMessage registration attempts. Please try again in an hour."
            )
        if (
            len(_PHOTON_REGISTRATION_ATTEMPTS_GLOBAL)
            >= _PHOTON_REGISTRATION_MAX_ATTEMPTS_GLOBAL
        ):
            raise GatewayClientError(
                "iMessage registration is busy. Please try again later."
            )

        user_attempts.append(now)
        _PHOTON_REGISTRATION_ATTEMPTS_BY_USER[user_id] = user_attempts
        _PHOTON_REGISTRATION_ATTEMPTS_GLOBAL.append(now)


def _imessage_phone_prompt(*, channel: str = "imessage") -> str:
    # Shared Photon registrations receive an assigned line per user. Showing a
    # static public line before that assignment caused people to text the wrong
    # number and made onboarding look broken.
    public_line = "" if _photon_auto_register_users_enabled() else _imessage_public_line()
    channel_label = _approval_channel_label(channel)
    target = f"\n\nSign402 {channel_label} line: {public_line}" if public_line else ""
    assignment_note = (
        "\n\nAfter you send it, Sign402 will show your private pairing line and code."
        if _photon_auto_register_users_enabled()
        else ""
    )
    extra = (
        "\n\nMake sure iMessage starts new conversations from this phone number, not your Apple ID email."
        if channel_label == "iMessage"
        else ""
    )
    return (
        f"Send your {channel_label} phone number in international format.\n"
        "Example: +420773173967"
        f"{extra}"
        f"{assignment_note}"
        f"{target}"
    )


def _register_photon_shared_user(phone_number: str, identity: TelegramIdentity) -> str:
    project_id, project_secret = _photon_project_credentials()

    payload = {
        "type": "shared",
        "phoneNumber": phone_number,
    }

    base_url = _photon_api_base_url().rstrip("/")
    request = Request(
        f"{base_url}/projects/{project_id}/users/",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            **_photon_auth_headers(project_id, project_secret),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        response = _photon_api_opener(request, timeout=_PHOTON_API_TIMEOUT_SECONDS)
    except (HTTPError, TimeoutError, URLError, OSError) as exc:
        raise GatewayClientError(
            "Could not register this iMessage number. Please check the phone number and try again."
        ) from exc
    parsed = _read_photon_json_response(response)
    if not isinstance(parsed, dict) or parsed.get("succeed") is not True:
        raise GatewayClientError(
            "Could not register this iMessage number. Please check the phone number and try again."
        )
    data = parsed.get("data")
    if isinstance(data, dict):
        user_id = str(data.get("id") or "").strip()
        phone = str(data.get("phoneNumber") or "").strip()
        if user_id and phone:
            _PHOTON_USER_PHONE_CACHE[user_id] = phone
        assigned_phone_number = str(data.get("assignedPhoneNumber") or "").strip()
        if _is_e164_phone_number(assigned_phone_number):
            return assigned_phone_number
    return ""


def _resolve_photon_sender_id(photon_user_id: str) -> str:
    raw_id = str(photon_user_id or "").strip()
    if not raw_id or _is_e164_phone_number(raw_id):
        return raw_id
    cached = _PHOTON_USER_PHONE_CACHE.get(raw_id)
    if cached:
        return cached

    project_id, project_secret = _photon_project_credentials()
    base_url = _photon_api_base_url().rstrip("/")
    request = Request(
        f"{base_url}/projects/{project_id}/users/{raw_id}/",
        headers=_photon_auth_headers(project_id, project_secret),
        method="GET",
    )
    try:
        response = _photon_api_opener(request, timeout=_PHOTON_API_TIMEOUT_SECONDS)
    except (HTTPError, TimeoutError, URLError, OSError) as exc:
        raise GatewayClientError(
            "Could not identify this iMessage sender. Please try connecting iMessage again."
        ) from exc
    parsed = _read_photon_json_response(response)
    data = parsed.get("data")
    phone = str(data.get("phoneNumber") or "").strip() if isinstance(data, dict) else ""
    if not _is_e164_phone_number(phone):
        raise GatewayClientError(
            "Could not identify this iMessage sender. Please try connecting iMessage again."
        )
    _PHOTON_USER_PHONE_CACHE[raw_id] = phone
    return phone


def _connect_imessage_after_phone_registration(
    *,
    identity: TelegramIdentity,
    phone_number: str,
    channel: str = "imessage",
) -> str:
    _reserve_photon_registration_attempt(identity)
    assigned_phone_number = _register_photon_shared_user(phone_number, identity)
    if not _is_e164_phone_number(assigned_phone_number):
        raise GatewayClientError(
            "iMessage registration could not assign a private line. Please try again."
        )
    client = _client_factory()
    payload = {"telegramUserId": identity.user_id}
    if channel != "imessage":
        payload["channel"] = channel
    result = client.execute_imessage(
        "connect-imessage",
        payload,
    )
    text = result.get("telegramText")
    if not isinstance(text, str) or not text.strip():
        return _IMESSAGE_UNEXPECTED_ERROR_MESSAGE
    return _telegram_imessage_pairing_text(
        text,
        public_line=assigned_phone_number,
        channel=channel,
    )


def _build_help_handler():
    async def handler(_raw_args: str) -> str:
        return _help_text()

    return handler


def handle_pre_gateway_dispatch(*, event, gateway=None, **kwargs):
    """Fail closed before Hermes can dispatch a Sign402 platform message."""

    source = None
    platform_name = "unknown"
    is_telegram = False
    try:
        source = getattr(event, "source", None)
        platform_name = _platform_name(source)
        is_telegram = platform_name == "telegram"
        if is_telegram and not _sign402_telegram_user_authorized(source):
            # This hook runs before Hermes' general authorization/agent dispatch.
            # Silently drop callers outside the Sign402-specific policy so a broad
            # Hermes setting can never turn them into general-agent users.
            return dict(_SKIP_RESULT)
        return _handle_pre_gateway_dispatch(event=event, gateway=gateway, **kwargs)
    except Exception as exc:
        logger.warning(
            "Sign402 pre-dispatch failed closed platform=%s error=%s",
            platform_name,
            type(exc).__name__,
        )
        if is_telegram:
            try:
                _send_fixed_reply(gateway, source, _UNEXPECTED_ERROR_MESSAGE)
            except Exception:
                logger.warning("Sign402 safe Telegram error reply failed")
        # Never hand an unexpected platform event back to Hermes' general
        # dispatcher. It could otherwise reach an unrelated agent/tool path.
        return dict(_SKIP_RESULT)


def _handle_pre_gateway_dispatch(*, event, gateway=None, **kwargs):
    """Capture trusted identities and consume configured approval messages."""

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

    imessage_registration_result = _handle_telegram_imessage_registration_message(
        event=event,
        source=source,
        gateway=gateway,
    )
    if imessage_registration_result:
        return imessage_registration_result

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

    navigation_result = _handle_telegram_global_navigation_message(
        event=event,
        source=source,
        gateway=gateway,
    )
    if navigation_result:
        return navigation_result

    telegram_tool = _telegram_paid_tool_intent(event, source)
    if telegram_tool:
        return _handle_telegram_paid_tool_request(
            tool=telegram_tool,
            source=source,
            gateway=gateway,
        )

    sign402_only_result = _handle_telegram_sign402_only_fallback(
        event=event,
        source=source,
        gateway=gateway,
    )
    if sign402_only_result:
        return sign402_only_result

    if _is_whatsapp_cloud_source(source):
        return _handle_whatsapp_cloud_event(
            event=event,
            source=source,
            gateway=gateway,
        )

    # The unofficial/personal WhatsApp adapter is not an approval provider.
    if _is_unconfigured_whatsapp_source(source):
        return dict(_SKIP_RESULT)

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

    # This shared Photon/iMessage line is an approval channel, not a public
    # Hermes conversation surface. Never let arbitrary iMessage text reach the
    # general agent and consume its tools or model credits.
    return dict(_SKIP_RESULT)


def _handle_telegram_sign402_only_fallback(*, event, source, gateway):
    if not _sign402_telegram_only_mode_enabled() or not _is_telegram_source(source):
        return None
    text = str(getattr(event, "text", "") or "").strip()
    if not text:
        return None
    _send_fixed_reply(
        gateway,
        source,
        _sign402_only_fallback_text(),
        reply_markup=_telegram_main_menu_reply_markup(),
    )
    return dict(_SKIP_RESULT)


def _sign402_telegram_only_mode_enabled() -> bool:
    explicitly_enabled = str(
        os.environ.get("SIGN402_TELEGRAM_SIGN402_ONLY", "") or ""
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if explicitly_enabled:
        return True
    # A wildcard makes this a public Sign402 bot. Keep it Sign402-only even
    # when an operator accidentally omits the companion mode flag; otherwise
    # ordinary text could fall through to Hermes' general agent.
    configured = os.environ.get(_SIGN402_TELEGRAM_ALLOWED_USERS_ENV)
    allowed_users = {
        value.strip()
        for value in str(configured or "").split(",")
        if value.strip()
    }
    return "*" in allowed_users


def _sign402_telegram_user_authorized(source) -> bool:
    """Apply an explicit public-beta policy before Hermes can dispatch a DM.

    `pre_gateway_dispatch` runs before Hermes' own allowlist. In public beta,
    this plugin therefore owns the access policy for Sign402 while the Hermes
    allowlist can remain restricted to the operator.
    """
    identity = _identity_from_telegram_source(source)
    if identity is None:
        return False

    configured = os.environ.get(_SIGN402_TELEGRAM_ALLOWED_USERS_ENV)
    if configured is None:
        configured = os.environ.get("TELEGRAM_ALLOWED_USERS", "")

    allowed_users = {
        value.strip()
        for value in str(configured or "").split(",")
        if value.strip()
    }
    return "*" in allowed_users or str(identity.user_id) in allowed_users


def _sign402_only_fallback_text() -> str:
    return (
        "Use the Sign402 menu: Wallet, Balance, Buy Bitrefill, Limits, or Withdraw."
    )


def _handle_telegram_global_navigation_message(*, event, source, gateway):
    identity = _identity_from_telegram_source(source)
    if identity is None:
        return None
    text = str(getattr(event, "text", "") or "").strip()
    if _normalize_button_text(text) != "back":
        return None
    _invalidate_telegram_operation(str(identity.user_id))
    _BITREFILL_SESSIONS.pop(str(identity.user_id), None)
    _WITHDRAW_SESSIONS.pop(str(identity.user_id), None)
    _IMESSAGE_CONNECT_SESSIONS.pop(str(identity.user_id), None)
    _send_fixed_reply(
        gateway,
        source,
        "Back to Sign402 main menu.",
        reply_markup=_telegram_main_menu_reply_markup(),
    )
    return dict(_SKIP_RESULT)


def _handle_telegram_imessage_registration_message(*, event, source, gateway):
    identity = _identity_from_telegram_source(source)
    if identity is None:
        return None
    user_id = str(identity.user_id)
    session = _IMESSAGE_CONNECT_SESSIONS.get(user_id)
    if not session or session.get("stage") != "awaiting-phone":
        return None
    channel = str(session.get("channel") or "imessage")

    text = str(getattr(event, "text", "") or "").strip()
    if not text:
        return None
    if _normalize_button_text(text) == "back":
        _IMESSAGE_CONNECT_SESSIONS.pop(user_id, None)
        _send_fixed_reply(
            gateway,
            source,
            "Back to Sign402 main menu.",
            reply_markup=_telegram_main_menu_reply_markup(),
        )
        return dict(_SKIP_RESULT)
    if not _is_e164_phone_number(text):
        _send_fixed_reply(
            gateway,
            source,
            _imessage_phone_prompt(channel=channel),
            reply_markup=_reply_keyboard((("Back",),)),
        )
        return dict(_SKIP_RESULT)

    try:
        _IMESSAGE_CONNECT_SESSIONS.pop(user_id, None)
        reply = _connect_imessage_after_phone_registration(
            identity=identity,
            phone_number=text,
            channel=channel,
        )
        _send_fixed_reply(
            gateway,
            source,
            reply,
            reply_markup=_telegram_main_menu_reply_markup(),
        )
        return dict(_SKIP_RESULT)
    except GatewayClientError as exc:
        _send_fixed_reply(gateway, source, exc.user_message)
        return dict(_SKIP_RESULT)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 iMessage registration failure error=%s",
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _IMESSAGE_UNEXPECTED_ERROR_MESSAGE)
        return dict(_SKIP_RESULT)


def _start_telegram_background_operation(
    *,
    identity: TelegramIdentity,
    action: str,
    started_text: str,
    source,
    gateway,
    work: Callable[[int], tuple[str, dict | None]],
    prepare: Callable[[int], None] | None = None,
    recover: Callable[[int], None] | None = None,
) -> dict:
    user_id = str(identity.user_id)
    generation = _reserve_telegram_operation(user_id, action)
    if generation is None:
        return dict(_SKIP_RESULT)
    if prepare is not None:
        prepare(generation)
    _send_fixed_reply(gateway, source, started_text)

    def execute() -> None:
        try:
            text, reply_markup = work(generation)
        except GatewayClientError as exc:
            if recover is not None and _telegram_operation_is_current(user_id, generation):
                recover(generation)
            text, reply_markup = exc.user_message, None
        except Exception as exc:
            if recover is not None and _telegram_operation_is_current(user_id, generation):
                recover(generation)
            logger.warning(
                "Unexpected Sign402 Telegram background action failure action=%s error=%s",
                action,
                type(exc).__name__,
            )
            text, reply_markup = _UNEXPECTED_ERROR_MESSAGE, None
        if not _finish_telegram_operation(user_id, generation):
            logger.debug(
                "Discarding stale Sign402 Telegram action result action=%s user=%s",
                action,
                user_id,
            )
            return
        _send_fixed_reply(
            gateway,
            source,
            text,
            reply_markup=reply_markup,
        )

    try:
        _run_in_background(execute)
    except Exception as exc:
        _finish_telegram_operation(user_id, generation)
        logger.warning(
            "Could not schedule Sign402 Telegram action action=%s error=%s",
            action,
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _UNEXPECTED_ERROR_MESSAGE)
    return dict(_SKIP_RESULT)


def _telegram_public_command_result(
    command: str,
    args: str,
    identity: TelegramIdentity,
) -> tuple[str, dict | None]:
    client = _client_factory()
    if command == "start":
        text = _start_text(
            _create_wallet_text(client, identity),
            support_id=identity.user_id,
        )
    elif command == "wallet":
        text = _create_wallet_text(client, identity)
    elif command in {"balance", "last-purchase"}:
        text = client.execute(
            command,
            identity,
            user_access_token=_user_access_token(client, identity),
        )
    elif command in {"limits", "set-limits"}:
        parsed_limits = _parse_limit_args(command, args)
        if parsed_limits is None:
            return _LIMITS_USAGE, _telegram_main_menu_reply_markup()
        max_per_tx_usdc, daily_cap_usdc = parsed_limits
        text = client.execute_spending_limits(
            identity,
            max_per_tx_usdc=max_per_tx_usdc,
            daily_cap_usdc=daily_cap_usdc,
            user_access_token=_user_access_token(client, identity),
        )
    elif command == "connect-imessage":
        channel = "imessage"
        selected_text = _select_existing_approval_channel(
            client,
            identity,
            channel,
        )
        if selected_text is not None:
            text = selected_text
        elif _photon_auto_register_users_enabled():
            phone_number = str(args or "").strip()
            if phone_number and not _is_e164_phone_number(phone_number):
                text = _imessage_phone_prompt(channel=channel)
            elif phone_number:
                text = _connect_imessage_after_phone_registration(
                    identity=identity,
                    phone_number=phone_number,
                    channel=channel,
                )
                _IMESSAGE_CONNECT_SESSIONS.pop(str(identity.user_id), None)
            else:
                _IMESSAGE_CONNECT_SESSIONS[str(identity.user_id)] = {
                    "stage": "awaiting-phone",
                    "channel": channel,
                }
                text = _imessage_phone_prompt(channel=channel)
        else:
            result = client.execute_imessage(
                "connect-imessage",
                {"telegramUserId": identity.user_id},
            )
            text = result.get("telegramText")
            if not isinstance(text, str) or not text.strip():
                text = _IMESSAGE_UNEXPECTED_ERROR_MESSAGE
            else:
                text = _telegram_imessage_pairing_text(text, channel=channel)
    elif command == "connect-whatsapp":
        channel = "whatsapp"
        selected_text = _select_existing_approval_channel(
            client,
            identity,
            channel,
        )
        if selected_text is not None:
            text = selected_text
        else:
            result = client.execute_approval(
                "connect-imessage",
                {"telegramUserId": identity.user_id, "channel": channel},
            )
            text = result.get("telegramText")
            if not isinstance(text, str) or not text.strip():
                text = _IMESSAGE_UNEXPECTED_ERROR_MESSAGE
            else:
                text = _telegram_imessage_pairing_text(
                    text,
                    public_line=_approval_public_line(channel),
                    channel=channel,
                )
    elif command in {"llm-buy", "llm-terms", "llm-credits"}:
        operation = {
            "llm-buy": "start",
            "llm-terms": "accept-terms",
            "llm-credits": "credits",
        }[command]
        payload = _llm_operation_payload(operation, args)
        if payload is None:
            return _llm_usage(operation), _telegram_main_menu_reply_markup()
        result = client.execute_llm(
            operation,
            identity,
            payload=payload,
            user_access_token=_user_access_token(client, identity),
        )
        text = _llm_result_text(result)
    else:
        raise ValueError("unsupported Telegram background command")
    return str(text), _telegram_main_menu_reply_markup()


def _handle_telegram_public_command_request(*, command: str, args: str = "", source, gateway):
    identity = consume_gateway_identity() or _identity_from_telegram_source(source)
    if identity is None:
        _send_fixed_reply(gateway, source, _TELEGRAM_ONLY_MESSAGE)
        return dict(_SKIP_RESULT)
    if command in _TELEGRAM_PUBLIC_COMMAND_STARTED_MESSAGES:
        if command in {"limits", "set-limits"} and _parse_limit_args(command, args) is None:
            _send_fixed_reply(gateway, source, _LIMITS_USAGE)
            return dict(_SKIP_RESULT)
        if command in {"llm-buy", "llm-terms", "llm-credits"}:
            operation = {
                "llm-buy": "start",
                "llm-terms": "accept-terms",
                "llm-credits": "credits",
            }[command]
            if _llm_operation_payload(operation, args) is None:
                _send_fixed_reply(gateway, source, _llm_usage(operation))
                return dict(_SKIP_RESULT)
        return _start_telegram_background_operation(
            identity=identity,
            action=f"command:{command}",
            started_text=_TELEGRAM_PUBLIC_COMMAND_STARTED_MESSAGES[command],
            source=source,
            gateway=gateway,
            work=lambda _generation: _telegram_public_command_result(
                command,
                args,
                identity,
            ),
        )
    try:
        if command == "start":
            client = _client_factory()
            wallet_text = _create_wallet_text(client, identity)
            text = _start_text(wallet_text, support_id=identity.user_id)
        elif command == "help":
            text = _help_text()
        elif command == "wallet":
            client = _client_factory()
            text = _create_wallet_text(client, identity)
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
                    user_access_token=_user_access_token(client, identity),
                )
        elif command == "connect-imessage":
            channel = "imessage"
            client = _client_factory()
            selected_text = _select_existing_approval_channel(
                client,
                identity,
                channel,
            )
            if selected_text is not None:
                text = selected_text
            elif _photon_auto_register_users_enabled():
                phone_number = str(args or "").strip()
                if phone_number:
                    if not _is_e164_phone_number(phone_number):
                        text = _imessage_phone_prompt(channel=channel)
                    else:
                        text = _connect_imessage_after_phone_registration(
                            identity=identity,
                            phone_number=phone_number,
                            channel=channel,
                        )
                        _IMESSAGE_CONNECT_SESSIONS.pop(str(identity.user_id), None)
                else:
                    _IMESSAGE_CONNECT_SESSIONS[str(identity.user_id)] = {
                        "stage": "awaiting-phone",
                        "channel": channel,
                    }
                    text = _imessage_phone_prompt(channel=channel)
            else:
                payload = {"telegramUserId": identity.user_id}
                if channel != "imessage":
                    payload["channel"] = channel
                result = client.execute_imessage(
                    "connect-imessage",
                    payload,
                )
                text = result.get("telegramText")
                if not isinstance(text, str) or not text.strip():
                    text = _IMESSAGE_UNEXPECTED_ERROR_MESSAGE
                else:
                    text = _telegram_imessage_pairing_text(text, channel=channel)
        elif command == "connect-whatsapp":
            channel = "whatsapp"
            client = _client_factory()
            selected_text = _select_existing_approval_channel(
                client,
                identity,
                channel,
            )
            if selected_text is not None:
                text = selected_text
            else:
                result = client.execute_approval(
                    "connect-imessage",
                    {"telegramUserId": identity.user_id, "channel": channel},
                )
                text = result.get("telegramText")
                if not isinstance(text, str) or not text.strip():
                    text = _IMESSAGE_UNEXPECTED_ERROR_MESSAGE
                else:
                    text = _telegram_imessage_pairing_text(
                        text,
                        public_line=_approval_public_line(channel),
                        channel=channel,
                    )
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
            product_id, package_id, country, token_selector = parsed
            client = _client_factory()
            payment_token = _load_bitrefill_payment_token(
                client,
                identity,
                token_selector,
                user_access_token=_user_access_token(client, identity),
            )
            _send_fixed_reply(gateway, source, _TELEGRAM_BITREFILL_STARTED_MESSAGE)
            _run_in_background(
                lambda: _execute_telegram_bitrefill_request(
                    product_id=product_id,
                    package_id=package_id,
                    country=country,
                    payment_token=payment_token,
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
    normalized = _canonical_button_text(text)
    stage = str(session.get("stage") or "")
    if normalized == "back":
        _invalidate_telegram_operation(user_id)
        if stage == "loading-catalog":
            _send_bitrefill_category_prompt(
                identity=identity,
                source=source,
                gateway=gateway,
            )
            return dict(_SKIP_RESULT)
        if stage == "loading-search":
            country = _bitrefill_country(user_id)
            _BITREFILL_SESSIONS[user_id] = {
                "stage": "awaiting-search",
                "country": country,
            }
            _send_fixed_reply(
                gateway,
                source,
                f"What do you want to buy in {country}?\n\nExample: amazon, playstation, mobile",
                reply_markup=_reply_keyboard((("Change Country", "Back"),)),
            )
            return dict(_SKIP_RESULT)
        if stage in {"loading-product", "loading-payment-tokens"}:
            previous = session.get("returnSession")
            if isinstance(previous, dict):
                _BITREFILL_SESSIONS[user_id] = previous
            else:
                _open_bitrefill_menu(identity=identity, source=source, gateway=gateway)
                return dict(_SKIP_RESULT)
            previous_stage = str(previous.get("stage") or "")
            if previous_stage == "select-product" and previous.get("source") == "catalog":
                products = _normalize_bitrefill_products(previous.get("products"))
                _send_fixed_reply(
                    gateway,
                    source,
                    _format_bitrefill_catalog_page(
                        str(previous.get("country") or _bitrefill_country(user_id)),
                        str(previous.get("category") or "all"),
                        int(previous.get("start") or 0),
                        products,
                    ),
                    reply_markup=_bitrefill_catalog_reply_keyboard(
                        len(products),
                        has_previous=bool(previous.get("hasPrevious")),
                        has_next=bool(previous.get("hasNext")),
                    ),
                )
            elif previous_stage == "select-product":
                products = _normalize_bitrefill_products(previous.get("products"))
                _send_fixed_reply(
                    gateway,
                    source,
                    _format_bitrefill_search_results(
                        str(previous.get("query") or "products"),
                        str(previous.get("country") or _bitrefill_country(user_id)),
                        products,
                    ),
                    reply_markup=_numbered_reply_keyboard(len(products)),
                )
            elif previous_stage == "select-package":
                packages = _normalize_bitrefill_packages(previous.get("packages"))
                product = previous.get("product") if isinstance(previous.get("product"), dict) else {}
                _send_fixed_reply(
                    gateway,
                    source,
                    _format_bitrefill_packages(product, packages),
                    reply_markup=_numbered_reply_keyboard(len(packages)),
                )
            return dict(_SKIP_RESULT)
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
        _invalidate_telegram_operation(user_id)
        _BITREFILL_SESSIONS[user_id] = {"stage": "awaiting-country"}
        _send_bitrefill_country_prompt(gateway, source)
        return dict(_SKIP_RESULT)
    if normalized == "browse catalog":
        _invalidate_telegram_operation(user_id)
        _send_bitrefill_category_prompt(identity=identity, source=source, gateway=gateway)
        return dict(_SKIP_RESULT)
    if normalized == "search products":
        _invalidate_telegram_operation(user_id)
        country = _bitrefill_country(user_id)
        _BITREFILL_SESSIONS[user_id] = {"stage": "awaiting-search", "country": country}
        _send_fixed_reply(
            gateway,
            source,
            f"What do you want to buy in {country}?\n\nExample: amazon, playstation, mobile",
            reply_markup=_reply_keyboard((("Change Country", "Back"),)),
        )
        return dict(_SKIP_RESULT)
    if stage.startswith("loading-") or stage == "purchasing":
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
        if stage == "select-payment-token":
            return _handle_bitrefill_payment_token_choice(
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
    user_id = str(identity.user_id)
    country = _bitrefill_country(user_id)
    clean_query = str(query or "").strip()
    if len(clean_query) < 2:
        _send_fixed_reply(
            gateway,
            source,
            f"What do you want to buy in {country}?\n\nType at least 2 characters.",
            reply_markup=_reply_keyboard((("Change Country", "Back"),)),
        )
        return dict(_SKIP_RESULT)

    def prepare(generation: int) -> None:
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "loading-search",
            "country": country,
            "query": clean_query,
            "operationGeneration": generation,
        }

    def work(generation: int) -> tuple[str, dict | None]:
        client = _client_factory()
        result = client.search_bitrefill_products(
            query=clean_query,
            country=country,
            search_all_countries=True,
            include_test_products=False,
        )
        products = _normalize_bitrefill_products(result.get("products"))
        if not _telegram_operation_is_current(user_id, generation):
            return "", None
        if not products:
            _BITREFILL_SESSIONS[user_id] = {
                "stage": "awaiting-search",
                "country": country,
            }
            return (
                f"No Bitrefill products found for \"{clean_query}\" in {country}.\n\nTry another search.",
                _reply_keyboard((("Change Country", "Back"),)),
            )
        limited = products[:_BITREFILL_MAX_SEARCH_RESULTS]
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "select-product",
            "country": country,
            "query": clean_query,
            "products": limited,
        }
        return (
            _format_bitrefill_search_results(clean_query, country, limited),
            _numbered_reply_keyboard(len(limited)),
        )

    def recover(_generation: int) -> None:
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "awaiting-search",
            "country": country,
        }

    return _start_telegram_background_operation(
        identity=identity,
        action="bitrefill:search",
        started_text="Searching products…",
        source=source,
        gateway=gateway,
        work=work,
        prepare=prepare,
        recover=recover,
    )


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
    user_id = str(identity.user_id)
    country = _bitrefill_country(user_id)

    def prepare(generation: int) -> None:
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "loading-catalog",
            "source": "catalog",
            "country": country,
            "category": category,
            "start": start,
            "operationGeneration": generation,
        }

    def work(generation: int) -> tuple[str, dict | None]:
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
        if not _telegram_operation_is_current(user_id, generation):
            return "", None
        has_previous = bool(result.get("hasPrevious"))
        has_next = bool(result.get("hasNext"))
        if not products:
            _BITREFILL_SESSIONS[user_id] = {
                "stage": "select-category",
                "source": "catalog",
                "country": country,
            }
            return (
                "No products found in this category. Choose another category.",
                _reply_keyboard(_BITREFILL_CATEGORY_BUTTONS),
            )
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "select-product",
            "source": "catalog",
            "country": country,
            "category": category,
            "start": start,
            "hasPrevious": has_previous,
            "hasNext": has_next,
            "products": products,
        }
        return (
            _format_bitrefill_catalog_page(country, category, start, products),
            _bitrefill_catalog_reply_keyboard(
                len(products),
                has_previous=has_previous,
                has_next=has_next,
            ),
        )

    def recover(_generation: int) -> None:
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "select-category",
            "source": "catalog",
            "country": country,
        }

    return _start_telegram_background_operation(
        identity=identity,
        action="bitrefill:catalog",
        started_text="Loading catalog…",
        source=source,
        gateway=gateway,
        work=work,
        prepare=prepare,
        recover=recover,
    )


def _handle_bitrefill_product_choice(*, identity: TelegramIdentity, text: str, source, gateway):
    user_id = str(identity.user_id)
    session = _BITREFILL_SESSIONS.get(user_id, {})
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
    product = products[index]
    country = str(product.get("country") or session.get("country") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        country = _bitrefill_country(user_id)
    product_id = str(product.get("productId") or product.get("id") or "").strip()
    return_session = dict(session)

    def prepare(generation: int) -> None:
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "loading-product",
            "country": country,
            "productId": product_id,
            "returnSession": return_session,
            "operationGeneration": generation,
        }

    def work(generation: int) -> tuple[str, dict | None]:
        client = _client_factory()
        details = client.get_bitrefill_product(
            product_id=product_id,
            country=country,
        )
        packages = _normalize_bitrefill_packages(details.get("packages"))
        if not _telegram_operation_is_current(user_id, generation):
            return "", None
        if not packages:
            _BITREFILL_SESSIONS[user_id] = return_session
            return (
                "This Bitrefill product has no available packages right now. Try another product.",
                _reply_keyboard((("Search Products", "Back"),)),
            )
        limited_packages = packages[:_BITREFILL_MAX_PACKAGES]
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "select-package",
            "country": country,
            "product": details,
            "packages": limited_packages,
        }
        return (
            _format_bitrefill_packages(details, limited_packages),
            _numbered_reply_keyboard(len(limited_packages)),
        )

    def recover(_generation: int) -> None:
        _BITREFILL_SESSIONS[user_id] = return_session

    return _start_telegram_background_operation(
        identity=identity,
        action="bitrefill:product",
        started_text="Loading product…",
        source=source,
        gateway=gateway,
        work=work,
        prepare=prepare,
        recover=recover,
    )


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
    _open_bitrefill_payment_token_selection(
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
    _open_bitrefill_payment_token_selection(
        identity=identity,
        product=product,
        package=package,
        country=country,
        recipient=recipient,
        source=source,
        gateway=gateway,
    )
    return dict(_SKIP_RESULT)


def _open_bitrefill_payment_token_selection(
    *,
    identity: TelegramIdentity,
    product: dict,
    package: dict,
    country: str,
    recipient: dict,
    source,
    gateway,
) -> None:
    user_id = str(identity.user_id)
    return_session = dict(_BITREFILL_SESSIONS.get(user_id, {}))

    def prepare(generation: int) -> None:
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "loading-payment-tokens",
            "country": country,
            "product": product,
            "package": package,
            "recipient": dict(recipient or {}),
            "returnSession": return_session,
            "operationGeneration": generation,
        }

    def work(generation: int) -> tuple[str, dict | None]:
        client = _client_factory()
        user_access_token = _user_access_token(client, identity)
        result = client.withdraw_tokens(identity, user_access_token=user_access_token)
        tokens = _normalize_withdraw_tokens(
            result.get("tokens") if isinstance(result, dict) else []
        )
        if not _telegram_operation_is_current(user_id, generation):
            return "", None
        if not tokens:
            _BITREFILL_SESSIONS.pop(user_id, None)
            return (
                "No funded Base wallet tokens are available for this Bitrefill purchase.",
                _telegram_main_menu_reply_markup(),
            )
        _BITREFILL_SESSIONS[user_id] = {
            "stage": "select-payment-token",
            "country": country,
            "product": product,
            "package": package,
            "recipient": dict(recipient or {}),
            "paymentTokens": tokens,
        }
        return (
            _format_bitrefill_payment_tokens(tokens),
            _withdraw_reply_keyboard(tokens),
        )

    def recover(_generation: int) -> None:
        _BITREFILL_SESSIONS[user_id] = return_session

    _start_telegram_background_operation(
        identity=identity,
        action="bitrefill:payment-tokens",
        started_text="Loading payment options…",
        source=source,
        gateway=gateway,
        work=work,
        prepare=prepare,
        recover=recover,
    )


def _handle_bitrefill_payment_token_choice(
    *, identity: TelegramIdentity, text: str, source, gateway
):
    user_id = str(identity.user_id)
    session = _BITREFILL_SESSIONS.get(user_id, {})
    tokens = _normalize_withdraw_tokens(session.get("paymentTokens"))
    index = _parse_choice_index(text, len(tokens))
    if index is None:
        _send_fixed_reply(
            gateway,
            source,
            "Reply with a payment token number from the list.",
            reply_markup=_withdraw_reply_keyboard(tokens),
        )
        return dict(_SKIP_RESULT)
    product = session.get("product") if isinstance(session.get("product"), dict) else {}
    package = session.get("package") if isinstance(session.get("package"), dict) else {}
    _start_bitrefill_purchase_from_wizard(
        identity=identity,
        product=product,
        package=package,
        country=str(session.get("country") or _bitrefill_country(user_id)),
        recipient=dict(session.get("recipient") or {}),
        payment_token=tokens[index],
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
    payment_token: dict,
    source,
    gateway,
) -> None:
    user_id = str(identity.user_id)
    generation = _reserve_telegram_operation(user_id, "bitrefill:purchase")
    if generation is None:
        return
    product_id = str(product.get("productId") or product.get("id") or "").strip()
    package_id = str(package.get("packageId") or package.get("id") or "").strip()
    previous_session = dict(_BITREFILL_SESSIONS.get(user_id, {}))
    _BITREFILL_SESSIONS[user_id] = {
        "stage": "purchasing",
        "previousSession": previous_session,
        "operationGeneration": generation,
    }
    _send_fixed_reply(gateway, source, _TELEGRAM_BITREFILL_STARTED_MESSAGE)
    try:
        _run_in_background(
            lambda: _execute_telegram_bitrefill_request(
                product_id=product_id,
                package_id=package_id,
                country=country,
                recipient=recipient,
                payment_token=payment_token,
                identity=identity,
                source=source,
                gateway=gateway,
                operation_generation=generation,
            )
        )
    except Exception:
        _finish_telegram_operation(user_id, generation)
        _BITREFILL_SESSIONS[user_id] = previous_session
        raise


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
    lines = [f"Found products for \"{query}\" globally (preferred country: {country}):"]
    for index, product in enumerate(products, start=1):
        name = str(product.get("name") or "Unknown product").strip()
        category = str(product.get("category") or product.get("productType") or "").strip()
        product_country = str(product.get("country") or "").strip().upper()
        country_suffix = _bitrefill_product_country_suffix(name, product_country, country)
        suffix = f" - {category}" if category else ""
        lines.append(f"{index}. {name}{country_suffix}{suffix}")
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

    def prepare(generation: int) -> None:
        _WITHDRAW_SESSIONS[user_id] = {
            "step": "loading-tokens",
            "operationGeneration": generation,
        }

    def work(generation: int) -> tuple[str, dict | None]:
        client = _client_factory()
        token = _user_access_token(client, identity)
        result = client.withdraw_tokens(identity, user_access_token=token)
        tokens = _normalize_withdraw_tokens(
            result.get("tokens") if isinstance(result, dict) else []
        )
        if not _telegram_operation_is_current(user_id, generation):
            return "", None
        if not tokens:
            text = (
                str(result.get("telegramText") or "").strip()
                if isinstance(result, dict)
                else ""
            )
            _WITHDRAW_SESSIONS.pop(user_id, None)
            return (
                text or "No Base asset balances are available to withdraw yet.",
                _telegram_main_menu_reply_markup(),
            )
        _WITHDRAW_SESSIONS[user_id] = {"step": "token", "tokens": tokens}
        return (
            _format_withdraw_tokens(tokens),
            _withdraw_reply_keyboard(tokens),
        )

    def recover(_generation: int) -> None:
        _WITHDRAW_SESSIONS.pop(user_id, None)

    _start_telegram_background_operation(
        identity=identity,
        action="withdraw:tokens",
        started_text="Loading assets…",
        source=source,
        gateway=gateway,
        work=work,
        prepare=prepare,
        recover=recover,
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
    if _canonical_button_text(text) == "back":
        _invalidate_telegram_operation(user_id)
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
            _send_fixed_reply(gateway, source, "Reply with an asset number.")
            return dict(_SKIP_RESULT)
        if index < 1 or index > len(tokens):
            _send_fixed_reply(gateway, source, "Reply with a valid asset number.")
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
            _send_fixed_reply(gateway, source, "Amount exceeds your asset balance.")
            return dict(_SKIP_RESULT)
        session.update({"step": "address", "amount": amount})
        _send_fixed_reply(gateway, source, "Send the Base address to receive the asset.")
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
_USER_ACCESS_TOKEN_ISSUED_AT: dict[str, float] = {}
_USER_ACCESS_TOKEN_LOCK = threading.RLock()


def _remember_user_access_token(identity: TelegramIdentity, result: object) -> str:
    if not isinstance(result, dict):
        return ""
    token = str(result.get("accessToken") or "").strip()
    if token:
        user_id = str(identity.user_id)
        now = time.time()
        with _USER_ACCESS_TOKEN_LOCK:
            _prune_user_access_token_cache(now)
            _USER_ACCESS_TOKENS[user_id] = token
            _USER_ACCESS_TOKEN_ISSUED_AT[user_id] = now
            _prune_user_access_token_cache(now)
    return token


def _create_wallet_text(client, identity: TelegramIdentity) -> str:
    result = client.create_wallet(identity)
    _remember_user_access_token(identity, result)
    telegram_text = result.get("telegramText") if isinstance(result, dict) else None
    if not isinstance(telegram_text, str) or not telegram_text.strip():
        raise GatewayClientError("Wallet service returned an invalid response. Please try again.")
    return telegram_text.strip()


def _user_access_token(client, identity: TelegramIdentity) -> str | None:
    """Return the caller's per-user gateway token, minting one if unseen.

    Cached in-process across requests. A cold cache (for example after a
    gateway restart) uses the trusted Telegram identity to mint a fresh token.
    Requests never fall back to the shared gateway token.
    """
    user_id = str(identity.user_id)
    now = time.time()
    with _USER_ACCESS_TOKEN_LOCK:
        _prune_user_access_token_cache(now)
        cached = _USER_ACCESS_TOKENS.get(user_id)
        issued_at = _USER_ACCESS_TOKEN_ISSUED_AT.get(user_id, 0.0)
        if cached and now < (
            issued_at
            + _USER_ACCESS_TOKEN_TTL_SECONDS
            - _USER_ACCESS_TOKEN_REFRESH_SKEW_SECONDS
        ):
            return cached
        _USER_ACCESS_TOKENS.pop(user_id, None)
        _USER_ACCESS_TOKEN_ISSUED_AT.pop(user_id, None)
    try:
        result = client.create_wallet(identity)
    except Exception:
        return None
    return _remember_user_access_token(identity, result) or None


def _prune_user_access_token_cache(now: float | None = None) -> None:
    current_time = time.time() if now is None else float(now)
    valid_after = current_time - _USER_ACCESS_TOKEN_TTL_SECONDS
    for user_id, issued_at in tuple(_USER_ACCESS_TOKEN_ISSUED_AT.items()):
        if issued_at <= valid_after:
            _USER_ACCESS_TOKEN_ISSUED_AT.pop(user_id, None)
            _USER_ACCESS_TOKENS.pop(user_id, None)

    overflow = len(_USER_ACCESS_TOKENS) - _USER_ACCESS_TOKEN_CACHE_MAX_USERS
    if overflow <= 0:
        return
    oldest_user_ids = sorted(
        _USER_ACCESS_TOKENS,
        key=lambda user_id: _USER_ACCESS_TOKEN_ISSUED_AT.get(user_id, 0.0),
    )[:overflow]
    for user_id in oldest_user_ids:
        _USER_ACCESS_TOKEN_ISSUED_AT.pop(user_id, None)
        _USER_ACCESS_TOKENS.pop(user_id, None)


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
        native = bool(raw.get("native"))
        if native:
            if symbol != "ETH" or contract.lower() != "native":
                continue
        elif re.fullmatch(r"0x[a-fA-F0-9]{40}", contract) is None:
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
                "native": native,
            }
        )
    return tokens


def _format_bitrefill_payment_tokens(tokens: list[dict]) -> str:
    lines = ["Choose a token to pay with:"]
    for index, token in enumerate(tokens, start=1):
        symbol = str(token.get("symbol") or "ERC20")
        balance = str(token.get("balance") or "0")
        line = f"{index}. {symbol}: {balance} available"
        if token.get("native"):
            line += " (network gas is reserved)"
        if not token.get("verified"):
            line += f" ({_short_address(str(token.get('contractAddress') or ''))})"
        lines.append(line)
    lines.extend(("", "Reply with a number."))
    return "\n".join(lines)


def _load_bitrefill_payment_token(
    client,
    identity: TelegramIdentity,
    selector: str,
    *,
    user_access_token: str,
) -> dict:
    result = client.withdraw_tokens(identity, user_access_token=user_access_token)
    tokens = _normalize_withdraw_tokens(
        result.get("tokens") if isinstance(result, dict) else []
    )
    requested = str(selector or "").strip()
    matches = [
        token
        for token in tokens
        if str(token.get("contractAddress") or "").casefold() == requested.casefold()
        or str(token.get("symbol") or "").casefold() == requested.casefold()
    ]
    if not matches:
        raise GatewayClientError(
            f"Token {requested or 'selection'} is not available in your Base wallet."
        )
    if len(matches) > 1:
        raise GatewayClientError(
            f"More than one {requested} token is available. Use the contract address."
        )
    return matches[0]


def _format_withdraw_tokens(tokens: list[dict]) -> str:
    lines = ["Choose an asset to withdraw:"]
    for index, token in enumerate(tokens, start=1):
        symbol = str(token.get("symbol") or "ERC20")
        balance = str(token.get("balance") or "0")
        line = f"{index}. {symbol}: {balance}"
        if token.get("native"):
            line += " (leave ETH for gas)"
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
    return _reply_keyboard(rows, placeholder="Choose asset")


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
    payment_token: dict,
    recipient: dict | None = None,
    identity: TelegramIdentity,
    source,
    gateway,
    operation_generation: int | None = None,
) -> None:
    user_id = str(identity.user_id)
    try:
        client = _client_factory()
        token = _user_access_token(client, identity)
        text = client.execute_bitrefill_purchase(
            identity,
            product_id=product_id,
            package_id=package_id,
            country=country,
            recipient=dict(recipient or {}),
            payment_token=payment_token,
            user_access_token=token,
        )
        _BITREFILL_SESSIONS.pop(user_id, None)
        _send_fixed_reply(gateway, source, text)
    except GatewayClientError as exc:
        session = _BITREFILL_SESSIONS.get(user_id, {})
        previous_session = (
            session.get("previousSession")
            if session.get("stage") == "purchasing"
            else session
        )
        if isinstance(previous_session, dict):
            _BITREFILL_SESSIONS[user_id] = previous_session
        _send_fixed_reply(
            gateway,
            source,
            exc.user_message,
            reply_markup=_withdraw_reply_keyboard(
                _normalize_withdraw_tokens(
                    _BITREFILL_SESSIONS.get(user_id, {}).get(
                        "paymentTokens"
                    )
                )
            )
            if _BITREFILL_SESSIONS.get(user_id, {}).get("stage")
            == "select-payment-token"
            else _telegram_main_menu_reply_markup(),
        )
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 Telegram Bitrefill failure error=%s",
            type(exc).__name__,
        )
        _send_fixed_reply(gateway, source, _UNEXPECTED_ERROR_MESSAGE)
    finally:
        if operation_generation is not None:
            _finish_telegram_operation(user_id, operation_generation)


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
        resolved_photon_user_id = _resolve_photon_sender_id(photon_user_id)
        result = _client_factory().execute_imessage(
            "link",
            {"code": code, "photonUserId": resolved_photon_user_id},
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


def _handle_whatsapp_cloud_event(*, event, source, gateway):
    wa_id = str(getattr(source, "user_id", "") or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{4,19}", wa_id):
        return dict(_SKIP_RESULT)
    text = str(getattr(event, "text", "") or "").strip()
    try:
        client = _client_factory()
        if _looks_like_pairing_code(text.upper()):
            result = client.execute_approval(
                "link",
                {
                    "code": text.upper(),
                    "approvalUserId": wa_id,
                    "channel": "whatsapp",
                },
            )
            _send_fixed_reply(gateway, source, _imessage_text(result))
            return dict(_SKIP_RESULT)

        button = _whatsapp_button_decision(event)
        if button is None:
            return dict(_SKIP_RESULT)
        decision, approval_id = button
        result = client.execute_approval(
            "decision",
            {
                "approvalUserId": wa_id,
                "channel": "whatsapp",
                "decision": decision,
                "approvalId": approval_id,
            },
        )
        _send_fixed_reply(gateway, source, _imessage_text(result))
        return dict(_SKIP_RESULT)
    except GatewayClientError:
        return dict(_SKIP_RESULT)
    except Exception as exc:
        logger.warning(
            "Unexpected Sign402 WhatsApp approval failure error=%s",
            type(exc).__name__,
        )
        return dict(_SKIP_RESULT)


def _whatsapp_button_decision(event) -> tuple[str, str] | None:
    candidates = [str(getattr(event, "text", "") or "").strip()]

    def collect(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {"payload", "id", "button_payload"}:
                    candidates.append(str(item or "").strip())
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(getattr(event, "raw_message", None))
    for candidate in candidates:
        match = re.fullmatch(
            r"sign402:(approve|reject):([A-Za-z0-9_-]{8,128})",
            candidate,
        )
        if match:
            decision = "YES" if match.group(1) == "approve" else "NO"
            return decision, match.group(2)
    return None


def _handle_photon_decision(*, decision: str, photon_user_id: str, source, gateway):
    try:
        resolved_photon_user_id = _resolve_photon_sender_id(photon_user_id)
        client = _client_factory()
        pending = client.execute_imessage(
            "pending",
            {"photonUserId": resolved_photon_user_id},
        )
        if not pending.get("pending"):
            # A stale YES/NO must not fall through into general Hermes chat.
            return dict(_SKIP_RESULT)
        result = client.execute_imessage(
            "decision",
            {"photonUserId": resolved_photon_user_id, "decision": decision},
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
    if platform_name in {
        "photon",
        "imessage",
        "imessage via photon",
        "platforms/photon",
    }:
        return True
    raw_message = getattr(event, "raw_message", None)
    if isinstance(raw_message, dict):
        raw_platform = str(raw_message.get("platform", "") or "").strip().lower()
        return raw_platform in {"imessage", "photon"}
    return False


def _is_unconfigured_whatsapp_source(source) -> bool:
    return _platform_name(source) in {
        "whatsapp",
        "whatsapp via photon",
        "platforms/whatsapp",
    }


def _is_whatsapp_cloud_source(source) -> bool:
    return _platform_name(source) in {
        "whatsapp_cloud",
        "whatsapp-cloud",
        "platforms/whatsapp_cloud",
    }


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
        "connect-whatsapp",
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


def _canonical_button_text(text: str) -> str:
    normalized_lines = [
        _normalize_button_text(line)
        for line in str(text or "").splitlines()
        if str(line).strip()
    ]
    if normalized_lines and all(
        line == normalized_lines[0] for line in normalized_lines
    ):
        return normalized_lines[0]
    return _normalize_button_text(text)


def _reserve_telegram_operation(user_id: str, action: str) -> int | None:
    user_key = str(user_id)
    action_key = str(action)
    with _TELEGRAM_OPERATION_LOCK:
        active = _TELEGRAM_ACTIVE_OPERATIONS.get(user_key)
        if active is not None:
            if active[1] == action_key:
                return None
            _TELEGRAM_OPERATION_GENERATIONS[user_key] = (
                _TELEGRAM_OPERATION_GENERATIONS.get(user_key, 0) + 1
            )
            _TELEGRAM_ACTIVE_OPERATIONS.pop(user_key, None)
        if (
            user_key not in _TELEGRAM_OPERATION_GENERATIONS
            and len(_TELEGRAM_OPERATION_GENERATIONS) >= _TELEGRAM_OPERATION_MAX_USERS
        ):
            for tracked_user_id in tuple(_TELEGRAM_OPERATION_GENERATIONS):
                if tracked_user_id not in _TELEGRAM_ACTIVE_OPERATIONS:
                    _TELEGRAM_OPERATION_GENERATIONS.pop(tracked_user_id, None)
                    break
            if len(_TELEGRAM_OPERATION_GENERATIONS) >= _TELEGRAM_OPERATION_MAX_USERS:
                return None
        generation = _TELEGRAM_OPERATION_GENERATIONS.get(user_key, 0) + 1
        _TELEGRAM_OPERATION_GENERATIONS[user_key] = generation
        _TELEGRAM_ACTIVE_OPERATIONS[user_key] = (generation, action_key)
        return generation


def _telegram_operation_is_current(user_id: str, generation: int) -> bool:
    user_key = str(user_id)
    with _TELEGRAM_OPERATION_LOCK:
        return (
            _TELEGRAM_OPERATION_GENERATIONS.get(user_key) == int(generation)
            and _TELEGRAM_ACTIVE_OPERATIONS.get(user_key, (None, ""))[0]
            == int(generation)
        )


def _finish_telegram_operation(user_id: str, generation: int) -> bool:
    user_key = str(user_id)
    with _TELEGRAM_OPERATION_LOCK:
        if not _telegram_operation_is_current(user_key, generation):
            return False
        _TELEGRAM_ACTIVE_OPERATIONS.pop(user_key, None)
        return True


def _invalidate_telegram_operation(user_id: str) -> None:
    user_key = str(user_id)
    with _TELEGRAM_OPERATION_LOCK:
        _TELEGRAM_OPERATION_GENERATIONS[user_key] = (
            _TELEGRAM_OPERATION_GENERATIONS.get(user_key, 0) + 1
        )
        _TELEGRAM_ACTIVE_OPERATIONS.pop(user_key, None)


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


def _parse_bitrefill_args(raw_args: str) -> tuple[str, str, str, str] | None:
    args = str(raw_args or "").strip().split()
    if len(args) != 4:
        return None
    country = args[2].upper()
    if re.fullmatch(r"[A-Z]{2}", country) is None:
        return None
    return (args[0], args[1], country, args[3])


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
        description="Buy Bitrefill with a wallet token",
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
