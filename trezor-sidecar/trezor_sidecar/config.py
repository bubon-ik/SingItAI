from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


_DEFAULT_STATE_PATH = Path("~/.sign402-trezor-poc/state.db")


def _is_enabled(env: Mapping[str, str]) -> bool:
    return env.get("SIGN402_TREZOR_POC_ENABLED") == "1"


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ValueError(f"{name} is required when SIGN402_TREZOR_POC_ENABLED=1")
    return value


def _positive_decimal(env: Mapping[str, str]) -> Decimal:
    name = "SIGN402_TREZOR_POC_MAX_USD"
    value = _required(env, name)
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a positive decimal") from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{name} must be a positive decimal")
    return amount


def _https_url(env: Mapping[str, str], name: str) -> str:
    value = _required(env, name)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTPS URL without query or fragment")
    return value


def _state_path(env: Mapping[str, str]) -> Path:
    value = env.get("SIGN402_TREZOR_STATE_PATH")
    return Path(value).expanduser() if value else _DEFAULT_STATE_PATH.expanduser()


@dataclass(frozen=True)
class SidecarSettings:
    enabled: bool
    mcp_token: str
    api_token: str
    max_usd: Decimal
    base_rpc_url: str
    state_path: Path
    host: str = "127.0.0.1"
    port: int = 8111
    chain_id: int = 8453
    derivation_path: str = "m/44'/60'/0'/0/0"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SidecarSettings":
        enabled = _is_enabled(env)
        if not enabled:
            return cls(False, "", "", Decimal("0"), "", _state_path(env))
        return cls(
            enabled=True,
            mcp_token=_required(env, "SIGN402_TREZOR_MCP_TOKEN"),
            api_token=_required(env, "SIGN402_TREZOR_SIDECAR_TOKEN"),
            max_usd=_positive_decimal(env),
            base_rpc_url=_https_url(env, "SIGN402_TREZOR_BASE_RPC_URL"),
            state_path=_state_path(env),
        )


@dataclass(frozen=True)
class RunnerSettings:
    enabled: bool
    sidecar_token: str
    max_usd: Decimal
    bitrefill_api_key: str
    sidecar_url: str = "http://127.0.0.1:8111"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RunnerSettings":
        enabled = _is_enabled(env)
        if not enabled:
            return cls(False, "", Decimal("0"), "")
        return cls(
            enabled=True,
            sidecar_token=_required(env, "SIGN402_TREZOR_SIDECAR_TOKEN"),
            max_usd=_positive_decimal(env),
            bitrefill_api_key=_required(env, "BITREFILL_API_KEY"),
        )
