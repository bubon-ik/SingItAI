"""Owner-only Graph query flow with local Ledger consent and durable results.

The Graph adapter still owns its policy, payment claim and query cache. This
module adds a stricter hardware approval gate before its payer callback. A
permanent attempt marker permits one paid query in this demo state directory.
Neither Telegram retries nor an interrupted payer can allocate another slot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from spending_memory import Action, Payment, SpendingMemory, SpendingPolicy
from spending_memory.adapters.thegraph import payment_requirements

from .ledger_approval import approval_for, verify_approval
from .ledger_payments import LedgerOperationStore
from .onchain_data import OnchainConfig, build_onchain_data_from_env
from .secure_state import atomic_write_private_json, ensure_private_directory

URL = OnchainConfig().resource_url
ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RECEIVER = "0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB"
AMOUNT = "10000"
PURCHASE = "The Graph - WETH price"


def check_quote(response):
    q = payment_requirements(*response)
    if (q.get("scheme") != "exact" or q.get("network") != "eip155:8453"
            or str(q.get("amount")) != AMOUNT
            or str(q.get("asset", "")).lower() != ASSET.lower()
            or str(q.get("payTo", "")).lower() != RECEIVER.lower()):
        raise ValueError("The Graph payment terms changed; nothing was paid.")
    return q


def format_result(result):
    cost = "0.01 USDC via x402" if result["paid"] else "0 USDC — cached answer"
    approval = "Ledger approval verified" if result["paid"] else "Previously approved data reused"
    receipt = "Confirmed on Base" if result["receiptVerified"] else "Settlement verification pending"
    return (f"WETH: {Decimal(result['priceUsdc']):,.6f} USDC\n"
            f"Source: The Graph / Uniswap V3 on Base\n"
            f"Indexed block: {result['indexedBlock']}\n"
            f"Query cost: {cost}\n{approval}\n{receipt}\n\n"
            f"https://basescan.org/tx/{result['transactionHash']}\n\n"
            "Repeat /graph_demo within 5 minutes to reuse the cached answer for free.\n"
            "/graph_demo status shows the saved result without buying again.")


class GraphLedgerDemo:
    def __init__(self, *, owner, approver, payer_address, state, cipher,
                 quote, payer, signer, receipt, historical_proof):
        if not str(owner).isdecimal() or not re.fullmatch(r"0x[0-9a-fA-F]{40}", approver):
            raise ValueError("Configure a numeric Telegram owner and Ledger address.")
        if not historical_proof.get("verified"):
            raise ValueError("A verified historical Graph settlement is required.")
        self.owner, self.approver, self.payer_address = str(owner), approver, payer_address
        self.state = Path(state)
        ensure_private_directory(self.state)
        self.store = LedgerOperationStore(self.state / "operations.sqlite3", cipher)
        self.policy = SpendingPolicy(SpendingMemory.local(str(self.state / "memory.sqlite3")),
                                     daily_cap_usd=Decimal("0.01"))
        if self.policy.memory.recall_merchant("gateway.thegraph.com") is None:
            self.policy.memory.remember_settlement(Payment(merchant="gateway.thegraph.com",
                pay_to=RECEIVER, amount_usd=Decimal("0.01"), owner="verified-graph-history"),
                tx_id=historical_proof["transactionHash"])
        self.quote, self.payer, self.signer, self.receipt = quote, payer, signer, receipt
        self.lock, self.active = threading.RLock(), None

    def start(self, owner, request_id, *, background=True):
        if str(owner) != self.owner:
            raise PermissionError("Demo is restricted to its owner.")
        with self.lock:
            if self.active:
                return self.status(owner, self.active)
            fresh, row = self.store.create(self.owner, request_id,
                {"tool": {"source": "The Graph", "name": "WETH price"}, "resourceUrl": URL})
            if not fresh:
                return self.status(owner, request_id)
            self.active = request_id
        if background:
            threading.Thread(target=self._run, args=(request_id,), daemon=True).start()
        else:
            self._run(request_id)
        return self.status(owner, request_id)

    def status(self, owner, request_id):
        if str(owner) != self.owner:
            raise PermissionError("Demo is restricted to its owner.")
        row = self.store.get(self.owner, request_id)
        messages = {
            "preparing": "Checking The Graph for a WETH/USDC quote…",
            "pending": (f"The Graph returned HTTP 402 Payment Required.\n"
                f"Price: 0.01 USDC on Base\nRecipient: {RECEIVER}\n\n"
                "Review ‘The Graph - WETH price’ on your connected Ledger and approve the displayed purchase."),
            "executing": "Ledger approval verified. Paying The Graph through x402…",
            "failed": "Query stopped before completion. No automatic payment retry. Use /graph_demo status.",
            "expired": "Ledger approval expired. No payment was submitted for this approval.",
            "uncertain": "Payment outcome requires verification. Do not start another purchase; use /graph_demo status.",
        }
        return {"requestId": request_id, "status": row["status"],
                "text": format_result(row["result"]) if row["status"] == "succeeded"
                        else messages.get(row["status"], "Check the saved operation."),
                **({"result": row["result"]} if row["status"] == "succeeded" else {})}

    def latest(self, owner):
        if str(owner) != self.owner:
            raise PermissionError("Demo is restricted to its owner.")
        with self.store.transaction() as db:
            row = db.execute("SELECT request_id FROM ledger_operations WHERE owner=? ORDER BY rowid DESC LIMIT 1",
                             (self.owner,)).fetchone()
        if row is None:
            return {"status": "empty", "text": "Send /graph_demo to request WETH data. Maximum cost: 0.01 USDC, with Ledger approval."}
        return self.status(owner, row["request_id"])

    def _run(self, request_id):
        sent = False
        try:
            def quote(url):
                if url != URL:
                    raise ValueError("Unexpected Graph endpoint.")
                response = self.quote(url)
                check_quote(response)
                return response

            def pay(url, **kwargs):
                nonlocal sent
                if (url != URL or kwargs.get("max_atomic") != AMOUNT or kwargs.get("method") != "POST"
                        or str(kwargs.get("expected_receiver", "")).lower() != RECEIVER.lower()
                        or str(kwargs.get("expected_asset", "")).lower() != ASSET.lower()):
                    raise ValueError("Unexpected Graph payment request.")
                # Freeze the exact GraphQL request; commit it in the signed resource.
                frozen = json.loads(json.dumps(kwargs))
                digest = hashlib.sha256(json.dumps(frozen["request_body"], sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()
                payment = Payment(merchant="gateway.thegraph.com", pay_to=RECEIVER,
                    amount_usd=Decimal("0.01"), owner=self.owner, resource=URL + "#query=" + digest)
                decision = self.policy.decide(payment)
                if decision.action is not Action.PAY:
                    raise ValueError("Spending policy refused this query.")
                row = self.store.get(self.owner, request_id)
                approval = replace(approval_for(payment, decision, purchase=PURCHASE), expires_at=row["expiresAt"])
                row = self.store.transition(self.owner, request_id, "preparing", "pending",
                    approval=approval.payload(chain=8453), frozenRequest=frozen)
                submitted = self.signer({**row, "status": "pending"}, self.owner)
                if submitted.get("expiresAt") != row["expiresAt"]:
                    raise ValueError("Approval expiry changed.")
                verify_approval(submitted, payment=payment, decision=decision, purchase=PURCHASE,
                    env={"SIGN402_LEDGER_APPROVER_ADDRESSES": self.approver,
                         "SIGN402_LEDGER_APPROVAL_CHAIN_ID": "8453"})
                quote(URL)  # Recheck the fixed terms after device interaction.
                if self.policy.decide(payment, record=False).action is not Action.PAY:
                    raise ValueError("Spending policy changed while awaiting approval.")
                if (self.state / "paused").exists():
                    raise ValueError("Demo payments are paused.")
                self.store.transition(self.owner, request_id, "pending", "executing")
                # Permanent across all Telegram messages and process restarts.
                fd = os.open(self.state / "payment-attempt.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump({"requestId": request_id, "amountUsdc": "0.01",
                               "startedAt": datetime.now(timezone.utc).isoformat()}, f)
                    f.flush()
                    os.fsync(f.fileno())
                sent = True
                response = self.payer(url, **frozen)
                safe = {k: response.get(k) for k in ("ok", "status", "payer", "transactionHash", "body")}
                self.store.transition(self.owner, request_id, "executing", "executing", paymentResult=safe)
                if (safe["ok"] is not True or safe["status"] != 200
                        or str(safe["payer"]).lower() != self.payer_address.lower()
                        or not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(safe["transactionHash"]))):
                    raise ValueError("Payment result needs reconciliation.")
                return safe

            client = build_onchain_data_from_env(policy=self.policy, pay=pay, fetch_402=quote,
                purchases_paused=lambda: (self.state / "paused").exists(),
                env={"SIGN402_ONCHAIN_DATA_ENABLED": "1", "SIGN402_ONCHAIN_MAX_PER_CALL_ATOMIC": AMOUNT})
            price = client.price(self.owner, "WETH")
            entries = self.policy.memory.journal(limit=100)
            entry = next(e for e in entries if e["id"] == price.journal_id)
            if not price.paid:
                entry = next(e for e in entries if e["id"] == entry["extra"]["served_from"])
            tx = entry["extra"]["tx_id"]
            try:
                receipt_verified = self.receipt(tx, self.payer_address).get("verified") is True
            except Exception:
                receipt_verified = False
            result = {"priceUsdc": str(price.usd), "pool": price.pool, "indexedBlock": price.block_number,
                "paid": price.paid, "transactionHash": tx, "receiptVerified": receipt_verified,
                "journalId": price.journal_id}
            row = self.store.get(self.owner, request_id)
            self.store.transition(self.owner, request_id, row["status"], "succeeded", result=result)
            if price.paid:
                atomic_write_private_json(self.state / "purchase-record.json", {
                    "invoice_id": tx, "product_slug": "thegraph.uniswap-v3-base.weth-price",
                    "amount": "0.01 USDC", "payment_method": "USDC on Base",
                    "timestamp": datetime.now(timezone.utc).isoformat()})
        except Exception:
            row = self.store.get(self.owner, request_id)
            if row["status"] not in {"succeeded", "expired"}:
                self.store.transition(self.owner, request_id, row["status"], "uncertain" if sent else "failed")
        finally:
            with self.lock:
                if self.active == request_id:
                    self.active = None
