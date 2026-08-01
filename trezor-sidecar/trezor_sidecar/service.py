"""Pairing and purchase-intent approval orchestration.

This layer is deliberately narrow: it accepts only fixed Base/Trezor result
shapes, verifies every typed-data signature locally, and never gives signing
material to the durable store.
"""

import fcntl
import math
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from typing import Any

from eth_utils import to_checksum_address

from .base import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    EVM_DERIVATION_PATH,
    BaseBalances,
    BaseRpcClient,
    encode_usdc_transfer,
    verify_signed_usdc_transfer,
)
from .config import SidecarSettings
from .errors import SafeError
from .intent import build_typed_data, recover_intent_signer
from .mcp_client import TrezorMcpClient
from .models import (
    IntentRecord,
    Pairing,
    PaymentRequest,
    PaymentState,
    PaymentView,
    PurchaseIntent,
)
from .store import SidecarStore


_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_SIGNATURE = re.compile(r"(?:0x)?[0-9a-fA-F]{130}\Z")
_USDC_ATOMIC_PER_USD = Decimal(1_000_000)
_SIGNED_TIMESTAMP_MAX = (1 << 63) - 1
_DEVICE_LOCK = threading.Lock()
_RAW_TRANSACTION = re.compile(r"0x(?:[0-9a-fA-F]{2})+\Z")
_TRANSACTION_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
MIN_ETH_GAS_RESERVE_WEI = 100_000_000_000_000


def _safe(code: str, message: str, status: int = 400) -> SafeError:
    return SafeError(code, message, status)


def _disabled() -> SafeError:
    return _safe("disabled", "Trezor proof mode is disabled.", 503)


def _unavailable() -> SafeError:
    return _safe("trezor_unavailable", "Trezor Suite is unavailable.", 503)


def _fixed_configuration() -> SafeError:
    return _safe(
        "invalid_configuration",
        "The sidecar is not configured for the fixed Base account.",
        503,
    )


def _invalid_clock() -> SafeError:
    return _safe("invalid_clock", "The sidecar clock is invalid.", 503)


def _device_lock_unavailable() -> SafeError:
    return _safe("device_lock_unavailable", "The Trezor device lock is unavailable.", 503)


def _invalid_signature() -> SafeError:
    return _safe("invalid_signature", "Trezor did not return a valid approval signature.")


def _reapproval_required() -> SafeError:
    return _safe(
        "reapproval_required",
        "Purchase intent must be reapproved for the current Trezor pairing.",
        409,
    )


def _normalize_address(value: Any) -> str:
    if not isinstance(value, str) or _ADDRESS.fullmatch(value) is None:
        raise _unavailable()
    try:
        return to_checksum_address(value)
    except (TypeError, ValueError):
        raise _unavailable() from None


def _closed_field(result: Any, field: str) -> Any:
    """Read exactly one of ``field`` or ``payload.field``.

    Rejecting ambiguous duplicate paths prevents an upstream response from
    influencing which value is trusted through mapping iteration or fallback
    order.
    """
    if not isinstance(result, Mapping):
        raise _unavailable()
    root_present = field in result
    payload = result.get("payload")
    nested_present = isinstance(payload, Mapping) and field in payload
    if root_present == nested_present:
        raise _unavailable()
    return result[field] if root_present else payload[field]


