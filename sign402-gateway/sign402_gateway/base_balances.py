import json
import re
import threading
from collections.abc import Callable, Mapping
from decimal import Decimal, localcontext
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


BASE_CHAIN_ID = 8453
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_SINGIT_TOKEN_ADDRESS = "0xc2c1e0b7C401e6217193732272444D928646eba3"

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_BALANCE_OF_SELECTOR = "70a08231"
_MAX_DISCOVERY_ROWS = 100
_MAX_UNVERIFIED_TOKENS = 10


class BaseBalanceError(RuntimeError):
    pass


class JsonRpcClient:
    def __init__(
        self,
        *,
        endpoint_url: str,
        opener: Callable[..., Any] = urlopen,
        timeout: float = 5.0,
        max_response_bytes: int = 256 * 1024,
    ):
        self.endpoint_url = str(endpoint_url)
        self.opener = opener
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._id_lock = threading.Lock()
        self._next_request_id = 1

    def call(self, method: str, params: list) -> object:
        request_id = self._reserve_ids(1)[0]
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        response = self._post(payload)
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise BaseBalanceError("Base RPC returned an invalid response")
        if "error" in response or "result" not in response:
            raise BaseBalanceError("Base RPC request failed")
        return response["result"]

    def batch(
        self,
        calls: list[tuple[str, list]],
        *,
        allow_errors: bool = False,
    ) -> list[object | None]:
        if not calls:
            return []
        request_ids = self._reserve_ids(len(calls))
        payload = [
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            for request_id, (method, params) in zip(request_ids, calls)
        ]
        response = self._post(payload)
        if not isinstance(response, list):
            raise BaseBalanceError("Base RPC returned an invalid batch response")

        by_id: dict[int, dict[str, Any]] = {}
        for item in response:
            if not isinstance(item, dict):
                raise BaseBalanceError("Base RPC returned an invalid batch response")
            response_id = item.get("id")
            if isinstance(response_id, int):
                by_id[response_id] = item

        results: list[object | None] = []
        for request_id in request_ids:
            item = by_id.get(request_id)
            if item is None:
                raise BaseBalanceError("Base RPC returned an incomplete batch response")
            if "error" in item or "result" not in item:
                if allow_errors:
                    results.append(None)
                    continue
                raise BaseBalanceError("Base RPC batch request failed")
            results.append(item["result"])
        return results

    def _reserve_ids(self, count: int) -> list[int]:
        with self._id_lock:
            first = self._next_request_id
            self._next_request_id += count
        return list(range(first, first + count))

    def _post(self, payload: object) -> object:
        request = Request(
            self.endpoint_url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response = None
        try:
            response = self.opener(request, timeout=self.timeout)
            body = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            exc.close()
            raise BaseBalanceError("Base RPC HTTP request failed") from None
        except (TimeoutError, URLError, OSError):
            raise BaseBalanceError("Base RPC is unavailable") from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        if len(body) > self.max_response_bytes:
            raise BaseBalanceError("Base RPC response is too large")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BaseBalanceError("Base RPC returned invalid JSON") from None


class AlchemyBaseBalanceProvider:
    def __init__(
        self,
        *,
        rpc_url: str = "",
        singit_token_address: str = DEFAULT_SINGIT_TOKEN_ADDRESS,
        rpc_client: JsonRpcClient | None = None,
    ):
        self.rpc = rpc_client or JsonRpcClient(endpoint_url=rpc_url)
        self.singit_token_address = _require_address(
            singit_token_address,
            field_name="SINGIT token address",
        )

    def __call__(self, wallet_address: str) -> dict[str, Any]:
        address = _require_address(wallet_address, field_name="wallet address")
        calldata = encode_erc20_balance_of(address)
        trusted_calls = [
            ("eth_chainId", []),
            ("eth_getBalance", [address, "latest"]),
            (
                "eth_call",
                [{"to": BASE_USDC_ADDRESS, "data": calldata}, "latest"],
            ),
            (
                "eth_call",
                [{"to": self.singit_token_address, "data": calldata}, "latest"],
            ),
        ]
        chain_raw, eth_raw, usdc_raw, singit_raw = self.rpc.batch(trusted_calls)
        chain_id = _parse_hex_quantity(chain_raw)
        if chain_id != BASE_CHAIN_ID:
            raise BaseBalanceError("RPC endpoint is not Base Mainnet")

        balances = {
            "ETH": format_atomic_amount(_parse_uint256_hex(eth_raw), 18),
            "USDC": format_atomic_amount(_parse_uint256_hex(usdc_raw), 6),
            "SINGIT": format_atomic_amount(_parse_uint256_hex(singit_raw), 18),
        }
        try:
            unverified_tokens = self._discover_tokens(address)
        except BaseBalanceError:
            unverified_tokens = []
        return {
            "balances": balances,
            "unverifiedTokens": unverified_tokens,
        }

    def _discover_tokens(self, wallet_address: str) -> list[dict[str, str]]:
        result = self.rpc.call(
            "alchemy_getTokenBalances",
            [wallet_address, "erc20", {"maxCount": _MAX_DISCOVERY_ROWS}],
        )
        if not isinstance(result, dict):
            raise BaseBalanceError("Alchemy Token API returned an invalid response")
        rows = result.get("tokenBalances")
        if not isinstance(rows, list):
            raise BaseBalanceError("Alchemy Token API returned an invalid response")

        trusted_addresses = {
            BASE_USDC_ADDRESS.lower(),
            self.singit_token_address.lower(),
        }
        candidates: dict[str, int] = {}
        for row in rows[:_MAX_DISCOVERY_ROWS]:
            if not isinstance(row, dict):
                continue
            contract = str(row.get("contractAddress", "") or "")
            if not _ADDRESS_RE.fullmatch(contract):
                continue
            normalized_contract = contract.lower()
            if normalized_contract in trusted_addresses:
                continue
            try:
                balance = _parse_uint256_hex(row.get("tokenBalance"))
            except BaseBalanceError:
                continue
            if balance > 0:
                candidates[normalized_contract] = balance

        selected = sorted(candidates.items())[:_MAX_UNVERIFIED_TOKENS]
        if not selected:
            return []
        metadata = self.rpc.batch(
            [
                ("alchemy_getTokenMetadata", [contract])
                for contract, _balance in selected
            ],
            allow_errors=True,
        )

        tokens: list[dict[str, str]] = []
        for (contract, atomic_balance), token_metadata in zip(selected, metadata):
            normalized = _normalize_token_metadata(token_metadata)
            if normalized is None:
                continue
            symbol, decimals = normalized
            tokens.append(
                {
                    "symbol": symbol,
                    "contractAddress": contract,
                    "balance": format_atomic_amount(atomic_balance, decimals),
                    "decimals": decimals,
                }
            )
        return tokens


def build_base_balance_provider_from_env(
    env: Mapping[str, str],
) -> AlchemyBaseBalanceProvider | None:
    rpc_url = str(env.get("SIGN402_BASE_RPC_URL", "") or "").strip()
    if not rpc_url:
        return None
    _validate_rpc_url(rpc_url)
    return AlchemyBaseBalanceProvider(
        rpc_url=rpc_url,
        singit_token_address=str(
            env.get(
                "SIGN402_SINGIT_TOKEN_ADDRESS",
                DEFAULT_SINGIT_TOKEN_ADDRESS,
            )
            or DEFAULT_SINGIT_TOKEN_ADDRESS
        ).strip(),
    )


def encode_erc20_balance_of(wallet_address: str) -> str:
    address = _require_address(wallet_address, field_name="wallet address")
    return "0x" + _BALANCE_OF_SELECTOR + address[2:].lower().rjust(64, "0")


def format_atomic_amount(value: int, decimals: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("atomic balance must be a non-negative integer")
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        raise ValueError("token decimals must be an integer")
    if decimals < 0 or decimals > 36:
        raise ValueError("token decimals must be between 0 and 36")
    if value == 0:
        return "0"
    with localcontext() as context:
        context.prec = max(len(str(value)) + decimals + 2, 50)
        amount = Decimal(value) / (Decimal(10) ** decimals)
        formatted = format(amount, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def _normalize_token_metadata(metadata: object) -> tuple[str, int] | None:
    if not isinstance(metadata, dict):
        return None
    raw_symbol = metadata.get("symbol")
    decimals = metadata.get("decimals")
    if not isinstance(raw_symbol, str):
        return None
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        return None
    if decimals < 0 or decimals > 36:
        return None
    symbol = "".join(
        char
        for char in raw_symbol
        if char.isascii() and (char.isalnum() or char in "._-")
    )[:16]
    if not symbol:
        return None
    return symbol, decimals


def _parse_hex_quantity(value: object) -> int:
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        raise BaseBalanceError("Base RPC returned an invalid hex quantity")
    return int(value[2:], 16)


def _parse_uint256_hex(value: object) -> int:
    if (
        not isinstance(value, str)
        or not _HEX_RE.fullmatch(value)
        or len(value[2:]) > 64
    ):
        raise BaseBalanceError("Base RPC returned an invalid uint256 balance")
    return int(value[2:], 16)


def _require_address(value: object, *, field_name: str) -> str:
    address = str(value or "").strip()
    if not _ADDRESS_RE.fullmatch(address):
        raise BaseBalanceError(f"{field_name} must be a 20-byte EVM address")
    return address


def _validate_rpc_url(rpc_url: str) -> None:
    parsed = urlparse(rpc_url)
    is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in {"https", "http"}
        or (parsed.scheme != "https" and not is_loopback)
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise BaseBalanceError("SIGN402_BASE_RPC_URL must be a secure RPC URL")
