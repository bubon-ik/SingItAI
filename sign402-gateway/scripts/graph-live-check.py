#!/usr/bin/env python3
"""Check the real WETH query once, then its journal-backed chat answer.

prepare and status cannot pay. Run only after explicit approval of the terms
printed by prepare. An on-disk attempt marker forbids a second payer invocation,
including after an uncertain result. Uses isolated local state, never production.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sign402-gateway"))

from spending_memory import Payment, SpendingMemory, SpendingPolicy
from spending_memory.adapters.thegraph import PaidGraphQueries, payment_requirements
from sign402_gateway.onchain_data import OnchainConfig, _urllib_402, build_onchain_data_from_env, read_price
from sign402_gateway.secure_state import atomic_write_private_json, ensure_private_directory
from sign402_gateway.server import CdpBaseX402PaymentClient
from sign402_gateway.venice_chat import VeniceChatClient

STATE = ROOT / ".graph-live"
OWNER = "thegraph-weth-verification"
URL = OnchainConfig().resource_url
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RECEIVER = "0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB"
AMOUNT = "10000"
HISTORICAL_TX = "0x57ddeebd74b89f8834c8627d7e1ad6878e44a651744da49fae677491ac2d7958"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ENV = {"SIGN402_ONCHAIN_DATA_ENABLED": "1", "SIGN402_ONCHAIN_MAX_PER_CALL_ATOMIC": AMOUNT}


def rpc(method, params):
    result = subprocess.run([
        "curl", "--fail", "--silent", "--show-error", "--max-time", "20",
        "https://mainnet.base.org", "-H", "Content-Type: application/json", "--data-binary", "@-",
    ], input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}),
        capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise ValueError("Public Base RPC is unavailable; no automatic payment retry.")
    payload = json.loads(result.stdout)
    if payload.get("error") or payload.get("result") is None:
        raise ValueError("RPC result unavailable; no automatic payment retry.")
    return payload["result"]


def verify_receipt(tx, payer=None):
    receipt = rpc("eth_getTransactionReceipt", [tx])
    if receipt.get("status") != "0x1" or receipt.get("transactionHash", "").lower() != tx.lower():
        raise ValueError("A successful matching transaction receipt is required.")
    transfers = [event for event in receipt.get("logs", [])
        if event.get("address", "").lower() == USDC.lower()
        and len(event.get("topics", [])) == 3
        and event["topics"][0].lower() == TRANSFER_TOPIC
        and event["topics"][2][-40:].lower() == RECEIVER[2:].lower()
        and int(event["data"], 16) == int(AMOUNT)
        and (payer is None or event["topics"][1][-40:].lower() == payer[2:].lower())]
    if len(transfers) != 1:
        raise ValueError("Receipt does not prove exactly one matching 0.01 USDC transfer.")
    return {"verified": True, "transactionHash": tx, "blockNumber": int(receipt["blockNumber"], 16),
            "amountUsdc": "0.01", "payer": "0x" + transfers[0]["topics"][1][-40:]}


def payer_address():
    for line in (ROOT / "cdp-x402-service/.env").read_text().splitlines():
        if line.startswith("CDP_EVM_ACCOUNT_ADDRESS="):
            address = line.split("=", 1)[1].strip().strip('"').strip("'")
            if len(address) == 42 and address.startswith("0x"):
                return address
    raise ValueError("The existing operator payer address is not configured.")


def balance(payer, block="latest"):
    return int(rpc("eth_call", [{"to": USDC, "data": "0x70a08231" + payer[2:].lower().zfill(64)}, block]), 16)


def validate_quote(headers, body):
    quote = payment_requirements(headers, body)
    if (quote.get("scheme") != "exact" or quote.get("network") != "eip155:8453"
            or str(quote.get("amount")) != AMOUNT
            or str(quote.get("asset", "")).lower() != USDC.lower()
            or str(quote.get("payTo", "")).lower() != RECEIVER.lower()):
        raise ValueError("The Graph quote differs from the fixed terms. Do not pay.")


def policy():
    return SpendingPolicy(SpendingMemory.local(str(STATE / "memory.sqlite3")), daily_cap_usd=Decimal("0.01"))


def video_demo_state(base, payer):
    """Allow one deliberate new demo only after the original attempt is proven settled."""
    if (base / "attempt.json").exists():
        result = json.loads((base / "result.json").read_text())
        verification = json.loads((base / "verification.json").read_text())
        tx = result.get("transactionHash")
        proof = verification.get("proof", {})
        if (not result.get("ok") or result.get("status") != 200 or not tx
                or not verification.get("restartCachePassed")
                or proof.get("verified") is not True or proof.get("transactionHash") != tx):
            raise ValueError("Reconcile the original attempt before preparing a new demo.")
        verify_receipt(tx, payer)
    # Fixed directory: repeating --video-demo never creates another payment slot.
    return base / "video-demo"


def save_completed_check(verification, tx):
    """Persist payment/data proof before the optional post-payment balance RPC."""
    atomic_write_private_json(STATE / "verification.json", verification)
    record_path = STATE / "purchase-record.json"
    if not record_path.exists():
        atomic_write_private_json(record_path, {"invoice_id": tx,
            "product_slug": "thegraph.uniswap-v3-base.weth-price", "amount": "0.01 USDC",
            "payment_method": "USDC on Base", "timestamp": datetime.now(timezone.utc).isoformat()})
    proof = verification["proof"]
    try:
        # A load-balanced RPC can answer latest from before settlement.
        amount = balance(proof["payer"], hex(proof["blockNumber"]))
        verification.update(balanceAfterUsdc=str(Decimal(amount) / 1_000_000),
                            balanceVerifiedAtBlock=proof["blockNumber"], balanceCheck="verified")
    except (ValueError, OSError, subprocess.TimeoutExpired):
        verification.update(balanceAfterUsdc=None, balanceCheck="unavailable")
        print("Payment and data verified. Optional balance RPC unavailable; no payment retry is needed.", flush=True)
    atomic_write_private_json(STATE / "verification.json", verification)
    return verification


def recover_completed_check(result, proof):
    """Recover evidence from the paid response and journal without fetching or paying."""
    if (result.get("ok") is not True or result.get("status") != 200
            or result.get("transactionHash") != proof["transactionHash"]):
        raise ValueError("Stored data does not match the verified payment.")
    price = read_price(result.get("body"), "WETH")
    if price is None:
        raise ValueError("Stored Graph response has no valid price.")
    reopened = policy()
    entries = reopened.memory.journal(limit=100)
    originals = [e for e in entries if e.get("extra", {}).get("paid") is True
        and e["extra"].get("owner") == OWNER
        and e["extra"].get("tx_id") == proof["transactionHash"]]
    if len(originals) != 1:
        raise ValueError("The journal must contain exactly one matching paid query.")
    original = originals[0]
    cached = [e for e in entries if e.get("extra", {}).get("paid") is False
        and e["extra"].get("owner") == OWNER
        and e["extra"].get("served_from") == original["id"]]
    spend = PaidGraphQueries(reopened, owner=OWNER).spent_on_data()
    if not cached or spend["queries_paid"] != 1 or Decimal(spend["spent_usd"]) != Decimal("0.01"):
        raise ValueError("The journal does not prove paid-once cache reuse.")
    plan = json.loads((STATE / "plan.json").read_text())
    verification = {"priceUsdc": str(price.usd), "pool": price.pool,
        "indexedBlock": price.block_number, "liquidityUsd": str(price.liquidity_usd),
        "paidJournalId": original["id"], "cachedJournalId": cached[0]["id"],
        "cachedRepeatRecorded": True, "recoveredFromSavedResponseAndJournal": True,
        "spend": spend, "proof": proof, "balanceBeforeUsdc": plan["balanceUsdc"],
        "readingStatus": "Previously purchased reading; no new query was made."}
    return save_completed_check(verification, result["transactionHash"])


def main():
    global STATE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "status"))
    parser.add_argument("--video-demo", action="store_true",
                        help="One separate recording session; preserves the original completed check.")
    args = parser.parse_args()
    payer = payer_address()
    if args.video_demo:
        STATE = video_demo_state(STATE, payer)
    ensure_private_directory(STATE)
    if args.action == "status":
        result = json.loads((STATE / "result.json").read_text())
        proof = verify_receipt(result["transactionHash"], payer)
        verification = (json.loads((STATE / "verification.json").read_text())
            if (STATE / "verification.json").exists() else recover_completed_check(result, proof))
        print(json.dumps({"proof": proof, "payerCallsThisRun": 0,
                          "verification": verification}, indent=2))
        print("View transaction: https://basescan.org/tx/" + result["transactionHash"], flush=True)
        return

    if (STATE / "attempt.json").exists():
        raise ValueError("A payment attempt already exists. Use status; never create a replacement attempt.")
    validate_quote(*_urllib_402(URL))
    print("HTTP 402 Payment Required: The Graph requests 0.01 USDC on Base for this query.", flush=True)
    historic = verify_receipt(HISTORICAL_TX)
    available = balance(payer)
    if available < int(AMOUNT):
        raise ValueError("The operator account has insufficient USDC.")
    plan = {"product": "One WETH/USDC price query, Uniswap V3 on Base via The Graph",
        "resourceUrl": URL, "amountUsdc": "0.01", "network": "Base (8453)",
        "asset": USDC, "receiver": RECEIVER, "payer": payer,
        "balanceUsdc": str(Decimal(available) / 1_000_000), "maxPayerCalls": 1,
        "historicalMerchantReceiptVerified": historic["verified"]}
    if args.action == "prepare":
        atomic_write_private_json(STATE / "plan.json", plan)
        print(json.dumps(plan, indent=2))
        return
    approved = json.loads((STATE / "plan.json").read_text())
    if any(approved.get(key) != plan[key] for key in ("resourceUrl", "amountUsdc", "network", "asset", "receiver", "payer")):
        raise ValueError("Prepared terms changed; do not pay.")

    memory_policy = policy()
    # Import only a verified historical settlement, never a fictional payment
    # to make the policy pass. It belongs to a separate historical owner.
    if memory_policy.memory.recall_merchant("gateway.thegraph.com") is None:
        memory_policy.memory.remember_settlement(Payment(merchant="gateway.thegraph.com",
            pay_to=RECEIVER, amount_usd=Decimal("0.01"), owner="verified-graph-history"), tx_id=HISTORICAL_TX)
    calls, quotes = [], []
    payer_client = CdpBaseX402PaymentClient(ROOT / "cdp-x402-service")

    def quote(url):
        quotes.append(url)
        response = _urllib_402(url)
        validate_quote(*response)
        print("The Graph: unpaid request returned HTTP 402; payment terms verified.", flush=True)
        return response

    def pay(url, **kwargs):
        if (url != URL or kwargs.get("max_atomic") != AMOUNT
                or kwargs.get("expected_receiver", "").lower() != RECEIVER.lower()
                or kwargs.get("expected_asset", "").lower() != USDC.lower()
                or kwargs.get("method") != "POST"):
            raise ValueError("Unexpected payer arguments.")
        # Atomic and permanent: even a crash, timeout, or TTL expiry cannot
        # turn another invocation of this verification script into a payment.
        fd = os.open(STATE / "attempt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"startedAt": datetime.now(timezone.utc).isoformat(), "amountUsdc": "0.01"}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        calls.append(url)
        print("Paying 0.01 USDC through x402 and retrying the GraphQL POST...", flush=True)
        result = payer_client(url, **kwargs)
        # Persist only public query data and receipt identifiers. Never store
        # payment headers, signatures, or a raw subprocess response.
        safe = {key: result.get(key) for key in ("ok", "status", "payer", "transactionHash", "body")}
        atomic_write_private_json(STATE / "result.json", safe)
        if not safe["ok"] or safe["status"] != 200 or not safe["transactionHash"] or str(safe["payer"]).lower() != payer.lower():
            raise ValueError("Payment outcome needs inspection; do not retry.")
        print("HTTP 200: paid GraphQL data received. Transaction: " + safe["transactionHash"], flush=True)
        return safe

    client = build_onchain_data_from_env(policy=memory_policy, pay=pay, fetch_402=quote, env=ENV)
    print("Question: price of WETH", flush=True)
    first = client.price(OWNER, "WETH")
    print(f"Answer: {first.usd} USDC/WETH; indexed Base block {first.block_number}.", flush=True)
    # Reopen the database, rebuild the client and enter the actual chat branch.
    reopened = policy()
    chat = VeniceChatClient.__new__(VeniceChatClient)
    chat.onchain_data = build_onchain_data_from_env(policy=reopened, pay=pay, fetch_402=quote, env=ENV)
    repeated = chat._onchain_footnote(OWNER, "price of WETH")
    if not first.paid or not repeated or "nothing paid" not in repeated[1] or len(calls) != 1 or len(quotes) != 1:
        raise ValueError("The query/cache check did not pass; inspect saved result, never pay again.")
    print("Repeated question after reopening storage: cached answer, 0 USDC, no second payment.", flush=True)
    result = json.loads((STATE / "result.json").read_text())
    entries = reopened.memory.journal(limit=100)
    original = next(entry for entry in entries if entry["id"] == first.journal_id)
    if original["extra"].get("tx_id") != result["transactionHash"]:
        raise ValueError("The journal lost the transaction receipt.")
    cached = next(entry for entry in entries if entry.get("extra", {}).get("served_from") == first.journal_id)
    spend = PaidGraphQueries(reopened, owner=OWNER).spent_on_data()
    if reopened.memory.spent_today(OWNER) != Decimal("0.01") or spend["queries_paid"] != 1 or spend["queries_from_memory"] != 1:
        raise ValueError("Unexpected accounting after the cached request.")
    proof = verify_receipt(result["transactionHash"], payer)
    print("Verified payment: https://basescan.org/tx/" + result["transactionHash"], flush=True)
    verification = {"priceUsdc": str(first.usd), "pool": first.pool, "indexedBlock": first.block_number,
        "liquidityUsd": str(first.liquidity_usd), "paidJournalId": first.journal_id,
        "cachedJournalId": cached["id"], "payerCalls": len(calls), "quoteCalls": len(quotes),
        "restartCachePassed": True, "spend": spend, "proof": proof, "chatFact": repeated[0],
        "chatFooter": repeated[1], "balanceBeforeUsdc": plan["balanceUsdc"]}
    save_completed_check(verification, result["transactionHash"])
    print(json.dumps(verification, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # External errors can contain secrets. Do not print their raw text.
        sys.exit(f"Graph verification stopped ({type(exc).__name__}). Inspect saved state; do not repeat a payment.")
