"""Separate, opt-in Hermes commands for a remotely enrolled Trezor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
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
_ACKNOWLEDGED = "Working on it. Confirm on your Trezor when it lights up."
_SKIP_RESULT = {"action": "skip", "reason": "sign402-trezor-remote-handled"}
_OPERATIONS = frozenset({"status", "test", "prepare", "confirm", "cancel"})
# Operations that wait on a physical Trezor confirmation, so the caller is told
# to look at the device before the worker thread blocks on it.
_DEVICE_OPERATIONS = frozenset({"status", "test", "confirm"})

_delivery_loop: asyncio.AbstractEventLoop | None = None

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


def _identity_user_id() -> str | None:
    identity = consume_identity()
    return None if identity is None else identity.user_id


def _normalize_arguments(operation: str, arguments: list[str]) -> list[str] | str:
    """Return normalized arguments, or a usage string to send back."""
    if operation in {"status", "test", "cancel"}:
        return [] if not arguments else f"Usage: /trezor_{operation}"
    if operation == "prepare":
        # Bitrefill package ids legitimately contain spaces, so the middle
        # tokens are rejoined rather than requiring the operator to quote
        # them. The first argument is always the product and the last is
        # always the country.
        if len(arguments) < 3:
            return _USAGE_PREPARE
        return [arguments[0], " ".join(arguments[1:-1]), arguments[-1]]
    if operation == "confirm":
        return arguments if len(arguments) == 1 else _USAGE_CONFIRM
    return _UNAVAILABLE


def _execute(operation: str, user_id: str, arguments: list[str]) -> str:
    """Run one remote-agent operation. Blocking; never raises."""
    try:
        controller = _get_controller()
        if operation == "status":
            return controller.pair(user_id)
        if operation == "test":
            return controller.intent_test(user_id)
        if operation == "prepare":
            return controller.prepare(
                user_id, arguments[0], arguments[1], arguments[2].upper()
            )
        if operation == "confirm":
            return controller.confirm(user_id, arguments[0].upper())
        if operation == "cancel":
            return controller.cancel(user_id)
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


def _default_spawn_worker(worker: Callable[[], None]) -> None:
    threading.Thread(
        target=worker, name="sign402-trezor-remote", daemon=True
    ).start()


# Replaced in tests so a request can be driven to completion synchronously.
_spawn_worker: Callable[[Callable[[], None]], None] = _default_spawn_worker


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _remember_loop() -> None:
    """Keep the gateway loop so worker threads can deliver their replies."""
    global _delivery_loop
    try:
        _delivery_loop = asyncio.get_running_loop()
    except RuntimeError:
        pass


def _send(gateway: Any, source: Any, text: str) -> None:
    if gateway is None:
        return
    adapters = getattr(gateway, "adapters", {}) or {}
    adapter = adapters.get(_platform_name(source)) or adapters.get(
        getattr(source, "platform", None)
    )
    send = getattr(adapter, "send", None)
    if not callable(send):
        return
    chat_id = str(
        getattr(source, "chat_id", "") or getattr(source, "user_id", "") or ""
    )
    if not chat_id:
        return
    coroutine = send(chat_id, text)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Worker thread: hand the send back to the gateway loop when there is
        # one, so the adapter is only ever touched from its own thread.
        loop = _delivery_loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(coroutine, loop)
        else:
            asyncio.run(coroutine)
    else:
        loop.create_task(coroutine)


def handle_pre_gateway_dispatch(
    *, event: Any = None, gateway: Any = None, **kwargs: Any
) -> dict[str, str] | None:
    """Own the /trezor_* commands before Hermes' dispatcher is reached.

    Another Sign402 plugin ends this hook with a catch-all skip, so a
    registered command handler would never run. The work therefore happens
    here, off the event loop: this hook must not block while the operator
    reads the Trezor screen, or the whole gateway stalls.
    """
    capture_identity(event=event, **kwargs)
    _remember_loop()

    text = str(getattr(event, "text", "") or "").strip()
    head, _, raw_args = text.partition(" ")
    if not head.startswith(("/trezor_", "/trezor-")):
        return None
    operation = head[len("/trezor_"):].split("@", 1)[0].strip().lower()
    operation = operation.replace("-", "_")
    if operation not in _OPERATIONS:
        return None

    source = getattr(event, "source", None)
    user_id = _identity_user_id()
    if user_id is None:
        _send(gateway, source, _TELEGRAM_ONLY)
        return dict(_SKIP_RESULT)

    normalized = _normalize_arguments(operation, raw_args.strip().split())
    if isinstance(normalized, str):
        _send(gateway, source, normalized)
        return dict(_SKIP_RESULT)

    if operation in _DEVICE_OPERATIONS:
        _send(gateway, source, _ACKNOWLEDGED)

    def worker() -> None:
        _send(gateway, source, _execute(operation, user_id, normalized))

    _spawn_worker(worker)
    return dict(_SKIP_RESULT)


def _handler(operation: str):
    """Command handler kept for setups without the Sign402 catch-all hook.

    When that plugin is active this never runs, because its skip result ends
    dispatch before commands; `handle_pre_gateway_dispatch` handles the same
    operations there. Both paths share one implementation.
    """

    async def handler(raw_args: str) -> str:
        user_id = _identity_user_id()
        if user_id is None:
            return _TELEGRAM_ONLY
        normalized = _normalize_arguments(
            operation, str(raw_args or "").strip().split()
        )
        if isinstance(normalized, str):
            return normalized
        return await asyncio.to_thread(_execute, operation, user_id, normalized)

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
