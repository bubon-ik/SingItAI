"""Local-only Hermes purchase coordinator backed by the Trezor proof runner."""

from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from .errors import SafeError
from .poc_runner import (
    PreparedAddressBitrefillClient,
    SidecarTreasuryClient,
    TrezorPocRunner,
    build_local_test_intent,
    render_exact_summary,
)
from .sidecar_client import SidecarClient


# Real Bitrefill catalog ids are not slugs: they carry a "<&>" separator and
# often spaces, as in "alza-czech-republic<&>100" or "…<&>1GB, 7 Days".
# Printable ASCII is therefore the constraint, not an alphanumeric subset.
# Control characters stay rejected so an id cannot smuggle newlines into a
# summary line or a log record.
_SELECTOR = re.compile(r"[ -~]{1,128}\Z")
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_CONFIRMATION = re.compile(r"[A-F0-9]{8}\Z")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
_TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_INVOICE_ID = re.compile(r"[A-Za-z0-9._:-]{1,110}\Z")


def _safe(code: str, message: str, status: int = 400) -> SafeError:
    return SafeError(code, message, status)


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "") or "").strip()
    if not value:
        raise ValueError(f"{name} is required when local Trezor agent mode is enabled")
    return value


def _maximum(env: Mapping[str, str]) -> Decimal:
    value = _required(env, "SIGN402_TREZOR_POC_MAX_USD")
    try:
        maximum = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("SIGN402_TREZOR_POC_MAX_USD must be a positive decimal") from error
    if not maximum.is_finite() or maximum <= 0:
        raise ValueError("SIGN402_TREZOR_POC_MAX_USD must be a positive decimal")
    return maximum


