"""Strict runner-side client for the fixed local Trezor sidecar."""

from __future__ import annotations

import http.client
import json
import math
import re
import time
import uuid
from typing import Any, Callable

from .errors import SafeError
from .models import PurchaseIntent


_HOST = "127.0.0.1"
_PORT = 8111
_MAX_RESPONSE_BYTES = 65_536
_TIMEOUT_SECONDS = 5.0
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")
_PAYMENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_STATES = frozenset(
    {
        "INVOICE_CREATED",
        "TX_SIGNED",
        "TX_BROADCAST",
        "COMPLETE",
        "CANCELLED",
        "FAILED",
        "RECONCILIATION_REQUIRED",
    }
)
_TERMINAL_FAILURES = frozenset({"CANCELLED", "FAILED", "RECONCILIATION_REQUIRED"})
_PUBLIC_MESSAGES = {
    "base_rpc_unavailable": "Base RPC is unavailable.",
    "broadcast_ambiguous": "Transaction broadcast outcome is ambiguous.",
    "device_busy": "Another Trezor operation is active.",
    "device_lock_unavailable": "The Trezor device lock is unavailable.",
    "device_rejected": "Trezor operation was cancelled.",
    "device_timeout": "Trezor operation timed out.",
    "disabled": "Trezor proof mode is disabled.",
    "forbidden": "Loopback access is required.",
    "insufficient_eth": "The paired Base account has insufficient ETH for gas.",
    "insufficient_usdc": "The paired Base account has insufficient USDC.",
    "intent_conflict": "Purchase intent conflicts with existing state.",
    "intent_expired": "Purchase intent has expired.",
    "intent_limit_exceeded": "Purchase intent exceeds the configured limit.",
    "intent_not_approved": "Purchase intent is not approved.",
    "intent_state_changed": "Purchase intent state does not allow approval.",
    "invalid_clock": "The sidecar clock is invalid.",
    "invalid_configuration": "The sidecar configuration is invalid.",
    "invalid_intent": "Purchase intent is invalid.",
    "invalid_json": "Request body must be one JSON object.",
    "invalid_request": "Request is invalid.",
    "invalid_signature": "Trezor returned an invalid approval signature.",
    "invalid_signed_transaction": "Trezor returned an invalid signed transaction.",
    "internal_error": "Request failed safely.",
    "invoice_expired": "Payment invoice has expired.",
    "method_not_allowed": "Method not allowed.",
    "not_paired": "A Trezor must be paired first.",
    "not_found": "Route not found.",
    "pairing_failed": "Trezor pairing could not be saved.",
    "pairing_mismatch": "Trezor pairing does not match.",
    "payment_conflict": "Payment conflicts with existing state.",
    "payment_failed": "Payment failed safely.",
    "payment_invalid": "Stored payment is invalid.",
    "payment_limit_exceeded": "Payment exceeds the approved limit.",
    "payment_not_found": "Payment was not found.",
    "payment_state_changed": "Payment state does not allow this operation.",
    "payment_state_unavailable": "Payment state could not be recorded safely.",
    "reapproval_required": "Purchase intent must be reapproved.",
    "reconciliation_required": "Transaction reconciliation is required.",
    "request_timeout": "Request timed out.",
    "request_too_large": "Request body is too large.",
    "signer_mismatch": "Purchase approval signer does not match.",
    "stale_request": "Request timestamp is outside the allowed window.",
    "trezor_unavailable": "Trezor Suite is unavailable.",
    "unauthorized": "Authentication failed.",
    "worker_unavailable": "Payment worker is unavailable.",
}


def _invalid_response() -> SafeError:
    return SafeError(
        "sidecar_invalid_response",
        "Trezor sidecar returned an invalid response.",
        502,
    )


