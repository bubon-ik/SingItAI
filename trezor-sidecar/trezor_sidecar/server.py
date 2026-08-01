"""Loopback-only HTTP boundary for the local Trezor proof sidecar."""

import hmac
import ipaddress
import json
import math
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

from .base import BaseRpcClient
from .config import SidecarSettings
from .errors import SafeError
from .mcp_client import McpToolCaller, TrezorMcpClient
from .models import Pairing, PaymentRequest, PaymentState, PaymentView, PurchaseIntent
from .service import TrezorSidecarService
from .store import SidecarStore


_MAX_BODY_BYTES = 65_536
_CONNECTION_TIMEOUT_SECONDS = 5.0
_SIGNED_TIMESTAMP_MAX = (1 << 63) - 1
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")
_PAYMENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")

_PUBLIC_ERRORS = {
    "base_rpc_unavailable": (503, "Base RPC is unavailable."),
    "broadcast_ambiguous": (409, "Transaction broadcast outcome is ambiguous."),
    "device_busy": (409, "Another Trezor operation is active."),
    "device_lock_unavailable": (503, "The Trezor device lock is unavailable."),
    "device_rejected": (400, "Trezor operation was cancelled."),
    "device_timeout": (504, "Trezor operation timed out."),
    "disabled": (503, "Trezor proof mode is disabled."),
    "insufficient_eth": (409, "The paired Base account has insufficient ETH for gas."),
    "insufficient_usdc": (409, "The paired Base account has insufficient USDC."),
    "intent_conflict": (409, "Purchase intent conflicts with existing state."),
    "intent_expired": (400, "Purchase intent has expired."),
    "intent_limit_exceeded": (400, "Purchase intent exceeds the configured limit."),
    "intent_not_approved": (409, "Purchase intent is not approved."),
    "intent_state_changed": (409, "Purchase intent state does not allow approval."),
    "invalid_clock": (503, "The sidecar clock is invalid."),
    "invalid_configuration": (503, "The sidecar configuration is invalid."),
    "invalid_intent": (400, "Purchase intent is invalid."),
    "invalid_request": (400, "Request is invalid."),
    "invalid_signature": (400, "Trezor returned an invalid approval signature."),
    "invalid_signed_transaction": (400, "Trezor returned an invalid signed transaction."),
    "invoice_expired": (400, "Payment invoice has expired."),
    "not_paired": (409, "A Trezor must be paired first."),
    "pairing_failed": (409, "Trezor pairing could not be saved."),
    "pairing_mismatch": (409, "Trezor pairing does not match."),
    "payment_conflict": (409, "Payment conflicts with existing state."),
    "payment_failed": (500, "Payment failed safely."),
    "payment_invalid": (409, "Stored payment is invalid."),
    "payment_limit_exceeded": (400, "Payment exceeds the approved limit."),
    "payment_not_found": (404, "Payment was not found."),
    "payment_state_changed": (409, "Payment state does not allow this operation."),
    "payment_state_unavailable": (503, "Payment state could not be recorded safely."),
    "reapproval_required": (409, "Purchase intent must be reapproved."),
    "reconciliation_required": (409, "Transaction reconciliation is required."),
    "signer_mismatch": (400, "Purchase approval signer does not match."),
    "trezor_unavailable": (503, "Trezor Suite is unavailable."),
    "worker_unavailable": (503, "Payment worker is unavailable."),
}

_HEALTH_STATES = frozenset({
    "ready", "disabled", "suite_unavailable", "device_unavailable"
})

_PAIR_PATH = "/v1/pair"
_APPROVE_PATH = "/v1/purchase-intents/approve"
_PAYMENTS_PATH = "/v1/payments"
_POST_PATHS = frozenset({_PAIR_PATH, _APPROVE_PATH, _PAYMENTS_PATH})

