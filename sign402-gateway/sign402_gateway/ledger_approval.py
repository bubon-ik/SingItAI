"""A payment memory escalated waits for a signature from the owner's Ledger.

When `SpendingPolicy` says `ESCALATE`, the gateway asks a person. Today that
question goes to iMessage or WhatsApp and the answer is a tap in a chat, which
proves that somebody pressed a button in a chat. It does not prove that the
owner saw *this* amount going to *this* address, and a phone that has been taken
over can press the button.

An EIP-712 signature from a Ledger proves both. The device renders the fields on
its own screen, which the host cannot repaint, and the signature is over exactly
those fields.

## What is signed, and why `journalId` is in it

    SpendingApproval {
        merchant   string
        payTo      address
        amountUsd  string
        owner      string
        rule       string
        journalId  string
        expiresAt  uint256
    }

`journalId` is the field that makes this safe to retry. It names the exact
journal entry that produced this escalation, so an approval is bound to one
decision about one payment. Without it a signature would authorise "pay this
merchant $25", and the same signature would authorise the next $25 to the same
merchant, forever. With it, an approval is spent when the decision it names is
spent — the same work-claim discipline the rest of the system already uses,
extended to a human's answer.

`amountUsd` is a string for the reason it is a string everywhere else here: it
is compared against a limit, and a float loses cents at the edges. It also has
to render on a small screen exactly as the person was told, and "25.00" does
that while a scaled integer does not.

## What this module does not do

It does not fetch signatures, hold them, or talk to a device. It builds the
payload and checks a signature against a decision. The signature arrives with
the request that needs it, so there is no waiting room to keep, nothing to
expire on a timer, and no new place for an approval to sit around being
replayable.

Off unless `SIGN402_LEDGER_APPROVAL_ENABLED` is set, and with it unset nothing
in this file runs — the escalation path is exactly the one that shipped.
"""

from __future__ import annotations

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

DEFAULT_CHAIN_ID = 8453
"""Base. The payments this approves settle there, so the domain says so."""

DOMAIN_NAME = "Sign402 Spending Approval"
DOMAIN_VERSION = "1"

MAX_LIFETIME_SECONDS = 3600
"""How far ahead `expiresAt` may be, whatever the signature says.

A signature that never expires is a bearer token for the payment it names. An
hour is long enough for a person to find their device and short enough that a
captured approval stops being useful before anyone could use it twice.
"""

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "SpendingApproval": [
        {"name": "merchant", "type": "string"},
        {"name": "payTo", "type": "address"},
        {"name": "amountUsd", "type": "string"},
        {"name": "owner", "type": "string"},
        {"name": "rule", "type": "string"},
        {"name": "journalId", "type": "string"},
        {"name": "expiresAt", "type": "uint256"},
    ],
}


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
    """The message a Ledger is asked to sign, and the device shows verbatim."""

    merchant: str
    pay_to: str
    amount_usd: str
    owner: str
    rule: str
    journal_id: str
    expires_at: int

    def message(self) -> dict[str, Any]:
        return {
            "merchant": self.merchant,
            "payTo": self.pay_to,
            "amountUsd": self.amount_usd,
            "owner": self.owner,
            "rule": self.rule,
            "journalId": self.journal_id,
            "expiresAt": self.expires_at,
        }

    def typed_data(self, *, chain: int) -> dict[str, Any]:
        return {
            "types": TYPES,
            "primaryType": "SpendingApproval",
            "domain": {
                "name": DOMAIN_NAME,
                "version": DOMAIN_VERSION,
                "chainId": chain,
            },
            "message": self.message(),
        }


def approval_for(payment: Any, decision: Any, *, lifetime_seconds: int = 900) -> SpendingApproval:
    """What the owner is being asked to approve, built from the decision itself.

    Built here rather than taken from the request, so the fields on the device's
    screen are the gateway's account of the payment and not the caller's.
    """
    return SpendingApproval(
        merchant=str(payment.merchant),
        pay_to=str(payment.pay_to_normalised),
        amount_usd=str(payment.amount_usd),
        owner=str(payment.owner),
        rule=str(getattr(decision, "rule", "") or ""),
        journal_id=str(getattr(decision, "journal_id", "") or ""),
        expires_at=int(time.time()) + int(lifetime_seconds),
    )


def _recover(approval: SpendingApproval, signature: str, *, chain: int) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    encoded = encode_typed_data(full_message=approval.typed_data(chain=chain))
    return Account.recover_message(encoded, signature=signature).lower()


def verify_approval(
    submitted: Any,
    *,
    payment: Any,
    decision: Any,
    claim_id: str | None,
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
    if claim_id is None:
        # The claim is what stops the approved payment being sent twice. An
        # approval with nothing held is an approval for a payment that is
        # already gone or already in flight.
        raise LedgerApprovalError(
            "This payment is no longer being held, so an approval cannot be "
            "applied to it. Start it again."
        )
    if not isinstance(submitted, Mapping):
        raise LedgerApprovalError("This payment needs an approval from your Ledger.")

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
