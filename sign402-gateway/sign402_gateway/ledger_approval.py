"""Readable approval of one escalated Base-USDC tool purchase.

The Ledger signs compact EIP-191 text: purchase name, amount/network, full
recipient and a SHA-256 reference. The reference commits the domain, purchase,
resource, merchant, amount, recipient, owner, rule, journal ID and expiry. The
verifier reconstructs it from the stored order, never from a caller's display
text. Changing any field changes the message that must be signed.

Signature validity proves control of the configured key; hardware custody and
readable review are separate device checks documented in docs/ledger-v1.md.
ledger_payments.py consumes approvals atomically before calling the payer and
retains completed results for retries. A signature alone is not replay control.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

ENABLED_ENV = "SIGN402_LEDGER_APPROVAL_ENABLED"
APPROVERS_ENV = "SIGN402_LEDGER_APPROVER_ADDRESSES"
CHAIN_ID_ENV = "SIGN402_LEDGER_APPROVAL_CHAIN_ID"
OWNER_ENV = "SIGN402_LEDGER_OWNER_ID"

DEFAULT_CHAIN_ID = 8453
"""Base. The payments this approves settle there, so the domain says so."""

DOMAIN_NAME = "SingIt Spending Approval"
"""Domain separator committed by the signed reference, independent of its UI label."""
DOMAIN_VERSION = "2"
SIGNING_METHOD = "personal_sign"

MAX_LIFETIME_SECONDS = 3600
"""How far ahead `expiresAt` may be, whatever the signature says.

