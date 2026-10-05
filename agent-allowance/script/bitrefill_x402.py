"""Buy one Bitrefill product through x402: the agent pays, the limiter funds it.

Design: docs/trezor-allowance-v1.md, "x402: the agent pays, the limiter funds it",
check T6. Run with the sidecar's Python, which has eth_account:

    ../trezor-sidecar/.venv/bin/python script/bitrefill_x402.py <slug> "<package value>"
    ../trezor-sidecar/.venv/bin/python script/bitrefill_x402.py <slug> "<package value>" --dry-run

The limiter is $LIMITER if set, else the one `t6-bitrefill.sh setup <cap>`
deployed (t6.env), else the T4 limiter (t4.env). It must already hold a grant.
The agent is the T4 agent.

Order of events, and why:

1. Unlock the agent key (password asked once) and sign in to Bitrefill with it.
   Sign-in is free and proves nothing but control of the key.
2. Read the product and its price; check the limiter could fund it.
3. Show the product, package, price, network and payment route, and wait for an
   explicit y. Nothing is created before this (AGENTS.md).
4. Create the invoice. If its price is above what was confirmed, ask again.
5. Read the payment terms from the pay route's 402. The recipient must be
   Bitrefill's published x402 address, and the amount no more than confirmed.
6. `spend` exactly that amount from the Trezor address to the agent.
7. Pay with the agent key through cdp-x402-service `buy-user`, which refuses to
   sign for any other amount, recipient or asset.
8. Find the settlement on chain: a USDC Transfer agent -> recipient of exactly
   the amount. The seller's word is not evidence.
9. Wait for delivery and show the code in this terminal only.

Codes are bearer value. They are never written to a file or a log; the log
gets the invoice id, product, amount, payment method and time, as AGENTS.md
requires, and nothing else from the invoice.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SKILL_SCRIPTS = REPO / ".agents" / "skills" / "bitrefill" / "scripts"
CDP_X402_DIR = Path(os.environ.get("CDP_X402_DIR", Path.home() / "Documents" / "Berlin Hack" / "cdp-x402-service"))
STATE_DIR = Path(os.environ.get("STATE_DIR", Path.home() / ".sign402-trezor-poc"))
KEYSTORES = Path(os.environ.get("KEYSTORES", Path.home() / ".foundry" / "keystores"))
RPC = os.environ.get("RPC", "https://mainnet.base.org")
LOG = STATE_DIR / "t6-log.md"

API = "https://api.bitrefill.com/x402"
# Cloudflare in front of the API refuses Python's default User-Agent (1010).
UA = "sign402-trezor-poc/0.1"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NETWORK = "eip155:8453"
# Published in the Bitrefill agent skill (references/touchpoints/x402.md). A pay
# route asking for any other recipient is refused, not trusted.
BITREFILL_PAY_TO = "0x480CD46E6faDe651a0437DeaddA53D5c8e7D846A"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class Stop(Exception):
    """A refusal with a message for the owner. Nothing after it runs."""


def say(text: str = "") -> None:
    print(text, flush=True)


def log(text: str) -> None:
    with open(LOG, "a") as f:
        f.write(text + "\n")
    os.chmod(LOG, 0o600)


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


# --- HTTP -------------------------------------------------------------------

def http(method: str, url: str, *, token: str | None = None, body: dict | None = None):
    headers = {"Accept": "application/json", "User-Agent": UA}
    data = None
    if token:
        headers["X-Access-Token"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, _json(response.read()), dict(response.headers)
        except urllib.error.HTTPError as error:
            if error.code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            return error.code, _json(error.read()), dict(error.headers)
        except urllib.error.URLError:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    raise Stop(f"{url} did not answer")


def _json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return {"_text": raw[:300].decode(errors="replace")}


def rpc(method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(6):
        request = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                reply = json.load(response)
            if "result" in reply:
                return reply["result"]
        except (urllib.error.URLError, ValueError):
            pass
        time.sleep(2 * (attempt + 1))
    raise Stop(f"Base RPC did not answer {method}")


def call_word(to: str, data: str) -> int:
    return int(rpc("eth_call", [{"to": to, "data": data}, "latest"]), 16)


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def word_arg(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def usdc_balance(address: str) -> int:
    return call_word(USDC, selector("balanceOf(address)") + word_arg(address))


# --- the agent key --------------------------------------------------------------

class Agent:
    def __init__(self, address: str, workdir: Path):
        self.address = address
        self.workdir = workdir
        self.password_file = workdir / "pw"
        self.key: str | None = None

    def unlock(self) -> None:
        keystore = KEYSTORES / "sign402-agent"
        if not keystore.exists():
            raise Stop(f"No agent keystore at {keystore}")
        password = os.environ.get("AGENT_PW") or getpass.getpass("sign402-agent password: ")
        try:
            key = Account.decrypt(json.loads(keystore.read_text()), password)
        except ValueError:
            raise Stop("Could not unlock sign402-agent: wrong password?") from None
        if Account.from_key(key).address.lower() != self.address.lower():
            raise Stop(f"That keystore is not the agent {self.address}.")
        self.key = key.to_0x_hex()
        # cast reads the password only from a regular file: private directory,
        # removed with it when the run ends.
        fd = os.open(self.password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(password)

    def sign(self, message: str) -> str:
        return Account.sign_message(encode_defunct(text=message), self.key).signature.to_0x_hex()

    def send(self, to: str, signature: str, *args: str) -> str:
        """cast send as the agent; returns the transaction hash."""
        result = subprocess.run(
            ["cast", "send", to, signature, *args,
             "--keystore", str(KEYSTORES / "sign402-agent"),
             "--password-file", str(self.password_file),
             "--rpc-url", RPC, "--json"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise Stop(f"Agent transaction failed: {result.stderr.strip()[:300]}")
        return json.loads(result.stdout)["transactionHash"]


# --- Bitrefill sign-in ------------------------------------------------------------

def siwx(agent: Agent, url: str, method: str):
    """Answer a sign-in-with-x challenge on `url` with the agent key.

    Uses the Bitrefill skill's own helpers to build the message and send the
    header, so the message format is theirs, not a copy of it. Returns
    (status, body). The body can hold redemption codes: it stays in memory.
    """
    status, challenge, _ = http(method, url)
    if status != 402 or "sign-in-with-x" not in (challenge.get("extensions") or {}):
        raise Stop(f"{url} did not answer with a sign-in challenge (HTTP {status})")
    challenge_file = agent.workdir / "challenge.json"
    skeleton_file = agent.workdir / "skeleton.json"
    challenge_file.write_text(json.dumps(challenge))
    built = subprocess.run(
        ["node", str(SKILL_SCRIPTS / "siwx_build_message.js"), agent.address, str(challenge_file), str(skeleton_file)],
        capture_output=True, text=True, check=True,
    ).stdout
    message = built.split("MESSAGE_START\n", 1)[1].split("\nMESSAGE_END", 1)[0]
    sent = subprocess.run(
        ["node", str(SKILL_SCRIPTS / "siwx_send.js"), agent.sign(message), str(skeleton_file), method],
        capture_output=True, text=True, check=True,
    ).stdout
    status_match = re.search(r"STATUS:::(\d+)", sent)
    body = sent.split("BODY:::", 1)[1].strip() if "BODY:::" in sent else ""
    return int(status_match.group(1)) if status_match else 0, _json(body.encode())


def sign_in(agent: Agent) -> str:
    status, body = siwx(agent, f"{API}/connect", "POST")
    token = body.get("token") if isinstance(body, dict) else None
    if status != 200 or not token:
        raise Stop(f"Bitrefill sign-in failed (HTTP {status})")
    return token


# --- the limiter ------------------------------------------------------------------

def limiter_state(limiter: str) -> dict[str, int]:
    return {
        name: call_word(limiter, selector(f"{name}()"))
        for name in ("perPurchaseCap", "remainingToday", "allowanceLeft", "paused")
    }


def settlement(agent: str, pay_to: str, amount: int, from_block: int) -> str | None:
    for _ in range(20):
        latest = int(rpc("eth_blockNumber", []), 16)
        logs = rpc("eth_getLogs", [{
            "fromBlock": hex(from_block), "toBlock": hex(latest), "address": USDC,
            "topics": [TRANSFER_TOPIC, "0x" + word_arg(agent), "0x" + word_arg(pay_to)],
        }])
        for entry in logs:
            if int(entry["data"], 16) == amount:
                return entry["transactionHash"]
        time.sleep(3)
    return None


def wait_balance(address: str, at_least: int) -> None:
    for _ in range(20):
        if usdc_balance(address) >= at_least:
            return
        time.sleep(2)
    raise Stop(f"The agent's USDC never showed {at_least} on the RPC; not paying.")


# --- invoices ---------------------------------------------------------------------

def find(obj, *names):
    """First value under any of `names`, searched depth-first."""
    if isinstance(obj, dict):
        for name in names:
            if obj.get(name) not in (None, ""):
                return obj[name]
        for value in obj.values():
            found = find(value, *names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find(value, *names)
            if found is not None:
                return found
    return None


def pay_terms(url: str, body: dict, token: str):
    """The Base USDC exact leg of the pay route's 402, without paying."""
    status, challenge, headers = http("POST", url, token=token, body=body)
    if status != 402:
        raise Stop(f"The pay route answered HTTP {status}, not a payment request.")
    header = headers.get("payment-required") or headers.get("Payment-Required")
    if header:
        import base64
        challenge = json.loads(base64.b64decode(header))
    legs = [a for a in challenge.get("accepts", [])
            if a.get("scheme") == "exact" and a.get("network") == NETWORK
            and a.get("asset", "").lower() == USDC.lower()]
    if not legs:
        raise Stop("The pay route offers no USDC-on-Base payment.")
    leg = legs[0]
    return int(leg.get("amount") or leg["maxAmountRequired"]), leg["payTo"]