class TrezorSidecarService:
    def __init__(
        self,
        settings: SidecarSettings,
        trezor: TrezorMcpClient,
        store: SidecarStore,
        *,
        clock: Callable[[], int | float] = time.time,
        rpc: BaseRpcClient | None = None,
    ):
        if not isinstance(settings, SidecarSettings):
            raise ValueError("settings must be SidecarSettings")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._settings = replace(settings)
        self.trezor = trezor
        self.store = store
        self._clock = clock
        self._rpc = BaseRpcClient(settings.base_rpc_url) if rpc is None else rpc
        self._device_lock_path = store.path.with_name(store.path.name + ".device.lock")

    @property
    def settings(self) -> SidecarSettings:
        return self._settings_snapshot()

    def _settings_snapshot(self) -> SidecarSettings:
        return replace(self._settings)

    @staticmethod
    def _require_enabled(settings: SidecarSettings) -> None:
        if not settings.enabled:
            raise _disabled()

    @staticmethod
    def _require_fixed_configuration(settings: SidecarSettings) -> None:
        if (
            settings.chain_id != BASE_CHAIN_ID
            or settings.derivation_path != EVM_DERIVATION_PATH
        ):
            raise _fixed_configuration()

    def _now(self) -> int:
        try:
            value = self._clock()
        except Exception:
            raise _invalid_clock() from None
        if isinstance(value, bool):
            raise _invalid_clock()
        if isinstance(value, int):
            timestamp = value
        elif isinstance(value, float) and math.isfinite(value):
            timestamp = int(value)
        else:
            raise _invalid_clock()
        if not 0 < timestamp <= _SIGNED_TIMESTAMP_MAX:
            raise _invalid_clock()
        return timestamp

    @staticmethod
    def _validate_lock_directory(info: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise _device_lock_unavailable()

    @staticmethod
    def _validate_lock_file(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise _device_lock_unavailable()

    def _open_device_lock(self) -> int:
        directory = None
        descriptor = None
        try:
            directory = os.open(
                self._device_lock_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            self._validate_lock_directory(os.fstat(directory))
            try:
                descriptor = os.open(
                    self._device_lock_path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                os.fchmod(descriptor, 0o600)
            except FileExistsError:
                info = os.stat(
                    self._device_lock_path.name,
                    dir_fd=directory,
                    follow_symlinks=False,
                )
                self._validate_lock_file(info)
                descriptor = os.open(
                    self._device_lock_path.name,
                    os.O_RDWR | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
            self._validate_lock_file(os.fstat(descriptor))
            result = descriptor
            descriptor = None
            return result
        except SafeError:
            raise
        except Exception:
            raise _device_lock_unavailable() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)

    @contextmanager
    def _device_guard(self, *, blocking: bool = True):
        """Coordinate device operations across threads and sidecar processes.

        The boolean result lets the later payment service use the same guard
        non-blockingly and map contention to its specified ``device_busy``
        response without weakening Task 6's blocking idempotent replay.
        """
        if not isinstance(blocking, bool):
            raise ValueError("blocking must be a boolean")
        thread_acquired = _DEVICE_LOCK.acquire(blocking=blocking)
        if not thread_acquired:
            yield False
            return
        descriptor = None
        file_acquired = False
        primary_error = None
        try:
            try:
                descriptor = self._open_device_lock()
                operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(descriptor, operation)
            except BlockingIOError:
                yield False
                return
            except SafeError:
                raise
            except Exception:
                raise _device_lock_unavailable() from None
            file_acquired = True
            yield True
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_error = None
            if file_acquired and descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except BaseException as error:
                    cleanup_error = error
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
            try:
                _DEVICE_LOCK.release()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
            if cleanup_error is not None and primary_error is None:
                raise _device_lock_unavailable() from None

    def _device_address(self, settings: SidecarSettings) -> str:
        try:
            result = self.trezor.get_base_address(settings.derivation_path)
            address = _closed_field(result, "address")
            return _normalize_address(address)
        except SafeError:
            raise _unavailable() from None
        except Exception:
            raise _unavailable() from None

    def pair(self, allow_repair: bool = False) -> Pairing:
        if not isinstance(allow_repair, bool):
            raise _safe("invalid_request", "Pairing repair flag must be a boolean.")

        with self._device_guard() as acquired:
            if not acquired:
                raise _device_lock_unavailable()
            settings = self._settings_snapshot()
            self._require_enabled(settings)
            self._require_fixed_configuration(settings)
            now = self._now()
            address = self._device_address(settings)
            current = self.store.get_pairing()
            if current is not None and (
                current.address != address
                or current.derivation_path != settings.derivation_path
            ):
                if not allow_repair:
                    raise _safe(
                        "pairing_mismatch",
                        "A different Trezor is paired; explicit repair is required.",
                        409,
                    )
                candidate = Pairing(
                    pairing_id=uuid.uuid4().hex,
                    address=address,
                    derivation_path=settings.derivation_path,
                    created_at=now,
                    updated_at=now,
                )
            elif current is not None:
                candidate = Pairing(
                    pairing_id=current.pairing_id,
                    address=current.address,
                    derivation_path=current.derivation_path,
                    created_at=current.created_at,
                    updated_at=max(current.updated_at, now),
                )
            else:
                candidate = Pairing(
                    pairing_id=uuid.uuid4().hex,
                    address=address,
                    derivation_path=settings.derivation_path,
                    created_at=now,
                    updated_at=now,
                )
            try:
                return self.store.save_pairing(candidate, allow_repair=allow_repair)
            except ValueError as error:
                if "different Trezor" in str(error):
                    raise _safe(
                        "pairing_mismatch",
                        "A different Trezor is paired; explicit repair is required.",
                        409,
                    ) from None
                raise _safe("pairing_failed", "Trezor pairing could not be saved.", 409) from None

    @staticmethod
    def _validate_now(now: int) -> int:
        if (
            isinstance(now, bool)
            or not isinstance(now, int)
            or not 0 < now <= _SIGNED_TIMESTAMP_MAX
        ):
            raise _safe("invalid_request", "Approval time must be a positive integer.")
        return now

    @staticmethod
    def _validate_fixed_intent(intent: PurchaseIntent) -> None:
        if not isinstance(intent, PurchaseIntent):
            raise _safe("invalid_intent", "Purchase intent is invalid.")
        if intent.payment_asset != "USDC" or intent.payment_network != "Base Mainnet":
            raise _safe("invalid_intent", "Purchase intent fixed fields are invalid.")

    def _validate_intent(
        self,
        settings: SidecarSettings,
        intent: PurchaseIntent,
        now: int,
    ) -> None:
        self._validate_fixed_intent(intent)
        if intent.expires_at <= now:
            raise _safe("intent_expired", "Purchase intent has expired.")
        maximum = settings.max_usd
        if (
            not isinstance(maximum, Decimal)
            or not maximum.is_finite()
            or maximum <= 0
        ):
            raise _fixed_configuration()
        if Decimal(intent.max_payment_usdc_atomic) > maximum * _USDC_ATOMIC_PER_USD:
            raise _safe(
                "intent_limit_exceeded",
                "Purchase intent exceeds the configured limit.",
            )

    def _existing_intent(self, intent: PurchaseIntent):
        existing = self.store.get_intent(intent.intent_id)
        if existing is None:
            return None
        if existing.intent != intent:
            raise _safe(
                "intent_conflict",
                "Purchase intent conflicts with an existing intent ID.",
                409,
            )
        if existing.state is PaymentState.DEVICE_APPROVED:
            return existing
        if existing.state is not PaymentState.QUOTED:
            raise _safe("intent_state_changed", "Purchase intent state does not allow approval.", 409)
        return existing

    @staticmethod
    def _require_current_approval(
        intent: IntentRecord,
        pairing: Pairing,
    ) -> None:
        if (
            intent.state is not PaymentState.DEVICE_APPROVED
            or intent.approved_at is None
            or intent.approved_at <= pairing.created_at
        ):
            raise _reapproval_required()

    def _insert_intent(self, intent: PurchaseIntent, now: int):
        try:
            return self.store.insert_intent(intent, created_at=now)
        except ValueError:
            existing = self._existing_intent(intent)
            if existing is None:
                raise _safe("intent_conflict", "Purchase intent could not be recorded.", 409) from None
            return existing

    def _signature(self, settings: SidecarSettings, intent: PurchaseIntent) -> str:
        try:
            result = self.trezor.sign_typed_data(
                settings.derivation_path,
                build_typed_data(intent),
            )
        except SafeError as error:
            if error.code in {"device_rejected", "device_cancelled", "action_cancelled"}:
                raise _safe(
                    "device_rejected",
                    "Purchase approval was cancelled on Trezor.",
                ) from None
            if error.code in {"device_timeout", "timeout"}:
                raise _safe("device_timeout", "Trezor approval timed out.", 504) from None
            raise _unavailable() from None
        except TimeoutError:
            raise _safe("device_timeout", "Trezor approval timed out.", 504) from None
        except Exception:
            raise _unavailable() from None

        try:
            signature = _closed_field(result, "signature")
        except SafeError:
            raise _invalid_signature() from None
        if not isinstance(signature, str) or _SIGNATURE.fullmatch(signature) is None:
            raise _invalid_signature()
        return signature

    def approve_intent(self, intent: PurchaseIntent, now: int) -> PurchaseIntent:
        with self._device_guard() as acquired:
            if not acquired:
                raise _device_lock_unavailable()
            settings = self._settings_snapshot()
            self._require_enabled(settings)
            self._require_fixed_configuration(settings)
            now = self._validate_now(now)
            pairing = self.store.get_pairing()
            if pairing is None:
                raise _safe("not_paired", "A Trezor must be paired before approval.", 409)
            if (
                pairing.derivation_path != EVM_DERIVATION_PATH
                or pairing.derivation_path != settings.derivation_path
            ):
                raise _safe("pairing_mismatch", "The stored Trezor pairing is invalid.", 409)

            self._validate_fixed_intent(intent)
            existing = self._existing_intent(intent)
            if existing is not None and existing.state is PaymentState.DEVICE_APPROVED:
                self._require_current_approval(existing, pairing)
                return existing.intent
            if now <= pairing.created_at:
                raise _reapproval_required()
            self._validate_intent(settings, intent, now)
            self._insert_intent(intent, now)

            signature = self._signature(settings, intent)
            try:
                signer = to_checksum_address(recover_intent_signer(intent, signature))
            except Exception:
                raise _invalid_signature() from None
            if signer != pairing.address:
                raise _safe(
                    "signer_mismatch",
                    "Purchase approval does not match the paired Trezor.",
                )

            try:
                approved = self.store.approve_intent(intent.intent_id, approved_at=now)
            except ValueError:
                raise _safe(
                    "intent_state_changed",
                    "Purchase intent state does not allow approval.",
                    409,
                ) from None
            return approved.intent

    def create_payment(
        self,
        request: PaymentRequest,
        idempotency_key: str,
        now: int,
    ) -> PaymentView:
        settings = self._settings_snapshot()
        now = self._validate_now(now)
        if type(request) is not PaymentRequest:
            raise _safe("invalid_request", "Payment request is invalid.")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 256
            or "\x00" in idempotency_key
        ):
            raise _safe("invalid_request", "Payment idempotency key is invalid.")
        replay = self._payment_replay(request, idempotency_key)
        pairing = self.store.get_pairing()
        if pairing is None:
            raise _safe("not_paired", "A Trezor must be paired before payment.", 409)
        if pairing.derivation_path != EVM_DERIVATION_PATH:
            raise _safe("pairing_mismatch", "The stored Trezor pairing is invalid.", 409)
        intent = self.store.get_intent(request.intent_id)
        if intent is None or intent.state is not PaymentState.DEVICE_APPROVED:
            raise _safe("intent_not_approved", "Purchase intent is not approved.", 409)
        self._require_current_approval(intent, pairing)
        if now < intent.approved_at:
            raise _invalid_clock()
        if replay is not None:
            return replay
        self._require_enabled(settings)
        self._require_fixed_configuration(settings)
        if pairing.derivation_path != settings.derivation_path:
            raise _safe("pairing_mismatch", "The stored Trezor pairing is invalid.", 409)
        self._validate_fixed_intent(intent.intent)
        if intent.intent.expires_at <= now:
            raise _safe("intent_expired", "Purchase intent has expired.")
        if request.expires_at <= now:
            raise _safe("invoice_expired", "Payment invoice has expired.")
        maximum = settings.max_usd
        if not isinstance(maximum, Decimal) or not maximum.is_finite() or maximum <= 0:
            raise _fixed_configuration()
        if (
            request.amount_atomic > intent.intent.max_payment_usdc_atomic
            or Decimal(request.amount_atomic) > maximum * _USDC_ATOMIC_PER_USD
        ):
            raise _safe("payment_limit_exceeded", "Payment exceeds the approved limit.")
        try:
            return self.store.create_payment(
                payment_id=uuid.uuid4().hex,
                intent_id=request.intent_id,
                invoice_id=request.invoice_id,
                idempotency_key=idempotency_key,
                pay_to=request.pay_to,
                amount_atomic=request.amount_atomic,
                expires_at=request.expires_at,
                created_at=now,
            )
        except (sqlite3.IntegrityError, ValueError):
            raise _safe(
                "payment_conflict",
                "Payment invoice or idempotency key conflicts with an existing payment.",
                409,
            ) from None

    def _payment_replay(
        self,
        request: PaymentRequest,
        idempotency_key: str,
    ) -> PaymentView | None:
        connection = self.store._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM payments
                WHERE invoice_id = ? OR idempotency_key = ?""",
                (request.invoice_id, idempotency_key),
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            return None
        invoice_row = rows[0]
        if (
            len(rows) != 1
            or invoice_row["intent_id"] != request.intent_id
            or invoice_row["invoice_id"] != request.invoice_id
            or invoice_row["idempotency_key"] != idempotency_key
            or invoice_row["pay_to"] != request.pay_to
            or invoice_row["amount_atomic"] != str(request.amount_atomic)
            or invoice_row["expires_at"] != request.expires_at
        ):
            raise _safe(
                "payment_conflict",
                "Payment invoice or idempotency key conflicts with an existing payment.",
                409,
            )
        return self.store._payment(invoice_row)

    def _payment_request(self, payment_id: str) -> PaymentRequest:
        connection = self.store._connect()
        try:
            row = connection.execute(
                """SELECT intent_id, invoice_id, pay_to, amount_atomic, expires_at
                FROM payments WHERE payment_id = ?""",
                (payment_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise _safe("payment_not_found", "Payment was not found.", 404)
        try:
            return PaymentRequest(
                intent_id=row["intent_id"],
                invoice_id=row["invoice_id"],
                pay_to=row["pay_to"],
                amount_atomic=int(row["amount_atomic"]),
                expires_at=row["expires_at"],
            )
        except (TypeError, ValueError):
            raise _safe("payment_invalid", "Stored payment is invalid.", 409) from None

    @staticmethod
    def _signed_transaction(result: Any) -> str:
        if not isinstance(result, Mapping):
            raise _safe(
                "invalid_signed_transaction",
                "Trezor did not return a valid signed transaction.",
            )
        payload = result.get("payload")
        if not isinstance(payload, Mapping) or "serializedTx" in result:
            raise _safe(
                "invalid_signed_transaction",
                "Trezor did not return a valid signed transaction.",
            )
        direct = "serializedTx" in payload
        signed = payload.get("signed")
        nested = isinstance(signed, Mapping) and "serializedTx" in signed
        if direct == nested:
            raise _safe(
                "invalid_signed_transaction",
                "Trezor did not return a valid signed transaction.",
            )
        raw = payload["serializedTx"] if direct else signed["serializedTx"]
        if not isinstance(raw, str) or _RAW_TRANSACTION.fullmatch(raw) is None:
            raise _safe(
                "invalid_signed_transaction",
                "Trezor did not return a valid signed transaction.",
            )
        return raw

    @staticmethod
    def _tx_hash(value: Any) -> str:
        if not isinstance(value, str) or _TRANSACTION_HASH.fullmatch(value) is None:
            raise _safe("broadcast_ambiguous", "Transaction broadcast outcome is ambiguous.", 409)
        return "0x" + value[2:].lower()

    @classmethod
    def _broadcast_hash(cls, result: Any) -> str:
        if not isinstance(result, Mapping):
            raise _safe("broadcast_ambiguous", "Transaction broadcast outcome is ambiguous.", 409)
        candidates = []
        payload = result.get("payload")
        for container in (result, payload if isinstance(payload, Mapping) else {}):
            for field in ("txid", "txId", "hash"):
                if field in container:
                    candidates.append(container[field])
        if len(candidates) != 1:
            raise _safe("broadcast_ambiguous", "Transaction broadcast outcome is ambiguous.", 409)
        return cls._tx_hash(candidates[0])

    @staticmethod
    def _run_timestamp(now: Callable[[], int]) -> int:
        if not callable(now):
            raise _safe("invalid_request", "Payment clock must be callable.")
        try:
            value = now()
        except Exception:
            raise _invalid_clock() from None
        if isinstance(value, bool) or not isinstance(value, int):
            raise _invalid_clock()
        if not 0 < value <= _SIGNED_TIMESTAMP_MAX:
            raise _invalid_clock()
        return value

    def get_payment(self, payment_id: str) -> PaymentView:
        try:
            payment = self.store.get_payment(payment_id)
        except (TypeError, ValueError):
            raise _safe("invalid_request", "Payment ID is invalid.") from None
        if payment is None:
            raise _safe("payment_not_found", "Payment was not found.", 404)
        return payment

    @staticmethod
    def _validate_payment_limits(
        settings: SidecarSettings,
        request: PaymentRequest,
        intent: PurchaseIntent,
    ) -> None:
        maximum = settings.max_usd
        if not isinstance(maximum, Decimal) or not maximum.is_finite() or maximum <= 0:
            raise _fixed_configuration()
        if (
            request.amount_atomic > intent.max_payment_usdc_atomic
            or Decimal(request.amount_atomic) > maximum * _USDC_ATOMIC_PER_USD
        ):
            raise _safe("payment_limit_exceeded", "Payment exceeds the approved limit.")

    @staticmethod
    def _require_fresh_payment(
        request: PaymentRequest,
        intent: PurchaseIntent,
        timestamp: int,
    ) -> None:
        if intent.expires_at <= timestamp:
            raise _safe("intent_expired", "Purchase intent has expired.")
        if request.expires_at <= timestamp:
            raise _safe("invoice_expired", "Payment invoice has expired.")

    @staticmethod
    def _valid_balances(value: Any) -> bool:
        return (
            type(value) is BaseBalances
            and type(value.eth_wei) is int
            and type(value.usdc_atomic) is int
            and 0 <= value.eth_wei < 1 << 256
            and 0 <= value.usdc_atomic < 1 << 256
        )

    def _balances(self, address: str) -> BaseBalances:
        try:
            balances = self._rpc.get_balances(address)
        except SafeError as error:
            if error.code == "base_rpc_unavailable":
                raise _safe("base_rpc_unavailable", "Base RPC is unavailable.", 503) from None
            raise _safe("base_rpc_unavailable", "Base RPC is unavailable.", 503) from None
        except Exception:
            raise _safe("base_rpc_unavailable", "Base RPC is unavailable.", 503) from None
        if not self._valid_balances(balances):
            raise _safe("base_rpc_unavailable", "Base RPC is unavailable.", 503)
        return balances

    def _sign_payment(
        self,
        settings: SidecarSettings,
        request: PaymentRequest,
    ) -> str:
        try:
            result = self.trezor.sign_base_transaction(
                settings.derivation_path,
                BASE_USDC_ADDRESS,
                encode_usdc_transfer(request.pay_to, request.amount_atomic),
            )
        except SafeError as error:
            if error.code in {"device_rejected", "device_cancelled", "action_cancelled"}:
                raise _safe(
                    "device_rejected",
                    "Payment signing was cancelled on Trezor.",
                ) from None
            if error.code in {"device_timeout", "timeout"}:
                raise _safe(
                    "device_timeout",
                    "Trezor payment signing timed out.",
                    504,
                ) from None
            raise _unavailable() from None
        except TimeoutError:
            raise _safe(
                "device_timeout",
                "Trezor payment signing timed out.",
                504,
            ) from None
        except Exception:
            raise _unavailable() from None
        return self._signed_transaction(result)

    def _fail_payment(
        self,
        payment_id: str,
        updated_at: int,
    ) -> None:
        """Persist a legal pre-push failure without replacing its safe error."""
        try:
            current = self.store.get_payment(payment_id)
        except Exception:
            raise _safe(
                "payment_state_unavailable",
                "Payment failure state could not be recorded safely.",
                503,
            ) from None
        if current is None:
            raise _safe(
                "payment_state_unavailable",
                "Payment failure state could not be recorded safely.",
                503,
            )
        if current.state is PaymentState.FAILED:
            return
        if current.state not in {PaymentState.INVOICE_CREATED, PaymentState.TX_SIGNED}:
            raise _safe(
                "payment_state_changed",
                "Payment state changed while recording failure.",
                409,
            )
        try:
            self.store.transition_payment(
                payment_id=payment_id,
                expected=current.state,
                target=PaymentState.FAILED,
                updated_at=max(updated_at, current.updated_at),
            )
        except Exception:
            try:
                refreshed = self.store.get_payment(payment_id)
            except Exception:
                refreshed = None
            if refreshed is not None and refreshed.state is PaymentState.FAILED:
                return
            if (
                refreshed is not None
                and refreshed.state not in {
                    PaymentState.INVOICE_CREATED,
                    PaymentState.TX_SIGNED,
                }
            ):
                raise _safe(
                    "payment_state_changed",
                    "Payment state changed while recording failure.",
                    409,
                ) from None
            raise _safe(
                "payment_state_unavailable",
                "Payment failure state could not be recorded safely.",
                503,
            ) from None

    def _raise_pre_push_failure(
        self,
        payment_id: str,
        error: SafeError,
        updated_at: int,
    ) -> None:
        self._fail_payment(payment_id, updated_at)
        raise error from None

    @staticmethod
    def _reconciliation_error() -> SafeError:
        return _safe(
            "reconciliation_required",
            "Transaction broadcast outcome requires reconciliation.",
            409,
        )

    def _reconcile_payment(
        self,
        payment_id: str,
        updated_at: int,
        tx_hash: str | None = None,
    ) -> PaymentView:
        try:
            current = self.store.get_payment(payment_id)
        except BaseException:
            raise self._reconciliation_error() from None
        if current is None:
            raise self._reconciliation_error() from None
        if current.state is PaymentState.RECONCILIATION_REQUIRED:
            if tx_hash is None or current.tx_hash == tx_hash:
                return current
            raise self._reconciliation_error() from None
        if (
            current.state is PaymentState.TX_BROADCAST
            and tx_hash is not None
            and current.tx_hash == tx_hash
        ):
            return current
        if current.state is not PaymentState.TX_SIGNED:
            raise self._reconciliation_error() from None
        try:
            return self.store.transition_payment(
                payment_id=payment_id,
                expected=PaymentState.TX_SIGNED,
                target=PaymentState.RECONCILIATION_REQUIRED,
                updated_at=max(updated_at, current.updated_at),
                tx_hash=tx_hash,
            )
        except BaseException:
            try:
                refreshed = self.store.get_payment(payment_id)
            except BaseException:
                raise self._reconciliation_error() from None
            if refreshed is None:
                raise self._reconciliation_error() from None
            if (
                refreshed.state is PaymentState.RECONCILIATION_REQUIRED
                and (tx_hash is None or refreshed.tx_hash == tx_hash)
            ):
                return refreshed
            if (
                refreshed.state is PaymentState.TX_BROADCAST
                and tx_hash is not None
                and refreshed.tx_hash == tx_hash
            ):
                return refreshed
            raise self._reconciliation_error() from None

    def run_payment(
        self,
        payment_id: str,
        now: Callable[[], int],
    ) -> PaymentView:
        if not callable(now):
            raise _safe("invalid_request", "Payment clock must be callable.")
        payment = self.get_payment(payment_id)
        if payment.state in {
            PaymentState.TX_BROADCAST,
            PaymentState.COMPLETE,
            PaymentState.CANCELLED,
            PaymentState.FAILED,
            PaymentState.RECONCILIATION_REQUIRED,
        }:
            return payment
        with self._device_guard(blocking=False) as acquired:
            if not acquired:
                raise _safe("device_busy", "Another Trezor approval is active.", 409)
            settings = self._settings_snapshot()
            payment = self.get_payment(payment_id)
            if payment.state in {
                PaymentState.TX_BROADCAST,
                PaymentState.COMPLETE,
                PaymentState.CANCELLED,
                PaymentState.FAILED,
                PaymentState.RECONCILIATION_REQUIRED,
            }:
                return payment
            if payment.state is PaymentState.TX_SIGNED:
                return self._reconcile_payment(
                    payment.payment_id,
                    self._run_timestamp(now),
                )
            if payment.state is not PaymentState.INVOICE_CREATED:
                raise _safe("payment_state_changed", "Payment state does not allow running.", 409)

            updated_at = payment.updated_at
            try:
                self._require_enabled(settings)
                self._require_fixed_configuration(settings)
                timestamp = self._run_timestamp(now)
                if timestamp < payment.updated_at:
                    raise _invalid_clock()
                updated_at = max(updated_at, timestamp)
                request = self._payment_request(payment.payment_id)
                if (
                    request.intent_id != payment.intent_id
                    or request.invoice_id != payment.invoice_id
                ):
                    raise _safe("payment_invalid", "Stored payment is invalid.", 409)
                pairing = self.store.get_pairing()
                if (
                    pairing is None
                    or pairing.derivation_path != EVM_DERIVATION_PATH
                    or pairing.derivation_path != settings.derivation_path
                ):
                    raise _safe(
                        "pairing_mismatch",
                        "The stored Trezor pairing is invalid.",
                        409,
                    )
                intent_record = self.store.get_intent(payment.intent_id)
                if (
                    intent_record is None
                    or intent_record.state is not PaymentState.DEVICE_APPROVED
                ):
                    raise _safe(
                        "intent_not_approved",
                        "Purchase intent is not approved.",
                        409,
                    )
                intent = intent_record.intent
                self._require_current_approval(intent_record, pairing)
                if (
                    timestamp < intent_record.approved_at
                    or timestamp < pairing.created_at
                ):
                    raise _invalid_clock()
                self._validate_fixed_intent(intent)
                self._validate_payment_limits(settings, request, intent)
                self._require_fresh_payment(request, intent, timestamp)

                balances = self._balances(pairing.address)
                if balances.usdc_atomic < request.amount_atomic:
                    raise _safe(
                        "insufficient_usdc",
                        "The paired Base account has insufficient USDC.",
                        409,
                    )
                if balances.eth_wei <= MIN_ETH_GAS_RESERVE_WEI:
                    raise _safe(
                        "insufficient_eth",
                        "The paired Base account has insufficient ETH for gas.",
                        409,
                    )

                raw = self._sign_payment(settings, request)
                local_hash = self._tx_hash(verify_signed_usdc_transfer(
                    raw,
                    pairing.address,
                    request.pay_to,
                    request.amount_atomic,
                ))
                try:
                    signed = self.store.transition_payment(
                        payment_id=payment.payment_id,
                        expected=PaymentState.INVOICE_CREATED,
                        target=PaymentState.TX_SIGNED,
                        updated_at=updated_at,
                    )
                except Exception:
                    raise _safe(
                        "payment_state_changed",
                        "Payment state does not allow signing.",
                        409,
                    ) from None

                push_timestamp = self._run_timestamp(now)
                if push_timestamp < timestamp:
                    raise _invalid_clock()
                updated_at = max(updated_at, push_timestamp)
                fresh_request = self._payment_request(payment.payment_id)
                fresh_intent_record = self.store.get_intent(payment.intent_id)
                if (
                    fresh_request != request
                    or fresh_intent_record is None
                    or fresh_intent_record.state is not PaymentState.DEVICE_APPROVED
                    or fresh_intent_record.intent != intent
                    or self.get_payment(payment.payment_id).state is not PaymentState.TX_SIGNED
                ):
                    raise _safe(
                        "payment_state_changed",
                        "Payment state changed before broadcast.",
                        409,
                    )
                self._require_fresh_payment(
                    fresh_request,
                    fresh_intent_record.intent,
                    push_timestamp,
                )
            except SafeError as error:
                self._raise_pre_push_failure(
                    payment.payment_id,
                    error,
                    updated_at,
                )
            except Exception:
                self._raise_pre_push_failure(
                    payment.payment_id,
                    _safe("payment_failed", "Payment signing failed safely.", 500),
                    updated_at,
                )

            try:
                broadcast = self.trezor.push_base_transaction(raw)
                returned_hash = self._broadcast_hash(broadcast)
                if returned_hash != local_hash:
                    raise self._reconciliation_error()
                return self.store.transition_payment(
                    payment_id=signed.payment_id,
                    expected=PaymentState.TX_SIGNED,
                    target=PaymentState.TX_BROADCAST,
                    updated_at=updated_at,
                    tx_hash=local_hash,
                )
            except BaseException:
                classified = self._reconcile_payment(
                    payment.payment_id,
                    updated_at,
                    tx_hash=local_hash,
                )
                if classified.state is PaymentState.TX_BROADCAST:
                    return classified
                raise self._reconciliation_error() from None
