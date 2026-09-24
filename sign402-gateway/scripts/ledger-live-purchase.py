#!/usr/bin/env python3
"""One real Otto news purchase through Ledger's HTTP approval lifecycle.

prepare/status never call a payer. approve can spend exactly 0.001 USDC from
the existing operator CDP account after a valid Ledger signature. This checks
the real Ledger gate with the operator payer; it does not provision a customer
wallet or modify production. State survives separate invocations and retries.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import threading
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sign402-gateway"))
sys.path.insert(0, str(ROOT / "tools" / "ledger-approve"))

from spending_memory import SpendingMemory, SpendingPolicy
from sign402_gateway import server as gateway
from sign402_gateway.ledger_payments import LedgerConfig, LedgerOperationStore, LedgerPayments
from sign402_gateway.secure_state import SensitiveStateCipher, atomic_write_private_json, ensure_private_directory
from sign402_gateway.user_wallets import UserWalletStore
import purchase

URL = "https://x402.ottoai.services/crypto-news"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RECEIVER = "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808"
AMOUNT = "1000"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def env_value(path, name):
    for raw in reversed(path.read_text().splitlines()):
        if raw.startswith(name + "="):
            return raw.split("=", 1)[1].strip().strip('"').strip("'")
    raise ValueError(f"Configure {name} locally before running this check.")


def validate_terms(requirements):
    if (str(requirements.get("amountAtomic")) != AMOUNT or
            str(requirements.get("receiver", "")).lower() != RECEIVER.lower() or
            str(requirements.get("asset", "")).lower() != USDC.lower() or
            requirements.get("x402Network") != "eip155:8453" or
            requirements.get("scheme") != "exact"):
        raise ValueError("The live quote differs from the fixed 0.001 USDC purchase. Do not pay.")


def verify_receipt(receipt, payer):
    if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
        raise ValueError("No successful onchain receipt yet; use status with the same request ID.")
    matches = []
    for event in receipt.get("logs", []):
        topics = event.get("topics", [])
        if event.get("address", "").lower() == USDC.lower() and len(topics) == 3 and topics[0].lower() == TRANSFER_TOPIC:
            if (topics[1][-40:].lower() == payer[2:].lower() and
                    topics[2][-40:].lower() == RECEIVER[2:].lower() and int(event["data"], 16) == int(AMOUNT)):
                matches.append(event)
    if len(matches) != 1:
        raise ValueError("The receipt does not contain exactly one matching USDC transfer.")
    return {"verified": True, "transactionHash": receipt["transactionHash"],
            "blockNumber": int(receipt["blockNumber"], 16), "amountUsdc": "0.001"}


def read_receipt(tx_id):
    # The public RPC failed through this host's urllib transport after the
    # payment succeeded. curl is also used by the operator's independent
    # receipt check; this call can only read an existing transaction.
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_id]}
    result = subprocess.run([
        "curl", "--fail", "--silent", "--show-error", "--max-time", "20",
        "https://mainnet.base.org", "-H", "Content-Type: application/json", "--data-binary", "@-",
    ], input=json.dumps(payload), capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise ValueError("Payment is saved, but the public RPC receipt check is unavailable")
    response = json.loads(result.stdout)
    if response.get("id") != 1 or response.get("error") or not response.get("result"):
        raise ValueError("Payment is saved; its receipt is not available yet")
    receipt = response["result"]
    if str(receipt.get("transactionHash", "")).lower() != tx_id.lower():
        raise ValueError("RPC returned a different transaction")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "approve", "status", "cancel"))
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--approver", required=True)
    parser.add_argument("--path", default="44'/60'/0'/0/0")
    args = parser.parse_args()
    if not args.owner.isdecimal():
        raise ValueError("Use the canonical numeric Telegram owner ID.")
    config = LedgerConfig.from_env({"SIGN402_LEDGER_APPROVAL_ENABLED": "1",
        "SIGN402_LEDGER_OWNER_ID": args.owner, "SIGN402_LEDGER_APPROVER_ADDRESSES": args.approver})
    payer_address = env_value(ROOT / "cdp-x402-service/.env", "CDP_EVM_ACCOUNT_ADDRESS")
    cipher = SensitiveStateCipher(env_value(ROOT / "sign402-gateway/.env.wallet-bitrefill", "SIGN402_WALLET_MASTER_KEY"))
    state = ROOT / ".ledger-live"
    ensure_private_directory(state)
    routes = {"/agent/buy-tool", "/agent/ledger-approve", "/agent/ledger-status", "/agent/ledger-cancel"}
    with ExitStack() as stack:
        class Handler(gateway.Sign402GatewayHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                if self.path not in routes:
                    self._send_json({"ok": False}, status=404)
                    return
                super().do_POST()

        server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        server.user_wallet_api_token = secrets.token_urlsafe(32)
        server.user_wallet_service = UserWalletStore(state / "auth.sqlite3")
        user_token = server.user_wallet_service.issue_access_token(args.owner)
        server.user_event_store = gateway.UserPurchaseStore(state / "results.json", cipher=cipher)
        server.user_spend_limit_store = gateway.UserSpendLimitStore(state / "limits.json")
        # Below one atomic USDC: every positive purchase must ask the Ledger,
        # including familiar merchants. The hard wallet cap is 0.001 USDC/day.
        server.spending_policy = SpendingPolicy(SpendingMemory.local(str(state / "memory.sqlite3")), daily_cap_usd=Decimal("0.0000001"))
        server.spending_memory_holds = {}
        payer = gateway.CdpBaseX402PaymentClient(ROOT / "cdp-x402-service")
        sent = []

        def inspect(owner, intent):
            if intent["resourceUrl"] != URL or intent["tool"].get("requestBody"):
                raise ValueError("This live check only supports the fixed Otto news request.")
            requirements = gateway.normalize_x402_payment_required(gateway.fetch_x402_payment_required(URL), resource_url=URL)
            validate_terms(requirements)
            return requirements, gateway._payment_from_requirements(requirements, owner=owner, resource_url=URL)

        def reserve(owner, requirements):
            validate_terms(requirements)
            server.user_event_store.preflight_write()
            hold = server.user_spend_limit_store.reserve_within_limits(
                owner, amount_atomic=int(AMOUNT), asset=USDC, network="base-mainnet",
                max_per_tx_atomic=int(AMOUNT), daily_cap_atomic=int(AMOUNT))
            if not hold:
                raise ValueError("The dedicated live-check budget is exhausted.")
            return hold

        def pay(owner, intent, requirements, approval):
            if args.action != "approve" or approval.get("source") != "ledger":
                raise ValueError("Only an explicit approve command with a Ledger signature may spend.")
            validate_terms(requirements)
            if not approval.get("approvedBy") or len(sent):
                raise ValueError("Refusing an unapproved or repeated payer call.")
            sent.append(args.request_id)
            result = payer(URL, max_atomic=AMOUNT, expected_receiver=RECEIVER, expected_asset=USDC)
            if result.get("status") != 200 or not result.get("ok") or not result.get("transactionHash"):
                raise ValueError("Live payment did not return both the resource and a transaction receipt.")
            if str(result.get("payer", "")).lower() != payer_address.lower():
                raise ValueError("Unexpected payer address; reconcile the stored operation.")
            return {"ok": True, "txId": result["transactionHash"], "amountAtomic": AMOUNT,
                    "asset": USDC, "network": "base-mainnet", "receiver": RECEIVER,
                    "payer": payer_address, "approvalId": approval["approvalId"],
                    "resourceResult": result, "paymentResponse": result.get("paymentResponse"),
                    "resourceUrl": URL, "telegramText": "Otto news access purchased through Ledger approval."}

        def settle(owner, reservation, intent, requirements, result, payment, claim):
            gateway._settle_user_wallet_spend(server, reservation, intent["tool"], URL, requirements,
                                             result, payment=payment, claim_id=claim)
            server.user_event_store.write(owner, result)

        server.ledger_payments = LedgerPayments(config, LedgerOperationStore(state / "operations.sqlite3", cipher),
            policy=lambda: server.spending_policy, inspect=inspect, reserve=reserve,
            release=server.user_spend_limit_store.release_reservation, pay=pay, settle=settle,
            paused=gateway._purchases_paused)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join)
        stack.callback(server.shutdown)
        client = purchase.Gateway(f"http://127.0.0.1:{server.server_port}", args.owner,
                                  server.user_wallet_api_token, user_token)
        if args.action == "prepare":
            result = client.post("/agent/buy-tool", {"requestId": args.request_id, "tool": "news"})
        else:
            result = purchase.run(client, args.action, args.request_id, device_path=args.path)
        if result.get("status") == "succeeded":
            # The second approve is a real HTTP retry; it must return the
            # stored response without touching either Ledger or the payer.
            duplicate = purchase.run(client, "approve", args.request_id)
            if duplicate != result:
                raise ValueError("Retry did not return the stored result.")
            duplicate_buy = client.post("/agent/buy-tool", {"requestId": args.request_id, "tool": "news"})
            if duplicate_buy != result:
                raise ValueError("Buy retry did not return the stored result.")
            receipt = read_receipt(result["txId"])
            proof = verify_receipt(receipt, payer_address)
            record_path = state / "purchase-record.json"
            if not record_path.exists():
                atomic_write_private_json(record_path, {
                    "invoice_id": result["txId"], "product_slug": "otto.crypto-news",
                    "amount": "0.001 USDC", "payment_method": "USDC on Base",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            resource = result["resourceResult"]
            body = resource.get("body", {})
            data = body.get("data", {}) if isinstance(body, dict) else {}
            summary = {"requestId": args.request_id, "status": "succeeded", "proof": proof,
                       "payer": payer_address, "receiver": RECEIVER, "httpStatus": resource["status"],
                       "payerCallsThisRun": len(sent), "retryReturnedStoredResult": True,
                       "buyRetryReturnedStoredResult": True,
                       "memorySpentUsdc": str(server.spending_policy.memory.spent_today(args.owner)),
                       "dataStatus": body.get("status") if isinstance(body, dict) else None,
                       "data": data, "meta": body.get("meta") if isinstance(body, dict) else None}
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"payer": payer_address, "payerCallsThisRun": len(sent), **result}, ensure_ascii=False, indent=2))
        if result.get("status") not in {"pending", "succeeded", "cancelled"}:
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("Interrupted. Read status with the same request ID before retrying.")
    except Exception as exc:
        # External exceptions can contain credentials or payment data.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        sys.exit(message + ". Read status with the same request ID; never create a replacement purchase.")
