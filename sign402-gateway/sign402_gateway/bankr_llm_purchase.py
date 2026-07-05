import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_BANKR_API_URL = "https://api.bankr.bot"
DEFAULT_BANKR_LLM_URL = "https://llm.bankr.bot"
DEFAULT_BANKR_LLM_STORE_PATH = Path.home() / ".sign402" / "bankr-llm.db"
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


class BankrLlmStore:
    TERMINAL_STATES = frozenset(
        {
            "COMPLETE",
            "COMPLETED",
            "REJECTED",
            "EXPIRED",
            "FAILED_BEFORE_TRANSFER",
            "RECONCILIATION_REQUIRED",
        }
    )
    TRANSITION_FIELDS = {
        "approvalRequestId": "approval_request_id",
        "sourceWalletAddress": "source_wallet_address",
        "singitAmountAtomic": "singit_amount_atomic",
        "commitmentHash": "commitment_hash",
        "transferHash": "transfer_hash",
        "topupResultJson": "topup_result_json",
        "creditsJson": "credits_json",
        "errorCode": "error_code",
        "errorMessage": "error_message",
        "otpAttempts": "otp_attempts",
    }

    def __init__(self, path: Path, *, master_key: str):
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            self.fernet = Fernet(str(master_key or "").encode("ascii"))
        except Exception as exc:
            raise BankrLlmError(
                "invalid_configuration",
                "Bankr LLM store master key must be a valid Fernet key.",
            ) from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _enforce_private_mode(self.path.parent, 0o700)
        self._init_db()
        _enforce_private_mode(self.path, 0o600)

    def create_purchase(
        self,
        *,
        telegram_user_id: str,
        email: str,
        amount_usd: str,
        state: str,
        expires_at: int,
    ) -> dict[str, Any]:
        now = int(time.time())
        purchase_id = uuid.uuid4().hex
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO bankr_llm_purchases (
                    purchase_id, telegram_user_id, email, amount_usd, state,
                    expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    purchase_id,
                    str(telegram_user_id),
                    str(email),
                    str(amount_usd),
                    str(state),
                    int(expires_at),
                    now,
                    now,
                ),
            )
            self._record_audit(
                db,
                purchase_id=purchase_id,
                telegram_user_id=str(telegram_user_id),
                event_type="purchase_created",
            )
        purchase = self.get_purchase(purchase_id)
        if purchase is None:
            raise RuntimeError("purchase insert failed")
        return purchase

    def get_active_purchase(
        self,
        telegram_user_id: str,
    ) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in self.TERMINAL_STATES)
        parameters = [str(telegram_user_id), *sorted(self.TERMINAL_STATES)]
        with self.lock, self._database() as db:
            row = db.execute(
                f"""
                SELECT *
                FROM bankr_llm_purchases
                WHERE telegram_user_id = ?
                  AND state NOT IN ({placeholders})
                ORDER BY created_at DESC, purchase_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return _purchase_row_to_dict(row)

    def get_purchase(self, purchase_id: str) -> dict[str, Any] | None:
        with self.lock, self._database() as db:
            row = db.execute(
                """
                SELECT *
                FROM bankr_llm_purchases
                WHERE purchase_id = ?
                """,
                (str(purchase_id),),
            ).fetchone()
        return _purchase_row_to_dict(row)

    def record_terms_acceptance(
        self,
        telegram_user_id: str,
        *,
        accepted_at: int,
    ) -> None:
        user_id = str(telegram_user_id)
        now = int(time.time())
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO bankr_llm_users (
                    telegram_user_id, terms_accepted_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id)
                DO UPDATE SET
                    terms_accepted_at = excluded.terms_accepted_at,
                    updated_at = excluded.updated_at
                """,
                (user_id, int(accepted_at), now, now),
            )
            self._record_audit(
                db,
                purchase_id="",
                telegram_user_id=user_id,
                event_type="terms_accepted",
            )

    def has_accepted_terms(self, telegram_user_id: str) -> bool:
        with self.lock, self._database() as db:
            row = db.execute(
                """
                SELECT terms_accepted_at
                FROM bankr_llm_users
                WHERE telegram_user_id = ?
                """,
                (str(telegram_user_id),),
            ).fetchone()
        return row is not None and int(row["terms_accepted_at"]) > 0

    def save_bankr_identity(
        self,
        purchase_id: str,
        *,
        bankr_wallet_address: str,
        api_key: str,
    ) -> None:
        encrypted_key = self.fernet.encrypt(str(api_key).encode("utf-8")).decode(
            "ascii"
        )
        fingerprint = _api_key_fingerprint(str(api_key))
        now = int(time.time())
        with self.lock, self._database() as db:
            result = db.execute(
                """
                UPDATE bankr_llm_purchases
                SET bankr_wallet_address = ?,
                    encrypted_api_key = ?,
                    api_key_fingerprint = ?,
                    updated_at = ?
                WHERE purchase_id = ?
                """,
                (
                    str(bankr_wallet_address),
                    encrypted_key,
                    fingerprint,
                    now,
                    str(purchase_id),
                ),
            )
            if result.rowcount != 1:
                raise ValueError("purchase not found")
            row = db.execute(
                """
                SELECT telegram_user_id
                FROM bankr_llm_purchases
                WHERE purchase_id = ?
                """,
                (str(purchase_id),),
            ).fetchone()
            self._record_audit(
                db,
                purchase_id=str(purchase_id),
                telegram_user_id=str(row["telegram_user_id"]),
                event_type="bankr_identity_saved",
                metadata={"apiKeyFingerprint": fingerprint},
            )

    def decrypt_api_key(self, purchase: Mapping[str, Any]) -> str:
        purchase_id = str(
            purchase.get("purchaseId") or purchase.get("purchase_id") or ""
        )
        if not purchase_id:
            raise ValueError("purchaseId is required")
        with self.lock, self._database() as db:
            row = db.execute(
                """
                SELECT encrypted_api_key
                FROM bankr_llm_purchases
                WHERE purchase_id = ?
                """,
                (purchase_id,),
            ).fetchone()
        if row is None or not row["encrypted_api_key"]:
            raise ValueError("purchase has no Bankr API key")
        try:
            return self.fernet.decrypt(
                str(row["encrypted_api_key"]).encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise BankrLlmError(
                "invalid_api_key",
                "Stored Bankr API key could not be decrypted.",
            ) from exc

    def transition(
        self,
        purchase_id: str,
        *,
        expected_state: str,
        new_state: str,
        fields: Mapping[str, Any] | None = None,
    ) -> bool:
        updates = {
            "state": str(new_state),
            "updated_at": int(time.time()),
        }
        for field, value in (fields or {}).items():
            column = self.TRANSITION_FIELDS.get(str(field))
            if column is None:
                raise ValueError(f"unsupported transition field: {field}")
            updates[column] = str(value)

        assignments = ", ".join(f"{column} = ?" for column in updates)
        values = [updates[column] for column in updates]
        values.extend([str(purchase_id), str(expected_state)])
        with self.lock, self._database() as db:
            result = db.execute(
                f"""
                UPDATE bankr_llm_purchases
                SET {assignments}
                WHERE purchase_id = ? AND state = ?
                """,
                values,
            )
            if result.rowcount != 1:
                return False
            row = db.execute(
                """
                SELECT telegram_user_id
                FROM bankr_llm_purchases
                WHERE purchase_id = ?
                """,
                (str(purchase_id),),
            ).fetchone()
            self._record_audit(
                db,
                purchase_id=str(purchase_id),
                telegram_user_id=str(row["telegram_user_id"]),
                event_type="state_transition",
                metadata={
                    "expectedState": str(expected_state),
                    "newState": str(new_state),
                    "fields": sorted(str(field) for field in (fields or {})),
                },
            )
        return True

    def _init_db(self) -> None:
        with self._database() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bankr_llm_purchases (
                    purchase_id TEXT PRIMARY KEY,
                    telegram_user_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    state TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    bankr_wallet_address TEXT NOT NULL DEFAULT '',
                    encrypted_api_key TEXT NOT NULL DEFAULT '',
                    api_key_fingerprint TEXT NOT NULL DEFAULT '',
                    approval_request_id TEXT NOT NULL DEFAULT '',
                    source_wallet_address TEXT NOT NULL DEFAULT '',
                    singit_amount_atomic TEXT NOT NULL DEFAULT '',
                    commitment_hash TEXT NOT NULL DEFAULT '',
                    transfer_hash TEXT NOT NULL DEFAULT '',
                    topup_result_json TEXT NOT NULL DEFAULT '',
                    credits_json TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    otp_attempts TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bankr_llm_users (
                    telegram_user_id TEXT PRIMARY KEY,
                    terms_accepted_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bankr_llm_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    purchase_id TEXT NOT NULL DEFAULT '',
                    telegram_user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=5.0)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _record_audit(
        db: sqlite3.Connection,
        *,
        purchase_id: str,
        telegram_user_id: str,
        event_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO bankr_llm_audit (
                purchase_id, telegram_user_id, event_type, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(purchase_id or ""),
                str(telegram_user_id),
                str(event_type),
                json.dumps(
                    dict(metadata or {}),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                int(time.time()),
            ),
        )


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


def _api_key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:12]


def _purchase_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    fields = {
        "purchase_id": "purchaseId",
        "telegram_user_id": "telegramUserId",
        "email": "email",
        "amount_usd": "amountUsd",
        "state": "state",
        "expires_at": "expiresAt",
        "bankr_wallet_address": "bankrWalletAddress",
        "api_key_fingerprint": "apiKeyFingerprint",
        "approval_request_id": "approvalRequestId",
        "source_wallet_address": "sourceWalletAddress",
        "singit_amount_atomic": "singitAmountAtomic",
        "commitment_hash": "commitmentHash",
        "transfer_hash": "transferHash",
        "topup_result_json": "topupResultJson",
        "credits_json": "creditsJson",
        "error_code": "errorCode",
        "error_message": "errorMessage",
        "otp_attempts": "otpAttempts",
        "created_at": "createdAt",
        "updated_at": "updatedAt",
    }
    result: dict[str, Any] = {}
    for column, key in fields.items():
        if column in row.keys():
            result[key] = row[column]
    return result


def _enforce_private_mode(path: Path, mode: int) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(mode)
        actual = path.stat().st_mode & 0o777
    except OSError as exc:
        raise BankrLlmError(
            "invalid_configuration",
            "Bankr LLM store files must be private.",
        ) from exc
    if actual != mode:
        raise BankrLlmError(
            "invalid_configuration",
            "Bankr LLM store files must be private.",
        )
