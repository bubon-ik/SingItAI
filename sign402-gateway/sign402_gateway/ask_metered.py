"""One bounded x402 upto request, with an actual-spend ledger and no paid retries.

The durable per-account journal deliberately blocks further Ask payments after
an ambiguous submission. An operator must reconcile its nonce/transaction first;
restarting the gateway never clears this protection. No keys or signatures are
written to the journal. Other payment adapters retain their existing behavior.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time

from .agent_allowance import AllowanceError, AllowanceUnavailable, USDC, TRANSFER_TOPIC, encode_call

URL = "https://ask.singitai.app/v1/chat/completions/metered"
PAY_TO = "0xC23d1Dc0f5fCe1abfFB051e06cB93f0329968B4e"
CAP = 3000
FEE = 1000
MARKUP_BPS = 3000
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"


def _save(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path):
    """A journal or checkpoint, or {} when missing or unreadable: an empty one proves nothing."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _journal_dir():
    root = Path.home() / ".sign402" / "ask-metered"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def _run_buyer(key, body, checkpoint):
    helper = Path(__file__).resolve().parents[2] / "singit-ask" / "src" / "buyer-cli.mjs"
    node = str(Path.home() / ".hermes" / "node" / "bin" / "node")
    if not Path(node).is_file():
        node = shutil.which("node")
    if not node or not helper.is_file():
        raise AllowanceUnavailable("Metered payment helper is not installed. Nothing was paid.")
    try:
        completed = subprocess.run([node, str(helper)], input=json.dumps({"privateKey": key,
            "body": body, "checkpoint": str(checkpoint)}), text=True, capture_output=True, timeout=210)
        # Never log stdout/stderr or exception arguments: only validated receipt fields escape.
        return json.loads(completed.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return {"ok": False}


def _validate_result(service, result, agent, account):
    if result.get("ok") is not True or str(result.get("payer", "")).lower() != agent.lower():
        raise AllowanceError("Payment result is unresolved. No automatic retry was made.")
    data = result.get("body") or {}
    usage, bill = data.get("usage") or {}, data.get("billing") or {}
    raw = usage.get("buyer_cost_micro")
    if type(raw) is int:
        cost = raw
    elif isinstance(raw, str) and raw.isascii() and raw.isdigit():
        cost = int(raw)
    else:
        raise AllowanceError("The usage invoice is missing; settlement needs review.")
    markup = (cost * MARKUP_BPS + 9999) // 10000
    amount = cost + markup + FEE
    expected = {"providerCostAtomic": str(cost), "markupAtomic": str(markup),
        "totalAtomic": str(amount), "settlementFeeAtomic": str(FEE), "maxChargeAtomic": str(CAP),
        "markupBps": MARKUP_BPS, "mode": "actual_usage", "version": 1, "currency": "USDC"}
    if cost < 0 or amount > CAP or any(bill.get(k) != v for k, v in expected.items()) or result.get("amountAtomic") != str(amount):
        raise AllowanceError("The usage invoice does not match the agreed price; settlement needs review.")
    tx = str(result.get("transaction", ""))
    if len(tx) != 66 or not tx.startswith("0x") or any(c not in "0123456789abcdefABCDEF" for c in tx[2:]):
        raise AllowanceError("Missing settlement transaction.")
    receipt = service.evm.call("eth_getTransactionReceipt", [tx]) or {}
    logs = [entry for entry in receipt.get("logs", []) if
        str(entry.get("address", "")).lower() == USDC.lower()
        and len(entry.get("topics", [])) == 3 and entry["topics"][0].lower() == TRANSFER_TOPIC.lower()
        and entry["topics"][1][-40:].lower() == agent[2:].lower()
        and entry["topics"][2][-40:].lower() == PAY_TO[2:].lower()]
    if receipt.get("status") != "0x1" or str(receipt.get("transactionHash", "")).lower() != tx.lower() or len(logs) != 1 or int(logs[0]["data"], 16) != amount:
        raise AllowanceError("Actual USDC settlement is not confirmed. No automatic retry was made.")
    index = hex(int(logs[0]["logIndex"], 16))
    if (tx.lower(), index) in service.store.counted_settlements(account):
        raise AllowanceError("This settlement was already counted. No automatic retry was made.")
    return amount, data, tx.lower(), index


def _archive(root, journal, checkpoint, proof):
    """Keep the settled-up files with their proof, out of the way of the next request."""
    folder = root / "reconciled" / f"{int(time.time())}-{secrets.token_hex(4)}"
    folder.mkdir(parents=True, mode=0o700)
    for path in (journal, checkpoint):
        if path.exists():
            os.replace(path, folder / path.name)
    _save(folder / "proof.json", proof)


def _release(server, gw, state):
    gw._release_user_wallet_spend(server, state.get("reservationId"))
    if state.get("claimId") and server.spending_policy is not None:
        server.spending_policy.memory.release_claim(state["claimId"])


def _recover(server, gw, journal, checkpoint):
    """Settle up a previous request that ended without an answer, from the chain alone.

    No checkpoint means no signature reached the merchant, so the reservation is released.
    With one, Permit2 decides: a nonce still unused after its deadline can never be spent, so the
    reservation is released too. A used nonce means the payment went through: that stays for review.
    """
    root = journal.parent
    state = _read(journal)
    if not checkpoint.exists():
        _release(server, gw, state)
        _archive(root, journal, checkpoint, {"outcome": "not_submitted"})
        return
    saved = _read(checkpoint)
    payer, nonce, deadline = saved.get("payer"), str(saved.get("nonce", "")), str(saved.get("deadline", ""))
    if not (isinstance(payer, str) and len(payer) == 42 and nonce.isdigit() and deadline.isdigit()):
        raise AllowanceError("A previous Ask payment needs settlement review. It was not repeated; "
                             "contact support before sending another question.")
    evm = server.allowance.evm
    block = evm.call("eth_getBlockByNumber", ["latest", False]) or {}
    # The nonce is read at that same block: a lagging node answers for it or fails, never for an older one.
    word = evm.call("eth_call", [{"to": PERMIT2, "data": encode_call("nonceBitmap(address,uint256)", payer,
                                                                     int(nonce) >> 8)}, block.get("number")])
    if not (isinstance(word, str) and len(word) == 66 and isinstance(block.get("timestamp"), str)):
        raise AllowanceError("Base did not answer the payment check. Try again shortly; nothing new was paid.")
    used = bool(int(word, 16) >> (int(nonce) & 0xFF) & 1)
    expired = int(block["timestamp"], 16) > int(deadline)
    if used:
        raise AllowanceError("A previous Ask payment went through without an answer and needs review. "
                             "No new payment was sent.")
    if not expired:
        raise AllowanceError("The previous Ask payment is still being checked. Try again in two minutes; "
                             "nothing new was paid.")
    _release(server, gw, state)
    _archive(root, journal, checkpoint, {"outcome": "unpaid", "block": block.get("number"),
                                         "timestamp": block.get("timestamp"), "deadline": deadline, "used": used})


def recover(server, gw, account):
    """Unblock the account if its previous Ask payment can be settled up; raise while it cannot."""
    root = _journal_dir()
    name = hashlib.sha256(account.encode()).hexdigest()
    journal, checkpoint = root / (name + ".json"), root / (name + ".submitted")
    if not (journal.exists() or checkpoint.exists()):
        return
    lock_fd = os.open(root / (name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if journal.exists() or checkpoint.exists():
            _recover(server, gw, journal, checkpoint)


def pay(server, gw, account, body):
    service = server.allowance
    active = service.lane_for(account)
    if active is None:
        raise AllowanceUnavailable("Set your limits and approve them first.")
    if CAP > int(active["per_purchase_cap"]):
        raise AllowanceError("This request needs permission for up to 0.003 USDC; its actual charge may be lower.")
    root = _journal_dir()
    name = hashlib.sha256(account.encode()).hexdigest()
    journal, checkpoint = root / (name + ".json"), root / (name + ".submitted")
    lock_fd = os.open(root / (name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if journal.exists() or checkpoint.exists():
            _recover(server, gw, journal, checkpoint)
        requirements = {"scheme": "upto", "network": "base-mainnet", "x402Network": "eip155:8453",
            "asset": USDC, "amountAtomic": str(CAP), "receiver": PAY_TO, "resource": URL,
            "paymentIntent": "ask-" + secrets.token_hex(16)}
        reservation, claim, complete = None, None, False
        state = {"account": account, "createdAt": int(time.time()), "ceilingAtomic": CAP, "resource": URL}
        try:
            # Persist intent BEFORE reserving: a process crash cannot permit another payment.
            _save(journal, state)
            reservation, _, claim = gw._reserve_user_wallet_spend(server, account, requirements,
                resource_url=URL, claim_scope=requirements["paymentIntent"])
            state.update(reservationId=reservation, claimId=claim)
            _save(journal, state)
            agent, key = service.agent_key(account)
            with service._spend_lock:
                service._fund(active, agent, key, CAP, requirements["paymentIntent"])
                result = _run_buyer(key, body, checkpoint)
                amount, data, tx, index = _validate_result(service, result, agent, account)
                state.update(amountAtomic=amount, txId=tx, logIndex=index, payer=agent)
                _save(journal, state)
                actual = {**requirements, "amountAtomic": str(amount)}
                payment = gw._payment_from_requirements(actual, owner=account, resource_url=URL)
                gw._settle_user_wallet_spend(server, reservation, {"id": "data.singit-ask"}, URL, actual,
                    {"txId": tx}, payment=payment, claim_id=claim, actual_amount_atomic=amount)
                service.store.count_settlement(tx, index, account, PAY_TO, amount, URL, int(time.time()))
                complete = True
            return amount, data, tx
        finally:
            # A saved checkpoint means a signature may already have reached the merchant.
            # Keep its hold and durable block on any uncertainty, even after a process restart.
            if complete or not checkpoint.exists():
                if not complete:
                    gw._release_user_wallet_spend(server, reservation)
                    if claim and server.spending_policy is not None:
                        server.spending_policy.memory.release_claim(claim)
                checkpoint.unlink(missing_ok=True)
                journal.unlink(missing_ok=True)
