"""Local-only Trezor proof runner for a tightly bounded Bitrefill purchase."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sign402_gateway.bitrefill_mcp import McpBitrefillClient

from .base import BASE_USDC_ADDRESS
from .config import RunnerSettings
from .errors import SafeError
from .intent import recipient_hash
from .models import PaymentState, PurchaseIntent
from .sidecar_client import SidecarClient
from .store import SidecarStore


_PROOF_STATE_PATH = Path("~/.sign402-trezor-poc/state.db").expanduser()
_USDC_ATOMIC = Decimal(1_000_000)
_INTENT_TTL_SECONDS = 600
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_INVOICE_ID = re.compile(r"[A-Za-z0-9._:-]{1,110}\Z")
_BUYER_EMAIL_COMMITMENT_KEY = "__sign402_buyer_email__"
_QUOTE_FIELDS = frozenset(
    {
        "productId",
        "name",
        "productType",
        "packageId",
        "packageValue",
        "country",
        "currency",
        "priceUsd",
        "recipientType",
        "requiredRecipientFields",
    }
)


def _safe(code: str, message: str, status: int = 400) -> SafeError:
    return SafeError(code, message, status)


def _timestamp(value: Any, name: str = "timestamp") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= (1 << 63) - 1:
        raise _safe("invalid_request", f"{name.capitalize()} is invalid.")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _safe("invalid_request", f"{name} is invalid.") from None
    if not result.is_finite() or result <= 0:
        raise _safe("invalid_request", f"{name} is invalid.")
    return result


def _atomic(value: Decimal, name: str) -> int:
    converted = value * _USDC_ATOMIC
    if converted != converted.to_integral_value() or converted > Decimal((1 << 256) - 1):
        raise _safe("invalid_request", f"{name} exceeds USDC precision.")
    return int(converted)


def _payment_address(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 42
        or _ADDRESS.fullmatch(value) is None
        or int(value[2:], 16) == 0
    ):
        raise ValueError("Bitrefill payment address is invalid")
    return value


def _snapshot_text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if (
        type(value) is not str
        or (not value and not allow_empty)
        or len(value) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _safe("invalid_request", f"{name} is invalid.")
    return value


@dataclass(frozen=True)
class _ApprovedPurchase:
    quote_items: tuple[tuple[str, str], ...]
    required_recipient_fields: tuple[str, ...]
    recipient_items: tuple[tuple[str, str], ...]
    buyer_email: str

    def quote_copy(self) -> dict[str, Any]:
        result: dict[str, Any] = dict(self.quote_items)
        result["requiredRecipientFields"] = list(self.required_recipient_fields)
        return result

    def recipient_copy(self) -> dict[str, str]:
        return dict(self.recipient_items)

    def committed_recipient_copy(self) -> dict[str, str]:
        result = self.recipient_copy()
        result[_BUYER_EMAIL_COMMITMENT_KEY] = self.buyer_email
        return result


def _approved_purchase_snapshot(
    quote: Any,
    recipient: Any,
    buyer_email: Any,
) -> _ApprovedPurchase:
    try:
        quote_copy = deepcopy(quote)
        recipient_copy = deepcopy(recipient)
        buyer_copy = deepcopy(buyer_email)
    except Exception:
        raise _safe("invalid_request", "Purchase details are invalid.") from None
    if type(quote_copy) is not dict or set(quote_copy) != _QUOTE_FIELDS:
        raise _safe("invalid_request", "Product quote is invalid.")
    if type(recipient_copy) is not dict:
        raise _safe("invalid_request", "Recipient fields are invalid.")
    buyer = _snapshot_text(buyer_copy, "Buyer email", allow_empty=True)
    required = quote_copy["requiredRecipientFields"]
    if type(required) is not list or len(required) > 32:
        raise _safe("invalid_request", "Recipient fields are invalid.")
    required_fields = tuple(
        _snapshot_text(field, "Recipient field") for field in required
    )
    if len(set(required_fields)) != len(required_fields):
        raise _safe("invalid_request", "Recipient fields are invalid.")
    if _BUYER_EMAIL_COMMITMENT_KEY in required_fields or _BUYER_EMAIL_COMMITMENT_KEY in recipient_copy:
        raise _safe("invalid_request", "Recipient fields are invalid.")
    if set(recipient_copy) != set(required_fields):
        raise _safe("invalid_request", "Recipient fields are invalid.")
    recipient_items = tuple(
        sorted(
            (
                _snapshot_text(key, "Recipient field"),
                _snapshot_text(value, "Recipient value"),
            )
            for key, value in recipient_copy.items()
        )
    )
    quote_items = tuple(
        sorted(
            (
                key,
                _snapshot_text(value, "Product quote field"),
            )
            for key, value in quote_copy.items()
            if key != "requiredRecipientFields"
        )
    )
    return _ApprovedPurchase(
        quote_items=quote_items,
        required_recipient_fields=required_fields,
        recipient_items=recipient_items,
        buyer_email=buyer,
    )


class PreparedAddressBitrefillClient(McpBitrefillClient):
    """Narrow bridge retaining only the gateway-validated invoice address."""

    def _validated_invoice_snapshot(
        self,
        invoice: dict[str, Any],
        *,
        quote: dict[str, Any],
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = super()._validated_invoice_snapshot(
            invoice,
            quote=quote,
            fallback=fallback,
        )
        if self.payment_method != "usdc_base":
            return snapshot
        payment_info = invoice.get("payment_info")
        if not isinstance(payment_info, dict):
            payment_info = invoice.get("paymentInfo")
        if isinstance(payment_info, dict):
            validated = self._validated_payment_requirements(invoice, quote=quote)
            address = _payment_address(validated.get("address"))
        else:
            if fallback is None:
                raise ValueError("Bitrefill payment address is missing")
            address = _payment_address(fallback.get("paymentAddress"))
        snapshot["paymentAddress"] = address
        return snapshot


@dataclass(frozen=True)
class _PreparedBinding:
    intent_id: str
    invoice_id: str
    expires_at: int
    payment_address: str
    amount_atomic: str


class SidecarTreasuryClient:
    """Exact-payment adapter used by the existing Bitrefill client."""

    def __init__(self, *, sidecar: SidecarClient, clock: Callable[[], int | float] = time.time):
        if not callable(clock):
            raise ValueError("treasury clock must be callable")
        self._sidecar = sidecar
        self._clock = clock
        self._approved: dict[str, PurchaseIntent] = {}
        self._bindings: dict[str, _PreparedBinding] = {}
        self._results: dict[str, dict[str, str]] = {}
        self._payment_receipts: dict[str, dict[str, Any]] = {}
        self._terminal_errors: dict[str, tuple[str, str, int]] = {}
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return "SidecarTreasuryClient(sidecar='<redacted>')"

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool):
            raise _safe("invalid_clock", "The runner clock is invalid.", 503)
        if isinstance(value, int):
            now = value
        elif isinstance(value, float) and math.isfinite(value):
            now = int(value)
        else:
            raise _safe("invalid_clock", "The runner clock is invalid.", 503)
        return _timestamp(now)

    def register_approved_intent(self, intent: PurchaseIntent) -> None:
        if type(intent) is not PurchaseIntent:
            raise _safe("invalid_intent", "Purchase intent is invalid.")
        with self._lock:
            existing = self._approved.get(intent.intent_id)
            if existing is not None and existing != intent:
                raise _safe("intent_conflict", "Purchase intent conflicts with approved state.", 409)
            self._approved[intent.intent_id] = intent

    def bind_prepared(self, intent_id: str, prepared: Mapping[str, Any]) -> None:
        if not isinstance(prepared, Mapping):
            raise _safe("invoice_invalid", "Prepared invoice is invalid.")
        with self._lock:
            intent = self._approved.get(intent_id)
            if intent is None:
                raise _safe("intent_not_approved", "Purchase intent is not approved.", 409)
            now = self._now()
            if intent.expires_at <= now:
                raise _safe("intent_expired", "Purchase intent has expired.")
            invoice_id = prepared.get("invoiceId")
            if not isinstance(invoice_id, str) or _INVOICE_ID.fullmatch(invoice_id) is None:
                raise _safe("invoice_invalid", "Prepared invoice ID is invalid.")
            if str(prepared.get("productId", "")) != intent.product_slug:
                raise _safe("invoice_invalid", "Prepared invoice product changed.")
            if str(prepared.get("packageValue", "")) != intent.denomination:
                raise _safe("invoice_invalid", "Prepared invoice denomination changed.")
            if str(prepared.get("paymentMethod", "")).lower() != "usdc_base":
                raise _safe("invoice_invalid", "Prepared invoice payment method changed.")
            if str(prepared.get("paymentAsset", "")).upper() != "USDC":
                raise _safe("invoice_invalid", "Prepared invoice asset is not USDC.")
            if str(prepared.get("paymentNetwork", "")).lower() != "base":
                raise _safe("invoice_invalid", "Prepared invoice network is not Base.")
            try:
                address = _payment_address(prepared.get("paymentAddress"))
            except ValueError:
                raise _safe("invoice_invalid", "Prepared invoice payment address is invalid.") from None
            amount_atomic = _atomic(_decimal(prepared.get("paymentAmount"), "Invoice amount"), "Invoice amount")
            if amount_atomic > intent.max_payment_usdc_atomic:
                raise _safe("payment_limit_exceeded", "Invoice exceeds the approved maximum.")
            expires_at = prepared.get("expiresAtEpoch")
            if type(expires_at) is not int or not 0 < expires_at <= (1 << 63) - 1:
                raise _safe("invoice_invalid", "Prepared invoice expiration is invalid.")
            if expires_at <= now:
                raise _safe("invoice_expired", "Prepared invoice has expired.")
            binding = _PreparedBinding(
                intent_id=intent_id,
                invoice_id=invoice_id,
                expires_at=expires_at,
                payment_address=address,
                amount_atomic=str(amount_atomic),
            )
            existing = self._bindings.get(invoice_id)
            if existing is not None and existing != binding:
                raise _safe("payment_conflict", "Prepared invoice conflicts with existing state.", 409)
            self._bindings[invoice_id] = binding

    def transfer_token_exact(
        self,
        token_address: str,
        to_address: str,
        amount_atomic: int | str,
        chain: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        invoice_id = (
            idempotency_key[len("bitrefill-pay:") :]
            if isinstance(idempotency_key, str) and idempotency_key.startswith("bitrefill-pay:")
            else ""
        )
        with self._lock:
            binding = self._bindings.get(invoice_id)
            if binding is None:
                raise _safe("payment_conflict", "Payment is not bound to a prepared invoice.", 409)
            if not isinstance(token_address, str) or token_address.casefold() != BASE_USDC_ADDRESS.casefold():
                raise _safe("payment_conflict", "Payment token is not Base USDC.", 409)
            if chain != "base":
                raise _safe("payment_conflict", "Payment network is not Base.", 409)
            if idempotency_key != f"bitrefill-pay:{binding.invoice_id}":
                raise _safe("payment_conflict", "Payment idempotency key is invalid.", 409)
            if not isinstance(to_address, str) or to_address.casefold() != binding.payment_address.casefold():
                raise _safe("payment_conflict", "Payment address changed after invoice preparation.", 409)
            if isinstance(amount_atomic, bool) or str(amount_atomic) != binding.amount_atomic:
                raise _safe("payment_conflict", "Payment amount changed after invoice preparation.", 409)
            now = self._now()
            intent = self._approved.get(binding.intent_id)
            if intent is None or intent.expires_at <= now:
                raise _safe("intent_expired", "Purchase intent has expired.")
            if binding.expires_at <= now:
                raise _safe("invoice_expired", "Prepared invoice has expired.")
            cached = self._results.get(invoice_id)
            if cached is not None:
                return dict(cached)
            terminal = self._terminal_errors.get(invoice_id)
            if terminal is not None:
                raise SafeError(*terminal)
            try:
                sidecar_result = self._sidecar.pay_invoice(
                    binding.intent_id,
                    binding.invoice_id,
                    binding.payment_address,
                    int(binding.amount_atomic),
                    binding.expires_at,
                    idempotency_key,
                )
            except SafeError as error:
                terminal = (error.code, error.message, error.status)
                self._terminal_errors[invoice_id] = terminal
                raise SafeError(*terminal) from None
            except Exception:
                terminal = ("payment_failed", "Local payment did not complete safely.", 500)
                self._terminal_errors[invoice_id] = terminal
                raise SafeError(*terminal) from None
            payment = sidecar_result.get("payment") if isinstance(sidecar_result, dict) else None
            if (
                not isinstance(payment, dict)
                or sidecar_result.get("ok") is not True
                or payment.get("intentId") != binding.intent_id
                or payment.get("invoiceId") != binding.invoice_id
                or payment.get("state") not in {"TX_BROADCAST", "COMPLETE"}
            ):
                raise _safe("payment_failed", "Local payment did not complete safely.", 500)
            tx_hash = payment.get("txHash")
            if not isinstance(tx_hash, str) or _TX_HASH.fullmatch(tx_hash) is None:
                raise _safe("payment_failed", "Local payment transaction is invalid.", 500)
            result = {
                "txId": tx_hash,
                "network": "base",
                "asset": "USDC",
                "amountAtomic": binding.amount_atomic,
            }
            self._payment_receipts[invoice_id] = deepcopy(sidecar_result)
            self._results[invoice_id] = result
            return dict(result)

    def payment_receipt(self, invoice_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._payment_receipts.get(invoice_id)
            return deepcopy(value) if value is not None else None

    def payment_binding(self, invoice_id: str) -> dict[str, Any] | None:
        """Terms this invoice was bound to, for a runner that did not store them."""
        with self._lock:
            binding = self._bindings.get(invoice_id)
            if binding is None:
                return None
            return {
                "intentId": binding.intent_id,
                "invoiceId": binding.invoice_id,
                "payTo": binding.payment_address,
                "amountAtomic": binding.amount_atomic,
                "expiresAt": binding.expires_at,
            }


def render_exact_summary(
    quote: Mapping[str, Any],
    recipient: Mapping[str, Any],
    intent: PurchaseIntent,
    buyer_email: str = "",
) -> str:
    snapshot = _approved_purchase_snapshot(quote, recipient, buyer_email)
    return _render_approved_summary(snapshot, intent)


def _render_approved_summary(snapshot: _ApprovedPurchase, intent: PurchaseIntent) -> str:
    quote = snapshot.quote_copy()
    quoted = _decimal(quote["priceUsd"], "Quoted total")
    max_usdc = Decimal(intent.max_payment_usdc_atomic) / _USDC_ATOMIC
    recipient_lines = [
        f"Recipient {field}: {value}" for field, value in snapshot.recipient_items
    ] or ["Recipient: none"]
    return "\n".join(
        [
            "Purchase approval",
            f"Product: {quote.get('name', intent.product_slug)} ({intent.product_slug})",
            f"Denomination: {intent.denomination} (package {intent.package_id})",
            f"Quoted total: ${format(quoted, 'f')}",
            f"Maximum payment: {format(max_usdc, '.6f')} USDC",
            "Payment method: USDC on Base Mainnet",
            *recipient_lines,
            f"Buyer email: {snapshot.buyer_email}",
            f"Approval expires at epoch second: {intent.expires_at}",
            "Non-refundable once issued.",
            "Confirm these exact details on your Trezor to continue.",
        ]
    )


class TrezorPocRunner:
    def __init__(
        self,
        *,
        bitrefill: Any,
        sidecar: Any,
        max_usd: str | Decimal,
        summary_sink: Callable[[str], None] = print,
        store: SidecarStore | None = None,
        state_path: Path | None = None,
        _test_store: Any | None = None,
        clock: Callable[[], int | float] = time.time,
        treasury: SidecarTreasuryClient | None = None,
    ):
        maximum = _decimal(max_usd, "Maximum payment")
        _atomic(maximum, "Maximum payment")
        if not callable(summary_sink) or not callable(clock):
            raise ValueError("runner callbacks must be callable")
        self.bitrefill = bitrefill
        self.sidecar = sidecar
        self.max_usd = maximum
        self.summary_sink = summary_sink
        if store is not None and _test_store is not None:
            raise ValueError("runner store overrides conflict")
        expected_state_path = Path(
            os.path.abspath(os.fspath(state_path or _PROOF_STATE_PATH))
        )
        if store is not None and (
            type(store) is not SidecarStore or store.path != expected_state_path
        ):
            raise ValueError("runner store must use the fixed proof state path or explicit isolated state path")
        self._state_path = expected_state_path
        self._store_override = store if store is not None else _test_store
        self._clock = clock
        self.treasury = treasury or SidecarTreasuryClient(sidecar=sidecar, clock=clock)
        self.bitrefill.treasury_client = self.treasury

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool):
            raise _safe("invalid_clock", "The runner clock is invalid.", 503)
        if isinstance(value, int):
            return _timestamp(value)
        if isinstance(value, float) and math.isfinite(value):
            return _timestamp(int(value))
        raise _safe("invalid_clock", "The runner clock is invalid.", 503)

    def _store(self) -> Any:
        if self._store_override is None:
            self._store_override = SidecarStore(self._state_path)
        return self._store_override

    def _require_no_unresolved_payment(self) -> int:
        try:
            return self._store().purchase_generation_if_clear()
        except ValueError:
            raise _safe(
                "payment_recovery_required",
                "An earlier purchase attempt or payment is unresolved; do not retry purchase. "
                "Inspect the existing invoice and local state.",
                409,
            ) from None
        except Exception:
            raise _safe(
                "payment_state_unavailable",
                "Existing payment state could not be checked safely.",
                503,
            ) from None

    def _record_approval(self, intent: PurchaseIntent, approved_at: int) -> None:
        """Make the runner's own store agree that this intent was approved.

        Locally the sidecar shares this database, so the row is already there
        and nothing happens. Remotely the approval was recorded on the user's
        machine instead, and `reserve_purchase_attempt` would refuse an intent
        it cannot see. This records only what `_verify_approval` just checked.

        It is not the double-payment guard. That stays in the sidecar next to
        the device, keyed on the invoice, where the VPS cannot reach it.
        """
        store = self._store()
        try:
            existing = store.get_intent(intent.intent_id)
        except Exception:
            raise _safe(
                "payment_state_unavailable",
                "Existing payment state could not be checked safely.",
                503,
            ) from None
        if existing is not None and existing.state is PaymentState.DEVICE_APPROVED:
            return
        try:
            # Only reached when the store is not the sidecar's own, so this
            # never adds a device prompt to the local flow.
            pairing_id = str(self.sidecar.pair()["pairing"]["pairingId"])
            store.insert_intent(intent, created_at=approved_at)
            store.approve_intent(
                intent.intent_id,
                approved_at=approved_at,
                pairing_id=pairing_id,
            )
        except SafeError:
            raise
        except (KeyError, TypeError, ValueError):
            raise _safe(
                "intent_conflict",
                "Purchase intent conflicts with recorded approval state.",
                409,
            ) from None
        except Exception:
            raise _safe(
                "payment_state_unavailable",
                "Purchase approval could not be recorded safely.",
                503,
            ) from None

    def _reserve_purchase_attempt(
        self,
        intent_id: str,
        created_at: int,
        expected_generation: int,
    ) -> None:
        try:
            self._store().reserve_purchase_attempt(
                intent_id=intent_id,
                created_at=created_at,
                expected_generation=expected_generation,
            )
        except ValueError:
            raise _safe(
                "payment_recovery_required",
                "Another purchase attempt or payment is unresolved; do not retry purchase. "
                "Inspect the existing invoice and local state.",
                409,
            ) from None
        except Exception:
            raise _safe(
                "payment_state_unavailable",
                "Purchase attempt could not be reserved safely.",
                503,
            ) from None

    def _bind_purchase_attempt(self, intent_id: str, prepared: Any) -> None:
        invoice_id = prepared.get("invoiceId") if isinstance(prepared, Mapping) else None
        try:
            self._store().bind_purchase_attempt_invoice(
                intent_id=intent_id,
                invoice_id=invoice_id,
                updated_at=self._now(),
            )
        except Exception:
            raise _safe(
                "payment_recovery_required",
                "The reserved purchase could not be bound safely; do not retry purchase. "
                "Inspect the existing invoice and local state.",
                409,
            ) from None

    def quote(
        self,
        *,
        product_id: str,
        package_id: str,
        country: str,
        recipient: dict[str, Any],
    ) -> dict[str, Any]:
        result = self.bitrefill.quote_product(
            product_id=product_id,
            package_id=package_id,
            country=country,
            recipient=dict(recipient),
        )
        if not isinstance(result, dict):
            raise _safe("quote_failed", "The product quote is invalid.")
        return result

    def build_intent(
        self,
        quote: dict[str, Any],
        recipient: dict[str, Any],
        now: int,
        *,
        buyer_email: str = "",
    ) -> PurchaseIntent:
        snapshot = _approved_purchase_snapshot(quote, recipient, buyer_email)
        return self._intent_from_snapshot(snapshot, now)

    def _intent_from_snapshot(self, snapshot: _ApprovedPurchase, now: int) -> PurchaseIntent:
        now = _timestamp(now)
        quote = snapshot.quote_copy()
        quoted = _decimal(quote["priceUsd"], "Quoted total")
        if quoted > self.max_usd:
            raise _safe("intent_limit_exceeded", "Quoted total exceeds the proof maximum.")
        try:
            committed_recipient_hash = recipient_hash(snapshot.committed_recipient_copy())
        except (TypeError, ValueError):
            raise _safe("invalid_request", "Recipient fields are invalid.") from None
        core = {
            "productSlug": quote["productId"],
            "packageId": quote["packageId"],
            "denomination": quote["packageValue"],
            "quotedTotalUsdMicros": _atomic(quoted, "Quoted total"),
            "maxPaymentUsdcAtomic": _atomic(self.max_usd, "Maximum payment"),
            "recipientHash": committed_recipient_hash,
            "expiresAt": now + _INTENT_TTL_SECONDS,
        }
        intent_id = "0x" + hashlib.sha256(
            json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            return PurchaseIntent(
                intent_id=intent_id,
                product_slug=core["productSlug"],
                package_id=core["packageId"],
                denomination=core["denomination"],
                quoted_total_usd_micros=core["quotedTotalUsdMicros"],
                max_payment_usdc_atomic=core["maxPaymentUsdcAtomic"],
                recipient_hash=committed_recipient_hash,
                expires_at=core["expiresAt"],
            )
        except (TypeError, ValueError):
            raise _safe("invalid_intent", "Purchase intent is invalid.") from None

    @staticmethod
    def _verify_approval(result: Any, intent: PurchaseIntent) -> None:
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or result.get("intentId") != intent.intent_id
            or result.get("state") != "DEVICE_APPROVED"
        ):
            raise _safe("device_rejected", "Trezor operation was cancelled.")

    def _record_payment(
        self,
        store: Any,
        payment_id: str,
        invoice_id: str,
        intent: PurchaseIntent,
        tx_hash: str,
    ) -> None:
        """Mirror a payment the sidecar executed into the runner's own store.

        Locally the sidecar shares this database and the row is already here.
        Remotely the payment was recorded on the user's machine, so
        `finalize_purchase` found nothing to finalize and every completed
        purchase ended as "finalization is unresolved" — the money had moved
        and the card had shipped, but the receipt with the redemption code was
        never returned.

        Only mirrors what the sidecar already reported and this runner already
        verified against the broadcast transaction.
        """
        try:
            if store.get_payment(payment_id) is not None:
                return
        except Exception:
            raise _safe(
                "payment_state_unavailable",
                "Existing payment state could not be checked safely.",
                503,
            ) from None
        binding = self.treasury.payment_binding(invoice_id)
        if not isinstance(binding, dict):
            raise _safe("payment_failed", "Local payment receipt is invalid.", 500)
        try:
            store.create_payment(
                payment_id=payment_id,
                intent_id=intent.intent_id,
                invoice_id=invoice_id,
                idempotency_key=f"bitrefill-pay:{invoice_id}",
                pay_to=binding["payTo"],
                amount_atomic=binding["amountAtomic"],
                expires_at=binding["expiresAt"],
            )
            store.transition_payment(
                payment_id=payment_id,
                expected=PaymentState.INVOICE_CREATED,
                target=PaymentState.TX_SIGNED,
                updated_at=self._now(),
            )
            store.transition_payment(
                payment_id=payment_id,
                expected=PaymentState.TX_SIGNED,
                target=PaymentState.TX_BROADCAST,
                updated_at=self._now(),
                tx_hash=tx_hash,
            )
        except SafeError:
            raise
        except Exception:
            raise _safe(
                "payment_state_unavailable",
                "Executed payment could not be recorded safely.",
                503,
            ) from None

    def _finalize(
        self,
        *,
        invoice_id: str,
        intent: PurchaseIntent,
        payment_receipt: dict[str, Any],
        amount_atomic: str,
        completed_at: int,
    ) -> None:
        payment = payment_receipt.get("payment")
        if not isinstance(payment, dict):
            raise _safe("payment_failed", "Local payment receipt is invalid.", 500)
        payment_id = payment.get("paymentId")
        tx_hash = payment.get("txHash")
        if not isinstance(payment_id, str) or not isinstance(tx_hash, str) or _TX_HASH.fullmatch(tx_hash) is None:
            raise _safe("payment_failed", "Local payment receipt is invalid.", 500)
        store = self._store()
        self._record_payment(store, payment_id, invoice_id, intent, tx_hash)
        try:
            store.finalize_purchase(
                payment_id=payment_id,
                intent_id=intent.intent_id,
                invoice_id=invoice_id,
                tx_hash=tx_hash,
                product_slug=intent.product_slug,
                amount=amount_atomic,
                payment_method="usdc_base",
                timestamp=completed_at,
            )
        except Exception:
            raise _safe(
                "payment_recovery_required",
                "Purchase transaction finalization is unresolved; do not retry purchase. "
                "Inspect the existing invoice and local state.",
                409,
            ) from None

    def buy(
        self,
        *,
        quote: dict[str, Any],
        recipient: dict[str, Any],
        buyer_email: str = "",
        now: int | None = None,
    ) -> dict[str, Any]:
        expected_generation = self._require_no_unresolved_payment()
        snapshot = _approved_purchase_snapshot(quote, recipient, buyer_email)
        started_at = self._now() if now is None else _timestamp(now)
        intent = self._intent_from_snapshot(snapshot, started_at)
        self.summary_sink(_render_approved_summary(snapshot, intent))
        approved = self.sidecar.approve_intent(intent)
        self._verify_approval(approved, intent)
        if self._intent_from_snapshot(snapshot, started_at) != intent:
            raise _safe("intent_conflict", "Purchase intent changed after approval.", 409)
        self.treasury.register_approved_intent(intent)
        self._record_approval(intent, started_at)
        self._reserve_purchase_attempt(
            intent.intent_id,
            started_at,
            expected_generation,
        )
        try:
            prepared = self.bitrefill.prepare_purchase(
                quote=snapshot.quote_copy(),
                recipient=snapshot.recipient_copy(),
                buyer_email=snapshot.buyer_email,
            )
        except SafeError:
            raise
        except Exception:
            raise _safe("purchase_failed", "Purchase could not be completed safely.") from None
        self._bind_purchase_attempt(intent.intent_id, prepared)
        self.treasury.bind_prepared(intent.intent_id, prepared)
        try:
            result = self.bitrefill.complete_purchase(
                quote=snapshot.quote_copy(),
                prepared=prepared,
                checkpoint_callback=lambda _checkpoint: None,
                invoice_access_token=str(prepared.get("invoiceAccessToken") or ""),
            )
        except SafeError:
            raise
        except Exception:
            raise _safe("purchase_failed", "Purchase could not be completed safely.") from None
        invoice_id = str(prepared.get("invoiceId") or "")
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or result.get("invoiceId") != invoice_id
            or str(result.get("status", "")).lower()
            not in {"complete", "completed", "delivered", "all_delivered"}
        ):
            raise _safe("purchase_failed", "Purchase did not complete safely.", 500)
        local = self.treasury.payment_receipt(invoice_id)
        treasury_payment = result.get("treasuryPayment")
        if local is None or not isinstance(treasury_payment, dict):
            raise _safe("purchase_failed", "Purchase payment receipt is missing.", 500)
        local_tx = local["payment"].get("txHash")
        if treasury_payment.get("txId") != local_tx:
            raise _safe("purchase_failed", "Purchase transaction does not match local state.", 500)
        amount_atomic = str(_atomic(_decimal(prepared.get("paymentAmount"), "Invoice amount"), "Invoice amount"))
        self._finalize(
            invoice_id=invoice_id,
            intent=intent,
            payment_receipt=local,
            amount_atomic=amount_atomic,
            completed_at=max(started_at, int(local["payment"]["updatedAt"])),
        )
        returned = dict(result)
        returned["status"] = "complete"
        return returned


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sign402-trezor-poc")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pair", help="Pair the local Trezor account")
    commands.add_parser("intent-test", help="Approve a fixed local test receipt")
    buy = commands.add_parser("buy", help="Buy one selected product")
    buy.add_argument("--product-id", required=True)
    buy.add_argument("--package-id", required=True)
    buy.add_argument("--country", required=True)
    return parser


def build_local_test_intent(now: int, max_usd: Decimal) -> PurchaseIntent:
    recipient_hash = "0x" + hashlib.sha256(b"local-intent-test").hexdigest()
    core = {
        "product": "local-intent-test",
        "package": "test-only",
        "amount": 1,
        "maximum": _atomic(max_usd, "Maximum payment"),
        "recipient": recipient_hash,
        "expires": now + _INTENT_TTL_SECONDS,
    }
    intent_id = "0x" + hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return PurchaseIntent(
        intent_id=intent_id,
        product_slug="local-intent-test",
        package_id="test-only",
        denomination="No purchase",
        quoted_total_usd_micros=1,
        max_payment_usdc_atomic=core["maximum"],
        recipient_hash=recipient_hash,
        expires_at=core["expires"],
    )


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    arguments = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        environment = dict(os.environ if env is None else env)
        if arguments.command != "buy":
            environment.setdefault("BITREFILL_API_KEY", "unused-local-command")
        settings = RunnerSettings.from_env(environment)
        if not settings.enabled:
            raise _safe("disabled", "Trezor proof mode is disabled.", 503)
        sidecar = SidecarClient(token=settings.sidecar_token)
        if arguments.command == "pair":
            result = sidecar.pair()
            print(f"Trezor paired for Base account {result['pairing']['address']}.")
            return 0
        now = int(time.time())
        if arguments.command == "intent-test":
            intent = build_local_test_intent(now, settings.max_usd)
            print("Review the fixed local intent test on your Trezor. No purchase or payment will occur.")
            result = sidecar.approve_intent(intent)
            TrezorPocRunner._verify_approval(result, intent)
            print("Local intent test approved.")
            return 0
        treasury = SidecarTreasuryClient(sidecar=sidecar, clock=time.time)
        bitrefill = PreparedAddressBitrefillClient(
            api_key=settings.bitrefill_api_key,
            max_purchase_usd=str(settings.max_usd),
            payment_method="usdc_base",
            treasury_client=treasury,
        )
        details = bitrefill.get_product_details(
            product_id=arguments.product_id,
            country=arguments.country,
        )
        recipient = {
            field: getpass.getpass(f"{field}: ")
            for field in details.get("requiredRecipientFields", [])
        }
        runner = TrezorPocRunner(
            bitrefill=bitrefill,
            sidecar=sidecar,
            max_usd=settings.max_usd,
            summary_sink=print,
            clock=time.time,
            treasury=treasury,
        )
        quote = runner.quote(
            product_id=arguments.product_id,
            package_id=arguments.package_id,
            country=arguments.country,
            recipient=recipient,
        )
        result = runner.buy(
            quote=quote,
            recipient=recipient,
            buyer_email=str(recipient.get("email") or ""),
            now=now,
        )
        print("Order complete.")
        print(f"Invoice: {result['invoiceId']}")
        print("Payment: USDC on Base Mainnet")
        redemption = result.get("redemption")
        if isinstance(redemption, dict):
            print(
                "Redemption: "
                + json.dumps(redemption.get("value"), ensure_ascii=True, separators=(",", ":"))
            )
            print("Non-refundable once issued. Keep redemption details safe.")
        return 0
    except SafeError as error:
        print(f"Error: {error.message}", file=sys.stderr)
        return 1
    except Exception:
        print("Error: The proof command failed safely.", file=sys.stderr)
        return 1
