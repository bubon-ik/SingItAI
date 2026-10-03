"""Solana Ask: one request escrow, actual settlement, automatic unused-USDC refund.

Uses the account's existing agent and SPL delegate grant. The small funding
shortfall (if any) is pulled into that user's agent, never a merchant balance.
All uncertain funding/payment outcomes retain a durable hold and block retry.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

from .agent_allowance import AllowanceError, AllowanceUnavailable
from .ask_metered import _journal_dir, _save

CAP, FEE, MARKUP_BPS = 3000, 2000, 3000
PAY_TO = "4an2sqamWWhny9mjLsMtGXCDXakeNtg6vSLq4QvhdmQu"


def _run(lane, **payload):
    helper = Path(__file__).resolve().parents[2] / "singit-ask/src/solana-buyer-cli.mjs"
    node = str(Path.home() / ".hermes/node/bin/node")
    if not Path(node).is_file():
        node = shutil.which("node")
    if not node or not helper.is_file():
        raise AllowanceUnavailable("Solana metered payment helper is not installed. Nothing was paid.")
    payload["rpcUrl"] = lane.bridge.rpc
    try:
        process = subprocess.run([node, str(helper)], input=json.dumps(payload), text=True,
                                 capture_output=True, timeout=240)
        result = json.loads(process.stdout)
        return result if isinstance(result, dict) else {"ok": False}
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return {"ok": False}


def _invoice(result, agent):
    if result.get("ok") is not True or result.get("payer") != agent:
        raise AllowanceError("The Solana payment/refund needs settlement review. It was not repeated.")
    data = result.get("body") or {}
    usage, bill = data.get("usage") or {}, data.get("billing") or {}
    raw = usage.get("buyer_cost_micro")
    if type(raw) is int:
        cost = raw
    elif isinstance(raw, str) and raw.isascii() and raw.isdigit():
        cost = int(raw)
    else:
        raise AllowanceError("No valid token usage invoice. Check the Solana payment before retrying.")
    markup = (cost * MARKUP_BPS + 9999) // 10000
    amount = cost + markup + FEE
    expected = {"version": 1, "mode": "actual_usage", "markupBps": MARKUP_BPS,
                "maxChargeAtomic": str(CAP), "settlementFeeAtomic": str(FEE), "providerCostAtomic": str(cost),
                "markupAtomic": str(markup), "totalAtomic": str(amount), "currency": "USDC"}
    if cost < 0 or amount > CAP or any(bill.get(k) != v for k, v in expected.items()) or result.get("amountAtomic") != str(amount):
        raise AllowanceError("Solana invoice differs from the agreed price. Do not retry payment.")
    if result.get("refundAtomic") != str(CAP - amount):
        raise AllowanceError("The unused Solana reserve was not confirmed returned. Do not retry payment.")
    return amount, data


def pay(server, gw, account, body):
    if not account.startswith("solana:"):
        raise AllowanceUnavailable("A Solana account is required. No Base fallback is available.")
    lane = getattr(server, "solana_allowance", None)
    if lane is None:
        raise AllowanceUnavailable("Solana allowance payments are not configured.")
    root = _journal_dir()
    name = hashlib.sha256(account.encode()).hexdigest() + "-solana"
    journal, checkpoint = root / (name + ".json"), root / (name + ".submitted")
    fd = os.open(root / (name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock, lane._spend_lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if journal.exists() or checkpoint.exists():
            raise AllowanceError("A previous Solana Ask payment/refund needs review. No new payment was sent.")
        now = int(lane.now())
        limits = lane.store.limits(account)
        if limits is None:
            raise AllowanceUnavailable("Set your Solana limits and approve them first.")
        if limits["expiry"] <= now:
            raise AllowanceError("Your Solana allowance has expired. Nothing was paid.")
        if CAP > min(limits["per_purchase_cap"], lane.max_per_purchase):
            raise AllowanceError("This question needs a maximum allowance of 0.003 USDC; the actual charge can be lower.")
        hold = "ask-" + secrets.token_hex(16)
        state = {"account": account, "holdId": hold, "ceilingAtomic": CAP, "createdAt": now, "fundingUncertain": False}
        complete, reserved = False, False
        try:
            _save(journal, state)
            lane.store.reserve_metered(hold, account, CAP, min(limits["daily_cap"], lane.max_daily), now)
            reserved = True
            ready = _run(lane, checkOnly=True, body=body)
            if ready.get("error") == "receiver_not_ready":
                raise AllowanceUnavailable("The SingIt Solana receiving wallet has no active native USDC account yet. No funds were moved.")
            if ready.get("ready") is not True:
                raise AllowanceUnavailable("The Solana payment quote could not be checked. Nothing was paid.")
            chain = lane.chain_state(account, fresh=True)
            owner, agent_balance = chain["owner"], int(chain["agent"]["usdcAtomic"])
            delegated = int(owner["delegatedToAgent"])
            shortfall = max(0, CAP - agent_balance)
            if delegated <= 0 or delegated < shortfall:
                raise AllowanceError("Your Solana grant is revoked or insufficient. Nothing was paid.")
            if int(owner["amount"]) < shortfall:
                raise AllowanceError("Not enough native USDC in the Solana wallet for this request's reserve.")
            if shortfall:
                funding_ready = lane._call(account, "allowance-funding-check", owner=lane.owner(account), amount=str(shortfall))
                if funding_ready.get("ready") is not True:
                    raise AllowanceUnavailable("Your agent needs SOL for network fees and its USDC account. Open Allowance → Agent network fees to add SOL from your wallet. Nothing was sent.")
                state["fundingUncertain"] = True
                _save(journal, state)
                try:
                    funding = lane._call(account, "allowance-pull", owner=lane.owner(account), amount=str(shortfall))
                except Exception:
                    raise AllowanceError("Solana funding could not be confirmed. No chat payment was attempted; funding must be checked before retrying.") from None
                state["fundingTx"] = funding.get("transaction")
                _save(journal, state)
                if funding.get("state") == "not_submitted" and funding.get("reason") == "agent_sol_required":
                    state["fundingUncertain"] = False
                    _save(journal, state)
                    raise AllowanceUnavailable("Your agent needs SOL. Open Allowance → Agent network fees. Nothing was sent.")
                if funding.get("state") != "confirmed":
                    raise AllowanceError("Solana funding needs confirmation. No chat payment was attempted; do not repeat funding.")
                state["fundingUncertain"] = False
                _save(journal, state)
            agent, key = lane.agent_key(account)
            result = _run(lane, privateKey=key, body=body, checkpoint=str(checkpoint))
            key = None
            refunded = result.get("refunded") is True and result.get("payer") == agent
            if refunded:
                if result.get("amountAtomic") != "0" or result.get("refundAtomic") != str(CAP):
                    raise AllowanceError("The complete Solana refund is not confirmed. Do not retry payment.")
                amount, data = 0, None
            else:
                amount, data = _invoice(result, agent)
            saved = json.loads(checkpoint.read_text())
            if saved.get("channelId") != result.get("channelId") or saved.get("payer") != agent:
                raise AllowanceError("Solana receipt belongs to a different request. Do not retry payment.")
            tx = result.get("transaction")
            # A second read verifies this request's channel, exact payout and full unused refund.
            verified = _run(lane, verify={"signature": tx, "channelId": saved["channelId"], "payer": agent, "amount": str(amount)})
            if verified.get("verified") is not True:
                raise AllowanceError("The Solana settlement/refund is not confirmed. No automatic retry was made.")
            state.update(txId=tx, amountAtomic=amount, channelId=saved["channelId"], refundAtomic=CAP-amount)
            _save(journal, state)
            lane.store.settle_metered(hold, amount, tx, int(lane.now()))
            complete = True
            if refunded:
                raise AllowanceError("The model did not answer. The entire Solana request reserve was returned to your agent; no usage fee was charged.")
            return amount, data, tx
        finally:
            lane._seen.pop(account, None)
            if complete or (not checkpoint.exists() and not state["fundingUncertain"]):
                if not complete and reserved:
                    lane.store.release_metered(hold)
                checkpoint.unlink(missing_ok=True)
                journal.unlink(missing_ok=True)