@dataclass(frozen=True, repr=False)
class LocalAgentSettings:
    enabled: bool
    purchases_enabled: bool = False
    allowed_user_id: str = ""
    sidecar_token: str = ""
    max_usd: Decimal = Decimal("0")
    bitrefill_api_key: str = ""
    buyer_email: str = ""

    def __repr__(self) -> str:
        return (
            "LocalAgentSettings("
            f"enabled={self.enabled!r}, purchases_enabled={self.purchases_enabled!r}, "
            "allowed_user_id='<redacted>', "
            f"max_usd={self.max_usd!r}, credentials='<redacted>')"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LocalAgentSettings:
        if env.get("SIGN402_TREZOR_LOCAL_AGENT_ENABLED") != "1":
            return cls(False)
        if env.get("SIGN402_TREZOR_POC_ENABLED") != "1":
            raise ValueError(
                "SIGN402_TREZOR_POC_ENABLED=1 is required when local Trezor agent mode is enabled"
            )
        user_id = _required(env, "SIGN402_TREZOR_LOCAL_AGENT_USER_ID")
        if not user_id.isascii() or not user_id.isdecimal() or len(user_id) > 32:
            raise ValueError("SIGN402_TREZOR_LOCAL_AGENT_USER_ID must be one numeric user ID")
        buyer_email = str(env.get("SIGN402_TREZOR_LOCAL_BUYER_EMAIL", "") or "").strip()
        if buyer_email and (len(buyer_email) > 254 or _EMAIL.fullmatch(buyer_email) is None):
            raise ValueError("SIGN402_TREZOR_LOCAL_BUYER_EMAIL must be a valid email address")
        return cls(
            enabled=True,
            purchases_enabled=env.get("SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED") == "1",
            allowed_user_id=user_id,
            sidecar_token=_required(env, "SIGN402_TREZOR_SIDECAR_TOKEN"),
            max_usd=_maximum(env),
            bitrefill_api_key=(
                _required(env, "BITREFILL_API_KEY")
                if env.get("SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED") == "1"
                else ""
            ),
            buyer_email=buyer_email,
        )


@dataclass(frozen=True)
class _PendingPurchase:
    user_id: str
    confirmation_code: str
    quote: dict[str, Any]
    recipient: dict[str, str]
    buyer_email: str
    started_at: int
    expires_at: int


class LocalAgentController:
    """One-user, two-phase adapter for an isolated local Hermes instance."""

    def __init__(
        self,
        *,
        settings: LocalAgentSettings,
        runner: TrezorPocRunner | Any,
        details_client: Any,
        clock: Callable[[], int | float] = time.time,
        code_factory: Callable[[], str] = lambda: secrets.token_hex(4).upper(),
    ):
        if not isinstance(settings, LocalAgentSettings):
            raise ValueError("settings must be LocalAgentSettings")
        if not callable(clock) or not callable(code_factory):
            raise ValueError("local agent callbacks must be callable")
        self.settings = settings
        self.runner = runner
        self.details_client = details_client
        self._clock = clock
        self._code_factory = code_factory
        self._lock = threading.RLock()
        self._pending: _PendingPurchase | None = None
        self._preparing = False
        self._purchase_active = False

    def __repr__(self) -> str:
        return "LocalAgentController(mode='local-only', credentials='<redacted>')"

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool):
            raise _safe("invalid_clock", "The local agent clock is invalid.", 503)
        if isinstance(value, int):
            result = value
        elif isinstance(value, float) and math.isfinite(value):
            result = int(value)
        else:
            raise _safe("invalid_clock", "The local agent clock is invalid.", 503)
        if not 0 < result <= (1 << 63) - 1:
            raise _safe("invalid_clock", "The local agent clock is invalid.", 503)
        return result

    def _authorize(self, user_id: str) -> str:
        if not self.settings.enabled:
            raise _safe("disabled", "Local Trezor agent mode is disabled.", 503)
        candidate = str(user_id or "").strip()
        if candidate != self.settings.allowed_user_id:
            raise _safe("unauthorized", "This user is not authorized for local Trezor mode.", 403)
        return candidate

    @staticmethod
    def _selector(value: str, name: str) -> str:
        candidate = str(value or "").strip()
        if _SELECTOR.fullmatch(candidate) is None:
            raise _safe("invalid_request", f"{name} is invalid.")
        return candidate

    def pair(self, user_id: str) -> str:
        self._authorize(user_id)
        result = self.runner.sidecar.pair()
        try:
            address = result["pairing"]["address"]
        except (KeyError, TypeError):
            raise _safe("sidecar_invalid_response", "Trezor pairing returned an invalid response.", 502) from None
        if not isinstance(address, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is None:
            raise _safe("sidecar_invalid_response", "Trezor pairing returned an invalid response.", 502)
        return f"Trezor paired for local Base account {address}."

    def intent_test(self, user_id: str) -> str:
        self._authorize(user_id)
        intent = build_local_test_intent(self._now(), self.settings.max_usd)
        result = self.runner.sidecar.approve_intent(intent)
        TrezorPocRunner._verify_approval(result, intent)
        return "Trezor test approved. No Bitrefill order or payment was created."

    def prepare(self, user_id: str, product_id: str, package_id: str, country: str) -> str:
        owner = self._authorize(user_id)
        if not self.settings.purchases_enabled:
            raise _safe(
                "purchases_disabled",
                "Local Trezor purchases are disabled.",
                503,
            )
        product = self._selector(product_id, "Product ID")
        package = self._selector(package_id, "Package ID")
        normalized_country = str(country or "").strip().upper()
        if _COUNTRY.fullmatch(normalized_country) is None:
            raise _safe("invalid_request", "Country is invalid.")

        with self._lock:
            now = self._now()
            if self._pending is not None and self._pending.expires_at <= now:
                self._pending = None
            if self._preparing or self._purchase_active or self._pending is not None:
                raise _safe(
                    "purchase_in_progress",
                    "A local Trezor quote is already pending; confirm or cancel it first.",
                    409,
                )
            self._preparing = True

        try:
            details = self.details_client.get_product_details(
                product_id=product,
                country=normalized_country,
            )
            fields = details.get("requiredRecipientFields") if isinstance(details, dict) else None
            if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields):
                raise _safe("quote_failed", "Bitrefill product details are invalid.", 502)
            if set(fields) - {"email"} or len(fields) != len(set(fields)):
                raise _safe(
                    "unsupported_recipient",
                    "This product requires recipient fields unsupported by the local Trezor proof.",
                    409,
                )
            if "email" in fields and not self.settings.buyer_email:
                raise _safe(
                    "recipient_required",
                    "This product requires a private local buyer email configuration.",
                    409,
                )
            recipient = {"email": self.settings.buyer_email} if "email" in fields else {}
            quote = self.runner.quote(
                product_id=product,
                package_id=package,
                country=normalized_country,
                recipient=recipient,
            )
            started_at = self._now()
            intent = self.runner.build_intent(
                quote,
                recipient,
                started_at,
                buyer_email=self.settings.buyer_email,
            )
            code = str(self._code_factory() or "").strip().upper()
            if _CONFIRMATION.fullmatch(code) is None:
                raise _safe("invalid_configuration", "Local confirmation code generation failed.", 503)
            summary = render_exact_summary(
                quote,
                recipient,
                intent,
                self.settings.buyer_email,
            )
            pending = _PendingPurchase(
                user_id=owner,
                confirmation_code=code,
                quote=deepcopy(quote),
                recipient=deepcopy(recipient),
                buyer_email=self.settings.buyer_email,
                started_at=started_at,
                expires_at=intent.expires_at,
            )
            with self._lock:
                self._pending = pending
            return summary + f"\n\nTo continue from this local agent: /trezor_confirm {code}"
        finally:
            with self._lock:
                self._preparing = False

    def confirm(self, user_id: str, confirmation_code: str) -> str:
        owner = self._authorize(user_id)
        code = str(confirmation_code or "").strip().upper()
        with self._lock:
            pending = self._pending
            if pending is None or pending.user_id != owner:
                raise _safe("purchase_not_pending", "No local Trezor purchase is pending.", 409)
            if pending.expires_at <= self._now():
                self._pending = None
                raise _safe("intent_expired", "The local Trezor quote has expired.", 409)
            if code != pending.confirmation_code:
                raise _safe("invalid_confirmation", "The local confirmation code is invalid.", 403)
            self._pending = None
            self._purchase_active = True

        try:
            result = self.runner.buy(
                quote=deepcopy(pending.quote),
                recipient=deepcopy(pending.recipient),
                buyer_email=pending.buyer_email,
                now=pending.started_at,
            )
            return self._receipt(result)
        finally:
            with self._lock:
                self._purchase_active = False

    def cancel(self, user_id: str) -> str:
        owner = self._authorize(user_id)
        with self._lock:
            if self._pending is None or self._pending.user_id != owner:
                raise _safe("purchase_not_pending", "No local Trezor purchase is pending.", 409)
            self._pending = None
        return "Local Trezor quote cancelled. Nothing was purchased."

    @staticmethod
    def _receipt(result: Any) -> str:
        try:
            invoice_id = result["invoiceId"]
            status = str(result["status"]).lower()
            tx_hash = result["treasuryPayment"]["txId"]
        except (KeyError, TypeError):
            raise _safe("purchase_failed", "The completed purchase receipt is invalid.", 500) from None
        if (
            not isinstance(invoice_id, str)
            or _INVOICE_ID.fullmatch(invoice_id) is None
            or status != "complete"
            or not isinstance(tx_hash, str)
            or _TX_HASH.fullmatch(tx_hash) is None
        ):
            raise _safe("purchase_failed", "The completed purchase receipt is invalid.", 500)
        lines = [
            "Local Trezor purchase complete.",
            f"Invoice: {invoice_id}",
            "Payment: USDC on Base Mainnet",
            f"Transaction: {tx_hash}",
        ]
        redemption = result.get("redemption")
        if isinstance(redemption, dict) and "value" in redemption:
            try:
                rendered = json.dumps(
                    redemption["value"],
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                raise _safe("purchase_failed", "The completed purchase receipt is invalid.", 500) from None
            if len(rendered.encode("utf-8")) > 16_384:
                raise _safe("purchase_failed", "The completed purchase receipt is invalid.", 500)
            lines.extend([f"Redemption: {rendered}", "Keep redemption details private."])
        return "\n".join(lines)


def build_local_agent_controller(
    env: Mapping[str, str],
    *,
    clock: Callable[[], int | float] = time.time,
) -> LocalAgentController:
    settings = LocalAgentSettings.from_env(env)
    if not settings.enabled:
        return LocalAgentController(
            settings=settings,
            runner=object(),
            details_client=object(),
            clock=clock,
        )
    sidecar = SidecarClient(token=settings.sidecar_token, clock=clock)
    if not settings.purchases_enabled:
        class PairOnlyRunner:
            def __init__(self, local_sidecar: SidecarClient):
                self.sidecar = local_sidecar

        return LocalAgentController(
            settings=settings,
            runner=PairOnlyRunner(sidecar),
            details_client=object(),
            clock=clock,
        )
    treasury = SidecarTreasuryClient(sidecar=sidecar, clock=clock)
    bitrefill = PreparedAddressBitrefillClient(
        api_key=settings.bitrefill_api_key,
        max_purchase_usd=str(settings.max_usd),
        payment_method="usdc_base",
        treasury_client=treasury,
    )
    runner = TrezorPocRunner(
        bitrefill=bitrefill,
        sidecar=sidecar,
        max_usd=settings.max_usd,
        summary_sink=lambda _summary: None,
        clock=clock,
        treasury=treasury,
    )
    return LocalAgentController(
        settings=settings,
        runner=runner,
        details_client=bitrefill,
        clock=clock,
    )