def _unavailable() -> SafeError:
    return SafeError(
        "sidecar_unavailable",
        "The local Trezor sidecar is unavailable.",
        503,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _bounded_text(value: Any, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid text")
    return value


def _positive_int(value: Any) -> int:
    if type(value) is not int or not 0 < value <= (1 << 63) - 1:
        raise ValueError("invalid integer")
    return value


def _amount_atomic(value: Any) -> int:
    if type(value) is int:
        numeric = value
    elif type(value) is str and value.isascii() and value.isdecimal():
        numeric = int(value)
    else:
        raise SafeError("invalid_request", "Payment request is invalid.")
    if not 0 < numeric <= (1 << 256) - 1:
        raise SafeError("invalid_request", "Payment request is invalid.")
    return numeric


class SidecarClient:
    """Authenticated client fixed to ``127.0.0.1:8111``.

    ``requester`` is a narrow test seam. Production calls use ``http.client``
    directly, which has no redirect behavior and streams the response under a
    hard byte limit.
    """

    def __init__(
        self,
        *,
        token: str,
        requester: Callable[[str, str, dict[str, str], bytes | None], Any] | None = None,
        clock: Callable[[], int | float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        poll_attempts: int = 20,
        poll_interval_seconds: float = 0.25,
    ):
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in token)
        ):
            raise ValueError("sidecar token is invalid")
        if not callable(clock) or not callable(sleeper):
            raise ValueError("sidecar clock and sleeper must be callable")
        if type(poll_attempts) is not int or not 1 <= poll_attempts <= 120:
            raise ValueError("poll_attempts is invalid")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(poll_interval_seconds)
            or not 0 <= poll_interval_seconds <= 10
        ):
            raise ValueError("poll_interval_seconds is invalid")
        self._token = token
        self._requester = requester or self._http_request
        self._clock = clock
        self._sleeper = sleeper
        self._poll_attempts = poll_attempts
        self._poll_interval_seconds = float(poll_interval_seconds)

    def __repr__(self) -> str:
        return "SidecarClient(base_url='http://127.0.0.1:8111', token='<redacted>')"

    @staticmethod
    def _timestamp(clock: Callable[[], int | float]) -> int:
        value = clock()
        if isinstance(value, bool):
            raise _unavailable()
        if isinstance(value, int):
            result = value
        elif isinstance(value, float) and math.isfinite(value):
            result = int(value)
        else:
            raise _unavailable()
        if not 0 < result <= (1 << 63) - 1:
            raise _unavailable()
        return result

    @staticmethod
    def _http_request(
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> Any:
        connection = http.client.HTTPConnection(_HOST, _PORT, timeout=_TIMEOUT_SECONDS)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
        except Exception:
            connection.close()
            raise

        original_close = response.close

        def close() -> None:
            try:
                original_close()
            finally:
                connection.close()

        response.close = close  # type: ignore[method-assign]
        return response

    def _headers(self, idempotency_key: str, *, has_body: bool) -> dict[str, str]:
        if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise SafeError("invalid_request", "Payment request is invalid.")
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": "Bearer " + self._token,
            "Connection": "close",
            "Idempotency-Key": idempotency_key,
            "X-Sign402-Timestamp": str(self._timestamp(self._clock)),
        }
        if has_body:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers

    @staticmethod
    def _header_values(headers: Any, name: str) -> list[str]:
        try:
            values = headers.get_all(name, failobj=[])
        except TypeError:
            values = headers.get_all(name) or []
        except AttributeError:
            value = headers.get(name) if hasattr(headers, "get") else None
            values = [] if value is None else [value]
        return values if isinstance(values, list) else []

    @classmethod
    def _read_response(cls, response: Any) -> tuple[int, dict[str, Any]]:
        status = getattr(response, "status", None)
        if type(status) is not int or not 100 <= status <= 599:
            raise _invalid_response()
        headers = getattr(response, "headers", None)
        if headers is None:
            raise _invalid_response()
        content_types = cls._header_values(headers, "Content-Type")
        if len(content_types) != 1:
            raise _invalid_response()
        parts = [part.strip().casefold() for part in content_types[0].split(";")]
        if parts[0] != "application/json" or any(
            part not in {"charset=utf-8", 'charset="utf-8"'} for part in parts[1:]
        ):
            raise _invalid_response()
        encodings = cls._header_values(headers, "Content-Encoding")
        if len(encodings) > 1 or (encodings and encodings[0].strip().casefold() != "identity"):
            raise _invalid_response()
        if cls._header_values(headers, "Transfer-Encoding"):
            raise _invalid_response()
        lengths = cls._header_values(headers, "Content-Length")
        declared: int | None = None
        if lengths:
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                raise _invalid_response()
            declared = int(lengths[0])
            if declared > _MAX_RESPONSE_BYTES:
                raise _invalid_response()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(8192, _MAX_RESPONSE_BYTES + 1 - total))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise _invalid_response()
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise _invalid_response()
            chunks.append(chunk)
        if declared is not None and declared != total:
            raise _invalid_response()
        try:
            decoded = json.loads(
                b"".join(chunks).decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise _invalid_response() from None
        if not isinstance(decoded, dict):
            raise _invalid_response()
        return status, decoded

    def _request(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str,
        payload: dict[str, Any] | None,
        expected_status: int,
    ) -> dict[str, Any]:
        body = None
        if payload is not None:
            try:
                body = json.dumps(
                    payload,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise SafeError("invalid_request", "Request is invalid.") from None
            if len(body) > _MAX_RESPONSE_BYTES:
                raise SafeError("invalid_request", "Request is invalid.")
        try:
            response = self._requester(
                method,
                path,
                self._headers(idempotency_key, has_body=payload is not None),
                body,
            )
        except SafeError:
            raise
        except Exception:
            raise _unavailable() from None
        try:
            status, decoded = self._read_response(response)
        except SafeError:
            raise
        except Exception:
            raise _unavailable() from None
        finally:
            try:
                response.close()
            except Exception:
                pass
        if status != expected_status:
            if set(decoded) != {"ok", "code", "message"} or decoded.get("ok") is not False:
                raise _invalid_response()
            code = decoded.get("code")
            if not isinstance(code, str) or _CODE.fullmatch(code) is None or code not in _PUBLIC_MESSAGES:
                raise _invalid_response()
            try:
                _bounded_text(decoded.get("message"), 256)
            except ValueError:
                raise _invalid_response() from None
            raise SafeError(code, _PUBLIC_MESSAGES[code], status)
        if decoded.get("ok") is not True:
            raise _invalid_response()
        return decoded

    @staticmethod
    def _pairing_response(value: dict[str, Any]) -> dict[str, Any]:
        if set(value) != {"ok", "pairing"} or not isinstance(value.get("pairing"), dict):
            raise _invalid_response()
        pairing = value["pairing"]
        if set(pairing) != {"pairingId", "address", "createdAt", "updatedAt"}:
            raise _invalid_response()
        try:
            _bounded_text(pairing["pairingId"])
            if not isinstance(pairing["address"], str) or _ADDRESS.fullmatch(pairing["address"]) is None:
                raise ValueError
            _positive_int(pairing["createdAt"])
            _positive_int(pairing["updatedAt"])
        except (KeyError, ValueError):
            raise _invalid_response() from None
        return value

    @staticmethod
    def _approval_response(value: dict[str, Any], intent_id: str) -> dict[str, Any]:
        if (
            set(value) != {"ok", "intentId", "state"}
            or value.get("intentId") != intent_id
            or value.get("state") != "DEVICE_APPROVED"
        ):
            raise _invalid_response()
        return value

    @staticmethod
    def _payment_response(
        value: dict[str, Any],
        *,
        intent_id: str,
        invoice_id: str,
        payment_id: str | None = None,
    ) -> dict[str, Any]:
        if set(value) != {"ok", "payment"} or not isinstance(value.get("payment"), dict):
            raise _invalid_response()
        payment = value["payment"]
        required = {"paymentId", "intentId", "invoiceId", "state", "createdAt", "updatedAt"}
        if set(payment) not in (required, required | {"txHash"}):
            raise _invalid_response()
        try:
            current_payment_id = _bounded_text(payment["paymentId"], 128)
            if _PAYMENT_ID.fullmatch(current_payment_id) is None:
                raise ValueError
            if payment_id is not None and current_payment_id != payment_id:
                raise ValueError
            if payment["intentId"] != intent_id or payment["invoiceId"] != invoice_id:
                raise ValueError
            _bounded_text(payment["invoiceId"])
            if payment["state"] not in _STATES:
                raise ValueError
            _positive_int(payment["createdAt"])
            _positive_int(payment["updatedAt"])
            tx_hash = payment.get("txHash")
            if payment["state"] in {"TX_BROADCAST", "COMPLETE"}:
                if not isinstance(tx_hash, str) or _TX_HASH.fullmatch(tx_hash) is None:
                    raise ValueError
            elif payment["state"] == "RECONCILIATION_REQUIRED":
                if tx_hash is not None and (
                    not isinstance(tx_hash, str) or _TX_HASH.fullmatch(tx_hash) is None
                ):
                    raise ValueError
            elif tx_hash is not None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise _invalid_response() from None
        return value

    def pair(self, allow_repair: bool = False) -> dict[str, Any]:
        if type(allow_repair) is not bool:
            raise SafeError("invalid_request", "Pairing request is invalid.")
        result = self._request(
            "POST",
            "/v1/pair",
            idempotency_key="pair:" + uuid.uuid4().hex,
            payload={"allowRepair": allow_repair},
            expected_status=200,
        )
        return self._pairing_response(result)

    def approve_intent(self, intent: PurchaseIntent) -> dict[str, Any]:
        if type(intent) is not PurchaseIntent:
            raise SafeError("invalid_request", "Purchase intent is invalid.")
        result = self._request(
            "POST",
            "/v1/purchase-intents/approve",
            idempotency_key="approve:" + intent.intent_id,
            payload={
                "intentId": intent.intent_id,
                "productSlug": intent.product_slug,
                "packageId": intent.package_id,
                "denomination": intent.denomination,
                "quotedTotalUsdMicros": intent.quoted_total_usd_micros,
                "maxPaymentUsdcAtomic": intent.max_payment_usdc_atomic,
                "paymentAsset": intent.payment_asset,
                "paymentNetwork": intent.payment_network,
                "recipientHash": intent.recipient_hash,
                "expiresAt": intent.expires_at,
            },
            expected_status=200,
        )
        return self._approval_response(result, intent.intent_id)

    def pay_invoice(
        self,
        intent_id: str,
        invoice_id: str,
        pay_to: str,
        amount_atomic: int | str,
        expires_at: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        canonical_amount = _amount_atomic(amount_atomic)
        result = self._request(
            "POST",
            "/v1/payments",
            idempotency_key=idempotency_key,
            payload={
                "intentId": intent_id,
                "invoiceId": invoice_id,
                "payTo": pay_to,
                "amountAtomic": canonical_amount,
                "expiresAt": expires_at,
            },
            expected_status=202,
        )
        result = self._payment_response(result, intent_id=intent_id, invoice_id=invoice_id)
        payment_id = result["payment"]["paymentId"]
        state = result["payment"]["state"]
        if state in {"TX_BROADCAST", "COMPLETE"}:
            return result
        if state in _TERMINAL_FAILURES:
            self._raise_terminal(state)
        for attempt in range(self._poll_attempts):
            polled = self._request(
                "GET",
                "/v1/payments/" + payment_id,
                idempotency_key=idempotency_key,
                payload=None,
                expected_status=200,
            )
            polled = self._payment_response(
                polled,
                intent_id=intent_id,
                invoice_id=invoice_id,
                payment_id=payment_id,
            )
            state = polled["payment"]["state"]
            if state in {"TX_BROADCAST", "COMPLETE"}:
                return polled
            if state in _TERMINAL_FAILURES:
                self._raise_terminal(state)
            if attempt < self._poll_attempts - 1 and self._poll_interval_seconds:
                self._sleeper(self._poll_interval_seconds)
        raise SafeError("payment_timeout", "Trezor payment polling timed out.", 504)

    @staticmethod
    def _raise_terminal(state: str) -> None:
        if state == "RECONCILIATION_REQUIRED":
            raise SafeError(
                "reconciliation_required",
                "Transaction reconciliation is required; payment was not resubmitted.",
                409,
            )
        if state == "CANCELLED":
            raise SafeError("device_rejected", "Trezor operation was cancelled.", 400)
        raise SafeError("payment_failed", "Payment failed safely.", 500)
