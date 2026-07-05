import json
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


DEFAULT_BANKR_API_URL = "https://api.bankr.bot"
DEFAULT_BANKR_LLM_URL = "https://llm.bankr.bot"
PRIVY_AUTH_URL = "https://auth.privy.io/api/v1"
MAX_RESPONSE_BYTES = 64 * 1024

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
OTP_RE = re.compile(r"^\d{6}$")
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

KEY_CAPABILITIES: dict[str, Any] = {
    "walletApiEnabled": True,
    "agentApiEnabled": False,
    "readOnly": False,
    "tokenLaunchApiEnabled": False,
    "llmGatewayEnabled": True,
    "allowedIps": [],
    "allowedRecipients": [],
}


def _key_capabilities() -> dict[str, Any]:
    return {
        **KEY_CAPABILITIES,
        "allowedIps": [],
        "allowedRecipients": [],
    }


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


_DEFAULT_OPENER = urllib.request.build_opener(_RejectRedirectHandler())


class BankrLlmError(RuntimeError):
    def __init__(self, code: str, user_message: str):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


class BankrIdentityClient:
    def __init__(
        self,
        *,
        api_url: str = DEFAULT_BANKR_API_URL,
        llm_url: str = DEFAULT_BANKR_LLM_URL,
        opener: Callable[..., Any] | None = None,
        timeout: float = 20.0,
    ):
        self.api_url = self._validate_service_url(api_url)
        self.llm_url = self._validate_service_url(llm_url)
        self.opener = _DEFAULT_OPENER.open if opener is None else opener
        self.timeout = timeout

    def send_otp(self, email: str) -> None:
        email_value = self._validate_email(email)
        app_id, client_id = self._privy_config()
        self._request_json(
            method="POST",
            url=f"{PRIVY_AUTH_URL}/passwordless/init",
            payload={"email": email_value, "type": "email"},
            headers=self._privy_headers(app_id, client_id),
            error_scope="auth",
        )

    def verify_and_create_key(
        self,
        *,
        email: str,
        code: str,
        key_name: str,
        accept_terms: bool,
    ) -> dict[str, Any]:
        email_value = self._validate_email(email)
        code_value = str(code)
        if OTP_RE.fullmatch(code_value) is None:
            raise BankrLlmError(
                "invalid_otp",
                "Enter the six-digit verification code.",
            )

        app_id, client_id = self._privy_config()
        auth = self._request_json(
            method="POST",
            url=f"{PRIVY_AUTH_URL}/passwordless/authenticate",
            payload={
                "email": email_value,
                "code": code_value,
                "mode": "login-or-sign-up",
            },
            headers=self._privy_headers(app_id, client_id),
            error_scope="otp",
        )
        identity_token = auth.get("identity_token")
        if not isinstance(identity_token, str) or not identity_token:
            self._invalid_response()

        wallet = self._request_json(
            method="POST",
            url=f"{self.api_url}/cli/generate-wallet",
            payload={},
            headers=self._identity_headers(identity_token),
            error_scope="auth",
        )
        evm_address = wallet.get("evmAddress")
        if (
            not isinstance(evm_address, str)
            or EVM_ADDRESS_RE.fullmatch(evm_address) is None
        ):
            self._invalid_response()

        has_accepted_terms = wallet.get("hasAcceptedTerms")
        if not isinstance(has_accepted_terms, bool):
            self._invalid_response()
        if not has_accepted_terms:
            if not accept_terms:
                raise BankrLlmError(
                    "terms_required",
                    "Accept Bankr's terms before continuing.",
                )
            self.accept_terms(identity_token)

        key_payload = {"name": str(key_name), **_key_capabilities()}
        key_response = self._request_json(
            method="POST",
            url=f"{self.api_url}/api-keys",
            payload=key_payload,
            headers=self._identity_headers(identity_token),
            error_scope="key_creation",
        )
        api_key = key_response.get("apiKey")
        if not isinstance(api_key, str) or not api_key.startswith("bk_"):
            self._invalid_response("key_creation")

        return {
            "evmAddress": evm_address,
            "apiKey": api_key,
            "key": self._normalize_key_metadata(
                key_response,
                key_name=str(key_name),
            ),
        }

    def accept_terms(self, identity_token: str) -> None:
        if not isinstance(identity_token, str) or not identity_token:
            self._invalid_response()
        self._request_json(
            method="POST",
            url=f"{self.api_url}/user/accept-terms",
            payload={},
            headers=self._identity_headers(identity_token),
            error_scope="auth",
        )

    def top_up(
        self,
        *,
        api_key: str,
        amount_usd: str,
        source_token: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        key = self._validate_api_key(api_key)
        result = self._request_json(
            method="POST",
            url=f"{self.api_url}/llm/credits/topup",
            payload={
                "amountUsd": str(amount_usd),
                "chain": str(chain),
                "sourceToken": str(source_token),
            },
            headers={"X-API-Key": key},
            error_scope="topup",
        )
        self._validate_topup_result(result)
        return result

    def credits(self, *, api_key: str) -> dict[str, Any]:
        key = self._validate_api_key(api_key)
        return self._request_json(
            method="GET",
            url=f"{self.llm_url}/v1/credits",
            headers={"X-API-Key": key},
            error_scope="llm",
        )

    def _privy_config(self) -> tuple[str, str]:
        config = self._request_json(
            method="GET",
            url=f"{self.api_url}/cli/config",
            error_scope="auth",
        )
        app_id = config.get("privyAppId")
        client_id = config.get("privyClientId")
        if (
            not isinstance(app_id, str)
            or not app_id
            or not isinstance(client_id, str)
            or not client_id
        ):
            self._invalid_response()
        return app_id, client_id

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        error_scope: str,
    ) -> dict[str, Any]:
        request_headers = dict(headers or {})
        request_headers["Accept"] = "application/json"
        data = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers=request_headers,
            method=method,
        )

        response = None
        http_status = None
        transport_failed = False
        try:
            response = self.opener(request, timeout=self.timeout)
            raw_body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            http_status = exc.code
            self._close_quietly(exc)
        except Exception:
            transport_failed = True
        finally:
            if response is not None:
                self._close_quietly(response)

        if http_status is not None:
            self._raise_http_error(http_status, error_scope)
        if transport_failed:
            self._raise_unavailable(error_scope)
        if (
            isinstance(raw_body, (bytes, str))
            and len(raw_body) > MAX_RESPONSE_BYTES
        ):
            self._invalid_response(error_scope)

        parse_failed = False
        try:
            if isinstance(raw_body, bytes):
                body = raw_body.decode("utf-8")
            elif isinstance(raw_body, str):
                body = raw_body
            else:
                body = ""
                parse_failed = True
            parsed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
            parse_failed = True
        if parse_failed or not isinstance(parsed, dict):
            self._invalid_response(error_scope)
        return parsed

    def _normalize_key_metadata(
        self,
        key_response: dict[str, Any],
        *,
        key_name: str,
    ) -> dict[str, Any]:
        requested_capabilities = _key_capabilities()
        if key_response.get("llmGatewayEnabled") is not True:
            self._invalid_response("key_creation")
        for field, expected in requested_capabilities.items():
            if field == "llmGatewayEnabled":
                continue
            if field in key_response and key_response[field] != expected:
                self._invalid_response("key_creation")

        name = key_response.get("name", key_name)
        if not isinstance(name, str) or not name:
            self._invalid_response("key_creation")
        metadata: dict[str, Any] = {}
        key_id = key_response.get("id")
        if key_id is not None:
            if not isinstance(key_id, str) or not key_id:
                self._invalid_response("key_creation")
            metadata["id"] = key_id
        metadata["name"] = name
        metadata["llmGatewayEnabled"] = True
        metadata["requestedCapabilities"] = requested_capabilities
        return metadata

    @staticmethod
    def _validate_email(email: str) -> str:
        value = str(email)
        if EMAIL_RE.fullmatch(value) is None:
            raise BankrLlmError(
                "invalid_email",
                "Enter a valid email address.",
            )
        return value

    @staticmethod
    def _validate_api_key(api_key: str) -> str:
        value = str(api_key)
        if not value.startswith("bk_"):
            raise BankrLlmError(
                "invalid_api_key",
                "The Bankr API key is invalid.",
            )
        return value

    @staticmethod
    def _validate_service_url(url: str) -> str:
        value = str(url).rstrip("/")
        parse_failed = False
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            parsed = None
            parse_failed = True
        if (
            parse_failed
            or parsed is None
            or parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise BankrLlmError(
                "invalid_configuration",
                "Bankr service URLs must use HTTPS.",
            )
        return value

    @staticmethod
    def _validate_topup_result(result: dict[str, Any]) -> None:
        if result.get("success") is not True:
            BankrIdentityClient._invalid_response("topup")
        credits = result.get("credits")
        if isinstance(credits, dict):
            balance = credits.get("balanceUsd")
            if BankrIdentityClient._is_valid_usd_balance(balance):
                return
        balance = result.get("balanceUsd")
        if BankrIdentityClient._is_valid_usd_balance(balance):
            return
        BankrIdentityClient._invalid_response("topup")

    @staticmethod
    def _is_valid_usd_balance(value: Any) -> bool:
        if isinstance(value, bool):
            return False
        if not isinstance(value, (str, int, float, Decimal)):
            return False
        text = str(value).strip()
        if not text:
            return False
        try:
            amount = Decimal(text)
        except (InvalidOperation, ValueError):
            return False
        return amount.is_finite() and amount >= 0

    @staticmethod
    def _privy_headers(app_id: str, client_id: str) -> dict[str, str]:
        return {
            "Privy-App-Id": app_id,
            "Privy-Client-Id": client_id,
        }

    @staticmethod
    def _identity_headers(identity_token: str) -> dict[str, str]:
        return {"Privy-Id-Token": identity_token}

    @staticmethod
    def _close_quietly(response: Any) -> None:
        try:
            response.close()
        except Exception:
            pass

    @staticmethod
    def _invalid_response(error_scope: str | None = None) -> None:
        if error_scope in {"key_creation", "topup"}:
            BankrIdentityClient._raise_unavailable(error_scope)
        raise BankrLlmError(
            "invalid_response",
            "Bankr returned an invalid response. Please try again.",
        )

    @staticmethod
    def _raise_http_error(status: int, error_scope: str) -> None:
        if status == 429:
            raise BankrLlmError(
                "rate_limited",
                "Too many requests. Please try again later.",
            )
        if error_scope == "otp" and status in {400, 401, 403, 422}:
            raise BankrLlmError(
                "invalid_otp",
                "That verification code is invalid or expired.",
            )
        if error_scope == "key_creation":
            if 400 <= status < 500:
                raise BankrLlmError(
                    "bankr_key_creation_rejected",
                    "Bankr rejected the API key creation request.",
                )
            BankrIdentityClient._raise_unavailable(error_scope)
        if error_scope == "topup":
            if 400 <= status < 500:
                raise BankrLlmError(
                    "bankr_topup_rejected",
                    "Bankr rejected the LLM credit top-up.",
                )
            BankrIdentityClient._raise_unavailable(error_scope)
        BankrIdentityClient._raise_unavailable(error_scope)

    @staticmethod
    def _raise_unavailable(error_scope: str) -> None:
        if error_scope == "key_creation":
            raise BankrLlmError(
                "bankr_key_creation_ambiguous",
                "Bankr API key creation result is unclear. Please check status before retrying.",
            )
        if error_scope == "topup":
            raise BankrLlmError(
                "bankr_topup_ambiguous",
                "Bankr LLM credit top-up result is unclear. Do not retry automatically.",
            )
        if error_scope == "llm":
            raise BankrLlmError(
                "bankr_llm_unavailable",
                "Bankr LLM credits are unavailable. Please try again.",
            )
        raise BankrLlmError(
            "bankr_auth_unavailable",
            "Bankr authentication is unavailable. Please try again.",
        )
