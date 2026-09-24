"""Bitrefill through its x402 API, on the Trezor allowance lane.

The owner decided on 24 September 2026 that allowance-lane purchases on Bitrefill
go through `https://api.bitrefill.com/x402`, paid by the user's agent key and
funded by their limiter — recorded as an exception in AGENTS.md. Everything else
in that file still applies: the user confirms the exact product and price before
an order exists, and codes stay out of files and logs.

The agent signs in with Sign-In-With-X (EIP-4361, eip191). The message is built
exactly as the Bitrefill agent skill's `siwx_build_message.js` builds it; a test
compares the two. The pay route's recipient must be Bitrefill's published x402
address and its amount no more than the confirmed price; the payment itself is
AllowanceService.pay_x402, which reads the settlement from the chain.

Codes are fetched when the user asks to see them and returned to the caller only.
"""

from __future__ import annotations

import base64
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

from .agent_allowance import USDC, USER_AGENT, AllowanceError, AllowanceService

API = "https://api.bitrefill.com/x402"
# Published in the Bitrefill agent skill (references/touchpoints/x402.md). A pay
# route that asks for any other recipient is refused, not trusted.
PAY_TO = "0x480CD46E6faDe651a0437DeaddA53D5c8e7D846A"
CHAIN = "eip155:8453"
TOKEN_SECONDS = 90 * 60           # Bitrefill's token lasts about two hours
DELIVERY_WAIT_SECONDS = 90
KINDS = {"gift-cards": "gift-cards", "topups": "topups", "esims": "esims"}


def siwe_message(info: dict[str, Any], address: str, chain_id: int) -> str:
    """The text to sign, exactly as the skill's buildSiweMessage writes it."""
    prefix = f"{info['domain']} wants you to sign in with your Ethereum account:\n{address}"
    if info.get("statement"):
        prefix += "\n\n" + info["statement"]
    suffix = [
        f"URI: {info['uri']}",
        f"Version: {info['version']}",
        f"Chain ID: {chain_id}",
        f"Nonce: {info['nonce']}",
        f"Issued At: {info['issuedAt']}",
    ]
    if info.get("expirationTime"):
        suffix.append(f"Expiration Time: {info['expirationTime']}")
    if info.get("notBefore"):
        suffix.append(f"Not Before: {info['notBefore']}")
    if info.get("requestId"):
        suffix.append(f"Request ID: {info['requestId']}")
    if info.get("resources"):
        suffix.append("\n".join(["Resources:", *[f"- {r}" for r in info["resources"]]]))
    return prefix + "\n\n" + "\n".join(suffix)


