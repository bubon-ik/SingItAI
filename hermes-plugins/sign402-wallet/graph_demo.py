"""Private ETHOnline command; existing wallet and chat routes remain unchanged."""
import hashlib
import json
import os
import time
import uuid
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def call_demo(path, payload):
    # Only the SSH loopback forward is permitted; never forward credentials elsewhere.
    port = int(os.environ.get("SIGN402_GRAPH_DEMO_PORT", "8107"))
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid demo port")
    token = os.environ.get("SIGN402_GRAPH_DEMO_TOKEN", "")
    if len(token) < 32:
        raise ValueError("Demo not configured")
    request = Request(f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with build_opener(NoRedirects()).open(request, timeout=15) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise ValueError("Demo response too large")
    result = json.loads(data)
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise ValueError("Invalid demo response")
    return result


def handle_graph_demo(*, event, source, gateway, send, background):
    text = str(getattr(event, "text", "") or "").strip()
    words = text.split()
    if not words or words[0].split("@", 1)[0].lower() != "/graph_demo":
        return None
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    owner = str(os.environ.get("SIGN402_GRAPH_DEMO_OWNER", ""))
    user = str(getattr(source, "user_id", ""))
    chat = str(getattr(source, "chat_id", ""))
    if platform != "telegram" or not owner or user != owner or chat != owner:
        return {"action": "skip", "reason": "private-graph-demo"}
    if len(words) > 2 or (len(words) == 2 and words[1].lower() != "status"):
        send(gateway, source, "Use /graph_demo for WETH data or /graph_demo status for the saved result.")
        return {"action": "skip", "reason": "private-graph-demo"}
    status_only = len(words) == 2
    message_id = str(getattr(event, "message_id", "") or getattr(event, "id", "") or uuid.uuid4())
    request_id = "tg-graph-" + hashlib.sha256(f"{chat}:{message_id}".encode()).hexdigest()[:32]
    send(gateway, source, "Loading the saved Graph result…" if status_only else
         "Requesting WETH/USDC from The Graph. Maximum cost: 0.01 USDC on Base; a new payment requires Ledger approval.")

    def work():
        last = None
        try:
            payload = {"owner": owner}
            if not status_only:
                payload["requestId"] = request_id
            result = call_demo("/status" if status_only else "/start", payload)
            deadline = time.monotonic() + 240
            while True:
                marker = (result.get("status"), result["text"])
                if marker != last:
                    send(gateway, source, result["text"])
                    last = marker
                if result.get("status") not in {"preparing", "pending", "executing"} or status_only:
                    return
                if time.monotonic() >= deadline:
                    send(gateway, source, "Still waiting. Use /graph_demo status; do not create a replacement payment.")
                    return
                time.sleep(1)
                result = call_demo("/status", {"owner": owner, "requestId": result["requestId"]})
        except Exception:
            send(gateway, source, "Demo connection interrupted. Use /graph_demo status to check the existing operation.")
    background(work)
    return {"action": "skip", "reason": "private-graph-demo"}
