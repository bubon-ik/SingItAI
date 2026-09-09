#!/usr/bin/env python3
"""Exercise the real HTTP approval flow with a Ledger and a test payer.

Run with the gateway dependencies installed and the Ethereum app open:
    python sign402-gateway/scripts/ledger-approval-rehearsal.py

All state, tokens and the encryption key are temporary. The quote and payer
are local doubles: this script cannot transfer money or access a real wallet.
It uses the production HTTP handler, policy, approval store, budget store and
local client. --software-signer is a device-free check of this rehearsal only.
"""
from __future__ import annotations

import argparse
import copy
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sign402-gateway"))
sys.path.insert(0, str(ROOT / "tools" / "ledger-approve"))

from cryptography.fernet import Fernet
from spending_memory import SpendingMemory, SpendingPolicy
from sign402_gateway import server as gateway
from sign402_gateway.ledger_payments import LedgerConfig
import purchase

OWNER = "ledger-rehearsal"
REQUIREMENTS = {
    "scheme": "exact", "network": "base-mainnet", "x402Network": "eip155:8453",
    "amountAtomic": "1000", "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "receiver": "0x0000000000000000000000000000000000000001",
    "paymentIntent": "ledger-rehearsal", "purpose": "x402_api_access",
    "extra": {"name": "USD Coin", "version": "2"},
}


def check(condition, message):
    if not condition:
        raise RuntimeError(message)
    print("PASS: " + message, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default="44'/60'/0'/0/0")
    parser.add_argument("--software-signer", action="store_true")
    args = parser.parse_args()
    print("TEST PAYER ONLY. No USDC will be transferred; no real wallet is loaded.", flush=True)
    with ExitStack() as stack:
        if args.software_signer:
            from eth_account import Account
            from eth_account.messages import encode_defunct
            key = Account.create()
            address = key.address
            def software_sign(pending, owner, **kwargs):
                msg = pending["approval"]["message"]
                return {"signingMethod": "personal_sign", "signature": key.sign_message(encode_defunct(text=pending["approval"]["displayText"])).signature.hex(),
                        "expiresAt": msg["expiresAt"], "journalId": msg["journalId"]}
            stack.enter_context(patch.object(purchase, "sign_pending", side_effect=software_sign))
        else:
            result = subprocess.run(["node", str(ROOT / "tools/ledger-approve/approve.cjs"),
                                     "--address", "--path", args.path],
                                    stdout=subprocess.PIPE, text=True, timeout=185)
            address = result.stdout.strip()
            if result.returncode or not LedgerConfig.from_env({
                "SIGN402_LEDGER_APPROVAL_ENABLED": "1", "SIGN402_LEDGER_OWNER_ID": OWNER,
                "SIGN402_LEDGER_APPROVER_ADDRESSES": address,
            }):
                raise RuntimeError("Could not read the Ledger address.")
        print("Approver read before preparing the operation: " + address, flush=True)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="ledger-http-rehearsal-")))
        stack.enter_context(patch.dict("os.environ", {
            "SIGN402_WALLET_MASTER_KEY": Fernet.generate_key().decode(),
            "SIGN402_PURCHASES_PAUSED": "0", "SIGN402_USER_PURCHASES_PER_HOUR": "0",
            "SIGN402_USER_REQUESTS_PER_MINUTE": "0", "SIGN402_USER_WALLET_MAX_ATOMIC_PER_TX": "10000",
            "SIGN402_USER_WALLET_DAILY_ATOMIC_CAP": "100000",
        }))
        tool = {**gateway.PAID_TOOLS["otto.crypto_news"], "resourceUrl": "https://ledger-rehearsal.invalid/news"}
        stack.enter_context(patch.dict(gateway.PAID_TOOLS, {"otto.crypto_news": tool}))
        stack.enter_context(patch.object(gateway, "fetch_x402_payment_required", return_value={"accepts": [{}]}))
        stack.enter_context(patch.object(gateway, "normalize_x402_payment_required", side_effect=lambda *a, **k: copy.deepcopy(REQUIREMENTS)))

        class QuietHandler(gateway.Sign402GatewayHandler):
            def log_message(self, *args):
                pass

        server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler))
        server.user_wallet_api_token = "rehearsal-gateway-token"
        server.user_wallet_service = Mock()
        server.user_wallet_service.resolve_telegram_user_id.side_effect = lambda token: OWNER if token == "rehearsal-user-token" else "wrong-owner"
        server.user_wallet_service.decrypt_private_key_for_future_signing.return_value = "NOT_A_PRIVATE_KEY"
        server.user_event_store = Mock()
        server.spending_memory_holds = {}
        server.spending_policy = SpendingPolicy(SpendingMemory.local(str(root / "memory.db")), daily_cap_usd=Decimal("5"))
        server.user_spend_limit_store = gateway.UserSpendLimitStore(root / "limits.json")
        server.user_x402_buyer = Mock(return_value={
            "ok": True, "txId": "TEST_PAYER_NO_TRANSACTION", "amountAtomic": "1000",
            "asset": REQUIREMENTS["asset"], "network": REQUIREMENTS["network"],
            "telegramText": "Rehearsal result. No money was transferred.",
            "resourceResult": {"status": 200, "body": {"rehearsal": True}},
        })
        config = LedgerConfig(OWNER, frozenset({address.lower()}))
        state_path = root / "ledger" / "operations.db"
        server.ledger_payments = gateway.build_ledger_payments(server, config, path=state_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join)
        stack.callback(server.shutdown)
        client = purchase.Gateway(f"http://127.0.0.1:{server.server_port}", OWNER,
                                  "rehearsal-gateway-token", "rehearsal-user-token")
        request_id = "ledger-http-rehearsal"
        pending = client.post("/agent/buy-tool", {"tool": "news", "requestId": request_id})
        check(pending.get("status") == "pending", "real policy escalates before any payer call")
        check(server.user_x402_buyer.call_count == 0, "no payer call while waiting for the device")
        check(client.post("/agent/buy-tool", {"tool": "news", "requestId": request_id}) == pending,
              "retry returns the same journal ID and challenge")
        server.ledger_payments = gateway.build_ledger_payments(server, config, path=state_path)
        result = purchase.run(client, "approve", request_id, device_path=args.path)
        check(result.get("status") == "succeeded", "local client signs; HTTP gateway verifies and runs the test payer")
        server.ledger_payments = gateway.build_ledger_payments(server, config, path=state_path)
        check(purchase.run(client, "approve", request_id) == result, "completed approval survives reopening without another signature")
        check(purchase.run(client, "buy", request_id) == result, "duplicate buy returns the original result")
        check(server.user_x402_buyer.call_count == 1, "exactly one test payer call")
        check(server.spending_policy.memory.spent_today(OWNER) == Decimal("0.001"), "spending memory accounts for the operation once")
        server.user_event_store.write.assert_called_once()
        print("COMPLETE: " + ("software" if args.software_signer else "hardware") + " HTTP rehearsal passed. Nothing was spent.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Rehearsal interrupted. Nothing was spent.")
    except Exception as exc:
        sys.exit("FAIL: " + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__))
