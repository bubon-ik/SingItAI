import hashlib
import json
import math
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
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_MAINNET_NETWORK = "base-mainnet"
DEFAULT_SINGIT_TOKEN_ADDRESS = "0xc2c1e0b7C401e6217193732272444D928646eba3"
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
        existing_purchase: dict[str, Any] | None = None
        with self.lock, self._database() as db:
            active = self._get_active_purchase_row(db, str(telegram_user_id))
            if active is not None:
                existing_purchase = _purchase_row_to_dict(active)
            else:
                try:
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
                except sqlite3.IntegrityError:
                    active = self._get_active_purchase_row(db, str(telegram_user_id))
                    if active is not None:
                        existing_purchase = _purchase_row_to_dict(active)
                    else:
                        raise
                else:
                    self._record_audit(
                        db,
                        purchase_id=purchase_id,
                        telegram_user_id=str(telegram_user_id),
                        event_type="purchase_created",
                    )
        if existing_purchase is not None:
            return existing_purchase
        purchase = self.get_purchase(purchase_id)
        if purchase is None:
            raise RuntimeError("purchase insert failed")
        return purchase

    def get_active_purchase(
        self,
        telegram_user_id: str,
    ) -> dict[str, Any] | None:
        with self.lock, self._database() as db:
            row = self._get_active_purchase_row(db, str(telegram_user_id))
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
            self._repair_duplicate_active_purchases(db)
            terminal_states = ",".join(
                f"'{state}'" for state in sorted(self.TERMINAL_STATES)
            )
            db.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    bankr_llm_one_active_purchase_per_user
                ON bankr_llm_purchases(telegram_user_id)
                WHERE state NOT IN ({terminal_states})
                """
            )

    def _repair_duplicate_active_purchases(self, db: sqlite3.Connection) -> None:
        placeholders = ",".join("?" for _ in self.TERMINAL_STATES)
        terminal_parameters = sorted(self.TERMINAL_STATES)
        users = db.execute(
            f"""
            SELECT telegram_user_id
            FROM bankr_llm_purchases
            WHERE state NOT IN ({placeholders})
            GROUP BY telegram_user_id
            HAVING COUNT(*) > 1
            """,
            terminal_parameters,
        ).fetchall()
        now = int(time.time())
        for user in users:
            rows = db.execute(
                f"""
                SELECT purchase_id
                FROM bankr_llm_purchases
                WHERE telegram_user_id = ?
                  AND state NOT IN ({placeholders})
                ORDER BY created_at DESC, purchase_id DESC
                """,
                [str(user["telegram_user_id"]), *terminal_parameters],
            ).fetchall()
            for row in rows[1:]:
                db.execute(
                    """
                    UPDATE bankr_llm_purchases
                    SET state = ?,
                        error_code = ?,
                        error_message = ?,
                        updated_at = ?
                    WHERE purchase_id = ?
                    """,
                    (
                        "EXPIRED",
                        "duplicate_active_purchase",
                        "Superseded by a newer active purchase during startup repair.",
                        now,
                        str(row["purchase_id"]),
                    ),
                )

    def _get_active_purchase_row(
        self,
        db: sqlite3.Connection,
        telegram_user_id: str,
    ) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in self.TERMINAL_STATES)
        parameters = [str(telegram_user_id), *sorted(self.TERMINAL_STATES)]
        return db.execute(
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


class BankrLlmPurchaseService:
    def __init__(
        self,
        *,
        store: BankrLlmStore,
        bankr: BankrIdentityClient,
        wallet_service: Any,
        pricer: Any,
        approval_service: Any,
        transfer_client: Any,
        enforce_spend: Callable[[str, dict[str, Any]], None],
        record_spend: Callable[[str, dict[str, Any], dict[str, Any]], None],
        singit_token_address: str,
        now: Callable[[], float] = time.time,
        otp_ttl_seconds: int = 600,
        max_otp_attempts: int = 3,
    ):
        self.store = store
        self.bankr = bankr
        self.wallet_service = wallet_service
        self.pricer = pricer
        self.approval_service = approval_service
        self.transfer_client = transfer_client
        self.enforce_spend = enforce_spend
        self.record_spend = record_spend
        self.singit_token_address = str(singit_token_address)
        self._now = now
        self.otp_ttl_seconds = int(otp_ttl_seconds)
        self.max_otp_attempts = int(max_otp_attempts)

    def start(
        self,
        *,
        telegram_user_id: str,
        email: str,
        amount_usd: str,
    ) -> dict[str, Any]:
        user_id = self._require_user_id(telegram_user_id)
        email_value = BankrIdentityClient._validate_email(email)
        amount_value = self._validate_amount(amount_usd)

        active = self.store.get_active_purchase(user_id)
        if active is not None:
            if self._is_expired(active):
                return self._expire_purchase(active)
            return self._safe_purchase_response(active)

        state = (
            "AWAITING_OTP"
            if self.store.has_accepted_terms(user_id)
            else "AWAITING_TERMS"
        )
        purchase = self.store.create_purchase(
            telegram_user_id=user_id,
            email=email_value,
            amount_usd=amount_value,
            state=state,
            expires_at=int(self._now()) + self.otp_ttl_seconds,
        )
        if state == "AWAITING_OTP":
            self.bankr.send_otp(str(purchase["email"]))
        return self._safe_purchase_response(purchase)

    def accept_terms(self, telegram_user_id: str) -> dict[str, Any]:
        user_id = self._require_user_id(telegram_user_id)
        purchase = self.store.get_active_purchase(user_id)
        if purchase is None:
            return {
                "ok": False,
                "state": "NOT_STARTED",
                "telegramText": "Start with /llm_buy before accepting terms.",
            }
        if self._is_expired(purchase):
            return self._expire_purchase(purchase)

        self.store.record_terms_acceptance(user_id, accepted_at=int(self._now()))
        if purchase["state"] == "AWAITING_TERMS":
            transitioned = self.store.transition(
                purchase["purchaseId"],
                expected_state="AWAITING_TERMS",
                new_state="AWAITING_OTP",
            )
            purchase = self.store.get_purchase(purchase["purchaseId"]) or purchase
            if transitioned:
                self.bankr.send_otp(purchase["email"])
        return self._safe_purchase_response(purchase)

    def verify_otp(
        self,
        *,
        telegram_user_id: str,
        code: str,
    ) -> dict[str, Any]:
        user_id = self._require_user_id(telegram_user_id)
        purchase = self.store.get_active_purchase(user_id)
        if purchase is None:
            latest = self._latest_purchase_for_user(user_id)
            if latest is not None:
                return self._safe_purchase_response(latest)
            return {
                "ok": False,
                "state": "NOT_STARTED",
                "telegramText": "Start with /llm_buy before entering an OTP.",
            }
        if self._is_expired(purchase):
            return self._expire_purchase(purchase)
        if purchase["state"] == "AWAITING_TERMS":
            return self._safe_purchase_response(purchase)
        if purchase["state"] not in {"AWAITING_OTP", "BANKR_KEY_CREATED"}:
            return self._safe_purchase_response(purchase)

        wallet_status = self.wallet_service.wallet_status(user_id)
        if not wallet_status.get("ok"):
            failure_state = (
                "FAILED_BEFORE_TRANSFER"
                if purchase["state"] == "AWAITING_OTP"
                else purchase["state"]
            )
            self.store.transition(
                purchase["purchaseId"],
                expected_state=purchase["state"],
                new_state=failure_state,
                fields={
                    "errorCode": "wallet_unavailable",
                    "errorMessage": wallet_status.get(
                        "telegramText",
                        "No Base agent wallet is available.",
                    ),
                },
            )
            latest = self.store.get_purchase(purchase["purchaseId"]) or purchase
            return self._safe_purchase_response(latest)
        source_wallet = self._wallet_address(wallet_status)

        saved_bankr_wallet = str(purchase.get("bankrWalletAddress") or "")
        saved_key_fingerprint = str(purchase.get("apiKeyFingerprint") or "")
        if saved_bankr_wallet and saved_key_fingerprint:
            bankr_wallet = self._require_evm_address(saved_bankr_wallet)
            if purchase["state"] == "AWAITING_OTP":
                transitioned = self.store.transition(
                    purchase["purchaseId"],
                    expected_state="AWAITING_OTP",
                    new_state="BANKR_KEY_CREATED",
                )
                purchase = self.store.get_purchase(purchase["purchaseId"]) or purchase
                if not transitioned:
                    return self._safe_purchase_response(purchase)
        elif saved_bankr_wallet or saved_key_fingerprint:
            raise BankrLlmError(
                "bankr_key_creation_ambiguous",
                "Bankr API key creation result is unclear. Please check status before retrying.",
            )
        else:
            transitioned = self.store.transition(
                purchase["purchaseId"],
                expected_state="AWAITING_OTP",
                new_state="CREATING_BANKR_KEY",
            )
            purchase = self.store.get_purchase(purchase["purchaseId"]) or purchase
            if not transitioned:
                return self._safe_purchase_response(purchase)
            try:
                identity = self.bankr.verify_and_create_key(
                    email=purchase["email"],
                    code=str(code),
                    key_name=f"Sign402-{purchase['purchaseId'][:8]}",
                    accept_terms=True,
                )
            except BankrLlmError as exc:
                if exc.code == "invalid_otp":
                    return self._record_invalid_otp(
                        purchase,
                        expected_state="CREATING_BANKR_KEY",
                    )
                return self._mark_key_creation_uncertain(purchase, exc)

            try:
                bankr_wallet = self._require_evm_address(identity.get("evmAddress"))
                api_key = str(identity.get("apiKey") or "")
                if not api_key.startswith("bk_"):
                    raise BankrLlmError(
                        "bankr_key_creation_ambiguous",
                        "Bankr API key creation result is unclear. Please check status before retrying.",
                    )
            except BankrLlmError as exc:
                return self._mark_key_creation_uncertain(purchase, exc)
            self.store.save_bankr_identity(
                purchase["purchaseId"],
                bankr_wallet_address=bankr_wallet,
                api_key=api_key,
            )
            transitioned = self.store.transition(
                purchase["purchaseId"],
                expected_state="CREATING_BANKR_KEY",
                new_state="BANKR_KEY_CREATED",
            )
            purchase = self.store.get_purchase(purchase["purchaseId"]) or purchase
            if not transitioned:
                return self._safe_purchase_response(purchase)

        pricing = self.pricer.price_for_usdc(purchase["amountUsd"])
        singit_atomic = str(pricing.get("requiredSingitAtomic") or "")
        if not singit_atomic.isdigit() or int(singit_atomic) <= 0:
            raise BankrLlmError(
                "invalid_pricing",
                "SINGIT pricing is unavailable. Please try again.",
            )
        spend_context = self._spend_context(
            purchase,
            singit_amount_atomic=singit_atomic,
            source_wallet_address=source_wallet,
        )
        self.enforce_spend(user_id, spend_context)

        commitment = self._commitment(
            purchase,
            singit_amount_atomic=singit_atomic,
            source_wallet_address=source_wallet,
            bankr_wallet_address=bankr_wallet,
        )
        commitment_hash = hashlib.sha256(
            json.dumps(commitment, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        transitioned = self.store.transition(
            purchase["purchaseId"],
            expected_state="BANKR_KEY_CREATED",
            new_state="AWAITING_IMESSAGE_APPROVAL",
            fields={
                "sourceWalletAddress": source_wallet,
                "singitAmountAtomic": singit_atomic,
                "commitmentHash": commitment_hash,
                "errorCode": "",
                "errorMessage": "",
            },
        )
        purchase = self.store.get_purchase(purchase["purchaseId"]) or purchase
        if not transitioned:
            return self._safe_purchase_response(purchase)

        approval = self.approval_service.request_hash_approval(
            telegram_user_id=user_id,
            action_type="sign402_bankr_llm",
            commitment_hash=commitment_hash,
            context_lines=self._approval_context_lines(commitment, commitment_hash),
        )
        return self._apply_approval_result(purchase, approval)

    def resume(self, purchase_id: str) -> dict[str, Any]:
        purchase = self._purchase_by_id(purchase_id)
        state = str(purchase.get("state") or "")
        if state == "AWAITING_TRANSFER":
            transitioned = self.store.transition(
                purchase["purchaseId"],
                expected_state="AWAITING_TRANSFER",
                new_state="TRANSFERRING_SINGIT",
            )
            purchase = self.store.get_purchase(purchase["purchaseId"]) or purchase
            if not transitioned:
                return self._safe_purchase_response(purchase)
            return self._execute_transfer(purchase)
        if state == "TRANSFERRING_SINGIT":
            if purchase.get("transferHash"):
                return self._top_up_from_funded_wallet(purchase)
            return self._hold_for_transfer_reconciliation(
                purchase,
                expected_state="TRANSFERRING_SINGIT",
            )
        if state == "TOPPING_UP_BANKR":
            return self._top_up_from_funded_wallet(purchase)
        return self._safe_purchase_response(purchase)

    def reconcile(self, purchase_id: str) -> dict[str, Any]:
        purchase = self._purchase_by_id(purchase_id)
        state = str(purchase.get("state") or "")
        if state == "COMPLETE":
            return self._safe_purchase_response(purchase)
        if state not in {
            "RECONCILIATION_REQUIRED",
            "TOPPING_UP_BANKR",
            "TRANSFERRING_SINGIT",
        }:
            return self._safe_purchase_response(purchase)
        if not purchase.get("transferHash"):
            return self._safe_purchase_response(purchase)

        api_key = self.store.decrypt_api_key(purchase)
        try:
            credits = self.bankr.credits(api_key=api_key)
        except BankrLlmError as exc:
            self.store.transition(
                purchase["purchaseId"],
                expected_state=state,
                new_state="RECONCILIATION_REQUIRED",
                fields={
                    "errorCode": exc.code,
                    "errorMessage": exc.user_message,
                },
            )
            loaded = self.store.get_purchase(purchase["purchaseId"]) or purchase
            return self._safe_purchase_response(loaded)

        if self._credits_cover_purchase(credits, purchase):
            return self._complete_purchase(
                purchase,
                expected_state=state,
                api_key=api_key,
                reveal_api_key=True,
                credits=credits,
            )
        return self._top_up_from_funded_wallet(
            purchase,
            api_key=api_key,
            expected_state=state,
            reveal_api_key=True,
        )

    def credits(self, telegram_user_id: str) -> dict[str, Any]:
        user_id = self._require_user_id(telegram_user_id)
        purchase = self.store.get_active_purchase(user_id)
        if purchase is None:
            purchase = self._latest_purchase_for_user(user_id)
        if purchase is None:
            return {
                "ok": False,
                "state": "NOT_STARTED",
                "telegramText": "No Bankr LLM purchase was found.",
            }
        if not purchase.get("apiKeyFingerprint"):
            return self._safe_purchase_response(purchase)
        credits = self.bankr.credits(api_key=self.store.decrypt_api_key(purchase))
        result = self._safe_purchase_response(purchase)
        result["credits"] = credits
        return result

    def _purchase_by_id(self, purchase_id: str) -> dict[str, Any]:
        purchase = self.store.get_purchase(str(purchase_id or ""))
        if purchase is None:
            raise BankrLlmError(
                "purchase_not_found",
                "Bankr LLM purchase was not found.",
            )
        return purchase

    def _execute_transfer(self, purchase: Mapping[str, Any]) -> dict[str, Any]:
        user_id = str(purchase["telegramUserId"])
        try:
            pricing = self.pricer.price_for_usdc(purchase["amountUsd"])
            fresh_atomic = self._pricing_atomic(pricing)
            fresh_amount = self._pricing_transfer_amount(
                pricing,
                expected_atomic=fresh_atomic,
            )
            approved_atomic = self._positive_int(purchase.get("singitAmountAtomic"))
            if fresh_atomic > approved_atomic:
                return self._fail_before_transfer(
                    purchase,
                    expected_state="TRANSFERRING_SINGIT",
                    code="price_exceeds_approved_max",
                    message="Fresh SINGIT pricing exceeds the approved maximum spend.",
                )

            wallet_status = self.wallet_service.wallet_status(user_id)
            if not wallet_status.get("ok"):
                return self._fail_before_transfer(
                    purchase,
                    expected_state="TRANSFERRING_SINGIT",
                    code="wallet_unavailable",
                    message=str(
                        wallet_status.get(
                            "telegramText",
                            "No Base agent wallet is available.",
                        )
                    ),
                )
            source_wallet = self._wallet_address(wallet_status)
            approved_source = self._require_evm_address(
                purchase.get("sourceWalletAddress")
            )
            if source_wallet.lower() != approved_source.lower():
                return self._fail_before_transfer(
                    purchase,
                    expected_state="TRANSFERRING_SINGIT",
                    code="source_wallet_changed",
                    message="The managed wallet changed after approval. Restart the purchase.",
                )

            spend_context = self._spend_context(
                purchase,
                singit_amount_atomic=str(fresh_atomic),
                source_wallet_address=source_wallet,
            )
            self.enforce_spend(user_id, spend_context)
            balance_error = self._balance_error(user_id, fresh_atomic)
            if balance_error is not None:
                return self._fail_before_transfer(
                    purchase,
                    expected_state="TRANSFERRING_SINGIT",
                    code=balance_error[0],
                    message=balance_error[1],
                )

            private_key = self.wallet_service.decrypt_private_key_for_future_signing(
                user_id
            )
        except BankrLlmError as exc:
            return self._fail_before_transfer(
                purchase,
                expected_state="TRANSFERRING_SINGIT",
                code=exc.code,
                message=exc.user_message,
            )
        except Exception:
            return self._fail_before_transfer(
                purchase,
                expected_state="TRANSFERRING_SINGIT",
                code="pre_transfer_failed",
                message="The purchase could not be prepared for transfer. No funds moved.",
            )

        try:
            transfer = self.transfer_client.transfer_token(
                private_key=private_key,
                to_address=self._require_evm_address(purchase["bankrWalletAddress"]),
                token_address=self.singit_token_address,
                amount=fresh_amount,
                chain="base",
            )
            transfer_hash = self._transfer_hash(transfer)
        except Exception:
            return self._hold_for_transfer_reconciliation(
                purchase,
                expected_state="TRANSFERRING_SINGIT",
            )

        transitioned = self.store.transition(
            purchase["purchaseId"],
            expected_state="TRANSFERRING_SINGIT",
            new_state="TOPPING_UP_BANKR",
            fields={
                "singitAmountAtomic": str(fresh_atomic),
                "transferHash": transfer_hash,
                "errorCode": "",
                "errorMessage": "",
            },
        )
        loaded = self.store.get_purchase(purchase["purchaseId"]) or purchase
        if transitioned:
            return self._top_up_from_funded_wallet(loaded)
        if (
            loaded.get("state") == "TOPPING_UP_BANKR"
            and loaded.get("transferHash") == transfer_hash
        ):
            return self._top_up_from_funded_wallet(loaded)
        if loaded.get("state") == "TRANSFERRING_SINGIT":
            return self._hold_for_transfer_reconciliation(
                loaded,
                expected_state="TRANSFERRING_SINGIT",
                transfer_hash=transfer_hash,
                singit_amount_atomic=str(fresh_atomic),
            )
        return self._safe_purchase_response(loaded)

    def _hold_for_transfer_reconciliation(
        self,
        purchase: Mapping[str, Any],
        *,
        expected_state: str,
        transfer_hash: str | None = None,
        singit_amount_atomic: str | None = None,
    ) -> dict[str, Any]:
        fields = {
            "errorCode": "transfer_ambiguous",
            "errorMessage": (
                "SINGIT transfer result is unclear. Reconciliation is required."
            ),
        }
        if transfer_hash:
            fields["transferHash"] = transfer_hash
        if singit_amount_atomic:
            fields["singitAmountAtomic"] = singit_amount_atomic
        self.store.transition(
            purchase["purchaseId"],
            expected_state=expected_state,
            new_state="RECONCILIATION_REQUIRED",
            fields=fields,
        )
        loaded = self.store.get_purchase(purchase["purchaseId"]) or purchase
        return self._safe_purchase_response(loaded)

    def _top_up_from_funded_wallet(
        self,
        purchase: Mapping[str, Any],
        *,
        api_key: str | None = None,
        expected_state: str | None = None,
        reveal_api_key: bool = True,
    ) -> dict[str, Any]:
        state = str(expected_state or purchase.get("state") or "")
        key = api_key if api_key is not None else self.store.decrypt_api_key(purchase)
        try:
            topup = self.bankr.top_up(
                api_key=key,
                amount_usd=str(purchase["amountUsd"]),
                source_token="SINGIT",
                chain="base",
            )
        except BankrLlmError as exc:
            self.store.transition(
                purchase["purchaseId"],
                expected_state=state,
                new_state="RECONCILIATION_REQUIRED",
                fields={
                    "errorCode": exc.code,
                    "errorMessage": exc.user_message,
                },
            )
            loaded = self.store.get_purchase(purchase["purchaseId"]) or purchase
            return self._safe_purchase_response(loaded)
        except Exception:
            self.store.transition(
                purchase["purchaseId"],
                expected_state=state,
                new_state="RECONCILIATION_REQUIRED",
                fields={
                    "errorCode": "bankr_topup_ambiguous",
                    "errorMessage": "Bankr LLM credit top-up result is unclear. Do not retry automatically.",
                },
            )
            loaded = self.store.get_purchase(purchase["purchaseId"]) or purchase
            return self._safe_purchase_response(loaded)

        return self._complete_purchase(
            purchase,
            expected_state=state,
            api_key=key,
            reveal_api_key=reveal_api_key,
            topup=topup,
        )

    def _complete_purchase(
        self,
        purchase: Mapping[str, Any],
        *,
        expected_state: str,
        api_key: str,
        reveal_api_key: bool,
        topup: Mapping[str, Any] | None = None,
        credits: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "errorCode": "",
            "errorMessage": "",
        }
        if topup is not None:
            fields["topupResultJson"] = json.dumps(
                dict(topup),
                sort_keys=True,
                separators=(",", ":"),
            )
        if credits is not None:
            fields["creditsJson"] = json.dumps(
                dict(credits),
                sort_keys=True,
                separators=(",", ":"),
            )
        transitioned = self.store.transition(
            purchase["purchaseId"],
            expected_state=expected_state,
            new_state="COMPLETE",
            fields=fields,
        )
        loaded = self.store.get_purchase(purchase["purchaseId"]) or purchase
        if transitioned:
            spend_context = self._spend_context(
                loaded,
                singit_amount_atomic=str(
                    loaded.get("singitAmountAtomic") or ""
                ),
                source_wallet_address=str(
                    loaded.get("sourceWalletAddress") or ""
                ),
            )
            self.record_spend(
                str(loaded["telegramUserId"]),
                loaded,
                {
                    "transferHash": str(loaded.get("transferHash") or ""),
                    **spend_context,
                },
            )
        result = self._safe_purchase_response(loaded)
        if transitioned and reveal_api_key:
            result["apiKey"] = api_key
        return result

    def _fail_before_transfer(
        self,
        purchase: Mapping[str, Any],
        *,
        expected_state: str,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        self.store.transition(
            purchase["purchaseId"],
            expected_state=expected_state,
            new_state="FAILED_BEFORE_TRANSFER",
            fields={
                "errorCode": code,
                "errorMessage": message,
            },
        )
        loaded = self.store.get_purchase(purchase["purchaseId"]) or purchase
        return self._safe_purchase_response(loaded)

    def _apply_approval_result(
        self,
        purchase: Mapping[str, Any],
        approval: Mapping[str, Any],
    ) -> dict[str, Any]:
        status = str(approval.get("status") or "").lower()
        approved = approval.get("approved") is True or status == "approved"
        fields: dict[str, Any] = {}
        approval_id = approval.get("approvalId")
        if isinstance(approval_id, str) and approval_id:
            fields["approvalRequestId"] = approval_id
        if approved:
            new_state = "AWAITING_TRANSFER"
        elif status == "expired":
            new_state = "EXPIRED"
            fields["errorCode"] = "approval_expired"
            fields["errorMessage"] = "The iMessage approval expired."
        else:
            new_state = "REJECTED"
            fields["errorCode"] = "approval_rejected"
            fields["errorMessage"] = "The iMessage approval was rejected."

        self.store.transition(
            purchase["purchaseId"],
            expected_state="AWAITING_IMESSAGE_APPROVAL",
            new_state=new_state,
            fields=fields,
        )
        loaded = self.store.get_purchase(purchase["purchaseId"]) or dict(purchase)
        return self._safe_purchase_response(loaded)

    def _mark_key_creation_uncertain(
        self,
        purchase: Mapping[str, Any],
        error: BankrLlmError,
    ) -> dict[str, Any]:
        self.store.transition(
            purchase["purchaseId"],
            expected_state="CREATING_BANKR_KEY",
            new_state="BANKR_KEY_CREATION_UNCERTAIN",
            fields={
                "errorCode": error.code,
                "errorMessage": error.user_message,
            },
        )
        loaded = self.store.get_purchase(purchase["purchaseId"]) or dict(purchase)
        return self._safe_purchase_response(loaded)

    def _record_invalid_otp(
        self,
        purchase: Mapping[str, Any],
        *,
        expected_state: str = "AWAITING_OTP",
    ) -> dict[str, Any]:
        attempts = self._otp_attempts(purchase) + 1
        if attempts >= self.max_otp_attempts:
            self.store.transition(
                purchase["purchaseId"],
                expected_state=expected_state,
                new_state="EXPIRED",
                fields={
                    "otpAttempts": attempts,
                    "errorCode": "invalid_otp",
                    "errorMessage": "Too many invalid verification codes.",
                },
            )
        else:
            self.store.transition(
                purchase["purchaseId"],
                expected_state=expected_state,
                new_state="AWAITING_OTP",
                fields={"otpAttempts": attempts},
            )
        loaded = self.store.get_purchase(purchase["purchaseId"]) or dict(purchase)
        return self._safe_purchase_response(loaded)

    def _expire_purchase(self, purchase: Mapping[str, Any]) -> dict[str, Any]:
        if purchase["state"] not in BankrLlmStore.TERMINAL_STATES:
            self.store.transition(
                purchase["purchaseId"],
                expected_state=purchase["state"],
                new_state="EXPIRED",
                fields={
                    "errorCode": "expired",
                    "errorMessage": "This Bankr LLM purchase expired.",
                },
            )
            purchase = self.store.get_purchase(purchase["purchaseId"]) or purchase
        return self._safe_purchase_response(purchase)

    def _safe_purchase_response(self, purchase: Mapping[str, Any]) -> dict[str, Any]:
        state = str(purchase.get("state") or "")
        result = {
            "ok": state not in {"REJECTED", "EXPIRED", "FAILED_BEFORE_TRANSFER"}
            and not bool(purchase.get("errorCode")),
            "purchaseId": purchase.get("purchaseId"),
            "state": state,
            "amountUsd": purchase.get("amountUsd"),
            "expiresAt": purchase.get("expiresAt"),
        }
        for key in (
            "commitmentHash",
            "singitAmountAtomic",
            "sourceWalletAddress",
            "bankrWalletAddress",
            "apiKeyFingerprint",
            "approvalRequestId",
            "transferHash",
            "errorCode",
            "errorMessage",
        ):
            if purchase.get(key):
                result[key] = purchase[key]
        result["telegramText"] = self._telegram_text(result)
        return result

    def _telegram_text(self, response: Mapping[str, Any]) -> str:
        state = response.get("state")
        if response.get("errorCode") == "wallet_unavailable":
            return str(
                response.get("errorMessage")
                or "Wallet service is temporarily unavailable. Please try again."
            )
        if state == "AWAITING_TERMS":
            return (
                "Review Bankr LLM purchase terms, then send /llm_terms accept "
                "to email your verification code."
            )
        if state == "AWAITING_OTP":
            return "Verification code sent. Reply with the six-digit OTP."
        if state == "CREATING_BANKR_KEY":
            return "Bankr key creation is already in progress. Please wait."
        if state == "BANKR_KEY_CREATION_UNCERTAIN":
            return (
                "Bankr key creation is unclear. Contact the operator before retrying."
            )
        if state == "BANKR_KEY_CREATED":
            return "Bankr key is ready. Continue with iMessage approval."
        if state == "AWAITING_IMESSAGE_APPROVAL":
            return "iMessage approval is pending. Reply there to continue."
        if state == "AWAITING_TRANSFER":
            return (
                "Approved. This purchase is waiting for SINGIT funding."
            )
        if state == "TRANSFERRING_SINGIT":
            return "SINGIT transfer is in progress."
        if state == "TOPPING_UP_BANKR":
            return "SINGIT transfer is confirmed. Bankr LLM credits are topping up."
        if state == "RECONCILIATION_REQUIRED":
            return (
                "SINGIT transfer status needs reconciliation before the Bankr key "
                "can be revealed."
            )
        if state == "COMPLETE":
            return (
                f"Bankr LLM purchase complete for ${response.get('amountUsd')}. "
                "Store the API key securely."
            )
        if state == "REJECTED":
            return "The Bankr LLM purchase was rejected. No funds moved."
        if state == "EXPIRED":
            return "This Bankr LLM purchase expired. No funds moved."
        if state == "FAILED_BEFORE_TRANSFER":
            return "This Bankr LLM purchase could not continue. No funds moved."
        return "Bankr LLM purchase status is available."

    def _commitment(
        self,
        purchase: Mapping[str, Any],
        *,
        singit_amount_atomic: str,
        source_wallet_address: str,
        bankr_wallet_address: str,
    ) -> dict[str, Any]:
        return {
            "purchaseId": str(purchase["purchaseId"]),
            "amountUsd": str(purchase["amountUsd"]),
            "singitAmountAtomic": str(singit_amount_atomic),
            "sourceWalletAddress": str(source_wallet_address),
            "bankrWalletAddress": str(bankr_wallet_address),
            "apiKeyFingerprint": str(purchase["apiKeyFingerprint"]),
            "expiresAt": int(purchase["expiresAt"]),
        }

    def _approval_context_lines(
        self,
        commitment: Mapping[str, Any],
        commitment_hash: str,
    ) -> list[str]:
        return [
            f"Commitment hash: {commitment_hash}",
            f"USD amount: {commitment['amountUsd']}",
            f"SINGIT atomic max: {commitment['singitAmountAtomic']}",
            f"Source wallet: {commitment['sourceWalletAddress']}",
            f"Bankr wallet: {commitment['bankrWalletAddress']}",
            f"Key fingerprint: {commitment['apiKeyFingerprint']}",
            f"Expires at: {commitment['expiresAt']}",
        ]

    def _spend_context(
        self,
        purchase: Mapping[str, Any],
        *,
        singit_amount_atomic: str,
        source_wallet_address: str,
    ) -> dict[str, Any]:
        return {
            "purchaseId": str(purchase["purchaseId"]),
            "amountUsd": str(purchase["amountUsd"]),
            "amountAtomic": self._usd_atomic(purchase["amountUsd"]),
            "asset": BASE_USDC_ADDRESS,
            "network": BASE_MAINNET_NETWORK,
            "singitAmountAtomic": str(singit_amount_atomic),
            "singitTokenAddress": self.singit_token_address,
            "sourceWalletAddress": str(source_wallet_address),
            "purpose": "bankr_llm_topup",
        }

    @staticmethod
    def _usd_atomic(value: Any) -> str:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            amount = Decimal("NaN")
        atomic = amount * Decimal("1000000")
        if (
            not amount.is_finite()
            or amount <= 0
            or atomic != atomic.to_integral_value()
        ):
            raise BankrLlmError(
                "invalid_amount",
                "Enter a USD amount from 1.00 to 1000.00.",
            )
        return str(int(atomic))

    @staticmethod
    def _pricing_atomic(pricing: Mapping[str, Any]) -> int:
        singit_atomic = str(pricing.get("requiredSingitAtomic") or "")
        if not singit_atomic.isdigit() or int(singit_atomic) <= 0:
            raise BankrLlmError(
                "invalid_pricing",
                "SINGIT pricing is unavailable. Please try again.",
            )
        return int(singit_atomic)

    @staticmethod
    def _pricing_transfer_amount(
        pricing: Mapping[str, Any],
        *,
        expected_atomic: int,
    ) -> str:
        try:
            amount = Decimal(str(pricing.get("requiredSingit") or ""))
        except (InvalidOperation, ValueError):
            amount = Decimal(0)
        atomic = amount * Decimal("1000000000000000000")
        if (
            not amount.is_finite()
            or amount <= 0
            or atomic != atomic.to_integral_value()
            or int(atomic) != expected_atomic
        ):
            raise BankrLlmError(
                "invalid_pricing",
                "SINGIT pricing is unavailable. Please try again.",
            )
        return format(amount, "f")

    @staticmethod
    def _positive_int(value: Any) -> int:
        text = str(value or "")
        if not text.isdigit() or int(text) <= 0:
            raise BankrLlmError(
                "invalid_purchase_state",
                "Bankr LLM purchase is missing approved SINGIT pricing.",
            )
        return int(text)

    def _balance_error(
        self,
        telegram_user_id: str,
        required_atomic: int,
    ) -> tuple[str, str] | None:
        wallet_balance = getattr(self.wallet_service, "wallet_balance", None)
        if not callable(wallet_balance):
            return None
        balance = wallet_balance(telegram_user_id)
        if not isinstance(balance, Mapping) or not balance.get("ok"):
            return (
                "wallet_balance_unavailable",
                "Managed wallet balance is unavailable. Try again before transfer.",
            )
        if balance.get("balanceUnavailable"):
            return (
                "wallet_balance_unavailable",
                "Managed wallet balance is unavailable. Try again before transfer.",
            )
        balances = balance.get("balances")
        if not isinstance(balances, Mapping):
            return (
                "wallet_balance_unavailable",
                "Managed wallet balance is unavailable. Try again before transfer.",
            )
        available = self._singit_balance_atomic(balances)
        if available < required_atomic:
            return (
                "insufficient_singit_balance",
                "Managed wallet does not have enough SINGIT for this purchase.",
            )
        return None

    @staticmethod
    def _singit_balance_atomic(balances: Mapping[str, Any]) -> int:
        value = None
        for key, balance in balances.items():
            if str(key).lower() == "singit":
                value = balance
                break
        if value is None:
            return 0
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return 0
        if not amount.is_finite() or amount < 0:
            return 0
        return int(amount * Decimal("1000000000000000000"))

    @staticmethod
    def _transfer_hash(transfer: Mapping[str, Any]) -> str:
        tx_hash = str(
            transfer.get("txId")
            or transfer.get("transactionHash")
            or transfer.get("hash")
            or ""
        ).strip()
        if not tx_hash:
            raise BankrLlmError(
                "transfer_ambiguous",
                "SINGIT transfer result is unclear. Reconciliation is required.",
            )
        return tx_hash

    @staticmethod
    def _credits_cover_purchase(
        credits: Mapping[str, Any],
        purchase: Mapping[str, Any],
    ) -> bool:
        balance = BankrLlmPurchaseService._credits_usd_balance(credits)
        if balance is None:
            return False
        try:
            expected = Decimal(str(purchase.get("amountUsd") or ""))
        except (InvalidOperation, ValueError):
            return False
        return balance >= expected

    @staticmethod
    def _credits_usd_balance(credits: Mapping[str, Any]) -> Decimal | None:
        candidates = [
            credits.get("credits"),
            credits.get("balanceUsd"),
        ]
        nested = credits.get("credits")
        if isinstance(nested, Mapping):
            candidates.append(nested.get("balanceUsd"))
        for candidate in candidates:
            if isinstance(candidate, bool) or candidate is None:
                continue
            try:
                amount = Decimal(str(candidate))
            except (InvalidOperation, ValueError):
                continue
            if amount.is_finite() and amount >= 0:
                return amount
        return None

    def _latest_purchase_for_user(self, telegram_user_id: str) -> dict[str, Any] | None:
        with self.store.lock, self.store._database() as db:
            row = db.execute(
                """
                SELECT *
                FROM bankr_llm_purchases
                WHERE telegram_user_id = ?
                ORDER BY created_at DESC, purchase_id DESC
                LIMIT 1
                """,
                (str(telegram_user_id),),
            ).fetchone()
        return _purchase_row_to_dict(row)

    @staticmethod
    def _otp_attempts(purchase: Mapping[str, Any]) -> int:
        try:
            return int(purchase.get("otpAttempts") or 0)
        except (TypeError, ValueError):
            return 0

    def _is_expired(self, purchase: Mapping[str, Any]) -> bool:
        return int(purchase.get("expiresAt") or 0) <= int(self._now())

    @staticmethod
    def _wallet_address(wallet_status: Mapping[str, Any]) -> str:
        wallet = wallet_status.get("wallet")
        if not isinstance(wallet, Mapping):
            raise BankrLlmError(
                "wallet_unavailable",
                "No Base agent wallet is available.",
            )
        return BankrLlmPurchaseService._require_evm_address(wallet.get("address"))

    @staticmethod
    def _require_evm_address(value: Any) -> str:
        address = str(value or "")
        if EVM_ADDRESS_RE.fullmatch(address) is None:
            raise BankrLlmError(
                "wallet_unavailable",
                "A valid Base wallet address is required.",
            )
        return address

    @staticmethod
    def _require_user_id(value: str) -> str:
        user_id = str(value or "").strip()
        if not user_id:
            raise BankrLlmError(
                "invalid_telegram_user",
                "Telegram user is required.",
            )
        return user_id

    @staticmethod
    def _validate_amount(value: str) -> str:
        text = str(value or "").strip()
        try:
            amount = Decimal(text)
        except (InvalidOperation, ValueError):
            amount = Decimal("NaN")
        if (
            not amount.is_finite()
            or amount < Decimal("1")
            or amount > Decimal("1000")
            or amount != amount.quantize(Decimal("0.01"))
        ):
            raise BankrLlmError(
                "invalid_amount",
                "Enter a USD amount from 1.00 to 1000.00.",
            )
        return format(amount, "f")


def build_bankr_llm_purchase_service_from_env(
    *,
    wallet_service: Any,
    pricer: Any,
    approval_service: Any,
    transfer_client: Any,
    enforce_spend: Callable[[str, dict[str, Any]], None],
    record_spend: Callable[[str, dict[str, Any], dict[str, Any]], None],
    env: Mapping[str, str] | None = None,
) -> BankrLlmPurchaseService:
    values = os.environ if env is None else env
    master_key = str(values.get("SIGN402_WALLET_MASTER_KEY") or "").strip()
    if not master_key:
        raise BankrLlmError(
            "invalid_configuration",
            "SIGN402_WALLET_MASTER_KEY is required for Bankr LLM purchases.",
        )
    if pricer is None:
        raise BankrLlmError(
            "invalid_configuration",
            "Real-rate SINGIT pricing is required for Bankr LLM purchases.",
        )

    try:
        timeout = float(
            str(values.get("SIGN402_BANKR_HTTP_TIMEOUT_SECONDS") or "20")
        )
        otp_ttl_seconds = int(
            str(values.get("SIGN402_BANKR_OTP_TTL_SECONDS") or "600")
        )
        max_otp_attempts = int(
            str(values.get("SIGN402_BANKR_MAX_OTP_ATTEMPTS") or "3")
        )
    except ValueError as exc:
        raise BankrLlmError(
            "invalid_configuration",
            "Bankr LLM timeout and OTP settings must be numeric.",
        ) from exc
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or otp_ttl_seconds <= 0
        or max_otp_attempts <= 0
    ):
        raise BankrLlmError(
            "invalid_configuration",
            "Bankr LLM timeout and OTP settings must be positive.",
        )

    singit_token_address = str(
        values.get("SIGN402_SINGIT_TOKEN_ADDRESS")
        or DEFAULT_SINGIT_TOKEN_ADDRESS
    ).strip()
    if EVM_ADDRESS_RE.fullmatch(singit_token_address) is None:
        raise BankrLlmError(
            "invalid_configuration",
            "SIGN402_SINGIT_TOKEN_ADDRESS must be an EVM address.",
        )

    store_path = Path(
        str(
            values.get("SIGN402_BANKR_LLM_STORE_PATH")
            or DEFAULT_BANKR_LLM_STORE_PATH
        )
    )
    return BankrLlmPurchaseService(
        store=BankrLlmStore(store_path, master_key=master_key),
        bankr=BankrIdentityClient(
            api_url=str(
                values.get("SIGN402_BANKR_API_URL")
                or DEFAULT_BANKR_API_URL
            ),
            llm_url=str(
                values.get("SIGN402_BANKR_LLM_URL")
                or DEFAULT_BANKR_LLM_URL
            ),
            timeout=timeout,
        ),
        wallet_service=wallet_service,
        pricer=pricer,
        approval_service=approval_service,
        transfer_client=transfer_client,
        enforce_spend=enforce_spend,
        record_spend=record_spend,
        singit_token_address=singit_token_address,
        otp_ttl_seconds=otp_ttl_seconds,
        max_otp_attempts=max_otp_attempts,
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