A signature that never expires is a bearer token for the payment it names. An
hour is long enough for a person to find their device and short enough that a
captured approval stops being useful before anyone could use it twice.
"""

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def display_text(message: Mapping[str, Any], *, chain: int) -> str:
    """Canonical text signed by the device and reconstructed by the verifier.

    Escape field values as ASCII JSON strings (without their surrounding
    quotes), so line breaks, control characters and Unicode direction controls
    cannot create misleading labels on the device. Never truncate signed data.
    A full SHA-256 reference commits the domain and every field, including
    resource, owner, rule, journal and expiry. They need not take up individual
    device screens. Verification reconstructs the text from the stored order.
    This lane only pays Base USDC; amountUsd is its existing decimal amount.
    """
    def field(name):
        return json.dumps(str(message[name]), ensure_ascii=True)[1:-1]

    network = "Base" if chain == DEFAULT_CHAIN_ID else f"chain {chain}"
    committed = {"domain": {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": chain},
                 "message": dict(message)}
    canonical = json.dumps(committed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    reference = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).decode("ascii").rstrip("=")
    text = "\n".join([
        f"SingIt purchase v{DOMAIN_VERSION}",
        f"Buy: {field('purchase')}",
        f"Pay: {field('amountUsd')} USDC on {network}",
        f"To: {field('payTo')}",
        f"Ref: {reference}",
    ])
    if len(text) > 2000:
        raise LedgerApprovalError("Purchase details are too long for device review.")
    return text


class LedgerApprovalError(ValueError):
    """The signature does not authorise this payment.

    A `ValueError` so the existing handlers turn it into a 400, with a message
    written to be shown: a refusal a person cannot read is a refusal they
    cannot act on.
    """


def approval_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    raw = str(values.get(ENABLED_ENV, "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def approver_addresses(env: Mapping[str, str] | None = None) -> set[str]:
    values = os.environ if env is None else env
    raw = str(values.get(APPROVERS_ENV, "") or "")
    return {a.strip().lower() for a in raw.replace(";", ",").split(",") if a.strip()}


def chain_id(env: Mapping[str, str] | None = None) -> int:
    values = os.environ if env is None else env
    raw = str(values.get(CHAIN_ID_ENV, "") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_CHAIN_ID
    except ValueError:
        return DEFAULT_CHAIN_ID


@dataclass(frozen=True)
class SpendingApproval:
    """The signed message; readable device display must be verified separately."""

    merchant: str
    pay_to: str
    amount_usd: str
    owner: str
    rule: str
    journal_id: str
    expires_at: int
    purchase: str
    resource: str

    def message(self) -> dict[str, Any]:
        return {
            "purchase": self.purchase,
            "resource": self.resource,
            "merchant": self.merchant,
            "payTo": self.pay_to,
            "amountUsd": self.amount_usd,
            "owner": self.owner,
            "rule": self.rule,
            "journalId": self.journal_id,
            "expiresAt": self.expires_at,
        }

    def payload(self, *, chain: int) -> dict[str, Any]:
        return {
            "signingMethod": SIGNING_METHOD,
            "domain": {
                "name": DOMAIN_NAME,
                "version": DOMAIN_VERSION,
                "chainId": chain,
            },
            "message": self.message(),
            "displayText": display_text(self.message(), chain=chain),
        }


def approval_for(payment: Any, decision: Any, *, purchase: str | None = None, lifetime_seconds: int = 900) -> SpendingApproval:
    """What the owner is being asked to approve, built from the decision itself.

    Built here rather than taken from the submitted approval, so the signed
    fields describe the gateway's payment and decision.
    """
    return SpendingApproval(
        merchant=str(payment.merchant),
        pay_to=str(payment.pay_to_normalised),
        amount_usd=str(payment.amount_usd),
        owner=str(payment.owner),
        rule=str(getattr(decision, "rule", "") or ""),
        journal_id=str(getattr(decision, "journal_id", "") or ""),
        expires_at=int(time.time()) + int(lifetime_seconds),
        purchase=str(purchase if purchase is not None else payment.resource or payment.merchant),
        resource=str(payment.resource or ""),
    )


def _recover(approval: SpendingApproval, signature: str, *, chain: int) -> str:
    from eth_account import Account
    from eth_account.messages import encode_defunct

    encoded = encode_defunct(text=display_text(approval.message(), chain=chain))
    return Account.recover_message(encoded, signature=signature).lower()


def verify_approval(
    submitted: Any,
    *,
    payment: Any,
    decision: Any,
    purchase: str | None = None,
    env: Mapping[str, str] | None = None,
    now: int | None = None,
) -> str:
    """Check one signature against one escalated payment. Returns the signer.

    Every failure raises. There is no partial pass and no "close enough": the
    thing being authorised is someone else's money leaving.
    """
    approvers = approver_addresses(env)
    if not approvers:
        raise LedgerApprovalError(
            f"{ENABLED_ENV} is on but {APPROVERS_ENV} is empty, so no device "
            "could approve anything. Refusing rather than waving the payment "
            "through."
        )
    if not isinstance(submitted, Mapping):
        raise LedgerApprovalError("This payment needs an approval from your Ledger.")
    if submitted.get("signingMethod") != SIGNING_METHOD:
        raise LedgerApprovalError("Use a new readable v2 Ledger approval; the old typed-data format is no longer accepted.")

    signature = str(submitted.get("signature") or "").strip()
    if not signature:
        raise LedgerApprovalError("This payment needs an approval from your Ledger.")

    try:
        expires_at = int(submitted.get("expiresAt"))
    except (TypeError, ValueError):
        raise LedgerApprovalError("The approval does not say when it expires.") from None

    moment = int(time.time()) if now is None else int(now)
    if expires_at <= moment:
        raise LedgerApprovalError(
            "That approval has expired. Approve it again on your Ledger."
        )
    if expires_at - moment > MAX_LIFETIME_SECONDS:
        # A signature valid for a week is a bearer token, whoever signed it.
        raise LedgerApprovalError(
            "That approval is valid for too long to accept. Approve it again "
            "with a shorter expiry."
        )

    journal_id = str(getattr(decision, "journal_id", "") or "")
    submitted_journal = str(submitted.get("journalId") or "")
    if not journal_id or submitted_journal != journal_id:
        # The check that makes an approval unrepeatable. Without it the same
        # signature authorises the next identical payment, and the one after.
        raise LedgerApprovalError(
            "That approval was signed for a different decision. Approve this "
            "one on your Ledger."
        )

    approval = SpendingApproval(
        merchant=str(payment.merchant),
        pay_to=str(payment.pay_to_normalised),
        amount_usd=str(payment.amount_usd),
        owner=str(payment.owner),
        rule=str(getattr(decision, "rule", "") or ""),
        journal_id=journal_id,
        expires_at=expires_at,
        purchase=str(purchase if purchase is not None else payment.resource or payment.merchant),
        resource=str(payment.resource or ""),
    )

    try:
        signer = _recover(approval, signature, chain=chain_id(env))
    except LedgerApprovalError:
        raise
    except Exception:
        # Never echo the signature or the recovered address on failure: a
        # rejected approval is not a place to leak which keys were tried.
        raise LedgerApprovalError(
            "That approval does not match this payment. It has to be signed "
            "for this exact merchant, address and amount."
        ) from None

    if signer not in approvers:
        raise LedgerApprovalError(
            "That approval was signed by a device that is not allowed to "
            "approve payments for this account."
        )

    logger.info(
        "ledger approval accepted: journal=%s rule=%s", journal_id, approval.rule
    )
    return signer
