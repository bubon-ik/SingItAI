"""Measured usage, paid directly from the user's delegated USDC account.

CDP is the network fee payer. No transfer to the agent or agent SOL is needed.
An uncertain submitted payment retains its durable hold and blocks retry.
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
import urllib.request

from .agent_allowance import AllowanceError, AllowanceUnavailable
from .ask_metered import _journal_dir, _read, _save

CAP, FEE, MARKUP_BPS = 3000, 1000, 3000
PAY_TO = "4an2sqamWWhny9mjLsMtGXCDXakeNtg6vSLq4QvhdmQu"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _transaction(rpc_url, signature):
    """The confirmed transaction, parsed, or None while the node does not show it yet."""
    request = urllib.request.Request(rpc_url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                               "maxSupportedTransactionVersion": 0}]}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read(2_000_000)).get("result")


def _verified(tx, *, signature, owner, agent, amount, memo, fee_payer):
    """The payment the chain shows is this request's, in this code, apart from the buyer's own check in Node: one
    USDC transfer of exactly `amount` from the owner's account to the merchant's, signed by the agent as delegate,
    fees paid by CDP, with this request's memo."""
    if not isinstance(tx, dict) or (tx.get("meta") or {}).get("err") is not None or "meta" not in tx:
        return False
    message = tx.get("transaction", {}).get("message", {})
    keys = message.get("accountKeys") or []
    if signature not in (tx.get("transaction", {}).get("signatures") or []) or not keys:
        return False
    pubkeys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    if pubkeys[0] != fee_payer or fee_payer in (owner, agent):
        return False
    if not any(isinstance(k, dict) and k.get("pubkey") == agent and k.get("signer") is True for k in keys):
        return False
    meta = tx["meta"]
    holders = {}  # token account -> its owner, for USDC accounts only
    def balances(name):
        found = {}
        for b in meta.get(name) or []:
            if b.get("mint") != USDC or (b.get("uiTokenAmount") or {}).get("decimals") != 6:
                continue
            account = pubkeys[b["accountIndex"]]
            holders[account] = b.get("owner")
            found[b.get("owner")] = int(b["uiTokenAmount"]["amount"])
        return found
    before, after = balances("preTokenBalances"), balances("postTokenBalances")
    if (before.get(owner, 0) - after.get(owner, 0) != amount or after.get(PAY_TO, 0) - before.get(PAY_TO, 0) != amount
            or owner not in before or PAY_TO not in after):
        return False
    instructions = message.get("instructions") or []
    transfers = [i for i in instructions if i.get("program") == "spl-token"
                 and (i.get("parsed") or {}).get("type") == "transferChecked"
                 and (info := i["parsed"].get("info") or {}).get("authority") == agent and info.get("mint") == USDC
                 and str((info.get("tokenAmount") or {}).get("amount")) == str(amount)
                 and holders.get(info.get("source")) == owner and holders.get(info.get("destination")) == PAY_TO]
    memos = [i for i in instructions if i.get("program") == "spl-memo" and i.get("parsed") == memo]
    return len(transfers) == 1 and len(memos) == 1


def _confirmed(lane, **payment):
    for _ in range(5):  # the buyer already waited for it; a lagging node may need a moment
        try:
            tx = _transaction(lane.bridge.rpc, payment["signature"])
        except (OSError, ValueError):
            tx = None
        if tx is not None:
            return _verified(tx, **payment)
        time.sleep(1)
    return False


def _run(lane, **payload):
    helper = Path(__file__).resolve().parents[2] / "singit-ask/src/solana-direct-cli.mjs"
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
        raise AllowanceError("The Solana payment needs settlement review. It was not repeated.")
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
    return amount, data


def _archive(root, journal, checkpoint, proof):
    """Keep the settled-up files with their proof, out of the way of the next request."""
    folder = root / "reconciled" / f"{int(time.time())}-{secrets.token_hex(4)}"
    folder.mkdir(parents=True, mode=0o700)
    for path in (journal, checkpoint):
        if path.exists():
            os.replace(path, folder / path.name)
    _save(folder / "proof.json", proof)


def _recover(lane, account, journal, checkpoint):
    """Settle up a previous request that ended without an answer, from the chain alone.

    No checkpoint means the payment was never handed to the merchant, so its hold is released.
    With one, the chain decides: paid counts the actual charge, a blockhash that can no longer
    land releases the hold, and anything else keeps the block until the chain can tell.
    """
    root = journal.parent
    state = _read(journal)
    hold = state.get("holdId")
    if not checkpoint.exists():
        if hold:
            lane.store.release_metered(hold)
        _archive(root, journal, checkpoint, {"outcome": "not_submitted"})
        return
    saved = _read(checkpoint)
    owner, agent = lane.owner(account), saved.get("payer")
    proof = _run(lane, reconcile={"owner": owner, "agent": agent, "amount": saved.get("amountAtomic"),
        "memo": saved.get("memo"), "feePayer": saved.get("feePayer"),
        "lastValidBlockHeight": saved.get("lastValidBlockHeight")})
    outcome = proof.get("state") if proof.get("ok") is True else None
    if (outcome == "paid" and hold and saved.get("owner") == owner
            and saved.get("memo") == "singit-ask:" + str(state.get("requestId"))
            and str(saved.get("amountAtomic", "")).isdigit() and int(saved["amountAtomic"]) <= CAP):
        lane.store.settle_metered(hold, int(saved["amountAtomic"]), proof["transaction"], int(lane.now()))
    elif outcome == "unpaid":
        if hold:
            lane.store.release_metered(hold)
    elif outcome == "pending":
        raise AllowanceError("The previous Ask payment is still confirming on Solana. Try again in a minute; "
                             "nothing new was paid.")
    else:
        raise AllowanceError("A previous Solana Ask payment needs review. No new payment was sent.")
    _archive(root, journal, checkpoint, {"outcome": outcome, "transaction": proof.get("transaction")})


