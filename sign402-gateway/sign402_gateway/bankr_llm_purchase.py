import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable


DEFAULT_BANKR_API_URL = "https://api.bankr.bot"
DEFAULT_BANKR_LLM_URL = "https://llm.bankr.bot"
PRIVY_AUTH_URL = "https://auth.privy.io/api/v1"

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
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = 20.0,
    ):
        self.api_url = str(api_url).rstrip("/")
        self.llm_url = str(llm_url).rstrip("/")
        self.opener = opener
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
            error_scope="auth",
        )
        api_key = key_response.get("apiKey")
        if not isinstance(api_key, str) or not api_key.startswith("bk_"):
            self._invalid_response()

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
        return self._request_json(
            method="POST",
            url=f"{self.api_url}/llm/credits/topup",
            payload={
                "amountUsd": str(amount_usd),
                "chain": str(chain),
                "sourceToken": str(source_token),
            },
            headers={"X-API-Key": key},
            error_scope="llm",
        )

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
        try:
            response = self.opener(request, timeout=self.timeout)
            raw_body = response.read()
        except urllib.error.HTTPError as exc:
            self._close_quietly(exc)
            self._raise_http_error(exc.code, error_scope)
        except Exception:
            self._raise_unavailable(error_scope)
        finally:
            if response is not None:
                self._close_quietly(response)

        try:
            if isinstance(raw_body, bytes):
                body = raw_body.decode("utf-8")
            elif isinstance(raw_body, str):
                body = raw_body
            else:
                self._invalid_response()
            parsed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._invalid_response()
        if not isinstance(parsed, dict):
            self._invalid_response()
        return parsed

    def _normalize_key_metadata(
        self,
        key_response: dict[str, Any],
        *,
        key_name: str,
    ) -> dict[str, Any]:
        capabilities = _key_capabilities()
        for field, expected in capabilities.items():
            if field in key_response and key_response[field] != expected:
                self._invalid_response()

        name = key_response.get("name", key_name)
        if not isinstance(name, str) or not name:
            self._invalid_response()
        metadata: dict[str, Any] = {}
        key_id = key_response.get("id")
        if key_id is not None:
            if not isinstance(key_id, str) or not key_id:
                self._invalid_response()
            metadata["id"] = key_id
        metadata["name"] = name
        metadata.update(capabilities)
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
    def _invalid_response() -> None:
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
        BankrIdentityClient._raise_unavailable(error_scope)

    @staticmethod
    def _raise_unavailable(error_scope: str) -> None:
        if error_scope == "llm":
            raise BankrLlmError(
                "bankr_llm_unavailable",
                "Bankr LLM credits are unavailable. Please try again.",
            )
        raise BankrLlmError(
            "bankr_auth_unavailable",
            "Bankr authentication is unavailable. Please try again.",
        )
