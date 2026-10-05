"""Separate VPS agent controller that pays Bitrefill through an enrolled Trezor."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from .broker_client import RemoteSidecarClient
from .local_agent import LocalAgentController, LocalAgentSettings
from .poc_runner import PreparedAddressBitrefillClient, SidecarTreasuryClient, TrezorPocRunner
from .store import SidecarStore


_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
_DEFAULT_STATE_PATH = Path("~/.sign402-trezor-broker/purchase-state.db").expanduser()


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "") or "").strip()
    if not value:
        raise ValueError(f"{name} is required when remote Trezor agent mode is enabled")
    return value


@dataclass(frozen=True, repr=False)
class RemoteAgentSettings:
    enabled: bool
    purchases_enabled: bool = False
    user_id: str = ""
    broker_url: str = ""
    broker_internal_token: str = ""
    max_usd: Decimal = Decimal("0")
    bitrefill_api_key: str = ""
    buyer_email: str = ""
    state_path: Path = _DEFAULT_STATE_PATH

    def __repr__(self) -> str:
        return (
            "RemoteAgentSettings("
            f"enabled={self.enabled!r}, purchases_enabled={self.purchases_enabled!r}, "
            "user_id='<redacted>', "
            f"max_usd={self.max_usd!r}, state_path={self.state_path!r}, "
            "credentials='<redacted>')"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RemoteAgentSettings":
        if env.get("SIGN402_TREZOR_REMOTE_AGENT_ENABLED") != "1":
            return cls(False)
        if env.get("SIGN402_TREZOR_POC_ENABLED") != "1":
            raise ValueError("SIGN402_TREZOR_POC_ENABLED=1 is required for remote Trezor mode")
        user_id = _required(env, "SIGN402_TREZOR_REMOTE_USER_ID")
        if not user_id.isascii() or not user_id.isdecimal() or len(user_id) > 32:
            raise ValueError("SIGN402_TREZOR_REMOTE_USER_ID must be one numeric user ID")
        maximum_text = _required(env, "SIGN402_TREZOR_POC_MAX_USD")
        try:
            maximum = Decimal(maximum_text)
        except InvalidOperation:
            raise ValueError("SIGN402_TREZOR_POC_MAX_USD must be a positive decimal") from None
        if not maximum.is_finite() or maximum <= 0:
            raise ValueError("SIGN402_TREZOR_POC_MAX_USD must be a positive decimal")
        buyer_email = str(env.get("SIGN402_TREZOR_REMOTE_BUYER_EMAIL", "") or "").strip()
        if buyer_email and (len(buyer_email) > 254 or _EMAIL.fullmatch(buyer_email) is None):
            raise ValueError("SIGN402_TREZOR_REMOTE_BUYER_EMAIL must be a valid email address")
        state_path = Path(
            str(env.get("SIGN402_TREZOR_REMOTE_STATE_PATH", _DEFAULT_STATE_PATH))
        ).expanduser()
        expected_parent = _DEFAULT_STATE_PATH.parent.resolve()
        resolved = Path(os.path.abspath(os.fspath(state_path)))
        if resolved.parent != expected_parent:
            raise ValueError("remote Trezor state must stay in ~/.sign402-trezor-broker")
        purchases_enabled = env.get("SIGN402_TREZOR_REMOTE_PURCHASES_ENABLED") == "1"
        return cls(
            enabled=True,
            purchases_enabled=purchases_enabled,
            user_id=user_id,
            broker_url=_required(env, "SIGN402_TREZOR_BROKER_URL"),
            broker_internal_token=_required(env, "SIGN402_TREZOR_BROKER_INTERNAL_TOKEN"),
            max_usd=maximum,
            bitrefill_api_key=(
                _required(env, "BITREFILL_API_KEY") if purchases_enabled else ""
            ),
            buyer_email=buyer_email,
            state_path=resolved,
        )


def build_remote_agent_controller(
    env: Mapping[str, str],
    *,
    clock=time.time,
) -> LocalAgentController:
    settings = RemoteAgentSettings.from_env(env)
    local_settings = LocalAgentSettings(
        enabled=settings.enabled,
        purchases_enabled=settings.purchases_enabled,
        allowed_user_id=settings.user_id,
        sidecar_token="",
        max_usd=settings.max_usd,
        bitrefill_api_key=settings.bitrefill_api_key,
        buyer_email=settings.buyer_email,
    )
    if not settings.enabled:
        return LocalAgentController(
            settings=local_settings,
            runner=object(),
            details_client=object(),
            clock=clock,
        )
    remote = RemoteSidecarClient(
        base_url=settings.broker_url,
        internal_token=settings.broker_internal_token,
        user_id=settings.user_id,
        clock=clock,
    )
    if not settings.purchases_enabled:
        class StatusOnlyRunner:
            def __init__(self, sidecar):
                self.sidecar = sidecar

        return LocalAgentController(
            settings=local_settings,
            runner=StatusOnlyRunner(remote),
            details_client=object(),
            clock=clock,
        )
    treasury = SidecarTreasuryClient(sidecar=remote, clock=clock)
    bitrefill = PreparedAddressBitrefillClient(
        api_key=settings.bitrefill_api_key,
        max_purchase_usd=str(settings.max_usd),
        payment_method="usdc_base",
        treasury_client=treasury,
    )
    store = SidecarStore(settings.state_path)
    runner = TrezorPocRunner(
        bitrefill=bitrefill,
        sidecar=remote,
        max_usd=settings.max_usd,
        summary_sink=lambda _summary: None,
        store=store,
        state_path=settings.state_path,
        clock=clock,
        treasury=treasury,
    )
    return LocalAgentController(
        settings=local_settings,
        runner=runner,
        details_client=bitrefill,
        clock=clock,
    )
