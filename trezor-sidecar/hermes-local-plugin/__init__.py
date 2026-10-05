"""Separate, opt-in Hermes commands for the local Trezor proof."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from trezor_sidecar.errors import SafeError
from trezor_sidecar.local_agent import build_local_agent_controller

from .identity import capture_identity, consume_identity


logger = logging.getLogger(__name__)

_TELEGRAM_ONLY = "Local Trezor commands require an authenticated Telegram message."
_UNAVAILABLE = "Local Trezor agent mode is temporarily unavailable."
_USAGE_PREPARE = "Usage: /trezor_prepare <productId> <packageId> <country>"
_USAGE_CONFIRM = "Usage: /trezor_confirm <8-character confirmation code>"

_controller_factory: Callable[[], Any] = lambda: build_local_agent_controller(os.environ)
_controller: Any | None = None


def _get_controller() -> Any:
    global _controller
    if _controller is None:
        _controller = _controller_factory()
    return _controller


def handle_pre_gateway_dispatch(*, event: Any, **kwargs: Any) -> None:
    """Capture identity only; never intercept existing channel traffic."""

    capture_identity(event=event, **kwargs)
    return None


def _identity_user_id() -> str | None:
    identity = consume_identity()
    return None if identity is None else identity.user_id


def _handler(operation: str):
    async def handler(raw_args: str) -> str:
        user_id = _identity_user_id()
        if user_id is None:
            return _TELEGRAM_ONLY
        arguments = str(raw_args or "").strip().split()
        if operation in {"pair", "cancel"} and arguments:
            return f"Usage: /trezor_{operation}"
        if operation == "prepare" and len(arguments) != 3:
            return _USAGE_PREPARE
        if operation == "confirm" and len(arguments) != 1:
            return _USAGE_CONFIRM
        try:
            controller = _get_controller()
            if operation == "pair":
                return await asyncio.to_thread(controller.pair, user_id)
            if operation == "prepare":
                product_id, package_id, country = arguments
                return await asyncio.to_thread(
                    controller.prepare,
                    user_id,
                    product_id,
                    package_id,
                    country.upper(),
                )
            if operation == "confirm":
                return await asyncio.to_thread(
                    controller.confirm,
                    user_id,
                    arguments[0].upper(),
                )
            if operation == "cancel":
                return await asyncio.to_thread(controller.cancel, user_id)
            return _UNAVAILABLE
        except SafeError as error:
            return error.message
        except Exception as error:
            logger.warning(
                "Local Trezor plugin failed safely operation=%s error=%s",
                operation,
                type(error).__name__,
            )
            return _UNAVAILABLE

    return handler


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
    ctx.register_command(
        "trezor-pair",
        handler=_handler("pair"),
        description="Pair the isolated local Trezor account",
    )
    ctx.register_command(
        "trezor-prepare",
        handler=_handler("prepare"),
        description="Prepare an exact local Trezor Bitrefill quote",
    )
    ctx.register_command(
        "trezor-confirm",
        handler=_handler("confirm"),
        description="Confirm one prepared local Trezor purchase",
    )
    ctx.register_command(
        "trezor-cancel",
        handler=_handler("cancel"),
        description="Cancel the pending local Trezor quote",
    )
