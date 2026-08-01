"""Separate, opt-in Hermes commands for a remotely enrolled Trezor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .identity import capture_identity, consume_identity


logger = logging.getLogger(__name__)

_TELEGRAM_ONLY = "Trezor commands require an authenticated Telegram message."
_UNAVAILABLE = "Trezor payment mode is temporarily unavailable."
_USAGE_PREPARE = "Usage: /trezor_prepare <productId> <packageId> <country>"
_USAGE_CONFIRM = "Usage: /trezor_confirm <8-character confirmation code>"

class RemoteAgentClientError(ValueError):
    pass


class RemoteAgentClient:
    def __init__(self, env):
        if env.get("SIGN402_TREZOR_REMOTE_PLUGIN_ENABLED") != "1":
            raise RemoteAgentClientError("Trezor payment mode is disabled.")
        self.url = str(env.get("SIGN402_TREZOR_REMOTE_AGENT_URL", "http://127.0.0.1:8123")).rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise RemoteAgentClientError(_UNAVAILABLE)
        self.token = str(env.get("SIGN402_TREZOR_REMOTE_AGENT_TOKEN", "") or "").strip()
        if len(self.token) < 32:
            raise RemoteAgentClientError(_UNAVAILABLE)

    def _call(self, operation, payload):
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.url + "/v1/" + operation,
            data=body,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=700) as response:
                raw = response.read(16_385)
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(16_385)
            finally:
                error.close()
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except Exception:
                raise RemoteAgentClientError(_UNAVAILABLE) from None
            message = decoded.get("message") if isinstance(decoded, dict) else None
            raise RemoteAgentClientError(str(message or _UNAVAILABLE)) from None
        except Exception:
            raise RemoteAgentClientError(_UNAVAILABLE) from None
        if len(raw) > 16_384:
            raise RemoteAgentClientError(_UNAVAILABLE)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except Exception:
            raise RemoteAgentClientError(_UNAVAILABLE) from None
        if not isinstance(decoded, dict) or decoded.get("ok") is not True or not isinstance(decoded.get("text"), str):
            raise RemoteAgentClientError(_UNAVAILABLE)
        return decoded["text"]

    def pair(self, user_id):
        return self._call("status", {"userId": user_id})

    def intent_test(self, user_id):
        return self._call("test", {"userId": user_id})

    def prepare(self, user_id, product_id, package_id, country):
        return self._call(
            "prepare",
            {"userId": user_id, "productId": product_id, "packageId": package_id, "country": country},
        )

    def confirm(self, user_id, code):
        return self._call("confirm", {"userId": user_id, "confirmationCode": code})

    def cancel(self, user_id):
        return self._call("cancel", {"userId": user_id})


_controller_factory: Callable[[], Any] = lambda: RemoteAgentClient(os.environ)
_controller: Any | None = None


def _get_controller() -> Any:
    global _controller
    if _controller is None:
        _controller = _controller_factory()
    return _controller


def handle_pre_gateway_dispatch(*, event: Any, **kwargs: Any) -> None:
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
        if operation in {"status", "test", "cancel"} and arguments:
            return f"Usage: /trezor_{operation}"
        if operation == "prepare":
            # Bitrefill package ids legitimately contain spaces, so the middle
            # tokens are rejoined rather than requiring the operator to quote
            # them. The first token is always the product and the last is
            # always the country.
            if len(arguments) < 3:
                return _USAGE_PREPARE
            arguments = [
                arguments[0],
                " ".join(arguments[1:-1]),
                arguments[-1],
            ]
        if operation == "confirm" and len(arguments) != 1:
            return _USAGE_CONFIRM
        try:
            controller = _get_controller()
            if operation == "status":
                return await asyncio.to_thread(controller.pair, user_id)
            if operation == "test":
                return await asyncio.to_thread(controller.intent_test, user_id)
            if operation == "prepare":
                return await asyncio.to_thread(
                    controller.prepare,
                    user_id,
                    arguments[0],
                    arguments[1],
                    arguments[2].upper(),
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
        except RemoteAgentClientError as error:
            return str(error)
        except Exception as error:
            logger.warning(
                "Remote Trezor plugin failed safely operation=%s error=%s",
                operation,
                type(error).__name__,
            )
            return _UNAVAILABLE

    return handler


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
    ctx.register_command(
        "trezor-status",
        handler=_handler("status"),
        description="Show the enrolled Trezor companion",
    )
    ctx.register_command(
        "trezor-test",
        handler=_handler("test"),
        description="Approve a no-purchase Trezor connection test",
    )
    ctx.register_command(
        "trezor-prepare",
        handler=_handler("prepare"),
        description="Prepare an exact Trezor Bitrefill quote",
    )
    ctx.register_command(
        "trezor-confirm",
        handler=_handler("confirm"),
        description="Confirm one prepared Trezor purchase",
    )
    ctx.register_command(
        "trezor-cancel",
        handler=_handler("cancel"),
        description="Cancel the pending Trezor quote",
    )