def recover(server, account):
    """Unblock the account if its previous Ask payment can be settled up; raise while it cannot."""
    lane = getattr(server, "solana_allowance", None)
    root = _journal_dir()
    name = hashlib.sha256(account.encode()).hexdigest() + "-solana"
    journal, checkpoint = root / (name + ".json"), root / (name + ".submitted")
    if lane is None or not (journal.exists() or checkpoint.exists()):
        return
    fd = os.open(root / (name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock, lane._spend_lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if journal.exists() or checkpoint.exists():
            _recover(lane, account, journal, checkpoint)


def pay(server, gw, account, body):
    if not account.startswith("solana:"):
        raise AllowanceUnavailable("A Solana account is required. No Base fallback is available.")
    lane = getattr(server, "solana_allowance", None)
    if lane is None:
        raise AllowanceUnavailable("Solana allowance payments are not configured.")
    token = os.environ.get("SINGIT_ASK_QUOTE_TOKEN", "")
    if len(token) < 32:
        raise AllowanceUnavailable("Direct Solana payments are not configured. Nothing was paid.")
    root = _journal_dir()
    name = hashlib.sha256(account.encode()).hexdigest() + "-solana"
    journal, checkpoint = root / (name + ".json"), root / (name + ".submitted")
    fd = os.open(root / (name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock, lane._spend_lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if journal.exists() or checkpoint.exists():
            _recover(lane, account, journal, checkpoint)
        now = int(lane.now())
        limits = lane.store.limits(account)
        if limits is None:
            raise AllowanceUnavailable("Set your Solana limits and approve them first.")
        if limits["expiry"] <= now:
            raise AllowanceError("Your Solana allowance has expired. Nothing was paid.")
        if CAP > min(limits["per_purchase_cap"], lane.max_per_purchase):
            raise AllowanceError("This question needs a maximum allowance of 0.003 USDC; the actual charge can be lower.")
        hold = "ask-" + secrets.token_hex(16)
        state = {"account": account, "holdId": hold, "ceilingAtomic": CAP, "createdAt": now, "requestId": secrets.token_hex(16), "mode": "direct_exact"}
        complete, reserved = False, False
        try:
            _save(journal, state)
            lane.store.reserve_metered(hold, account, CAP, min(limits["daily_cap"], lane.max_daily), now)
            reserved = True
            chain = lane.chain_state(account, fresh=True)
            owner = lane.owner(account)
            if int(chain["owner"]["delegatedToAgent"]) < CAP:
                raise AllowanceError("Your Solana grant is revoked or insufficient. Nothing was paid.")
            if int(chain["owner"]["amount"]) < CAP:
                raise AllowanceError("At least 0.003 USDC must be available; only actual usage will be charged.")
            agent, key = lane.agent_key(account)
            try:
                result = _run(lane, privateKey=key, owner=owner, requestId=state["requestId"],
                              token=token, body=body, checkpoint=str(checkpoint))
            finally:
                key = None
            if not checkpoint.exists():
                raise AllowanceUnavailable("The answer or payment could not be prepared. No payment was submitted.")
            if result.get("settled") is False and result.get("submitted") is True:
                # The merchant refused before asking for settlement: nothing was or will be charged.
                checkpoint.unlink()
                raise AllowanceUnavailable("The payment was refused before settlement. Nothing was charged; ask again.")
            amount, data = _invoice(result, agent)
            saved = json.loads(checkpoint.read_text())
            if (saved.get("requestId") != state["requestId"] or saved.get("payer") != agent
                    or saved.get("owner") != owner or result.get("owner") != owner
                    or saved.get("amountAtomic") != str(amount)
                    or saved.get("memo") != "singit-ask:" + state["requestId"]):
                raise AllowanceError("Solana receipt belongs to a different request. Do not retry payment.")
            tx = result.get("transaction")
            # Preserve receipt identifiers before the independent chain read, for reconciliation.
            state.update(txId=tx, amountAtomic=amount)
            _save(journal, state)
            if not _confirmed(lane, signature=tx, owner=owner, agent=agent, amount=amount, memo=saved["memo"],
                              fee_payer=saved["feePayer"]):
                raise AllowanceError("The Solana settlement is not confirmed. No automatic retry was made.")
            lane.store.settle_metered(hold, amount, tx, int(lane.now()))
            complete = True
            return amount, data, tx
        finally:
            lane._seen.pop(account, None)
            if complete or not checkpoint.exists():
                if not complete and reserved:
                    lane.store.release_metered(hold)
                checkpoint.unlink(missing_ok=True)
                journal.unlink(missing_ok=True)
