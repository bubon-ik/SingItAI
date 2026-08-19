"""Authenticated localhost client for managed Sign402 wallets."""

from __future__ import annotations

import json
import logging
import os
import re
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
    "select-existing": "/agent/approval-channel/select-existing",
    "link": "/agent/imessage/link",
    "pending": "/agent/imessage/pending",
    "decision": "/agent/imessage/decision",
    "unlink": "/agent/imessage/unlink",
}
_BITREFILL_QUOTE_PATH = "/agent/quote-bitrefill"
_BITREFILL_BUY_PATH = "/agent/buy-wallet-bitrefill"
_BITREFILL_SEARCH_PATH = "/agent/search-bitrefill"
_BITREFILL_LIST_PATH = "/agent/list-bitrefill-products"
_BITREFILL_PRODUCT_PATH = "/agent/get-bitrefill-product"
_PAID_TOOL_PATH = "/agent/buy-tool"
_SPENDING_LIMITS_PATH = "/agent/spending-limits"
_BUYER_EMAIL_PATH = "/agent/buyer-email"
# The gateway writes this one for the buyer, so it travels to chat unchanged;
# only the command that fixes it is added here, where the commands are defined.
_SPEND_LIMIT_PREFIX = "Raise your spending limit to continue."
_SPEND_LIMIT_HINT = (
    "Send /limits to see your current limits, or "
    "/set_limits <max per transaction> <daily cap> to raise them."
)
# Gateway error codes that are states, not faults: the buyer can act on them,
# so they travel as plain sentences rather than as the identifier we log.
_GATEWAY_ERROR_TEXTS = {
    "firefly_busy": (
        "Another purchase is waiting for approval right now. Nothing was "
        "charged — give it a moment and send your order again."
    ),
    "purchase_in_progress": (
        "Your previous order is still waiting for approval. Approve or decline "
        "it first — nothing was charged for this one."
    ),
}
_WITHDRAW_TOKENS_PATH = "/agent/withdraw/tokens"
_WITHDRAW_PATH = "/agent/withdraw"
_LLM_OPERATION_PATHS = {
    "start": "/agent/llm-key/start",
    "accept-terms": "/agent/llm-key/accept-terms",
    "verify": "/agent/llm-key/verify",
    "credits": "/agent/llm-credits",
}
_CHAT_OPERATION_PATHS = {
    "start": "/agent/chat/start",
    "message": "/agent/chat/message",
    "end": "/agent/chat/end",
    "approve-policy": "/agent/chat/approve-policy",
    "models": "/agent/chat/models",
}
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

    def execute(
        self,
        operation: str,
        identity: TelegramIdentity,
        *,
        user_access_token: str | None = None,
    ) -> str:
        path = _OPERATION_PATHS.get(operation)
        if path is None:
            raise GatewayClientError(_UNSUPPORTED)

        payload = {"telegramUserId": identity.user_id}
        if identity.username:
            payload["telegramUsername"] = identity.username
        result = self._post(
            path,
            payload,
            token=self.api_token,
            operation=operation,
            user_token=user_access_token,
        )

        telegram_text = result.get("telegramText")
        if not isinstance(telegram_text, str) or not telegram_text.strip():
            raise GatewayClientError(_INVALID_RESPONSE)
        return telegram_text.strip()

    def execute_imessage(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.execute_approval(operation, payload)

    def execute_approval(
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

    def create_wallet(self, identity: TelegramIdentity) -> dict[str, Any]:
        """Create/return the user's wallet, exposing the per-user access token."""
        payload = {"telegramUserId": identity.user_id}
        if identity.username:
            payload["telegramUsername"] = identity.username
        return self._post(
            _OPERATION_PATHS["create-wallet"],
            payload,
            token=self.api_token,
            operation="create-wallet",
        )

    def execute_paid_tool(
        self,
        tool: str,
        identity: TelegramIdentity,
        *,
        user_access_token: str | None = None,
    ) -> str:
        payload = {"tool": str(tool or "").strip(), "telegramUserId": identity.user_id}
        if identity.username:
            payload["telegramUsername"] = identity.username
        result = self._post(
            _PAID_TOOL_PATH,
            payload,
            token=self.api_token,
            operation="buy-tool",
            timeout=self.purchase_timeout,
            user_token=user_access_token,
        )
        telegram_text = result.get("telegramText")
        if not isinstance(telegram_text, str) or not telegram_text.strip():
            raise GatewayClientError(_INVALID_RESPONSE)
        return telegram_text.strip()

    def execute_bitrefill_purchase(
        self,
        identity: TelegramIdentity,
        *,
        product_id: str,
        package_id: str,
        payment_token: Mapping[str, Any],
        country: str = "US",
        recipient: Mapping[str, Any] | None = None,
        user_access_token: str | None = None,
    ) -> str:
        payment_token_address = str(
            payment_token.get("contractAddress") or payment_token.get("address") or ""
        ).strip()
        if not payment_token_address:
            raise GatewayClientError("Choose a wallet token before buying with Bitrefill.")
        quote_payload: dict[str, Any] = {
            "productId": str(product_id or "").strip(),
            "packageId": str(package_id or "").strip(),
            "country": str(country or "US").strip().upper(),
            "recipient": dict(recipient or {}),
            "paymentToken": {
                "address": payment_token_address,
                "symbol": str(payment_token.get("symbol") or "").strip(),
                "decimals": int(payment_token["decimals"]),
                "native": bool(payment_token.get("native", False)),
            },
            "telegramUserId": identity.user_id,
        }
        if identity.username:
            quote_payload["telegramUsername"] = identity.username
        quote = self._post(
            _BITREFILL_QUOTE_PATH,
            quote_payload,
            token=self.api_token,
            operation="quote-bitrefill",
            timeout=self.purchase_timeout,
            user_token=user_access_token,
        )
        quote_id = str(quote.get("quoteId") or "").strip()
        if not quote_id:
            raise GatewayClientError(_INVALID_RESPONSE)

        buy_payload: dict[str, Any] = {
            "quoteId": quote_id,
            "recipient": dict(recipient or {}),
            "telegramUserId": identity.user_id,
        }
        if identity.username:
            buy_payload["telegramUsername"] = identity.username
        result = self._post(
            _BITREFILL_BUY_PATH,
            buy_payload,
            token=self.api_token,
            operation="buy-wallet-bitrefill",
            timeout=self.purchase_timeout,
            user_token=user_access_token,
        )
        telegram_text = result.get("telegramText")
        if not isinstance(telegram_text, str) or not telegram_text.strip():
            raise GatewayClientError(_INVALID_RESPONSE)
        return telegram_text.strip()

    def search_bitrefill_products(
        self,
        *,
        query: str,
        country: str,
        search_all_countries: bool = True,
        include_test_products: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "query": str(query or "").strip(),
            "country": str(country or "US").strip().upper(),
            "searchAllCountries": bool(search_all_countries),
            "includeTestProducts": bool(include_test_products),
        }
        return self._post(
            _BITREFILL_SEARCH_PATH,
            payload,
            token=self.api_token,
            operation="search-bitrefill",
            timeout=self.purchase_timeout,
        )

    def list_bitrefill_products(
        self,
        *,
        country: str,
        category: str,
        start: int,
        limit: int,
        include_international: bool = True,
        include_test_products: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "country": str(country or "US").strip().upper(),
            "category": str(category or "all").strip().lower(),
            "start": int(start),
            "limit": int(limit),
            "includeInternational": bool(include_international),
            "includeTestProducts": bool(include_test_products),
        }
        return self._post(
            _BITREFILL_LIST_PATH,
            payload,
            token=self.api_token,
            operation="list-bitrefill-products",
            timeout=self.purchase_timeout,
        )

    def get_bitrefill_product(self, *, product_id: str, country: str) -> dict[str, Any]:
        payload = {
            "productId": str(product_id or "").strip(),
            "country": str(country or "US").strip().upper(),
        }
        return self._post(
            _BITREFILL_PRODUCT_PATH,
            payload,
            token=self.api_token,
            operation="get-bitrefill-product",
            timeout=self.purchase_timeout,
        )

    def _buyer_email_request(
        self,
        identity: TelegramIdentity,
        *,
        action: str,
        email: str | None = None,
        user_access_token: str | None = None,
    ) -> dict:
        user_token = str(user_access_token or "").strip()
        if not user_token:
            raise GatewayClientError(_AUTH_FAILED)
        payload = {"telegramUserId": identity.user_id, "action": str(action)}
        if email is not None:
            payload["email"] = str(email).strip()
        return self._post(
            _BUYER_EMAIL_PATH,
            payload,
            token=self.api_token,
            operation="buyer-email",
            user_token=user_token,
        )

    def execute_buyer_email(
        self,
        identity: TelegramIdentity,
        *,
        action: str,
        email: str | None = None,
        user_access_token: str | None = None,
    ) -> str:
        """Read, set, or forget the address a guest invoice delivers to.

        Returns the masked address; the gateway never sends the full one back.
        """
        result = self._buyer_email_request(
            identity,
            action=action,
            email=email,
            user_access_token=user_access_token,
        )
        masked = result.get("email")
        if not isinstance(masked, str):
            raise GatewayClientError(_INVALID_RESPONSE)
        return masked.strip()

    def execute_buyer_email_state(
        self,
        identity: TelegramIdentity,
        *,
        user_access_token: str | None = None,
    ) -> dict:
        """Report the masked address and whether a purchase needs one.

        Only the gateway knows which checkout it buys through, so it is also
        the only place that can say whether an address is mandatory.
        """
        result = self._buyer_email_request(
            identity,
            action="get",
            user_access_token=user_access_token,
        )
        masked = result.get("email")
        if not isinstance(masked, str):
            raise GatewayClientError(_INVALID_RESPONSE)
        return {
            "email": masked.strip(),
            "required": bool(result.get("required")),
        }

    def execute_spending_limits(
        self,
        identity: TelegramIdentity,
        *,
        max_per_tx_usdc: str | None = None,
        daily_cap_usdc: str | None = None,
        user_access_token: str | None = None,
    ) -> str:
        user_token = str(user_access_token or "").strip()
        if not user_token:
            raise GatewayClientError(_AUTH_FAILED)
        payload = {"telegramUserId": identity.user_id}
        if identity.username:
            payload["telegramUsername"] = identity.username
        if max_per_tx_usdc is not None:
            payload["maxPerTxUsdc"] = str(max_per_tx_usdc).strip()
        if daily_cap_usdc is not None:
            payload["dailyCapUsdc"] = str(daily_cap_usdc).strip()
        result = self._post(
            _SPENDING_LIMITS_PATH,
            payload,
            token=self.api_token,
            operation="spending-limits",
            user_token=user_token,
        )
        telegram_text = result.get("telegramText")
        if not isinstance(telegram_text, str) or not telegram_text.strip():
            raise GatewayClientError(_INVALID_RESPONSE)
        return telegram_text.strip()

    def execute_llm(
        self,
        operation: str,
        identity: TelegramIdentity,
        *,
        payload: Mapping[str, Any] | None = None,
        user_access_token: str,
    ) -> dict[str, Any]:
        path = _LLM_OPERATION_PATHS.get(operation)
        if path is None:
            raise GatewayClientError(_UNSUPPORTED)
        user_token = str(user_access_token or "").strip()
        if not user_token:
            raise GatewayClientError(_AUTH_FAILED)

        body = dict(payload or {})
        body["telegramUserId"] = identity.user_id
        return self._post(
            path,
            body,
            token=self.api_token,
            operation=f"llm-{operation}",
            timeout=self.purchase_timeout,
            user_token=user_token,
        )

    def execute_chat(
        self,
        operation: str,
        identity: TelegramIdentity,
        *,
        payload: Mapping[str, Any] | None = None,
        user_access_token: str,
    ) -> dict[str, Any]:
        """Call an /agent/chat/* route.

        The prompt travels in the request body and is never logged here; the
        gateway is the only thing that sees it.
        """
        path = _CHAT_OPERATION_PATHS.get(operation)
        if path is None:
            raise GatewayClientError(_UNSUPPORTED)
        user_token = str(user_access_token or "").strip()
        if not user_token:
            raise GatewayClientError(_AUTH_FAILED)

        body = dict(payload or {})
        body["telegramUserId"] = identity.user_id
        return self._post(
            path,
            body,
            token=self.api_token,
            operation=f"chat-{operation}",
            timeout=self.purchase_timeout,
            user_token=user_token,
        )

    def withdraw_tokens(
        self,
        identity: TelegramIdentity,
        *,
        user_access_token: str,
    ) -> dict[str, Any]:
        user_token = str(user_access_token or "").strip()
        if not user_token:
            raise GatewayClientError(_AUTH_FAILED)
        return self._post(
            _WITHDRAW_TOKENS_PATH,
            {"telegramUserId": identity.user_id},
            token=self.api_token,
            operation="withdraw-tokens",
            user_token=user_token,
        )

    def execute_withdrawal(
        self,
        identity: TelegramIdentity,
        *,
        token_address: str,
        amount: str,
        to_address: str,
        user_access_token: str,
    ) -> str:
        user_token = str(user_access_token or "").strip()
        if not user_token:
            raise GatewayClientError(_AUTH_FAILED)
        result = self._post(
            _WITHDRAW_PATH,
            {
                "telegramUserId": identity.user_id,
                "tokenAddress": str(token_address or "").strip(),
                "amount": str(amount or "").strip(),
                "toAddress": str(to_address or "").strip(),
            },
            token=self.api_token,
            operation="withdraw",
            timeout=self.purchase_timeout,
            user_token=user_token,
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
        user_token: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if user_token:
            # Authenticates the caller AS a specific user so the gateway does
            # not trust the body-supplied telegramUserId alone.
            headers["X-Sign402-User-Token"] = str(user_token)
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        response = None
        try:
            response = self.opener(request, timeout=self.timeout if timeout is None else timeout)
            body = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            error_message = self._safe_http_error_message(exc, operation=operation)
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
            raise GatewayClientError(error_message or _REQUEST_FAILED) from None
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

    def _safe_http_error_message(self, exc: HTTPError, *, operation: str) -> str | None:
        is_bitrefill = operation in {"quote-bitrefill", "buy-wallet-bitrefill"}
        is_llm = operation.startswith("llm-")
        is_imessage = operation in _IMESSAGE_OPERATION_PATHS
        if not is_bitrefill and not is_llm and not is_imessage:
            return None
        try:
            body = exc.read(self.max_response_bytes + 1)
        except OSError:
            return None
        if len(body) > self.max_response_bytes:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if is_imessage:
            for key in ("imessageText", "telegramText"):
                text = payload.get(key)
                if isinstance(text, str) and text.strip():
                    return text.strip()
            return None
        if is_llm:
            telegram_text = payload.get("telegramText")
            if isinstance(telegram_text, str) and telegram_text.strip():
                return telegram_text.strip()
            return None
        error = str(payload.get("error") or "").strip()
        if not error:
            return None
        if error.startswith(_SPEND_LIMIT_PREFIX):
            # Already written for the buyer, and it leads with the fix — a
            # "request failed" prefix would bury that under noise.
            return f"{error}\n\n{_SPEND_LIMIT_HINT}"
        readable = _GATEWAY_ERROR_TEXTS.get(error)
        if readable is not None:
            return readable
        live_max_match = re.search(
            r"exceeds live Bitrefill max\s+(\$[0-9]+(?:\.[0-9]+)?)",
            error,
            flags=re.IGNORECASE,
        )
        if live_max_match:
            return (
                "This product exceeds the Bitrefill product maximum "
                f"({live_max_match.group(1)}), which is separate from your wallet limits. "
                "Choose a smaller product or ask the operator to raise the Bitrefill limit."
            )
        if (
            "\n" in error
            or "node_modules" in error.casefold()
            or "file://" in error.casefold()
            or error.casefold().startswith("apierror:")
        ):
            return "Bitrefill request failed. Please try another token or amount."
        return f"Bitrefill request failed: {error}"