def siwx_header(extension: dict[str, Any], private_key: str) -> str:
    """The SIGN-IN-WITH-X header answering one challenge with this key."""
    info = extension["info"]
    chains = extension.get("supportedChains") or []
    chain = next((c for c in chains if c.get("chainId") == CHAIN), None) or {"chainId": CHAIN, "type": "eip191"}
    address = to_checksum_address(Account.from_key(private_key).address)
    message = siwe_message(info, address, int(chain["chainId"].split(":")[1]))
    signature = Account.sign_message(encode_defunct(text=message), private_key).signature.to_0x_hex()
    payload = {
        "domain": info["domain"], "address": address, "statement": info.get("statement"), "uri": info["uri"],
        "version": info["version"], "chainId": chain["chainId"], "type": chain.get("type") or "eip191",
        "nonce": info["nonce"], "issuedAt": info["issuedAt"], "expirationTime": info.get("expirationTime"),
        "resources": info.get("resources"),
    }
    for optional in ("notBefore", "requestId"):
        if info.get(optional):
            payload[optional] = info[optional]
    payload["signature"] = signature
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _http(method: str, url: str, *, token: str | None = None, body: dict | None = None,
          headers: dict[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    data = None
    if token:
        request_headers["X-Access-Token"] = token
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, _json(response.read()), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, _json(error.read()), dict(error.headers)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AllowanceError("Bitrefill is not answering right now. Nothing was bought.") from None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}


def _find(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        if obj.get(name) not in (None, ""):
            return obj[name]
        for value in obj.values():
            found = _find(value, name)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find(value, name)
            if found is not None:
                return found
    return None


class BitrefillX402:
    def __init__(
        self,
        allowance: AllowanceService,
        x402_client: Callable[..., dict[str, Any]],
        *,
        http: Callable[..., tuple[int, Any, dict[str, str]]] = _http,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.allowance = allowance
        self.x402_client = x402_client
        self.http = http
        self.now = now
        self.sleep = sleep
        self._tokens: dict[str, tuple[str, float]] = {}

    # -- sign-in --

    def _challenge(self, method: str, url: str) -> dict[str, Any]:
        status, body, _ = self.http(method, url)
        extension = (body.get("extensions") or {}).get("sign-in-with-x") if isinstance(body, dict) else None
        if status != 402 or not extension:
            raise AllowanceError("Bitrefill did not offer a sign-in. Nothing was bought.")
        return extension

    def token(self, user_id: str) -> str:
        agent, key = self.allowance.agent_key(user_id)
        cached = self._tokens.get(agent)
        if cached and cached[1] > self.now():
            return cached[0]
        header = siwx_header(self._challenge("POST", f"{API}/connect"), key)
        status, body, _ = self.http("POST", f"{API}/connect", headers={"SIGN-IN-WITH-X": header})
        token = body.get("token") if isinstance(body, dict) else None
        if status != 200 or not token:
            raise AllowanceError("Bitrefill refused the agent's sign-in. Nothing was bought.")
        self._tokens[agent] = (token, self.now() + TOKEN_SECONDS)
        return token

    # -- catalog --

    def search(self, user_id: str, query: str, *, kind: str = "gift-cards", country: str = "") -> list[dict[str, Any]]:
        if kind not in KINDS:
            raise AllowanceError("Search gift-cards, topups or esims.")
        params = {"q": query}
        if country:
            params["country"] = country.upper()
        status, body, _ = self.http("GET", f"{API}/{KINDS[kind]}/search?" + urllib.parse.urlencode(params),
                                    token=self.token(user_id))
        if status != 200 or not isinstance(body, dict):
            raise AllowanceError("Bitrefill search failed. Try again.")
        return [{"slug": p.get("slug"), "name": p.get("name"), "currency": p.get("currency"),
                 "countries": p.get("countries")} for p in body.get("products", [])]

    def quote(self, user_id: str, slug: str, package: str) -> dict[str, Any]:
        """Product, package and the price Bitrefill quotes now, in USDC."""
        status, body, _ = self.http("GET", f"{API}/products/detail?" + urllib.parse.urlencode({"slug": slug}),
                                    token=self.token(user_id))
        product = body.get("product", body) if isinstance(body, dict) else {}
        match = [p for p in product.get("packages") or [] if str(p.get("package_value")) == str(package)]
        if status != 200 or not match:
            raise AllowanceError(f"Bitrefill has no package {package!r} for {slug}.")
        if product.get("recipient_required"):
            raise AllowanceError("This product needs a recipient; only products delivered as a code can be bought here.")
        try:
            price = Decimal(str(match[0]["payment_price"]))
        except (InvalidOperation, KeyError):
            raise AllowanceError("Bitrefill did not quote a price for it.") from None
        return {
            "slug": slug, "name": product.get("name") or slug, "package": str(package),
            "packageCurrency": match[0].get("package_currency"), "priceUsd": str(price),
            "priceAtomic": math.ceil(price * 1_000_000),
        }

    # -- purchase --

    def buy(self, user_id: str, slug: str, package: str, ceiling_atomic: int) -> dict[str, Any]:
        """Create the order and pay it from the agent, never above `ceiling_atomic`.

        The ceiling is the price the user confirmed. A price that rose since, a
        pay route asking for another recipient, or more than the ceiling: nothing
        is paid. The result carries no code.
        """
        quoted = self.quote(user_id, slug, package)
        if quoted["priceAtomic"] > ceiling_atomic:
            raise AllowanceError(
                f"Bitrefill now asks {quoted['priceUsd']} USDC, above the price you confirmed. Nothing was bought; quote again."
            )
        token = self.token(user_id)
        status, invoice, _ = self.http("POST", f"{API}/invoice/create", token=token,
                                       body={"items": [{"product_id": slug, "package_value": str(package)}]})
        next_step = invoice.get("next_step") if isinstance(invoice, dict) else None
        invoice_id = ((next_step or {}).get("body") or {}).get("invoice_id") or _find(invoice, "invoice_id")
        if status not in (200, 201) or not next_step or not invoice_id:
            raise AllowanceError("Bitrefill did not create the order. Nothing was paid.")

        pay_url, pay_body = next_step["url"], next_step.get("body") or {}
        status, challenge, headers = self.http("POST", pay_url, token=token, body=pay_body)
        header = headers.get("payment-required") or headers.get("Payment-Required")
        if header:
            challenge = json.loads(base64.b64decode(header))
        legs = [a for a in (challenge or {}).get("accepts", [])
                if a.get("scheme") == "exact" and a.get("network") == CHAIN and str(a.get("asset", "")).lower() == USDC.lower()]
        if status != 402 or not legs:
            raise AllowanceError("Bitrefill did not ask for USDC on Base for this order. Nothing was paid.")
        amount = int(legs[0].get("amount") or legs[0]["maxAmountRequired"])
        pay_to = to_checksum_address(legs[0]["payTo"])
        if pay_to != PAY_TO:
            raise AllowanceError(f"Bitrefill's pay route asked for {pay_to}, not its published address. Nothing was paid.")
        if amount > ceiling_atomic:
            raise AllowanceError("The order costs more than the price you confirmed. Nothing was paid.")

        paid = self.allowance.pay_x402(
            user_id, pay_url, {"amountAtomic": str(amount), "receiver": pay_to, "asset": USDC},
            self.x402_client, method="POST", request_body=pay_body,
        )
        if not paid["ok"]:
            if paid["settlementTx"] and not paid["delivered"]:
                raise AllowanceError(
                    f"Bitrefill took the payment ({paid['settlementTx']}) but did not confirm order {invoice_id}. "
                    "Keep both to claim it; Bitrefill support after 3 hours."
                )
            raise AllowanceError(f"Order {invoice_id} was not paid (HTTP {paid['status']}). Nothing was charged.")

        delivered = self._wait_delivery(token, invoice_id)
        name = f"{quoted['name']} {quoted['package']} {quoted.get('packageCurrency') or ''}".strip()
        text = (
            f"Bought {name} for {Decimal(amount) / 1_000_000} USDC from your Trezor allowance ({paid['funding']}).\n"
            f"Payment: https://basescan.org/tx/{paid['settlementTx']}\n"
            + ("Your code is ready: send /last_purchase to see it." if delivered
               else "Bitrefill is still delivering it; /last_purchase will show it when it is ready.")
        )
        return {
            "ok": True, "mode": "bitrefill_x402_allowance", "invoiceId": invoice_id, "productSlug": slug,
            "package": str(package), "productName": quoted["name"], "priceUsd": str(Decimal(amount) / 1_000_000),
            "amountAtomic": amount, "txId": paid["settlementTx"], "funding": paid["funding"],
            "fundingTx": paid["fundingTx"], "payer": paid["payer"], "limiter": paid["limiter"],
            "delivered": delivered, "telegramText": text,
        }

    def _status(self, token: str, invoice_id: str) -> Any:
        status, body, _ = self.http("GET", f"{API}/invoice/status?" + urllib.parse.urlencode({"invoice_id": invoice_id}),
                                    token=token)
        return body if status == 200 else None

    def _wait_delivery(self, token: str, invoice_id: str) -> bool:
        deadline = self.now() + DELIVERY_WAIT_SECONDS
        while True:
            body = self._status(token, invoice_id)
            if _find(body, "delivery_status") == "all_delivered":
                return True
            if self.now() >= deadline:
                return False
            self.sleep(5)

    def redemption(self, user_id: str, invoice_id: str) -> Any:
        """The code for one delivered order, or None. Returned to the caller only."""
        token = self.token(user_id)
        body = self._status(token, invoice_id)
        if _find(body, "delivery_status") != "all_delivered":
            return None
        codes = _find(body, "redemption_info")
        if codes is not None:
            return codes
        # Codes are shown to the paying wallet: sign the status route itself.
        url = f"{API}/invoice/status?" + urllib.parse.urlencode({"invoice_id": invoice_id})
        _, key = self.allowance.agent_key(user_id)
        status, body, _ = self.http("GET", url, headers={"SIGN-IN-WITH-X": siwx_header(self._challenge("GET", url), key)})
        return _find(body, "redemption_info") if status == 200 else None
