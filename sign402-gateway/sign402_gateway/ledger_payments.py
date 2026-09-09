"""A durable, single-owner Ledger approval lane for /agent/buy-tool.

No funds are reserved while the device is being found. Limits are checked and
funds reserved immediately before execution. A database transition, committed
before the payer is called, permits at most one submission per request ID.
An interrupted submission is never automatically retried: only its operator
can establish whether a transfer happened. Signatures and wallet keys are not
stored; requests and successful responses are encrypted with the wallet key.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from spending_memory import Action, Decision, Payment

from .ledger_approval import (
    ADDRESS_RE, APPROVERS_ENV, CHAIN_ID_ENV, DEFAULT_CHAIN_ID, OWNER_ENV,
    SpendingApproval, approval_enabled, approval_for, approver_addresses,
    verify_approval,
)
from .secure_state import SensitiveStateCipher, ensure_private_directory, ensure_private_file

REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
TTL_SECONDS = 600


class LedgerOperationError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LedgerConfig:
    owner: str
    approvers: frozenset[str]
    chain: int = DEFAULT_CHAIN_ID

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        values = os.environ if env is None else env
        if not approval_enabled(values):
            return None
        owner = str(values.get(OWNER_ENV, "")).strip()
        addresses = approver_addresses(values)
        if not owner or not addresses or any(not ADDRESS_RE.fullmatch(a) for a in addresses):
            raise ValueError("Ledger approval requires SIGN402_LEDGER_OWNER_ID and valid approver addresses.")
        if str(values.get(CHAIN_ID_ENV, DEFAULT_CHAIN_ID)) != str(DEFAULT_CHAIN_ID):
            raise ValueError("Ledger v1 supports Base mainnet (chain 8453) only.")
        return cls(owner, frozenset(addresses))

    def env(self):
        return {APPROVERS_ENV: ",".join(sorted(self.approvers)), CHAIN_ID_ENV: str(self.chain)}


class LedgerOperationStore:
    def __init__(self, path: Path, cipher: SensitiveStateCipher):
        self.path, self.cipher = path.expanduser(), cipher
        ensure_private_directory(self.path.parent)
        ensure_private_file(self.path)
        # Create with restricted permissions before SQLite opens the file.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ledger_operations (
                owner TEXT NOT NULL, request_id TEXT NOT NULL,
                state TEXT NOT NULL, expires_at INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(owner, request_id)
            )""")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _read(self, db, owner, request_id):
        row = db.execute("SELECT * FROM ledger_operations WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
        if row is None:
            raise LedgerOperationError("Ledger operation not found.", 404)
        data = self.cipher.decrypt_json(row["payload"])
        metadata = {"owner": row["owner"], "requestId": row["request_id"],
                    "status": row["state"], "expiresAt": row["expires_at"]}
        if any(data.get(key) != value for key, value in metadata.items()):
            raise LedgerOperationError("Stored operation metadata is inconsistent; reconcile it before continuing.", 503)
        return data

    def _write(self, db, row):
        db.execute("UPDATE ledger_operations SET state=?, payload=? WHERE owner=? AND request_id=?",
                   (row["status"], self.cipher.encrypt_json(row), row["owner"], row["requestId"]))

    def get(self, owner, request_id):
        with self.transaction() as db:
            row = self._read(db, owner, request_id)
            if row["status"] in {"preparing", "pending"} and row["expiresAt"] <= time.time():
                row["status"] = "expired"
                self._write(db, row)
            return row

    def create(self, owner, request_id, intent):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise LedgerOperationError("Provide a requestId of 8–128 letters, digits, underscores or hyphens; reuse it on retries.", 400)
        with self.transaction() as db:
            existing = db.execute("SELECT 1 FROM ledger_operations WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if existing:
                row = self._read(db, owner, request_id)
                if row["intent"] != intent:
                    raise LedgerOperationError("This requestId belongs to a different purchase.")
                return False, row
            if db.execute("SELECT 1 FROM ledger_operations WHERE owner=? AND state IN ('executing','uncertain')", (owner,)).fetchone():
                raise LedgerOperationError("A previous Ledger payment needs reconciliation before another can start.")
            row = {"owner": owner, "requestId": request_id, "status": "preparing",
                   "expiresAt": int(time.time()) + TTL_SECONDS, "intent": intent}
            db.execute("INSERT INTO ledger_operations VALUES (?,?,?,?,?)",
                       (owner, request_id, row["status"], row["expiresAt"], self.cipher.encrypt_json(row)))
            return True, row

    def transition(self, owner, request_id, expected, state, **fields):
        with self.transaction() as db:
            row = self._read(db, owner, request_id)
            if row["status"] != expected:
                raise LedgerOperationError("This operation has already advanced; read its status before continuing.")
            if state == "executing" and expected == "pending":
                if row["expiresAt"] <= time.time():
                    raise LedgerOperationError("This approval has expired.", 410)
                other = db.execute("SELECT 1 FROM ledger_operations WHERE owner=? AND request_id<>? AND state IN ('executing','uncertain')",
                                   (owner, request_id)).fetchone()
                if other:
                    raise LedgerOperationError("Another Ledger payment is executing or needs reconciliation.")
            row.update(fields, status=state)
            self._write(db, row)
            return row


class LedgerPayments:
    """Hooks contain the existing gateway's quote, budget and payer functions."""

    def __init__(self, config, store, *, policy, inspect, reserve, release, pay, settle, paused):
        self.config, self.store = config, store
        self.policy, self.inspect = policy, inspect
        self.reserve, self.release, self.pay, self.settle, self.paused = reserve, release, pay, settle, paused

    def _policy(self):
        policy = self.policy()
        if policy is None:
            raise LedgerOperationError("Ledger payments require spending memory; approval cannot fall back to chat.", 503)
        return policy

    def _owner(self, owner):
        if owner != self.config.owner:
            raise LedgerOperationError("Ledger is not configured for this owner.", 403)

    @staticmethod
    def _purchase(row):
        tool = row["intent"]["tool"]
        return " - ".join(str(tool[k]) for k in ("source", "name") if tool.get(k)) or row["intent"]["resourceUrl"]

    @staticmethod
    def _payment(row):
        msg = row["approval"]["message"]
        return Payment(merchant=msg["merchant"], pay_to=msg["payTo"], amount_usd=Decimal(msg["amountUsd"]),
                       owner=msg["owner"], resource=row["intent"]["resourceUrl"])

    def status(self, owner, request_id):
        self._owner(owner)
        return self.response(self.store.get(owner, request_id))

    def start(self, owner, request_id, intent):
        self._owner(owner)
        policy = self._policy()
        fresh, row = self.store.create(owner, request_id, intent)
        if not fresh:
            return self.status(owner, request_id)
        try:
            requirements, payment = self.inspect(owner, intent)
            decision = policy.decide(payment)
            approval = approval_for(payment, decision, purchase=self._purchase(row), lifetime_seconds=TTL_SECONDS)
            # The persisted expiry is also the exact value shown and signed.
            approval = SpendingApproval(**{**approval.__dict__, "expires_at": row["expiresAt"]})
            if decision.action is Action.BLOCK:
                row = self.store.transition(owner, request_id, "preparing", "failed", error=decision.reason)
            else:
                row = self.store.transition(owner, request_id, "preparing", "pending",
                    requirements=requirements, approval=approval.payload(chain=self.config.chain),
                    reason=decision.reason, needsSignature=decision.action is not Action.PAY)
                if not row["needsSignature"]:
                    return self.approve(owner, request_id, None)
        except Exception:
            # Never overwrite executing/succeeded if the automatic payment ran.
            current = self.store.get(owner, request_id)
            if current["status"] == "preparing":
                self.store.transition(owner, request_id, "preparing", "failed", error="Could not prepare this purchase; no payment was sent.")
            raise
        return self.response(row)

    def cancel(self, owner, request_id):
        self._owner(owner)
        row = self.store.get(owner, request_id)
        if row["status"] == "pending":
            row = self.store.transition(owner, request_id, "pending", "cancelled")
        return self.response(row)

    def approve(self, owner, request_id, submitted):
        self._owner(owner)
        row = self.store.get(owner, request_id)
        if row["status"] != "pending":
            return self.response(row)
        if row["approval"].get("signingMethod") != "personal_sign" or row["approval"].get("domain", {}).get("version") != "2":
            raise LedgerOperationError("This is an old approval format. Cancel it and start a new purchase request.", 400)
        if self.paused():
            raise LedgerOperationError("Purchases are paused.", 503)
        policy = self._policy()
        payment = self._payment(row)
        msg = row["approval"]["message"]
        if row["needsSignature"]:
            if not isinstance(submitted, Mapping) or submitted.get("expiresAt") != row["expiresAt"]:
                raise LedgerOperationError("Sign the unmodified approval supplied by the gateway.", 400)
            decision = Decision(action=Action.ESCALATE, reason=row["reason"], rule=msg["rule"], journal_id=msg["journalId"])
            signer = verify_approval(submitted, payment=payment, decision=decision,
                                     purchase=self._purchase(row), env=self.config.env())
        else:
            signer = None
        # Only the winner of this transaction may reach the payer.
        row = self.store.transition(owner, request_id, "pending", "executing")
        reservation = claim = None
        payment_started = False
        try:
            # Inspect again, but execute the frozen request. A new quote cannot
            # silently change the meaning of an already signed approval.
            requirements, current_payment = self.inspect(owner, row["intent"])
            if (current_payment.merchant, current_payment.pay_to_normalised, current_payment.amount_usd) != (
                    payment.merchant, payment.pay_to_normalised, payment.amount_usd):
                raise LedgerOperationError("The quote changed. Start a new purchase and approve its new details.")
            if any(str(requirements.get(k, "")).lower() != str(row["requirements"].get(k, "")).lower()
                   for k in ("asset", "network", "x402Network", "scheme")):
                raise LedgerOperationError("The payment network, token or scheme changed.")
            current = policy.decide(payment, record=False)
            if current.action is Action.BLOCK or (current.action is Action.ESCALATE and
                    (not row["needsSignature"] or current.rule != msg["rule"])):
                raise LedgerOperationError(current.reason)
            reservation = self.reserve(owner, row["requirements"])
            claim = policy.memory.claim_payment(payment, scope="ledger:" + request_id)
            if not claim:
                raise LedgerOperationError("This payment has already been claimed.")
            # Persist accounting handles before submission, for reconciliation.
            row = self.store.transition(owner, request_id, "executing", "executing", reservationId=reservation, claimId=claim)
            approval = {"ok": True, "status": "approved", "source": "ledger" if signer else "spending_memory",
                        "approvalId": "ledger-" + msg["journalId"], "rule": msg["rule"], "reason": row["reason"]}
            if signer:
                approval["approvedBy"] = signer
            if self.paused() or row["expiresAt"] <= time.time():
                raise LedgerOperationError("Purchases were paused or the approval expired before submission.")
            payment_started = True
            result = self.pay(owner, row["intent"], row["requirements"], approval)
            if not isinstance(result, dict) or not result.get("ok"):
                raise LedgerOperationError("The payer did not confirm success.")
            # Save evidence before local accounting; failure afterwards must not
            # lose the transaction or make the request sendable a second time.
            row = self.store.transition(owner, request_id, "executing", "executing", result=result)
            self.settle(owner, reservation, row["intent"], row["requirements"], result, payment, claim)
            row = self.store.transition(owner, request_id, "executing", "succeeded")
        except Exception as exc:
            if payment_started:
                row = self.store.transition(owner, request_id, "executing", "uncertain",
                    error="Payment outcome needs reconciliation. Do not submit a new purchase.")
            else:
                if reservation:
                    self.release(reservation)
                if claim:
                    policy.memory.release_claim(claim)
                message = str(exc) if isinstance(exc, LedgerOperationError) else "Pre-payment checks failed; no payment was sent."
                row = self.store.transition(owner, request_id, "executing", "failed", error=message)
        return self.response(row)

    @staticmethod
    def response(row):
        state = row["status"]
        body = {"requestId": row["requestId"], "status": state, "ok": state == "succeeded"}
        if state == "succeeded":
            body.update(row["result"])
            body.update(requestId=row["requestId"], status=state, ok=True)
            return 200, body
        if state == "pending":
            body.update(decision="needs_ledger_approval" if row["needsSignature"] else "ready",
                        approval=row["approval"], expiresAt=row["expiresAt"], reason=row["reason"],
                        resourceUrl=row["intent"]["resourceUrl"],
                        telegramText=f"Confirm this purchase on your Ledger using the local client. Request: {row['requestId']}")
            return 202, body
        body["error"] = row.get("error") or {
            "preparing": "Purchase is being prepared; retry with the same requestId.",
            "executing": "Payment execution started. Read status; never submit a replacement purchase.",
            "uncertain": "Payment needs reconciliation before retrying.",
            "expired": "Approval expired; no payment was sent.",
            "cancelled": "Purchase cancelled; no payment was sent.",
        }.get(state, "Purchase failed.")
        return (410 if state in {"expired", "cancelled"} else 409), body
