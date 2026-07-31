"""Pairing and purchase-intent approval orchestration.

This layer is deliberately narrow: it accepts only fixed Base/Trezor result
shapes, verifies every typed-data signature locally, and never gives signing
material to the durable store.
"""

import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from eth_utils import to_checksum_address

from .base import BASE_CHAIN_ID, EVM_DERIVATION_PATH
from .config import SidecarSettings
from .errors import SafeError
from .intent import build_typed_data, recover_intent_signer
from .mcp_client import TrezorMcpClient
from .models import Pairing, PaymentState, PurchaseIntent
from .store import SidecarStore


_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_SIGNATURE = re.compile(r"(?:0x)?[0-9a-fA-F]{130}\Z")
_USDC_ATOMIC_PER_USD = Decimal(1_000_000)
_DEVICE_LOCK = threading.Lock()


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


def _invalid_signature() -> SafeError:
    return _safe("invalid_signature", "Trezor did not return a valid approval signature.")


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
    ):
        if not isinstance(settings, SidecarSettings):
            raise ValueError("settings must be SidecarSettings")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.settings = settings
        self.trezor = trezor
        self.store = store
        self._clock = clock

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise _disabled()

    def _require_fixed_configuration(self) -> None:
        if (
            self.settings.chain_id != BASE_CHAIN_ID
            or self.settings.derivation_path != EVM_DERIVATION_PATH
        ):
            raise _fixed_configuration()

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fixed_configuration()
        timestamp = int(value)
        if timestamp <= 0:
            raise _fixed_configuration()
        return timestamp

    def _device_address(self) -> str:
        try:
            result = self.trezor.get_base_address(self.settings.derivation_path)
            address = _closed_field(result, "address")
            return _normalize_address(address)
        except SafeError as error:
            if error.code == "trezor_unavailable":
                raise _unavailable() from None
            raise _unavailable() from None
        except Exception:
            raise _unavailable() from None

    def pair(self, allow_repair: bool = False) -> Pairing:
        self._require_enabled()
        self._require_fixed_configuration()
        if not isinstance(allow_repair, bool):
            raise _safe("invalid_request", "Pairing repair flag must be a boolean.")

        with _DEVICE_LOCK:
            address = self._device_address()
            now = self._now()
            current = self.store.get_pairing()
            if current is not None and (
                current.address != address
                or current.derivation_path != self.settings.derivation_path
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
                    derivation_path=self.settings.derivation_path,
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
                    derivation_path=self.settings.derivation_path,
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
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise _safe("invalid_request", "Approval time must be a positive integer.")
        return now

    @staticmethod
    def _validate_fixed_intent(intent: PurchaseIntent) -> None:
        if not isinstance(intent, PurchaseIntent):
            raise _safe("invalid_intent", "Purchase intent is invalid.")
        if intent.payment_asset != "USDC" or intent.payment_network != "Base Mainnet":
            raise _safe("invalid_intent", "Purchase intent fixed fields are invalid.")

    def _validate_intent(self, intent: PurchaseIntent, now: int) -> None:
        self._validate_fixed_intent(intent)
        if intent.expires_at <= now:
            raise _safe("intent_expired", "Purchase intent has expired.")
        maximum = self.settings.max_usd
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

    def _insert_intent(self, intent: PurchaseIntent, now: int):
        try:
            return self.store.insert_intent(intent, created_at=now)
        except ValueError:
            existing = self._existing_intent(intent)
            if existing is None:
                raise _safe("intent_conflict", "Purchase intent could not be recorded.", 409) from None
            return existing

    def _signature(self, intent: PurchaseIntent) -> str:
        try:
            result = self.trezor.sign_typed_data(
                self.settings.derivation_path,
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
        self._require_enabled()
        self._require_fixed_configuration()
        now = self._validate_now(now)

        with _DEVICE_LOCK:
            pairing = self.store.get_pairing()
            if pairing is None:
                raise _safe("not_paired", "A Trezor must be paired before approval.", 409)
            if (
                pairing.derivation_path != EVM_DERIVATION_PATH
                or pairing.derivation_path != self.settings.derivation_path
            ):
                raise _safe("pairing_mismatch", "The stored Trezor pairing is invalid.", 409)

            self._validate_fixed_intent(intent)
            existing = self._existing_intent(intent)
            if existing is not None and existing.state is PaymentState.DEVICE_APPROVED:
                return existing.intent
            self._validate_intent(intent, now)
            self._insert_intent(intent, now)

            signature = self._signature(intent)
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