def ask(question: str) -> bool:
    if os.environ.get("ASSUME_YES") == "1":
        return True
    return input(f"{question} [y/N] ").strip().lower() == "y"


# --- the purchase -----------------------------------------------------------------

def buy(slug: str, package: str, dry_run: bool) -> None:
    agent_env = read_env(STATE_DIR / "t4.env")
    limiter = (os.environ.get("LIMITER") or read_env(STATE_DIR / "t6.env").get("LIMITER")
               or agent_env.get("LIMITER"))
    if not agent_env.get("AGENT") or not limiter:
        raise Stop("No agent or limiter: run t4-mainnet.sh first.")
    with tempfile.TemporaryDirectory() as workdir:
        os.chmod(workdir, 0o700)
        agent = Agent(agent_env["AGENT"], Path(workdir))
        agent.unlock()
        say(f"Agent {agent.address} unlocked. Signing in to Bitrefill with it (free).")
        token = sign_in(agent)

        status, detail, _ = http("GET", f"{API}/products/detail?" + urllib.parse.urlencode({"slug": slug}), token=token)
        product = detail.get("product", detail) if isinstance(detail, dict) else {}
        packages = [p for p in product.get("packages", []) if p.get("package_value") == package]
        if status != 200 or not packages:
            raise Stop(f"No package {package!r} for {slug} (HTTP {status}).")
        if product.get("recipient_required"):
            raise Stop(f"{slug} needs a recipient; this tool only buys products delivered as a code.")
        price = Decimal(packages[0]["payment_price"])
        ceiling = math.ceil(price * 1_000_000)

        state = limiter_state(limiter)
        say()
        say(f"  product     {product.get('name')} ({slug})")
        say(f"  package     {package} {packages[0].get('package_currency', '')}".rstrip())
        say(f"  price       {price} USDC on Base")
        say(f"  paid by     the agent key, via x402 to Bitrefill")
        say(f"  funded by   the limiter {limiter}, from the Trezor address")
        say(f"  limiter     per purchase {state['perPurchaseCap']}, left today {state['remainingToday']}, "
            f"allowance {state['allowanceLeft']} (atomic USDC)")
        say(f"  delivery    a code, shown here only; not refundable once delivered")
        fundable = not state["paused"] and ceiling <= min(
            state["perPurchaseCap"], state["remainingToday"], state["allowanceLeft"])
        if dry_run:
            say(f"\nThe limiter {'could' if fundable else 'could NOT'} fund this now.")
            say("Dry run: stopping before the order is created.")
            return
        if not fundable:
            raise Stop("The limiter cannot fund this price. Nothing was created.")
        if not ask("\nCreate this order and pay for it?"):
            raise Stop("Not bought. Nothing was created.")

        status, invoice, _ = http("POST", f"{API}/invoice/create", token=token,
                                  body={"items": [{"product_id": slug, "package_value": package}]})
        next_step = invoice.get("next_step") if isinstance(invoice, dict) else None
        # The pay step names the invoice it pays; prefer that to guessing which
        # "id" in the response is the invoice's.
        invoice_id = ((next_step or {}).get("body") or {}).get("invoice_id") or find(invoice, "invoice_id")
        if status not in (200, 201) or not invoice_id or not next_step:
            raise Stop(f"Bitrefill did not create the order (HTTP {status}). Nothing was paid.")
        log(f"- {datetime.datetime.now(datetime.UTC):%Y-%m-%d %H:%M UTC} invoice {invoice_id} created: {slug} {package!r}")

        amount, pay_to = pay_terms(next_step["url"], next_step.get("body") or {}, token)
        say(f"\nOrder {invoice_id}: pay {Decimal(amount) / 1_000_000} USDC to {pay_to}")
        if pay_to.lower() != BITREFILL_PAY_TO.lower():
            raise Stop(f"The pay route asks for {pay_to}, not Bitrefill's published {BITREFILL_PAY_TO}. Not paying.")
        if amount > ceiling and not ask(f"The order costs {amount} atomic, above the {ceiling} confirmed. Pay anyway?"):
            raise Stop("Not paid. The order will expire unpaid.")
        state = limiter_state(limiter)
        if amount > min(state["perPurchaseCap"], state["remainingToday"], state["allowanceLeft"]):
            raise Stop("The limiter cannot fund the order's amount. Not paid.")

        before = usdc_balance(agent.address)
        fund_tx = agent.send(limiter, "spend(address,uint256,bytes32)", agent.address, str(amount),
                             "0x" + keccak(text=f"bitrefill:{invoice_id}").hex())
        say(f"Funded the agent with exactly {amount} through the limiter: {fund_tx}")
        wait_balance(agent.address, before + amount)

        start = int(rpc("eth_blockNumber", []), 16)
        paid = subprocess.run(
            ["node", "src/index.mjs", "buy-user", "--url", next_step["url"], "--method", "POST",
             "--body-json", json.dumps(next_step.get("body") or {}),
             "--max-atomic", str(amount), "--expected-receiver", pay_to, "--expected-asset", USDC],
            cwd=CDP_X402_DIR, capture_output=True, text=True,
            env={**os.environ, "SIGN402_EVM_PRIVATE_KEY": agent.key},
        )
        result = _json(paid.stdout[paid.stdout.find("{"):].encode()) if "{" in paid.stdout else {}
        http_status = result.get("status") if isinstance(result, dict) else None
        settle_tx = settlement(agent.address, pay_to, amount, start)
        say(f"Payment answered HTTP {http_status}; settlement on chain: {settle_tx or 'NOT FOUND'}")
        log(f"- invoice {invoice_id}: {amount} atomic USDC, usdc_base via x402 from agent {agent.address}, "
            f"funding {fund_tx}, settlement {settle_tx or 'not found'}")
        if not settle_tx:
            raise Stop("No settlement on chain. The funded USDC is with the agent; nothing more is paid.")

        say("Waiting for delivery…")
        status_url = f"{API}/invoice/status?" + urllib.parse.urlencode({"invoice_id": invoice_id})
        for _ in range(60):
            status, body, _ = http("GET", status_url, token=token)
            delivery = find(body, "delivery_status")
            if delivery == "all_delivered":
                break
            time.sleep(5)
        else:
            raise Stop(f"Not delivered yet (last status {delivery}). Poll later; Bitrefill support after 3 hours.")
        codes = find(body, "redemption_info")
        if not codes:
            status, body = siwx(agent, status_url, "GET")
            codes = find(body, "redemption_info")
        log(f"- invoice {invoice_id}: delivered {datetime.datetime.now(datetime.UTC):%Y-%m-%d %H:%M UTC}")
        say("\nYour code is ready. It is shown only here and written nowhere — store it safely:\n")
        say(json.dumps(codes, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("slug")
    parser.add_argument("package", help='exact package_value, e.g. "5" or "1GB, 7 Days"')
    parser.add_argument("--dry-run", action="store_true", help="stop before creating the order")
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        buy(args.slug, args.package, args.dry_run)
    except Stop as stop:
        say(f"\n{stop}")
        return 1
    except KeyboardInterrupt:
        say("\nStopped.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
