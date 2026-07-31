import json
import math
import re
from dataclasses import dataclass
from typing import Any

import httpx
import rlp
from eth_account import Account
from eth_utils import keccak

from .errors import SafeError


BASE_CHAIN_ID = 8453
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EVM_DERIVATION_PATH = "m/44'/60'/0'/0/0"

_TRANSFER_SELECTOR = bytes.fromhex("a9059cbb")
_BALANCE_OF_SELECTOR = "70a08231"
_MAX_RPC_RESPONSE_BYTES = 65_536
_MAX_RAW_TRANSACTION_BYTES = 131_072
_SECP256K1_ORDER = int(
    "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141", 16
)
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_HEX_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)\Z")
_HEX_DATA_WORD = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_HEX_BYTES = re.compile(r"0x(?:[0-9a-fA-F]{2})+\Z")


def _rpc_unavailable() -> SafeError:
    return SafeError("base_rpc_unavailable", "Base RPC is unavailable.", 503)


def _invalid_transaction() -> SafeError:
    return SafeError(
        "invalid_signed_transaction",
        "Signed transaction does not match the approved payment.",
        400,
    )


def _address_bytes(value: Any) -> bytes:
    if not isinstance(value, str) or _HEX_ADDRESS.fullmatch(value) is None:
        raise ValueError("Invalid EVM address.")
    return bytes.fromhex(value[2:])


def _uint256(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 1 << 256:
        raise ValueError("Invalid uint256 value.")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


def encode_usdc_transfer(to_address: str, amount_atomic: int) -> str:
    recipient = _address_bytes(to_address)
    amount = _uint256(amount_atomic)
    return "0x" + (
        _TRANSFER_SELECTOR
        + b"\x00" * 12
        + recipient
        + amount.to_bytes(32, "big")
    ).hex()


@dataclass(frozen=True, slots=True)
class BaseBalances:
    eth_wei: int
    usdc_atomic: int


class BaseRpcClient:
    def __init__(
        self,
        url: str,
        timeout_seconds: float = 10.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        if not isinstance(url, str) or not url:
            raise ValueError("Invalid Base RPC URL.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Invalid Base RPC timeout.")
        self._url = url
        self.timeout_seconds = float(timeout_seconds)
        self._transport = transport

    def __repr__(self) -> str:
        return f"BaseRpcClient(timeout_seconds={self.timeout_seconds})"

    def _request(self, request_id: int, method: str, params: list[Any]) -> Any:
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                with client.stream(
                    "POST",
                    self._url,
                    json=request,
                    headers={"accept": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        raise _rpc_unavailable()
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            if int(content_length) > _MAX_RPC_RESPONSE_BYTES:
                                raise _rpc_unavailable()
                        except ValueError:
                            raise _rpc_unavailable() from None
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > _MAX_RPC_RESPONSE_BYTES:
                            raise _rpc_unavailable()
                        chunks.append(chunk)
            decoded = json.loads(
                b"".join(chunks), object_pairs_hook=_unique_json_object
            )
            if (
                not isinstance(decoded, dict)
                or decoded.get("jsonrpc") != "2.0"
                or type(decoded.get("id")) is not int
                or decoded["id"] != request_id
                or "error" in decoded
                or "result" not in decoded
            ):
                raise _rpc_unavailable()
            return decoded["result"]
        except SafeError:
            raise
        except Exception:
            raise _rpc_unavailable() from None

    @staticmethod
    def _quantity(value: Any) -> int:
        if not isinstance(value, str) or _HEX_QUANTITY.fullmatch(value) is None:
            raise _rpc_unavailable()
        return int(value[2:], 16)

    @staticmethod
    def _word(value: Any) -> int:
        if not isinstance(value, str) or _HEX_DATA_WORD.fullmatch(value) is None:
            raise _rpc_unavailable()
        return int(value[2:], 16)

    def get_balances(self, address: str) -> BaseBalances:
        account = _address_bytes(address)
        normalized = "0x" + account.hex()
        chain_id = self._quantity(self._request(1, "eth_chainId", []))
        if chain_id != BASE_CHAIN_ID:
            raise _rpc_unavailable()
        eth_wei = self._quantity(
            self._request(2, "eth_getBalance", [normalized, "latest"])
        )
        balance_data = "0x" + _BALANCE_OF_SELECTOR + "0" * 24 + account.hex()
        usdc_atomic = self._word(
            self._request(
                3,
                "eth_call",
                [{"to": BASE_USDC_ADDRESS, "data": balance_data}, "latest"],
            )
        )
        return BaseBalances(eth_wei=eth_wei, usdc_atomic=usdc_atomic)


def _rlp_uint(value: Any) -> int:
    if not isinstance(value, bytes) or len(value) > 32:
        raise _invalid_transaction()
    if value[:1] == b"\x00":
        raise _invalid_transaction()
    return int.from_bytes(value, "big")


def verify_signed_usdc_transfer(
    raw_tx: str,
    expected_signer: str,
    expected_recipient: str,
    expected_amount_atomic: int,
) -> str:
    try:
        signer = _address_bytes(expected_signer)
        recipient = _address_bytes(expected_recipient)
        amount = _uint256(expected_amount_atomic)
        if (
            not isinstance(raw_tx, str)
            or len(raw_tx) > 2 + _MAX_RAW_TRANSACTION_BYTES * 2
            or _HEX_BYTES.fullmatch(raw_tx) is None
        ):
            raise _invalid_transaction()
        raw = bytes.fromhex(raw_tx[2:])
        if not raw or raw[0] != 2:
            raise _invalid_transaction()
        fields = rlp.decode(raw[1:], strict=True)
        if not isinstance(fields, list) or len(fields) != 12:
            raise _invalid_transaction()

        chain_id = _rlp_uint(fields[0])
        _rlp_uint(fields[1])  # nonce
        max_priority_fee = _rlp_uint(fields[2])
        max_fee = _rlp_uint(fields[3])
        gas_limit = _rlp_uint(fields[4])
        destination = fields[5]
        value = _rlp_uint(fields[6])
        data = fields[7]
        access_list = fields[8]
        y_parity = _rlp_uint(fields[9])
        signature_r = _rlp_uint(fields[10])
        signature_s = _rlp_uint(fields[11])

        if (
            chain_id != BASE_CHAIN_ID
            or max_priority_fee <= 0
            or max_fee <= 0
            or max_fee < max_priority_fee
            or gas_limit <= 0
            or not isinstance(destination, bytes)
            or len(destination) != 20
            or destination != _address_bytes(BASE_USDC_ADDRESS)
            or value != 0
            or not isinstance(data, bytes)
            or not isinstance(access_list, list)
            or access_list
            or y_parity not in (0, 1)
            or not 0 < signature_r < _SECP256K1_ORDER
            or not 0 < signature_s <= _SECP256K1_ORDER // 2
        ):
            raise _invalid_transaction()
        if (
            len(data) != 68
            or data[:4] != _TRANSFER_SELECTOR
            or data[4:16] != b"\x00" * 12
            or data[16:36] != recipient
            or int.from_bytes(data[36:68], "big") != amount
        ):
            raise _invalid_transaction()

        recovered = _address_bytes(Account.recover_transaction(raw_tx))
        if recovered != signer:
            raise _invalid_transaction()
        return "0x" + keccak(raw).hex()
    except SafeError:
        raise
    except Exception:
        raise _invalid_transaction() from None
