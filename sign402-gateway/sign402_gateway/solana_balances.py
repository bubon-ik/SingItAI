"""Read-only mainnet SOL and native-USDC balances; no signing or payment APIs."""

import json
import re
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .solana_keys import b58decode

MAINNET_GENESIS_HASH = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_MAX_RESPONSE = 256 * 1024


class SolanaBalanceError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SolanaBalanceProvider:
    def __init__(self, *, endpoint_url: str, opener=None, timeout: float = 4.0):
        url = urlparse(endpoint_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("SIGN402_SOLANA_RPC_URL must be an HTTPS endpoint without user credentials or a fragment")
        self.endpoint_url = endpoint_url
        self.opener = opener or build_opener(_NoRedirect()).open
        self.timeout = timeout

    def _call(self, method: str, params: list):
        request = Request(self.endpoint_url, data=json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                raise ValueError("Response too large")
            body = json.loads(raw)
            if (not isinstance(body, dict) or body.get("jsonrpc") != "2.0"
                    or type(body.get("id")) is not int or body["id"] != 1
                    or "error" in body or "result" not in body):
                raise ValueError("Invalid RPC response")
            return body["result"]
        except Exception:
            raise SolanaBalanceError("Solana balance lookup failed") from None

    def __call__(self, address: str) -> dict[str, str]:
        try:
            if len(b58decode(address)) != 32:
                raise ValueError("Invalid address")
            # Recheck on every read: a configurable provider must never label
            # devnet balances as mainnet, including after a backend change.
            if self._call("getGenesisHash", []) != MAINNET_GENESIS_HASH:
                raise ValueError("Wrong network")
            native = self._call("getBalance", [address, {"commitment": "confirmed"}])["value"]
            if type(native) is not int or not 0 <= native <= 2**64 - 1:
                raise ValueError("Invalid SOL balance")
            accounts = self._call("getTokenAccountsByOwner", [address, {"mint": USDC_MINT},
                                  {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
            if not isinstance(accounts, list):
                raise ValueError("Invalid token accounts")
            total, seen = 0, set()
            for entry in accounts:
                pubkey = entry["pubkey"]
                if len(b58decode(pubkey)) != 32 or pubkey in seen:
                    raise ValueError("Invalid or duplicate token account")
                seen.add(pubkey)
                account = entry["account"]
                parsed = account["data"]["parsed"]
                info = parsed["info"]
                amount = info["tokenAmount"]
                if (account["owner"] != TOKEN_PROGRAM or parsed["type"] != "account"
                        or info["mint"] != USDC_MINT or info["owner"] != address
                        or type(amount["decimals"]) is not int or amount["decimals"] != 6
                        or not isinstance(amount["amount"], str)
                        or not re.fullmatch(r"0|[1-9][0-9]{0,19}", amount["amount"])
                        or int(amount["amount"]) > 2**64 - 1):
                    raise ValueError("Invalid native-USDC account")
                total += int(amount["amount"])
            # Include all owned native-USDC accounts. This is a holdings view,
            # not a promise that the payment SDK's source ATA has these funds.
            return {"SOL": _units(native, 9), "USDC": _units(total, 6)}
        except Exception:
            raise SolanaBalanceError("Solana balance lookup failed") from None


def _units(amount: int, decimals: int) -> str:
    scale = 10**decimals
    return f"{amount // scale}.{amount % scale:0{decimals}d}"