_PAIR_FIELDS = frozenset({"allowRepair"})
_INTENT_FIELDS = frozenset({
    "intentId",
    "productSlug",
    "packageId",
    "denomination",
    "quotedTotalUsdMicros",
    "maxPaymentUsdcAtomic",
    "paymentAsset",
    "paymentNetwork",
    "recipientHash",
    "expiresAt",
})
_PAYMENT_FIELDS = frozenset({
    "intentId", "invoiceId", "payTo", "amountAtomic", "expiresAt"
})


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _clock_timestamp(clock: Callable[[], int | float]) -> int:
    value = clock()
    if isinstance(value, bool):
        raise ValueError("invalid clock")
    if isinstance(value, int):
        timestamp = value
    elif isinstance(value, float) and math.isfinite(value):
        timestamp = int(value)
    else:
        raise ValueError("invalid clock")
    if not 0 < timestamp <= _SIGNED_TIMESTAMP_MAX:
        raise ValueError("invalid clock")
    return timestamp


def _loopback_peer(client_address: Any) -> bool:
    try:
        host = client_address[0]
        return isinstance(host, str) and ipaddress.ip_address(host).is_loopback
    except (IndexError, TypeError, ValueError):
        return False


def _bounded_text(value: Any, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid response field")
    return value


def _bounded_timestamp(value: Any) -> int:
    if type(value) is not int or not 0 < value <= _SIGNED_TIMESTAMP_MAX:
        raise ValueError("invalid response timestamp")
    return value


def _serialize_pairing(pairing: Any) -> dict[str, Any]:
    if type(pairing) is not Pairing or _ADDRESS.fullmatch(pairing.address) is None:
        raise ValueError("invalid pairing result")
    return {
        "pairingId": _bounded_text(pairing.pairing_id),
        "address": pairing.address,
        "createdAt": _bounded_timestamp(pairing.created_at),
        "updatedAt": _bounded_timestamp(pairing.updated_at),
    }


def _serialize_payment(payment: Any) -> dict[str, Any]:
    if type(payment) is not PaymentView or type(payment.state) is not PaymentState:
        raise ValueError("invalid payment result")
    result: dict[str, Any] = {
        "paymentId": _bounded_text(payment.payment_id, maximum=128),
        "intentId": _bounded_text(payment.intent_id, maximum=66),
        "invoiceId": _bounded_text(payment.invoice_id),
        "state": payment.state.value,
        "createdAt": _bounded_timestamp(payment.created_at),
        "updatedAt": _bounded_timestamp(payment.updated_at),
    }
    if payment.tx_hash is not None:
        if not isinstance(payment.tx_hash, str) or _TX_HASH.fullmatch(payment.tx_hash) is None:
            raise ValueError("invalid transaction hash")
        result["txHash"] = payment.tx_hash
    return result


class _SidecarHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        settings: SidecarSettings,
        service: TrezorSidecarService,
        clock: Callable[[], int | float],
        connection_timeout: float,
    ):
        self.settings = settings
        self.service = service
        self.clock = clock
        self.connection_timeout = connection_timeout
        self._payment_lock = threading.Lock()
        self._device_operation_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._health_status = "disabled" if not settings.enabled else "suite_unavailable"
        self._worker_active: set[str] = set()
        self._worker_launched: set[str] = set()
        super().__init__(server_address, _SidecarHandler)

    def get_request(self) -> tuple[Any, Any]:
        request, client_address = super().get_request()
        request.settimeout(self.connection_timeout)
        return request, client_address

    def handle_error(self, request: Any, client_address: Any) -> None:
        return

    def create_and_schedule(
        self,
        request: PaymentRequest,
        idempotency_key: str,
        timestamp: int,
    ) -> tuple[PaymentView, dict[str, Any]]:
        """Serialize creation and launch so concurrent replays cannot race."""
        with self._payment_lock:
            payment = self.service.create_payment(request, idempotency_key, timestamp)
            serialized = _serialize_payment(payment)
            if (
                payment.state is PaymentState.INVOICE_CREATED
                and payment.payment_id not in self._worker_active
                and payment.payment_id not in self._worker_launched
            ):
                self._worker_active.add(payment.payment_id)
                try:
                    worker = threading.Thread(
                        target=self._run_payment,
                        args=(payment.payment_id,),
                        name="trezor-payment-worker",
                        daemon=True,
                    )
                    worker.start()
                except Exception:
                    self._worker_active.discard(payment.payment_id)
                    try:
                        durable = self.service.get_payment(payment.payment_id)
                    except Exception:
                        self._worker_launched.add(payment.payment_id)
                    else:
                        if (
                            type(durable) is not PaymentView
                            or durable.state is not PaymentState.INVOICE_CREATED
                        ):
                            self._worker_launched.add(payment.payment_id)
                    raise SafeError(
                        "worker_unavailable",
                        "Payment worker is unavailable.",
                        503,
                    ) from None
                self._worker_launched.add(payment.payment_id)
            return payment, serialized

    @property
    def health_status(self) -> str:
        with self._health_lock:
            return self._health_status

    def _set_health(self, status: str) -> None:
        if status not in _HEALTH_STATES:
            raise ValueError("invalid health state")
        with self._health_lock:
            self._health_status = status

    def _observe_safe_error(self, error: SafeError) -> None:
        if type(error) is not SafeError or type(error.code) is not str:
            return
        if error.code == "disabled":
            self._set_health("disabled")
        elif error.code == "trezor_unavailable":
            self._set_health("suite_unavailable")
        elif error.code in {"device_busy", "device_lock_unavailable", "device_timeout"}:
            self._set_health("device_unavailable")

    def _device_operation(self, operation: Callable[[], Any]) -> Any:
        try:
            result = operation()
        except SafeError as error:
            self._observe_safe_error(error)
            raise
        except Exception:
            self._set_health("device_unavailable")
            raise
        self._set_health("ready")
        return result

    def pair(self, allow_repair: bool) -> Pairing:
        with self._device_operation_lock:
            return self._device_operation(
                lambda: self.service.pair(allow_repair=allow_repair)
            )

    def approve_intent(self, intent: PurchaseIntent, timestamp: int) -> PurchaseIntent:
        with self._device_operation_lock:
            return self._device_operation(
                lambda: self.service.approve_intent(intent, timestamp)
            )

    def _run_payment(self, payment_id: str) -> None:
        try:
            with self._device_operation_lock:
                self._device_operation(
                    lambda: self.service.run_payment(
                        payment_id,
                        now=lambda: _clock_timestamp(self.clock),
                    )
                )
        except Exception:
            pass
        finally:
            with self._payment_lock:
                self._worker_active.discard(payment_id)


