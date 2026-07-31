from dataclasses import dataclass
from enum import Enum

from eth_utils import to_checksum_address


_BYTES32_HEX_LENGTH = 66
_UINT64_MAX = (1 << 64) - 1
_UINT256_MAX = (1 << 256) - 1


class PaymentState(str, Enum):
    QUOTED = "QUOTED"
    DEVICE_APPROVED = "DEVICE_APPROVED"
    INVOICE_CREATED = "INVOICE_CREATED"
    TX_SIGNED = "TX_SIGNED"
    TX_BROADCAST = "TX_BROADCAST"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _bytes32(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _BYTES32_HEX_LENGTH
        or not value.startswith("0x")
        or any(character not in "0123456789abcdef" for character in value[2:])
    ):
        raise ValueError(f"{name} must be 0x plus 64 lowercase hex characters")
    return value


def _positive_int(value: object, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} exceeds its maximum value")
    return value


def _address(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an EVM address")
    try:
        return to_checksum_address(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an EVM address") from error


def _state(value: object, name: str = "state") -> PaymentState:
    if not isinstance(value, PaymentState):
        raise ValueError(f"{name} must be a PaymentState")
    return value


@dataclass(frozen=True)
class PurchaseIntent:
    intent_id: str
    product_slug: str
    package_id: str
    denomination: str
    quoted_total_usd_micros: int
    max_payment_usdc_atomic: int
    recipient_hash: str
    expires_at: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _bytes32(self.intent_id, "intent_id"))
        object.__setattr__(self, "product_slug", _nonempty_string(self.product_slug, "product_slug"))
        object.__setattr__(self, "package_id", _nonempty_string(self.package_id, "package_id"))
        object.__setattr__(self, "denomination", _nonempty_string(self.denomination, "denomination"))
        object.__setattr__(
            self,
            "quoted_total_usd_micros",
            _positive_int(
                self.quoted_total_usd_micros,
                "quoted_total_usd_micros",
                maximum=_UINT256_MAX,
            ),
        )
        object.__setattr__(
            self,
            "max_payment_usdc_atomic",
            _positive_int(
                self.max_payment_usdc_atomic,
                "max_payment_usdc_atomic",
                maximum=_UINT256_MAX,
            ),
        )
        object.__setattr__(self, "recipient_hash", _bytes32(self.recipient_hash, "recipient_hash"))
        object.__setattr__(
            self,
            "expires_at",
            _positive_int(self.expires_at, "expires_at", maximum=_UINT64_MAX),
        )

    @property
    def payment_asset(self) -> str:
        return "USDC"

    @property
    def payment_network(self) -> str:
        return "Base Mainnet"


@dataclass(frozen=True)
class IntentRecord:
    intent: PurchaseIntent
    state: PaymentState
    created_at: int
    approved_at: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, PurchaseIntent):
            raise ValueError("intent must be a PurchaseIntent")
        object.__setattr__(self, "state", _state(self.state))
        object.__setattr__(self, "created_at", _positive_int(self.created_at, "created_at"))
        if self.approved_at is not None:
            object.__setattr__(
                self,
                "approved_at",
                _positive_int(self.approved_at, "approved_at"),
            )


@dataclass(frozen=True)
class PaymentRequest:
    intent_id: str
    invoice_id: str
    pay_to: str
    amount_atomic: int
    expires_at: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _bytes32(self.intent_id, "intent_id"))
        object.__setattr__(self, "invoice_id", _nonempty_string(self.invoice_id, "invoice_id"))
        object.__setattr__(self, "pay_to", _address(self.pay_to, "pay_to"))
        object.__setattr__(self, "amount_atomic", _positive_int(self.amount_atomic, "amount_atomic"))
        object.__setattr__(
            self,
            "expires_at",
            _positive_int(self.expires_at, "expires_at", maximum=_UINT64_MAX),
        )


@dataclass(frozen=True)
class Pairing:
    pairing_id: str
    address: str
    derivation_path: str
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "pairing_id", _nonempty_string(self.pairing_id, "pairing_id"))
        object.__setattr__(self, "address", _address(self.address, "address"))
        object.__setattr__(
            self,
            "derivation_path",
            _nonempty_string(self.derivation_path, "derivation_path"),
        )
        object.__setattr__(self, "created_at", _positive_int(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _positive_int(self.updated_at, "updated_at"))


@dataclass(frozen=True)
class PaymentView:
    payment_id: str
    intent_id: str
    invoice_id: str
    state: PaymentState
    created_at: int
    updated_at: int
    tx_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payment_id", _nonempty_string(self.payment_id, "payment_id"))
        object.__setattr__(self, "intent_id", _bytes32(self.intent_id, "intent_id"))
        object.__setattr__(self, "invoice_id", _nonempty_string(self.invoice_id, "invoice_id"))
        object.__setattr__(self, "state", _state(self.state))
        object.__setattr__(self, "created_at", _positive_int(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _positive_int(self.updated_at, "updated_at"))
        if self.tx_hash is not None:
            object.__setattr__(self, "tx_hash", _nonempty_string(self.tx_hash, "tx_hash"))
