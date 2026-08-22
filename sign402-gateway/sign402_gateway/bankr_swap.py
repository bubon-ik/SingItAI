import json
import os
import re
import subprocess
import uuid
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any


TX_HASH_RE = re.compile(r"\bTx Hash:\s*(0x[a-fA-F0-9]{64})\b")
AMOUNT_PATTERN = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
RECEIVE_RE = re.compile(rf"You receive:\s*~?\s*{AMOUNT_PATTERN}\s+([A-Za-z0-9_.$-]+)")
PAY_RE = re.compile(rf"You pay:\s*{AMOUNT_PATTERN}\s+([A-Za-z0-9_.$-]+)")
MIN_RE = re.compile(rf"Min received:\s*{AMOUNT_PATTERN}\s+([A-Za-z0-9_.$-]+)")
DEFAULT_BANKR_API_BASE_URL = "https://api.bankr.bot"
BASE_USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MAX_BANKR_RESPONSE_BYTES = 1024 * 1024
# Bankr dedupes /wallet/swap by ``idempotencyKey``, so the key must stay the
# same across a retry of one logical swap. Sign402 quote ids are not UUIDs,
# so derive a stable UUIDv5 from the quote id instead of a fresh uuid4.
SWAP_IDEMPOTENCY_NAMESPACE = uuid.UUID("6f0d6bd6-2f1f-5a1a-9d0e-7a0a1c6a4f11")


def swap_idempotency_key(quote_id: str | None) -> str | None:
    value = str(quote_id or "").strip()
    if not value:
        return None
    return str(uuid.uuid5(SWAP_IDEMPOTENCY_NAMESPACE, f"sign402-swap:{value}"))


def usdc_balance_from_portfolio(payload: dict[str, Any], *, chain: str = "base") -> Decimal:
    """Extract the USDC balance from a Bankr wallet-portfolio payload.

    Shared by the CLI (BankrTreasuryClient) and HTTP (BankrWalletApiClient)
    clients; only the transport that fetches ``payload`` differs.
    """
    chain_balances = payload.get("balances", {}).get(str(chain), {})
    token_balances = chain_balances.get("tokenBalances", [])
    if not isinstance(token_balances, list):
        raise ValueError("Bankr portfolio returned invalid token balances")
    for token_balance in token_balances:
        if not isinstance(token_balance, dict):
            continue
        token = token_balance.get("token")
        if not isinstance(token, dict):
            continue
        base_token = token.get("baseToken")
        if isinstance(base_token, dict):
            symbol = str(base_token.get("symbol", ""))
            address = str(base_token.get("address") or token_balance.get("address") or "")
        else:
            symbol = ""
            address = str(token_balance.get("address") or "")
        if symbol.upper() == "USDC" or address.lower() == BASE_USDC_MAINNET.lower():
            return Decimal(str(token.get("balance", "0")))
    return Decimal("0")


def parse_bankr_transaction_hash(stdout: str) -> str | None:
    match = TX_HASH_RE.search(stdout)
    return match.group(1) if match else None


def parse_bankr_swap_quote(stdout: str) -> dict[str, str]:
    pay = PAY_RE.search(stdout)
    receive = RECEIVE_RE.search(stdout)
    minimum = MIN_RE.search(stdout)
    if not pay or not receive:
        raise ValueError("Bankr swap quote output did not include pay/receive amounts")
    return {
        "fromAmount": normalize_amount(pay.group(1)),
        "fromToken": pay.group(2),
        "toAmount": normalize_amount(receive.group(1)),
        "toToken": receive.group(2),
        "minToAmount": normalize_amount(minimum.group(1)) if minimum else normalize_amount(receive.group(1)),
    }


def normalize_amount(value: str) -> str:
    return value.replace(",", "")


def load_bankr_api_key(
    env: dict[str, str] | None = None,
    *,
    config_path: Path | None = None,
) -> str | None:
    values = os.environ if env is None else env
    for name in ("BANKR_API_KEY", "BANKR_WALLET_API_KEY"):
        value = str(values.get(name, "")).strip()
        if value:
            return value
    path = config_path or Path.home() / ".bankr" / "config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    value = str(payload.get("apiKey", "")).strip()
    return value or None


class BankrWalletApiClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BANKR_API_BASE_URL,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def quote(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: str,
        chain: str = "base",
        decimals: int = 18,
    ) -> dict[str, Any]:
        payload = self._post_json(
            "/wallet/swap-quote",
            {
                "fromChain": str(chain),
                "fromToken": self._normalize_token(from_token),
                "toChain": str(chain),
                "toToken": self._normalize_token(to_token),
                "amount": str(amount),
            },
        )
        from_payload = payload.get("from", {})
        to_payload = payload.get("to", {})
        if not isinstance(from_payload, dict) or not isinstance(to_payload, dict):
            raise ValueError("Bankr Wallet API swap quote returned invalid JSON")
        return {
            "ok": True,
            "fromAmount": str(from_payload.get("formattedAmount") or from_payload.get("amount")),
            "fromToken": str(from_payload.get("symbol") or from_payload.get("token")),
            "toAmount": str(to_payload.get("formattedAmount") or to_payload.get("amount")),
            "toToken": str(to_payload.get("symbol") or to_payload.get("token")),
            "minToAmount": str(payload.get("minBuyAmount") or to_payload.get("formattedAmount") or to_payload.get("amount")),
            "raw": payload,
        }

    def swap(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: str,
        chain: str = "base",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        quote = self.quote(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            chain=chain,
        )
        payload = self._post_json(
            "/wallet/swap",
            {
                "fromChain": str(chain),
                "fromToken": self._normalize_token(from_token),
                "toChain": str(chain),
                "toToken": self._normalize_token(to_token),
                "amount": str(amount),
                "minBuyAmount": str(quote["minToAmount"]),
                **(
                    {"idempotencyKey": str(idempotency_key)}
                    if idempotency_key
                    else {}
                ),
            },
        )
        tx_id = payload.get("hash")
        return {
            "ok": bool(payload.get("success", False)),
            "txId": str(tx_id) if tx_id else None,
            "amountSold": payload.get("amountSold"),
            "amountReceived": payload.get("amountReceived"),
            "amountSoldRaw": payload.get("amountSoldRaw"),
            "amountReceivedRaw": payload.get("amountReceivedRaw"),
            "quote": quote,
            "raw": payload,
        }

    def usdc_balance(self, *, chain: str = "base") -> Decimal:
        payload = self._get_json(
            f"/wallet/portfolio?chains={chain}&showLowValueTokens=true"
        )
        return usdc_balance_from_portfolio(payload, chain=chain)

    def _get_json(self, path: str) -> dict[str, Any]:
        return self._request_json("GET", path)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", path, payload)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {
            # Bankr documents X-API-Key for api.bankr.bot; Authorization: Bearer
            # is the LLM-gateway form and is undocumented here.
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = _read_bankr_response_text(response)
        except urllib.error.HTTPError as exc:
            body = _read_bankr_response_text(exc, errors="replace")
            raise ValueError(f"Bankr Wallet API error {exc.code}: {body}") from exc
        payload_json = json.loads(body)
        if not isinstance(payload_json, dict):
            raise ValueError("Bankr Wallet API returned non-object JSON")
        return payload_json

    def _normalize_token(self, token: str) -> str:
        value = str(token).strip()
        if value.upper() == "USDC":
            return BASE_USDC_MAINNET
        return value


def _read_bankr_response_text(response: Any, *, errors: str = "strict") -> str:
    raw = response.read(MAX_BANKR_RESPONSE_BYTES + 1)
    if len(raw) > MAX_BANKR_RESPONSE_BYTES:
        raise ValueError("Bankr Wallet API response is too large")
    return raw.decode("utf-8", errors)


class BankrSwapClient:
    def __init__(self, *, bankr_cli: str):
        self.bankr_cli = bankr_cli

    def quote(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: str,
        chain: str = "base",
        decimals: int = 18,
    ) -> dict[str, Any]:
        command = self._command(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            chain=chain,
        ) + ["--quote-only"]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise ValueError(
                result.stderr.strip() or result.stdout.strip() or "Bankr swap quote failed"
            )
        quote = parse_bankr_swap_quote(result.stdout)
        quote.update(
            {
                "ok": True,
                "command": command,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        )
        return quote

    def swap(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: str,
        chain: str = "base",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        # The CLI exposes no idempotency flag; the parameter keeps this client
        # interchangeable with BankrWalletApiClient.
        del idempotency_key
        command = self._command(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            chain=chain,
        )
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        payload = {
            "ok": result.returncode == 0,
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "txId": parse_bankr_transaction_hash(result.stdout),
        }
        if result.returncode != 0:
            raise ValueError(payload["stderr"] or payload["stdout"] or "Bankr swap failed")
        return payload

    def _command(self, *, from_token: str, to_token: str, amount: str, chain: str) -> list[str]:
        return [
            self.bankr_cli,
            "wallet",
            "swap",
            "--from",
            str(from_token),
            "--to",
            str(to_token),
            "--amount",
            str(amount),
            "--chain",
            str(chain),
        ]
