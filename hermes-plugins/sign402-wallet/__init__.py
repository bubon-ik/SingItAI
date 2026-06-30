"""Hermes plugin for trusted Sign402 Telegram wallet commands."""

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
_COMMANDS = {
    "wallet": ("wallet", "Show your managed Base wallet"),
    "create-wallet": ("create-wallet", "Create your managed Base wallet"),
    "balance": ("balance", "Show your managed Base wallet balance"),
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


def register(ctx) -> None:
    """Register trusted Telegram identity capture and wallet commands."""

    ctx.register_hook("pre_gateway_dispatch", capture_gateway_identity)
    for command, (operation, description) in _COMMANDS.items():
        ctx.register_command(
            command,
            handler=_build_handler(operation),
            description=description,
        )
