"""Authenticated localhost client for managed Sign402 wallets."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from .identity import TelegramIdentity
except ImportError:  # Direct module loading in standalone unit tests.
    from identity import TelegramIdentity


logger = logging.getLogger(__name__)

_OPERATION_PATHS = {
    "wallet": "/agent/wallet",
    "create-wallet": "/agent/create-wallet",
    "balance": "/agent/wallet-balance",
    "last-purchase": "/agent/last-purchase",
}
_IMESSAGE_OPERATION_PATHS = {
    "connect-imessage": "/agent/imessage/pairing",
    "test-imessage-approval": "/agent/test-imessage-approval",
    "link": "/agent/imessage/link",
    "pending": "/agent/imessage/pending",
    "decision": "/agent/imessage/decision",
}
_PAID_TOOL_PATH = "/agent/buy-tool"
_MAX_RESPONSE_BYTES = 64 * 1024
_NOT_CONFIGURED = "Wallet service is not configured. Please contact the operator."
_LOCALHOST_REQUIRED = "Wallet service must use a localhost gateway URL."
_UNAVAILABLE = "Wallet service is temporarily unavailable. Please try again."
_AUTH_FAILED = "Wallet service authentication failed. Please contact the operator."
_REQUEST_FAILED = "Wallet request failed. Please try again or contact the operator."
_INVALID_RESPONSE = "Wallet service returned an invalid response."
_UNSUPPORTED = "This wallet operation is not supported."


class GatewayClientError(RuntimeError):
    """A gateway failure with a fixed Telegram-safe message."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


class GatewayClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        photon_api_token: str = "",
        opener: Callable[..., Any] = urlopen,
        timeout: float = 5.0,
        purchase_timeout: float = 180.0,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.photon_api_token = photon_api_token
        self.opener = opener
        self.timeout = timeout
        self.purchase_timeout = purchase_timeout
        self.max_response_bytes = max_response_bytes

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> GatewayClient:
        values = os.environ if env is None else env
        base_url = str(values.get("SIGN402_GATEWAY_URL", "") or "").strip()
        api_token = str(values.get("SIGN402_WALLET_API_TOKEN", "") or "").strip()
        photon_api_token = str(
            values.get("SIGN402_PHOTON_API_TOKEN", "") or ""
        ).strip()
        if not base_url or not api_token:
            raise GatewayClientError(_NOT_CONFIGURED)

        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
        ):
            raise GatewayClientError(_LOCALHOST_REQUIRED)
        return cls(
            base_url=base_url,
            api_token=api_token,
            photon_api_token=photon_api_token,
            **kwargs,
        )

    def execute(self, operation: str, identity: TelegramIdentity) -> str:
        path = _OPERATION_PATHS.get(operation)
        if path is None:
            raise GatewayClientError(_UNSUPPORTED)

        payload = {"telegramUserId": identity.user_id}
        if identity.username:
            payload["telegramUsername"] = identity.username
        result = self._post(path, payload, token=self.api_token, operation=operation)

        telegram_text = result.get("telegramText")
        if not isinstance(telegram_text, str) or not telegram_text.strip():
            raise GatewayClientError(_INVALID_RESPONSE)
        return telegram_text.strip()

    def execute_imessage(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        path = _IMESSAGE_OPERATION_PATHS.get(operation)
        if path is None:
            raise GatewayClientError(_UNSUPPORTED)
        if not self.photon_api_token:
            raise GatewayClientError(_NOT_CONFIGURED)
        return self._post(
            path,
            payload,
            token=self.photon_api_token,
            operation=operation,
        )

    def execute_paid_tool(self, tool: str, identity: TelegramIdentity) -> str:
        payload = {"tool": str(tool or "").strip(), "telegramUserId": identity.user_id}
        if identity.username:
            payload["telegramUsername"] = identity.username
        result = self._post(
            _PAID_TOOL_PATH,
            payload,
            token=self.api_token,
            operation="buy-tool",
            timeout=self.purchase_timeout,
        )
        telegram_text = result.get("telegramText")
        if not isinstance(telegram_text, str) or not telegram_text.strip():
            raise GatewayClientError(_INVALID_RESPONSE)
        return telegram_text.strip()

    def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        token: str,
        operation: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        response = None
        try:
            response = self.opener(request, timeout=self.timeout if timeout is None else timeout)
            body = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            exc.close()
            if exc.code in {401, 403}:
                logger.warning(
                    "Sign402 wallet request rejected operation=%s status=auth",
                    operation,
                )
                raise GatewayClientError(_AUTH_FAILED) from None
            logger.warning(
                "Sign402 wallet request failed operation=%s status=http_%s",
                operation,
                exc.code,
            )
            raise GatewayClientError(_REQUEST_FAILED) from None
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning(
                "Sign402 wallet request unavailable operation=%s error=%s",
                operation,
                type(exc).__name__,
            )
            raise GatewayClientError(_UNAVAILABLE) from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        if len(body) > self.max_response_bytes:
            logger.warning(
                "Sign402 wallet response rejected operation=%s reason=oversized",
                operation,
            )
            raise GatewayClientError(_INVALID_RESPONSE)

        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            result = None
        if not isinstance(result, dict):
            raise GatewayClientError(_INVALID_RESPONSE)
        return result
