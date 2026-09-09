#!/usr/bin/env python3
"""Buy a GET x402 tool through SingIt, signing an escalation on a local Ledger.

Only Python's standard library is required. API tokens come from environment
variables, never command arguments. Signatures stay in memory. Remote gateways
require HTTPS; plain HTTP is allowed only on loopback (including SSH tunnels).
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Neither credentials nor a payment signature may follow a redirect.
        return None


class Gateway:
    def __init__(self, url, owner, wallet_token, user_token):
        parsed = urlparse(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Use the gateway origin only, without credentials, paths or query parameters.")
        if not parsed.hostname or (parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})):
            raise ValueError("Use HTTPS or a loopback SSH tunnel for the gateway.")
        if not owner or not wallet_token or not user_token:
            raise ValueError("Set SIGN402_LEDGER_OWNER_ID, SIGN402_WALLET_API_TOKEN and SIGN402_USER_ACCESS_TOKEN.")
        self.url, self.owner = url.rstrip("/"), owner
        self.headers = {"Content-Type": "application/json", "Authorization": "Bearer " + wallet_token,
                        "X-Sign402-User-Token": user_token}
        self.opener = build_opener(NoRedirects())

    def post(self, path, payload):
        request = Request(self.url + path, data=json.dumps({"telegramUserId": self.owner, **payload}).encode(),
                          headers=self.headers, method="POST")
        try:
            response = self.opener.open(request, timeout=90)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ValueError("Gateway redirects are refused.") from None
            response = exc
        with response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Gateway response is too large.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Gateway response must be an object.")
        return result


def sign_pending(pending, owner, *, device_path="44'/60'/0'/0/0"):
    approval = pending["approval"]
    msg, domain = approval["message"], approval["domain"]
    if (msg["owner"] != owner or domain != {"name": "SingIt Spending Approval", "version": "2", "chainId": 8453}
            or approval.get("signingMethod") != "personal_sign" or msg["expiresAt"] != pending["expiresAt"]
            or not isinstance(approval.get("displayText"), str) or not approval["displayText"]):
        raise ValueError("The approval owner, domain or expiry does not match this client.")
    remaining = int(msg["expiresAt"]) - int(time.time())
    if remaining <= 0:
        raise ValueError("The approval expired. No signature was requested.")
    print(f"Review on Ledger: {msg['purchase']}; {msg['amountUsd']} USDC on Base to {msg['payTo']}", file=sys.stderr)
    completed = subprocess.run(
        ["node", str(Path(__file__).with_name("approve.cjs")), "--path", device_path,
         "--timeout-seconds", str(min(remaining, 180))],
        input=json.dumps(approval), text=True, stdout=subprocess.PIPE, timeout=min(remaining, 180) + 5,
        check=False,
    )
    signature = completed.stdout.strip()
    if completed.returncode != 0 or not signature.startswith("0x") or len(signature) != 132:
        raise ValueError("Ledger did not produce a signature. Nothing was submitted.")
    return {"signingMethod": "personal_sign", "signature": signature, "expiresAt": msg["expiresAt"], "journalId": msg["journalId"]}


def run(client, action, request_id, *, tool="news", device_path="44'/60'/0'/0/0"):
    if action == "buy":
        result = client.post("/agent/buy-tool", {"requestId": request_id, "tool": tool})
    else:
        endpoint = "cancel" if action == "cancel" else "status"
        result = client.post("/agent/ledger-" + endpoint, {"requestId": request_id})
    if action in {"buy", "approve"} and result.get("status") == "pending":
        if result.get("requestId") != request_id:
            raise ValueError("The gateway returned another operation.")
        signature = None
        if result.get("decision") == "needs_ledger_approval":
            signature = sign_pending(result, client.owner, device_path=device_path)
        elif result.get("decision") != "ready":
            raise ValueError("Unrecognised approval state.")
        result = client.post("/agent/ledger-approve", {"requestId": request_id, "ledgerApproval": signature})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("buy", "approve", "status", "cancel"))
    parser.add_argument("--request-id")
    parser.add_argument("--tool", default="news")
    parser.add_argument("--gateway", default=os.getenv("SIGN402_GATEWAY_URL", "http://127.0.0.1:8099"))
    parser.add_argument("--path", default="44'/60'/0'/0/0")
    args = parser.parse_args()
    if args.action != "buy" and not args.request_id:
        parser.error("--request-id is required for approve, status and cancel")
    request_id = args.request_id or str(uuid.uuid4())
    print(f"Request: {request_id}. Keep this ID; use status after any interruption.", file=sys.stderr)
    try:
        client = Gateway(args.gateway, os.getenv("SIGN402_LEDGER_OWNER_ID", ""),
                         os.getenv("SIGN402_WALLET_API_TOKEN", ""), os.getenv("SIGN402_USER_ACCESS_TOKEN", ""))
        result = run(client, args.action, request_id, tool=args.tool, device_path=args.path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"succeeded", "pending", "cancelled"} else 1
    except KeyboardInterrupt:
        print("Interrupted. Read status with this request ID before doing anything else.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Never dump a subprocess (it contains a signature) or request headers.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"{message}. Use status --request-id {request_id}; do not create a replacement purchase.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
