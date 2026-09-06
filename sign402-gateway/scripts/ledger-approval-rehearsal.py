#!/usr/bin/env python
"""Sign one SpendingApproval on a real Ledger, then prove it cannot be reused.

Run from `sign402-gateway/` with the device connected and the Ethereum app open:

    .venv/bin/python scripts/ledger-approval-rehearsal.py

Nothing is spent. No wallet is touched. This builds the payload the gateway
would build for an escalated payment, asks the device to sign it, verifies the
signature the way the gateway verifies it, and then replays the same signature
against a second payment to show it is refused.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spending_memory import Action, Decision, Payment

from sign402_gateway.ledger_approval import (
    APPROVERS_ENV,
    DEFAULT_CHAIN_ID,
    ENABLED_ENV,
    LedgerApprovalError,
    SpendingApproval,
    verify_approval,
)

TOOL = Path(__file__).resolve().parents[2] / "tools" / "ledger-approve" / "approve.cjs"

payment = Payment(
    merchant="giftcards.example.com",
    pay_to="0x8f3a1c2b4d5e6f708192a3b4c5d6e7f809a1b2c3",
    amount_usd=Decimal("25.00"),
    owner="agent-7",
)
decision = Decision(
    action=Action.ESCALATE,
    reason="I have never paid giftcards.example.com before.",
    rule="unknown_merchant",
    journal_id="01JB8Z4A1B2C3D4E5F6G7H8J9K",
)

expires_at = int(time.time()) + 600
approval = SpendingApproval(
    merchant=payment.merchant,
    pay_to=payment.pay_to_normalised,
    amount_usd=str(payment.amount_usd),
    owner=payment.owner,
    rule=decision.rule,
    journal_id=decision.journal_id,
    expires_at=expires_at,
)
typed = approval.typed_data(chain=DEFAULT_CHAIN_ID)

print("== 1. what the device is asked to show ==", flush=True)
for field, value in approval.message().items():
    print(f"   {field:<10} {value}")

print("\n== 2. signing on the Ledger ==", flush=True)
# stderr is inherited, not captured: it carries "confirm on the device", and a
# prompt shown only after the process exits is not a prompt. Only stdout is
# read, because only stdout carries the signature.
result = subprocess.run(
    ["node", str(TOOL)], input=json.dumps(typed).encode(), stdout=subprocess.PIPE
)
signature = result.stdout.decode().strip()
if result.returncode != 0 or not signature:
    sys.exit("   no signature came back")
print(f"   signature: {signature[:20]}…{signature[-8:]}")

submitted = {
    "signature": signature,
    "expiresAt": expires_at,
    "journalId": decision.journal_id,
}

print("\n== 3. the gateway verifies it ==")
env = {ENABLED_ENV: "1", APPROVERS_ENV: sys.argv[1] if len(sys.argv) > 1 else ""}
if not env[APPROVERS_ENV]:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    signer = Account.recover_message(
        encode_typed_data(full_message=typed), signature=signature
    )
    print(f"   no approver given, so trusting this run's device: {signer}")
    env[APPROVERS_ENV] = signer

accepted = verify_approval(
    submitted, payment=payment, decision=decision, claim_id="claim-1", env=env
)
print(f"   accepted, signed by {accepted}")

print("\n== 4. the same signature on the next payment ==")
next_decision = Decision(
    action=Action.ESCALATE,
    reason="Same merchant, same amount, a new escalation.",
    rule="unknown_merchant",
    journal_id="01JB8Z9ZZZZZZZZZZZZZZZZZZZ",
)
try:
    verify_approval(
        submitted, payment=payment, decision=next_decision, claim_id="claim-2", env=env
    )
    print("   *** FAILED: the approval was reusable ***")
    sys.exit(1)
except LedgerApprovalError as exc:
    print(f"   refused, as designed: {exc}")

print("\nAll four steps behaved. Nothing was spent.")
