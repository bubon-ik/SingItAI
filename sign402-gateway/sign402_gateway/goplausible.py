import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.request
from typing import Any, Callable


ALGORAND_MAINNET_CAIP2 = "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="
ALGORAND_TESTNET_CAIP2 = "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="
BASE_MAINNET_CAIP2 = "eip155:8453"
BASE_SEPOLIA_CAIP2 = "eip155:84532"
MAX_X402_RESPONSE_BYTES = 1024 * 1024

NETWORK_ALIASES = {
    ALGORAND_MAINNET_CAIP2: "algorand-mainnet",
    ALGORAND_TESTNET_CAIP2: "algorand-testnet",
    "algorand-mainnet": "algorand-mainnet",
    "algorand-testnet": "algorand-testnet",
    BASE_MAINNET_CAIP2: "base-mainnet",
    "base-mainnet": "base-mainnet",
}


def fetch_x402_payment_required(
    resource_url: str,
    *,
    request_body: dict[str, Any] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: int = 20,
) -> dict[str, Any]:
    body_bytes = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "Hermes-Sign402/0.1 (+https://github.com/bubon-ik/-402)",
    }
    if request_body is not None:
        body_bytes = json.dumps(request_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        resource_url,
        data=body_bytes,
        headers=headers,
        method="POST" if request_body is not None else "GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            body = _read_x402_response_text(response)
            payload = json.loads(body) if body else {}
            raise ValueError(
                f"Expected x402 resource to return HTTP 402, got HTTP {response.status}."
            )
    except urllib.error.HTTPError as exc:
        body = _read_x402_response_text(exc)
        if exc.code != 402:
            raise ValueError(f"Expected HTTP 402 from x402 resource, got HTTP {exc.code}: {body}")
        header_payload = exc.headers.get("Payment-Required")
        if header_payload:
            payload = _decode_payment_required_header(header_payload)
        else:
            payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("x402 402 response must be a JSON object")
        payload.setdefault("status", 402)
        return payload


def fetch_x402_paid_resource(
    resource_url: str,
    *,
    payment_signature_header: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: int = 90,
) -> dict[str, Any]:
    request = urllib.request.Request(
        resource_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Hermes-Sign402/0.1 (+https://github.com/bubon-ik/-402)",
            "PAYMENT-SIGNATURE": payment_signature_header,
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            body = _read_x402_response_text(response)
            payload = json.loads(body) if body else {}
            if not isinstance(payload, dict):
                payload = {"body": payload}
            payload.setdefault("status", response.status)
            payment_response = response.headers.get("Payment-Response")
            if payment_response:
                payload["paymentResponse"] = _decode_payment_required_header(payment_response)
            return payload
    except urllib.error.HTTPError as exc:
        body = _read_x402_response_text(exc)
        payload = json.loads(body) if body else {}
        if not isinstance(payload, dict):
            payload = {"body": payload}
        payload.setdefault("status", exc.code)
        payment_response = exc.headers.get("Payment-Response")
        if payment_response:
            payload["paymentResponse"] = _decode_payment_required_header(payment_response)
        return payload


def _read_x402_response_text(response: Any) -> str:
    raw = response.read(MAX_X402_RESPONSE_BYTES + 1)
    if len(raw) > MAX_X402_RESPONSE_BYTES:
        raise ValueError("x402 response is too large")
    return raw.decode("utf-8")


def normalize_x402_payment_required(
    payload: dict[str, Any],
    *,
    resource_url: str,
    purpose: str = "x402_api_access",
) -> dict[str, Any]:
    requirement = _extract_payment_requirement(payload)
    network = str(requirement.get("network", ""))
    legacy_network = NETWORK_ALIASES.get(network)
    if legacy_network is None:
        raise ValueError(f"Unsupported x402 network: {network}")

    if "amountAtomic" in requirement and "receiver" in requirement:
        normalized = dict(requirement)
        normalized.setdefault("sourceFormat", "sign402-demo")
        normalized.setdefault("x402Network", network)
        normalized.setdefault("resource", resource_url)
        normalized.setdefault("purpose", purpose)
        normalized["amountAtomic"] = str(normalized["amountAtomic"])
        normalized["network"] = legacy_network
        normalized["originalPaymentRequirements"] = requirement
        return normalized

    amount = requirement.get("amount")
    receiver = requirement.get("payTo", requirement.get("pay_to"))
    asset = requirement.get("asset")
    if amount is None:
        raise ValueError("x402 paymentRequirements.amount is required")
    if receiver is None:
        raise ValueError("x402 paymentRequirements.payTo is required")
    if asset is None:
        raise ValueError("x402 paymentRequirements.asset is required")

    extra = requirement.get("extra", {})
    if not isinstance(extra, dict):
        extra = {}

    payment_intent = _payment_intent(requirement, resource_url)
    return {
        "scheme": str(requirement.get("scheme", "exact")),
        "network": legacy_network,
        "x402Network": network,
        "asset": str(asset),
        "amountAtomic": str(amount),
        "receiver": str(receiver),
        "resource": str(requirement.get("resource", resource_url)),
        "paymentIntent": payment_intent,
        "purpose": str(requirement.get("purpose", purpose)),
        "maxTimeoutSeconds": requirement.get("maxTimeoutSeconds"),
        "extra": extra,
        "sourceFormat": _source_format_for_network(legacy_network),
        "originalPaymentRequirements": requirement,
    }


def _source_format_for_network(network: str) -> str:
    if network.startswith("base-"):
        return "x402-evm-v2"
    return "x402-avm-v2"


def _extract_payment_requirement(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("x402 response payload must be a JSON object")

    requirement = payload.get("paymentRequirements")
    if isinstance(requirement, list):
        if not requirement:
            raise ValueError("paymentRequirements list is empty")
        requirement = requirement[0]

    accepts = payload.get("accepts")
    if requirement is None and isinstance(accepts, list):
        if not accepts:
            raise ValueError("accepts list is empty")
        requirement = accepts[0]

    if not isinstance(requirement, dict):
        raise ValueError("x402 response must contain paymentRequirements or accepts[0]")
    return requirement


def _decode_payment_required_header(header_value: str) -> dict[str, Any]:
    try:
        padded = header_value + "=" * (-len(header_value) % 4)
        decoded = base64.b64decode(padded.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid Payment-Required header. Expected base64 JSON.") from exc

    if not isinstance(payload, dict):
        raise ValueError("Invalid Payment-Required header. Expected JSON object.")
    return payload


def _payment_intent(requirement: dict[str, Any], resource_url: str) -> str:
    extra = requirement.get("extra", {})
    if isinstance(extra, dict):
        for key in ("paymentIntent", "intent", "nonce"):
            value = extra.get(key)
            if value:
                return str(value)

    for key in ("paymentIntent", "intent", "nonce"):
        value = requirement.get(key)
        if value:
            return str(value)

    canonical = json.dumps(
        {"resourceUrl": resource_url, "paymentRequirements": requirement},
        sort_keys=True,
        separators=(",", ":"),
    )
    prefix = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"x402-local-{prefix}-{secrets.token_hex(8)}"