class _SidecarHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Sign402Sidecar"
    sys_version = ""

    @property
    def sidecar_server(self) -> _SidecarHttpServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        if code == 501:
            if self._known_path(self._path()):
                self._error(405, "method_not_allowed", "Method not allowed.")
            else:
                self._error(404, "not_found", "Route not found.")
        elif code == 414:
            self._error(414, "invalid_request", "Request target is invalid.")
        elif code == 431:
            self._error(431, "invalid_request", "Request headers are invalid.")
        else:
            self._error(400, "invalid_request", "HTTP request is invalid.")

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if not parsed:
            return False
        if not _loopback_peer(self.client_address):
            self.close_connection = True
            self._error(403, "forbidden", "Loopback access is required.")
            return False
        return True

    def _path(self) -> str | None:
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return None
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return None
        return parsed.path

    @staticmethod
    def _known_path(path: str | None) -> bool:
        return path == "/health" or path in _POST_PATHS or (
            path is not None
            and path.startswith(_PAYMENTS_PATH + "/")
            and _PAYMENT_ID.fullmatch(path[len(_PAYMENTS_PATH) + 1 :]) is not None
        )

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except Exception:
            status = 500
            body = b'{"ok":false,"code":"internal_error","message":"Request failed safely."}'
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"ok": False, "code": code, "message": message})

    def _safe_error(self, error: SafeError) -> None:
        if type(error) is not SafeError or type(error.code) is not str:
            self._error(500, "internal_error", "Request failed safely.")
            return
        public = _PUBLIC_ERRORS.get(error.code)
        if public is None:
            self._error(500, "internal_error", "Request failed safely.")
            return
        status, message = public
        self._error(status, error.code, message)

    def _one_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        if len(values) != 1 or not isinstance(values[0], str):
            return None
        return values[0]

    def _authorize(self) -> bool:
        supplied = self._one_header("Authorization")
        expected = "Bearer " + self.sidecar_server.settings.api_token
        if supplied is None:
            return False
        try:
            return hmac.compare_digest(supplied, expected)
        except TypeError:
            return False

    def _mutation_credentials(self) -> tuple[int, str] | None:
        if not self._authorize():
            self._error(401, "unauthorized", "Authentication failed.")
            return None
        timestamp_text = self._one_header("X-Sign402-Timestamp")
        idempotency_key = self._one_header("Idempotency-Key")
        if (
            timestamp_text is None
            or len(timestamp_text) > 20
            or _INTEGER.fullmatch(timestamp_text) is None
            or idempotency_key is None
            or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
        ):
            self._error(400, "invalid_request", "Request authentication metadata is invalid.")
            return None
        try:
            timestamp = int(timestamp_text)
        except (TypeError, ValueError):
            self._error(400, "invalid_request", "Request authentication metadata is invalid.")
            return None
        try:
            now = _clock_timestamp(self.sidecar_server.clock)
        except Exception:
            self._error(503, "invalid_clock", "The sidecar clock is invalid.")
            return None
        if (
            not 0 < timestamp <= _SIGNED_TIMESTAMP_MAX
            or not 0 < now <= _SIGNED_TIMESTAMP_MAX
            or abs(now - timestamp) > 60
        ):
            self._error(400, "stale_request", "Request timestamp is outside the allowed window.")
            return None
        return timestamp, idempotency_key

    def _read_json_object(self) -> dict[str, Any] | None:
        transfer_encodings = self.headers.get_all("Transfer-Encoding", failobj=[])
        lengths = self.headers.get_all("Content-Length", failobj=[])
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if transfer_encodings or len(lengths) != 1:
            self._error(400, "invalid_request", "Request framing is invalid.")
            return None
        length_text = lengths[0]
        if (
            not isinstance(length_text, str)
            or not length_text.isascii()
            or not length_text.isdecimal()
            or len(length_text) > 20
        ):
            self._error(400, "invalid_request", "Request framing is invalid.")
            return None
        length = int(length_text)
        if length > _MAX_BODY_BYTES:
            self._error(413, "request_too_large", "Request body is too large.")
            return None
        if len(content_types) != 1 or not isinstance(content_types[0], str):
            self._error(400, "invalid_request", "Content type must be application/json.")
            return None
        parts = [part.strip().casefold() for part in content_types[0].split(";")]
        if parts[0] != "application/json" or any(
            part not in {"charset=utf-8", "charset=\"utf-8\""} for part in parts[1:]
        ):
            self._error(400, "invalid_request", "Content type must be application/json.")
            return None
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining:
                chunk = self.rfile.read(min(8192, remaining))
                if not chunk:
                    raise ValueError
                chunks.append(chunk)
                remaining -= len(chunk)
            decoded = json.loads(
                b"".join(chunks).decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (TimeoutError, OSError):
            self._error(408, "request_timeout", "Request timed out.")
            return None
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._error(400, "invalid_json", "Request body must be one JSON object.")
            return None
        if not isinstance(decoded, dict):
            self._error(400, "invalid_json", "Request body must be one JSON object.")
            return None
        return decoded

    def _dispatch(self, operation: Callable[[], tuple[int, dict[str, Any]]]) -> None:
        try:
            status, payload = operation()
        except SafeError as error:
            self._safe_error(error)
        except Exception:
            self._error(500, "internal_error", "Request failed safely.")
        else:
            self._send(status, payload)

    def do_GET(self) -> None:
        path = self._path()
        if path == "/health":
            self._send(200, {"ok": True, "status": self.sidecar_server.health_status})
            return
        if path is not None and path.startswith(_PAYMENTS_PATH + "/"):
            payment_id = path[len(_PAYMENTS_PATH) + 1 :]
            if _PAYMENT_ID.fullmatch(payment_id) is None:
                self._error(404, "not_found", "Route not found.")
                return
            if not self._authorize():
                self._error(401, "unauthorized", "Authentication failed.")
                return

            def operation() -> tuple[int, dict[str, Any]]:
                payment = self.sidecar_server.service.get_payment(payment_id)
                return 200, {"ok": True, "payment": _serialize_payment(payment)}

            self._dispatch(operation)
            return
        if path in _POST_PATHS:
            self._error(405, "method_not_allowed", "Method not allowed.")
        else:
            self._error(404, "not_found", "Route not found.")

    def do_POST(self) -> None:
        path = self._path()
        if path not in _POST_PATHS:
            if path == "/health" or (
                path is not None and path.startswith(_PAYMENTS_PATH + "/")
                and _PAYMENT_ID.fullmatch(path[len(_PAYMENTS_PATH) + 1 :]) is not None
            ):
                self._error(405, "method_not_allowed", "Method not allowed.")
            else:
                self._error(404, "not_found", "Route not found.")
            return
        credentials = self._mutation_credentials()
        if credentials is None:
            return
        timestamp, idempotency_key = credentials
        body = self._read_json_object()
        if body is None:
            return
        if path == _PAIR_PATH:
            self._handle_pair(body)
        elif path == _APPROVE_PATH:
            self._handle_approve(body, timestamp)
        else:
            self._handle_payment(body, timestamp, idempotency_key)

    def _handle_pair(self, body: dict[str, Any]) -> None:
        if not set(body) <= _PAIR_FIELDS or (
            "allowRepair" in body and type(body["allowRepair"]) is not bool
        ):
            self._error(400, "invalid_request", "Pairing request is invalid.")
            return
        allow_repair = body.get("allowRepair", False)

        def operation() -> tuple[int, dict[str, Any]]:
            pairing = self.sidecar_server.pair(allow_repair)
            return 200, {"ok": True, "pairing": _serialize_pairing(pairing)}

        self._dispatch(operation)

    def _handle_approve(self, body: dict[str, Any], timestamp: int) -> None:
        if set(body) != _INTENT_FIELDS:
            self._error(400, "invalid_request", "Purchase intent is invalid.")
            return
        if body["paymentAsset"] != "USDC" or body["paymentNetwork"] != "Base Mainnet":
            self._error(400, "invalid_request", "Purchase intent is invalid.")
            return
        try:
            intent = PurchaseIntent(
                intent_id=body["intentId"],
                product_slug=body["productSlug"],
                package_id=body["packageId"],
                denomination=body["denomination"],
                quoted_total_usd_micros=body["quotedTotalUsdMicros"],
                max_payment_usdc_atomic=body["maxPaymentUsdcAtomic"],
                recipient_hash=body["recipientHash"],
                expires_at=body["expiresAt"],
            )
        except (TypeError, ValueError):
            self._error(400, "invalid_request", "Purchase intent is invalid.")
            return

        def operation() -> tuple[int, dict[str, Any]]:
            approved = self.sidecar_server.approve_intent(intent, timestamp)
            if type(approved) is not PurchaseIntent:
                raise ValueError("invalid approval result")
            return 200, {
                "ok": True,
                "intentId": _bounded_text(approved.intent_id, maximum=66),
                "state": PaymentState.DEVICE_APPROVED.value,
            }

        self._dispatch(operation)

    def _handle_payment(
        self,
        body: dict[str, Any],
        timestamp: int,
        idempotency_key: str,
    ) -> None:
        if set(body) != _PAYMENT_FIELDS:
            self._error(400, "invalid_request", "Payment request is invalid.")
            return
        try:
            request = PaymentRequest(
                intent_id=body["intentId"],
                invoice_id=body["invoiceId"],
                pay_to=body["payTo"],
                amount_atomic=body["amountAtomic"],
                expires_at=body["expiresAt"],
            )
        except (TypeError, ValueError):
            self._error(400, "invalid_request", "Payment request is invalid.")
            return

        def operation() -> tuple[int, dict[str, Any]]:
            _, serialized = self.sidecar_server.create_and_schedule(
                request, idempotency_key, timestamp
            )
            return 202, {"ok": True, "payment": serialized}

        self._dispatch(operation)

    def _unsupported_method(self) -> None:
        path = self._path()
        if self._known_path(path):
            self._error(405, "method_not_allowed", "Method not allowed.")
        else:
            self._error(404, "not_found", "Route not found.")

    do_DELETE = _unsupported_method
    do_HEAD = _unsupported_method
    do_OPTIONS = _unsupported_method
    do_PATCH = _unsupported_method
    do_PUT = _unsupported_method
    do_TRACE = _unsupported_method


def build_server(
    settings: SidecarSettings,
    service: TrezorSidecarService,
    *,
    clock: Callable[[], int | float] = time.time,
    _allow_test_port: bool = False,
    _test_only_connection_timeout: float | None = None,
) -> ThreadingHTTPServer:
    """Build the sidecar server on the one approved IPv4 loopback address."""
    if not isinstance(settings, SidecarSettings):
        raise ValueError("settings must be SidecarSettings")
    if settings.host != "127.0.0.1":
        raise ValueError("sidecar host must be 127.0.0.1")
    if type(_allow_test_port) is not bool:
        raise ValueError("test port flag is invalid")
    if type(settings.port) is not int or (
        settings.port != 8111
        and not (_allow_test_port and settings.port == 0)
    ):
        raise ValueError("sidecar port is invalid")
    if not callable(clock):
        raise ValueError("clock must be callable")
    if _test_only_connection_timeout is None:
        connection_timeout = _CONNECTION_TIMEOUT_SECONDS
    elif (
        not _allow_test_port
        or settings.port != 0
        or isinstance(_test_only_connection_timeout, bool)
        or not isinstance(_test_only_connection_timeout, (int, float))
        or not math.isfinite(_test_only_connection_timeout)
        or not 0 < _test_only_connection_timeout <= _CONNECTION_TIMEOUT_SECONDS
    ):
        raise ValueError("test connection timeout is invalid")
    else:
        connection_timeout = float(_test_only_connection_timeout)
    return _SidecarHttpServer(
        (settings.host, settings.port),
        settings,
        service,
        clock,
        connection_timeout,
    )


def main() -> None:
    """Construct and run the isolated proof sidecar, failing closed."""
    server: ThreadingHTTPServer | None = None
    try:
        settings = SidecarSettings.from_env(os.environ)
        if not settings.enabled:
            raise ValueError("sidecar disabled")
        store = SidecarStore(settings.state_path)
        trezor = TrezorMcpClient(McpToolCaller(settings.mcp_token))
        rpc = BaseRpcClient(settings.base_rpc_url)
        service = TrezorSidecarService(settings, trezor, store, rpc=rpc)
        server = build_server(settings, service)
        server.serve_forever()
    except KeyboardInterrupt:
        return
    except Exception:
        raise SystemExit("Trezor sidecar failed to start safely.") from None
    finally:
        if server is not None:
            server.server_close()
