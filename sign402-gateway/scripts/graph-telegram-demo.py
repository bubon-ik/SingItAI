#!/usr/bin/env python3
"""Local Ledger + Graph service for the owner's private Telegram demo command."""
import argparse
from contextlib import closing
import fcntl
import hmac
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sign402-gateway"))

from sign402_gateway.graph_ledger_demo import GraphLedgerDemo
from sign402_gateway.onchain_data import _urllib_402
from sign402_gateway.secure_state import SensitiveStateCipher, ensure_private_directory
from sign402_gateway.server import CdpBaseX402PaymentClient


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env_value(path, name):
    for raw in reversed(path.read_text().splitlines()):
        if raw.startswith(name + "="):
            return raw.split("=", 1)[1].strip().strip('"').strip("'")
    raise ValueError("Required local configuration is missing.")


def recording_state(base, owner, cipher, payer, verify):
    """Select one fixed extra recording session after reconciling the original.

    Never clear either session's cache, attempt marker or encrypted operations.
    Repeated starts select the same directory and cannot buy another paid slot.
    """
    marker = json.loads((base / "payment-attempt.json").read_text())
    database = (base / "operations.sqlite3").resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT owner,request_id,state,payload FROM ledger_operations").fetchall()
    if any(row[2] in {"preparing", "pending", "executing", "uncertain"} for row in rows):
        raise ValueError("Original demo has an unresolved operation.")
    matching = [row for row in rows if row[0] == owner and row[1] == marker["requestId"]]
    if len(matching) != 1:
        raise ValueError("Original payment must belong to the recording owner.")
    saved_owner, request_id, status, payload = matching[0]
    operation = cipher.decrypt_json(payload)
    if (status != "succeeded" or operation.get("status") != status
            or operation.get("owner") != saved_owner or operation.get("requestId") != request_id
            or operation.get("result", {}).get("paid") is not True):
        raise ValueError("Original payment must have a completed saved result.")
    tx = operation["result"]["transactionHash"]
    proof = verify(tx, payer)
    if proof.get("verified") is not True or proof.get("transactionHash") != tx:
        raise ValueError("Original transaction did not verify.")
    return base / "recording", proof


def handler_for(service, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404, {"ok": self.path == "/health"})

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.reply(401, {"error": "Unauthorised"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict) or set(payload) - {"owner", "requestId"}:
                    raise ValueError("Invalid request")
                owner = str(payload.get("owner", ""))
                if self.path == "/start":
                    data = service.start(owner, payload.get("requestId"))
                elif self.path == "/status":
                    data = service.status(owner, payload["requestId"]) if payload.get("requestId") else service.latest(owner)
                else:
                    self.reply(404, {"error": "Unknown route"})
                    return
                self.reply(200, data)
            except PermissionError:
                self.reply(403, {"error": "Owner only"})
            except Exception:
                self.reply(409, {"error": "Read the existing demo status; no automatic payment retry."})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--approver", required=True)
    parser.add_argument("--port", type=int, default=8117)
    parser.add_argument("--recording", action="store_true",
        help="Use one separately authorised recording session after the original payment verifies")
    args = parser.parse_args()
    state = ROOT / ".graph-live/telegram-demo"
    ensure_private_directory(state)
    lock_file = open(state / "service.lock", "a")
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token_path = state / "bridge-token"
    if not token_path.exists():
        fd = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    token = token_path.read_text().strip()
    if len(token) < 32:
        raise ValueError("Invalid demo bridge token.")
    graph = load_module("graph_verification", ROOT / "sign402-gateway/scripts/graph-live-check.py")
    signer = load_module("ledger_demo_signer", ROOT / "tools/ledger-approve/purchase.py")
    payer_address = graph.payer_address()
    cipher = SensitiveStateCipher(env_value(ROOT / "sign402-gateway/.env.wallet-bitrefill", "SIGN402_WALLET_MASTER_KEY"))
    if args.recording:
        state, proof = recording_state(state, args.owner, cipher, payer_address, graph.verify_receipt)
    else:
        previous = json.loads((ROOT / ".graph-live/video-demo/result.json").read_text())
        proof = graph.verify_receipt(previous["transactionHash"], payer_address)
    service = GraphLedgerDemo(owner=args.owner, approver=args.approver, payer_address=payer_address,
        state=state, cipher=cipher,
        quote=_urllib_402, payer=CdpBaseX402PaymentClient(ROOT / "cdp-x402-service"),
        signer=signer.sign_pending, receipt=graph.verify_receipt, historical_proof=proof)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(service, token)) as server:
        print(f"Telegram Graph + Ledger demo ready on 127.0.0.1:{args.port}; owner {args.owner}.", flush=True)
        print("Session: " + ("recording" if args.recording else "original"), flush=True)
        print("One paid query maximum: 0.01 USDC, requiring Ledger approval. Waiting for /graph_demo.", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        sys.exit("Demo service stopped: " + type(exc).__name__)
