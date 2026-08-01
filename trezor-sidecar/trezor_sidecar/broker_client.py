"""Strict clients for the companion broker's two authentication domains."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from .errors import SafeError
from .models import PurchaseIntent
from .sidecar_client import SidecarClient


_MAX_RESPONSE_BYTES = 65_536
_JOB_ID = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_PUBLIC_ERRORS = frozenset(
    {
        "broker_failed",
        "companion_failed",
        "device_rejected",
        "device_timeout",
        "disabled",
        "insufficient_eth",
        "insufficient_usdc",
        "intent_expired",
        "invalid_signature",
        "invalid_signed_transaction",
        "invoice_expired",
        "not_paired",
        "pairing_mismatch",
        "payment_failed",
        "reconciliation_required",
        "signer_mismatch",
        "trezor_unavailable",
    }
)


def _base_url(value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        parsed.scheme not in ({"https"} if not loopback else {"http", "https"})
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or ".." in parsed.path.split("/")
        or re.fullmatch(r"[A-Za-z0-9._~/-]*", parsed.path) is None
    ):
        raise ValueError("broker URL must use HTTPS (loopback HTTP is allowed for tests)")
    return candidate


class JsonBrokerClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout: float = 10.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        self.base_url = _base_url(base_url)
        self._token = str(token or "")
        self._timeout = float(timeout)
        self._opener = opener

    def __repr__(self) -> str:
        return f"JsonBrokerClient(base_url={self.base_url!r}, credentials='<redacted>')"

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any] | None:
        body = None
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(body) > _MAX_RESPONSE_BYTES:
                raise SafeError("invalid_request", "Broker request is invalid.")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            response = self._opener(request, timeout=self._timeout)
            status = int(response.status)
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = int(error.code)
            raw = error.read(_MAX_RESPONSE_BYTES + 1)
        except Exception:
            raise SafeError("broker_failed", "Trezor companion broker is unavailable.", 503) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise SafeError("broker_failed", "Trezor companion broker returned an invalid response.", 502)
        if status == 204 and status in expected:
            return None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except Exception:
            raise SafeError("broker_failed", "Trezor companion broker returned an invalid response.", 502) from None
        if not isinstance(decoded, dict):
            raise SafeError("broker_failed", "Trezor companion broker returned an invalid response.", 502)
        if status not in expected:
            code = str(decoded.get("code") or "broker_failed")
            if code not in _PUBLIC_ERRORS:
                code = "broker_failed"
            raise SafeError(code, "Trezor companion request failed safely.", status)
        if decoded.get("ok") is not True:
            raise SafeError("broker_failed", "Trezor companion broker returned an invalid response.", 502)
        return decoded


class RemoteSidecarClient:
    """Sidecar-compatible VPS client that waits on one enrolled companion."""

    def __init__(
        self,
        *,
        base_url: str,
        internal_token: str,
        user_id: str,
        clock: Callable[[], int | float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = 1.0,
    ):
        if not str(user_id).isascii() or not str(user_id).isdecimal():
            raise ValueError("remote Trezor user ID is invalid")
        self._client = JsonBrokerClient(base_url=base_url, token=internal_token)
        self.user_id = str(user_id)
        self._clock = clock
        self._sleeper = sleeper
        self._poll_interval = float(poll_interval_seconds)

    def __repr__(self) -> str:
        return f"RemoteSidecarClient(user_id={self.user_id!r}, credentials='<redacted>')"

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SafeError("broker_failed", "Trezor companion clock is invalid.", 503)
        return int(value)

    def pair(self, allow_repair: bool = False) -> dict[str, Any]:
        if allow_repair:
            raise SafeError("invalid_request", "Remote repair requires a new enrollment.", 409)
        result = self._client.request(
            "GET", "/v1/internal/companions/" + quote(self.user_id), expected=(200,)
        )
        companion = result.get("companion") if isinstance(result, dict) else None
        if not isinstance(companion, dict) or _ADDRESS.fullmatch(str(companion.get("walletAddress") or "")) is None:
            raise SafeError("not_paired", "No active Trezor companion is enrolled.", 409)
        return {
            "ok": True,
            "pairing": {
                "pairingId": str(companion["companionId"]),
                "address": str(companion["walletAddress"]),
                "createdAt": int(companion["createdAt"]),
                "updatedAt": int(companion["updatedAt"]),
            },
        }

    def _submit_wait(
        self,
        *,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
        expires_at: int,
    ) -> dict[str, Any]:
        created = self._client.request(
            "POST",
            "/v1/internal/jobs",
            payload={
                "userId": self.user_id,
                "kind": kind,
                "idempotencyKey": idempotency_key,
                "payload": payload,
                "expiresAt": int(expires_at),
            },
            expected=(202,),
        )
        job = created.get("job") if isinstance(created, dict) else None
        job_id = job.get("jobId") if isinstance(job, dict) else None
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise SafeError("broker_failed", "Trezor companion job is invalid.", 502)
        while self._now() < int(expires_at):
            current = self._client.request(
                "GET", "/v1/internal/jobs/" + quote(job_id), expected=(200,)
            )
            job = current.get("job") if isinstance(current, dict) else None
            state = job.get("state") if isinstance(job, dict) else None
            if state == "SUCCEEDED" and isinstance(job.get("result"), dict):
                return job["result"]
            if state in {"FAILED", "EXPIRED"}:
                code = str(job.get("errorCode") or ("intent_expired" if state == "EXPIRED" else "companion_failed"))
                if code not in _PUBLIC_ERRORS:
                    code = "companion_failed"
                raise SafeError(code, "Trezor companion operation failed safely.", 409)
            if state not in {"QUEUED", "LEASED"}:
                raise SafeError("broker_failed", "Trezor companion job is invalid.", 502)
            self._sleeper(self._poll_interval)
        raise SafeError("device_timeout", "Trezor confirmation timed out.", 504)

    def approve_intent(self, intent: PurchaseIntent) -> dict[str, Any]:
        if type(intent) is not PurchaseIntent:
            raise SafeError("invalid_request", "Purchase intent is invalid.")
        result = self._submit_wait(
            kind="purchase_intent",
            idempotency_key="approve:" + intent.intent_id,
            expires_at=intent.expires_at,
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
        )
        return SidecarClient._approval_response(result, intent.intent_id)

    def pay_invoice(
        self,
        intent_id: str,
        invoice_id: str,
        pay_to: str,
        amount_atomic: int | str,
        expires_at: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        result = self._submit_wait(
            kind="usdc_payment",
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            payload={
                "intentId": intent_id,
                "invoiceId": invoice_id,
                "payTo": pay_to,
                "amountAtomic": amount_atomic,
                "expiresAt": expires_at,
                "idempotencyKey": idempotency_key,
            },
        )
        return SidecarClient._payment_response(
            result,
            intent_id=intent_id,
            invoice_id=invoice_id,
        )


class CompanionBrokerClient:
    def __init__(self, *, base_url: str, token: str = ""):
        self._client = JsonBrokerClient(base_url=base_url, token=token)

    def enroll(self, code: str, wallet_address: str) -> dict[str, Any]:
        result = self._client.request(
            "POST",
            "/v1/enroll",
            payload={"enrollmentCode": code, "walletAddress": wallet_address},
            expected=(201,),
        )
        companion = result.get("companion") if isinstance(result, dict) else None
        if not isinstance(companion, dict) or not isinstance(companion.get("token"), str):
            raise SafeError("broker_failed", "Trezor enrollment response is invalid.", 502)
        return companion

    def claim(self) -> dict[str, Any] | None:
        result = self._client.request(
            "POST", "/v1/companion/jobs/claim", payload={}, expected=(200, 204)
        )
        if result is None:
            return None
        job = result.get("job")
        if not isinstance(job, dict):
            raise SafeError("broker_failed", "Trezor companion job is invalid.", 502)
        return job

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        self._client.request(
            "POST",
            "/v1/companion/jobs/" + quote(job_id) + "/complete",
            payload={"result": result},
            expected=(200,),
        )

    def fail(self, job_id: str, error_code: str) -> None:
        self._client.request(
            "POST",
            "/v1/companion/jobs/" + quote(job_id) + "/fail",
            payload={"errorCode": error_code},
            expected=(200,),
        )
