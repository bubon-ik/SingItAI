"""Venice AI chat client.

Venice does not price per request. Its x402 lane tops up a prepaid balance held
against a wallet address, and meters requests against that balance internally.
So exactly one step of this flow is an x402 payment:

    sign SIWE  ->  read balance  ->  [402 top-up: the only payment]
                                 ->  chat, chat, chat, ...

One $5 top-up buys on the order of a thousand messages. Everything after it is
an ordinary authenticated request carrying an EIP-4361 signature, and the true
cost of each message is read back from the `X-Balance-Remaining` header rather
than estimated.

The x402 settlement callable is injected, so tests never move funds.

Nothing here logs or persists prompt text or model output.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from .chat_store import (
    DEFAULT_CHAT_STORE_PATH,
    SECONDS_PER_DAY,
    ChatStore,
    PrefundClaimUnavailable,
)

logger = logging.getLogger(__name__)


def _provider_error_code(response: Any) -> str:
    """A short, safe identifier for why the provider refused.

    Only a code or a short error string is returned. Provider bodies can echo
    request content, so nothing longer than a token is ever taken from them.
    """
    try:
        body = response.json() or {}
    except Exception:
        return "unparseable"
    if not isinstance(body, dict):
        return "unparseable"
    for key in ("code", "error", "message", "detail"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value[:60]
        if isinstance(value, dict):
            nested = value.get("code") or value.get("message")
            if isinstance(nested, str) and nested:
                return nested[:60]
    return "unknown"


VENICE_BASE_URL = "https://api.venice.ai/api/v1"
USDC_DECIMALS = 6
SIWX_HEADER = "X-Sign-In-With-X"
BALANCE_REMAINING_HEADER = "X-Balance-Remaining"
# The alias `venice-uncensored` is not in Venice's own model list, so the
# provider decides what it points at and the price can move without notice.
# Offer explicit ids instead.
DEFAULT_MODEL = "venice-uncensored-1-2"


class UnknownModel(ValueError):
    """A model that is not on the offered list. Never forwarded."""


@dataclass(frozen=True)
class ChatModel:
    model_id: str
    label: str
    blurb: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float


# Seven of Venice's 113, chosen by Venice rather than by us: every model it
# tags as the default for a job — uncensored, vision, tools, code, reasoning,
# most intelligent — plus the cheapest one it offers. A curated list is
# unavoidable on a phone keyboard, so the selection criterion should at least
# be the provider's own and not our taste. Prices are per million tokens.
# Labels are the models' own names, as Venice reports them. Someone choosing
# an LLM wants to know it is Grok or GLM; "Smartest" tells them nothing they
# can look up or compare anywhere else.
CHAT_MODELS: tuple[ChatModel, ...] = (
    ChatModel(
        "qwen3-5-9b", "Qwen 3.5 9B", "Fastest, and the cheapest by far.", 0.10, 0.15
    ),
    ChatModel(
        "venice-uncensored-1-2",
        "Venice Uncensored 1.2",
        "Fewest refusals.",
        0.20,
        0.90,
    ),
    ChatModel(
        "qwen-3-8-27b", "Qwen 3.8 27B", "Reads images.", 0.45, 3.20
    ),
    ChatModel(
        "zai-org-glm-5-2",
        "GLM 5.2",
        "Venice's own default. Long documents, tool use.",
        1.40,
        4.40,
    ),
    ChatModel(
        "deepseek-v4-pro-0813",
        "DeepSeek V4 Pro",
        "Best at code.",
        1.65,
        4.95,
    ),
    ChatModel("grok-4-6", "Grok 4.6", "Best reasoning.", 2.27, 6.80),
    ChatModel(
        "kimi-k3",
        "Kimi K3",
        "Deepest thinking. Spends a daily budget fastest.",
        3.75,
        18.75,
    ),
)


MODEL_CACHE_TTL_SECONDS = 6 * 60 * 60
_MAX_BLURB = 160


@dataclass(frozen=True)
class ModelCategory:
    key: str
    label: str


# Ordered as they are offered. `all` is always present; the rest appear only
# when the live list actually contains something for them.
_CATEGORY_RULES: tuple[tuple[str, str, Callable[[dict], bool]], ...] = (
    ("all", "All by price", lambda caps: True),
    ("vision", "Reads images", lambda caps: bool(caps.get("supportsVision"))),
    ("code", "Writes code", lambda caps: bool(caps.get("optimizedForCode"))),
    ("reasoning", "Thinks step by step", lambda caps: bool(caps.get("supportsReasoning"))),
    ("video", "Watches video", lambda caps: bool(caps.get("supportsVideoInput"))),
    ("audio", "Hears audio", lambda caps: bool(caps.get("supportsAudioInput"))),
    ("private", "Extra privacy", lambda caps: bool(
        caps.get("supportsE2EE") or caps.get("supportsTeeAttestation")
    )),
)


class VeniceModelCatalogue:
    """Every model Venice sells, described in Venice's own words.

    Maintaining a list of a hundred models by hand goes stale the day the
    provider adds one, so this reads theirs. A model with no published price
    is dropped: the daily cap is enforced in money, and a cost we cannot show
    is a cost the user cannot consent to.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], dict[str, Any]],
        now: Callable[[], int] | None = None,
    ):
        self.fetch = fetch
        self.now = now or (lambda: int(time.time()))
        self._cached: tuple[ChatModel, ...] | None = None
        self._cached_at = 0
        self._caps: dict[str, dict] = {}

    def models(
        self,
        *,
        category: str = "all",
        query: str = "",
        page: int = 0,
        per_page: int = 0,
    ) -> list[ChatModel]:
        models = [
            m
            for m in self._all()
            if category == "all" or self._matches(m.model_id, category)
        ]
        needle = _search_key(query)
        if needle:
            models = [
                m
                for m in models
                if needle in _search_key(m.label) or needle in _search_key(m.model_id)
            ]
        if per_page <= 0:
            return models
        start = max(0, page) * per_page
        return models[start : start + per_page]

    def categories(self) -> list[ModelCategory]:
        self._all()
        offered = []
        for key, label, rule in _CATEGORY_RULES:
            if key == "all" or any(rule(caps) for caps in self._caps.values()):
                offered.append(ModelCategory(key, label))
        return offered

    def resolve(self, model_id: str) -> ChatModel:
        wanted = str(model_id or "").strip()
        for model in self._all():
            if model.model_id == wanted:
                return model
        raise UnknownModel("that model is not available")

    # -- internals -------------------------------------------------------

    def _matches(self, model_id: str, category: str) -> bool:
        rule = next((r for k, _l, r in _CATEGORY_RULES if k == category), None)
        return bool(rule) and rule(self._caps.get(model_id, {}))

    def _all(self) -> tuple[ChatModel, ...]:
        fresh = self.now() - self._cached_at < MODEL_CACHE_TTL_SECONDS
        if self._cached is not None and fresh:
            return self._cached
        try:
            models, caps = _parse_model_list(self.fetch())
        except Exception:
            # A stale list beats no list, and the built-in seven beat nothing.
            if self._cached is not None:
                return self._cached
            logger.warning("Venice model list unavailable; using the built-in set")
            self._caps = {}
            return CHAT_MODELS
        if not models:
            return self._cached or CHAT_MODELS
        self._cached, self._caps, self._cached_at = models, caps, self.now()
        return models


def _parse_model_list(
    payload: dict[str, Any],
) -> tuple[tuple[ChatModel, ...], dict[str, dict]]:
    entries = (payload or {}).get("data") or []
    models: list[ChatModel] = []
    caps: dict[str, dict] = {}
    for entry in entries:
        spec = (entry or {}).get("model_spec") or {}
        pricing = spec.get("pricing") or {}
        out = (pricing.get("output") or {}).get("usd")
        inp = (pricing.get("input") or {}).get("usd")
        if out is None or inp is None:
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            continue
        models.append(
            ChatModel(
                model_id=model_id,
                label=str(spec.get("name") or model_id),
                blurb=_trim_blurb(spec.get("description")),
                input_usd_per_mtok=float(inp),
                output_usd_per_mtok=float(out),
            )
        )
        caps[model_id] = spec.get("capabilities") or {}
    models.sort(key=lambda m: m.output_usd_per_mtok)
    return tuple(models), caps


def _search_key(text: Any) -> str:
    """Fold a name for matching.

    People type "grok 4.6" for `grok-4-6`; separators carry no meaning here,
    so drop them and compare what is left.
    """
    return "".join(
        ch for ch in str(text or "").lower() if ch.isalnum()
    )


def _trim_blurb(description: Any) -> str:
    """One sentence, short enough for a phone."""
    text = " ".join(str(description or "").split())
    if len(text) <= _MAX_BLURB:
        return text
    cut = text[:_MAX_BLURB]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: stop + 1] if stop > 60 else cut.rstrip() + "…").strip()


def resolve_model(model_id: str) -> ChatModel:
    for model in CHAT_MODELS:
        if model.model_id == str(model_id or "").strip():
            return model
    raise UnknownModel("that model is not available")

# The floor for deciding whether existing credit is enough to send a message.
# Venice refuses a chat request below `minimumBalanceUsd`, observed live as
# $0.10, so a lower threshold here would send a doomed request instead of
# topping up. The real cost of a message is far smaller and is read back from
# the response, not guessed.
VENICE_MINIMUM_BALANCE_ATOMIC = 100_000  # $0.10, quoted by Venice's own 402
DEFAULT_ESTIMATED_COST_ATOMIC = VENICE_MINIMUM_BALANCE_ATOMIC

# Matches _TELEGRAM_OPERATION_MAX_USERS in the plugin.
WALLET_OWNER_CACHE_MAX = 4096


class ChatState:
    WINDOW_EXHAUSTED = "WINDOW_EXHAUSTED"
    MERCHANT_CHANGED = "MERCHANT_CHANGED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PREFUND_FAILED = "PREFUND_FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    EXPIRED = "EXPIRED"


class ChatError(RuntimeError):
    """Base class. The message is always safe to show and never quotes a prompt."""

    state = ""


class WindowExhausted(ChatError):
    state = ChatState.WINDOW_EXHAUSTED


class MerchantChanged(ChatError):
    state = ChatState.MERCHANT_CHANGED


class ProviderUnavailable(ChatError):
    state = ChatState.PROVIDER_UNAVAILABLE


class PrefundFailed(ChatError):
    state = ChatState.PREFUND_FAILED


class ReconciliationRequired(ChatError):
    state = ChatState.RECONCILIATION_REQUIRED


class ProviderOutOfFunds(ChatError):
    """Venice answered and refused for lack of balance. Retrying cannot help."""

    state = ChatState.PREFUND_FAILED


class PolicyExpired(ChatError):
    state = ChatState.EXPIRED


class PolicyMissing(ChatError):
    state = ChatState.EXPIRED


class PolicyRejected(ValueError):
    """The standing authorization is not one we are willing to ask for."""


@dataclass(frozen=True)
class ChatPolicy:
    """One standing authorization: this merchant, this much per day, until then.

    `pay_to` is the binding. The resource host is advisory — a domain is never
    sufficient to authorise a payment.
    """

    pay_to: str
    network: str
    asset: str
    daily_cap_atomic: int
    expires_at: int
    merchant_name: str
    policy_hash: str


def build_chat_policy(
    *,
    pay_to: str,
    network: str,
    asset: str,
    daily_cap_atomic: int,
    expires_at: int,
    merchant_name: str = "Venice AI",
    now: Callable[[], int] | int | None = None,
) -> ChatPolicy:
    """Validate and hash a policy before it is ever shown for approval.

    An expiry is mandatory: a standing authorization with no end date is a
    standing authorization forever, and that is not something to ask a user to
    approve on a three-line screen.
    """
    address = str(pay_to or "").strip().lower()
    if not address:
        raise PolicyRejected("a policy must name the merchant address it binds to")

    try:
        cap = int(daily_cap_atomic)
    except (TypeError, ValueError):
        raise PolicyRejected("the daily cap must be a whole number") from None
    if cap <= 0:
        raise PolicyRejected("the daily cap must be greater than zero")

    try:
        expiry = int(expires_at or 0)
    except (TypeError, ValueError):
        raise PolicyRejected("a policy must have an expiry") from None
    if expiry <= 0:
        raise PolicyRejected("a policy must have an expiry")

    current = now() if callable(now) else now
    if current is not None and expiry <= int(current):
        raise PolicyRejected("a policy cannot expire in the past")

    canonical = json.dumps(
        {
            "version": "1",
            "allowedPurpose": "ai_chat",
            "payTo": address,
            "network": str(network or "").strip().lower(),
            "asset": str(asset or "").strip().lower(),
            "maxSpendAtomicPerWindow": str(cap),
            "window": "utc_day",
            "expiresAt": expiry,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return ChatPolicy(
        pay_to=address,
        network=str(network or "").strip().lower(),
        asset=str(asset or "").strip().lower(),
        daily_cap_atomic=cap,
        expires_at=expiry,
        merchant_name=str(merchant_name or "Venice AI").strip(),
        policy_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def policy_approval_context(policy: ChatPolicy) -> list[str]:
    """The approval screen. Order matters.

    The user is granting a standing authorization, so the screen must say that
    first rather than reading like a single purchase.
    """
    return [
        "Standing approval — not a one-off",
        f"{policy.merchant_name}  ({_short_address(policy.pay_to)})",
        f"Up to {_usd(policy.daily_cap_atomic)} per day",
        f"Resets 00:00 UTC · Expires {_expiry_date(policy.expires_at)}",
    ]


def _usd_plain(atomic: int) -> str:
    """Dollars without the sign, rounded down so a balance is never overstated."""
    value = Decimal(int(atomic)) / (10**USDC_DECIMALS)
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _usd(atomic: int) -> str:
    return f"${Decimal(int(atomic)) / (10**USDC_DECIMALS):.2f}"


def _short_address(address: str) -> str:
    text = str(address or "")
    return f"{text[:6]}…{text[-4:]}" if len(text) >= 12 else text


def _expiry_date(epoch: int) -> str:
    return time.strftime("%d %b %Y", time.gmtime(int(epoch)))


CHAT_POLICY_ACTION_TYPE = "sign402_chat_policy"


class ChatPolicyApprovalService:
    """Ask for a standing chat authorization on the user's approval channel.

    Reuses the same hash-approval path as every other spend approval, so the
    policy is shown, recorded and audited exactly like a payment is. The
    difference is what it authorises: a daily allowance rather than one charge.

    Nothing is bound until the approver says yes. A declined approval — or a
    declined raise — leaves whatever was already approved untouched.
    """

    def __init__(
        self,
        *,
        store: ChatStore,
        approval_service: Any,
        pay_to: str,
        network: str,
        asset: str,
        merchant_name: str = "Venice AI",
        now: Callable[[], int] | None = None,
    ):
        self.store = store
        self.approval_service = approval_service
        self.pay_to = pay_to
        self.network = network
        self.asset = asset
        self.merchant_name = merchant_name
        self.now = now or (lambda: int(time.time()))

    def build(self, *, daily_cap_atomic: int, days: int) -> ChatPolicy:
        """Validate before anyone is asked. An invalid policy is never shown."""
        return build_chat_policy(
            pay_to=self.pay_to,
            network=self.network,
            asset=self.asset,
            daily_cap_atomic=daily_cap_atomic,
            expires_at=self.now() + (int(days) * SECONDS_PER_DAY),
            merchant_name=self.merchant_name,
            now=self.now,
        )

    def approve(
        self, user_id: str, *, daily_cap_atomic: int, days: int
    ) -> dict[str, Any]:
        policy = self.build(daily_cap_atomic=daily_cap_atomic, days=days)

        result = self.approval_service.request_hash_approval(
            telegram_user_id=str(user_id),
            action_type=CHAT_POLICY_ACTION_TYPE,
            commitment_hash=policy.policy_hash,
            context_lines=policy_approval_context(policy),
        )

        approved = bool(result.get("ok")) and bool(result.get("approved"))
        if not approved:
            return {
                "ok": False,
                "approved": False,
                "telegramText": str(
                    result.get("telegramText")
                    or "The chat approval was not confirmed. Nothing changed."
                ),
            }

        # Only now does the binding exist. `payTo` is fixed at approval time and
        # every later 402 challenge is checked against it.
        self.store.approve_policy(user_id, policy)
        return {
            "ok": True,
            "approved": True,
            "policyHash": policy.policy_hash,
            "dailyCapAtomic": policy.daily_cap_atomic,
            "expiresAt": policy.expires_at,
            "telegramText": (
                f"Chat approved: up to {_usd(policy.daily_cap_atomic)} a day "
                f"with {policy.merchant_name}, until "
                f"{_expiry_date(policy.expires_at)}. Resets 00:00 UTC."
            ),
        }


class ChatService:
    """What the `/agent/chat/*` routes call.

    Holds the per-user plumbing the client needs but should not know about:
    which wallet address a Telegram user owns, and how to sign with it.
    """

    def __init__(
        self,
        *,
        store: ChatStore,
        client: Any,
        wallet_service: Any,
        daily_cap_atomic: int,
        default_model: str = DEFAULT_MODEL,
        catalogue: Any = None,
    ):
        self.store = store
        self.client = client
        self.wallet_service = wallet_service
        self.daily_cap_atomic = daily_cap_atomic
        # Held here rather than read off the client: reaching through one
        # object for another's field is how the settle path broke.
        self.default_model = default_model
        self.catalogue = catalogue
        # Address -> owning user. One address belongs to exactly one user, so
        # caching this is safe; the signer needs it to find the right key.
        # Bounded like every other per-user map here: a public bot must not
        # grow state without limit. An evicted entry is refilled on the next
        # message, because every send resolves the wallet first.
        self.wallet_owners: dict[str, str] = {}

    def start(self, user_id: str) -> dict[str, Any]:
        session = self.store.get_session(user_id)
        cap = session.daily_cap_atomic or self.daily_cap_atomic
        # Two different numbers, deliberately not merged: `outstanding` is
        # credit already paid for and still sitting at Venice, `remaining` is
        # how much more may be paid today. Showing one as the other would
        # misstate what the user can actually do.
        remaining = max(0, cap - session.spent_atomic_this_window)
        chosen_id = session.model or self.default_model
        try:
            chosen_label = self._catalogue().resolve(chosen_id).label
        except (UnknownModel, Exception):
            chosen_label = chosen_id
        return {
            "ok": True,
            "hasPolicy": bool(session.bound_pay_to),
            "dailyCapAtomic": cap,
            "dailyCapUsdc": _usd_plain(cap),
            "model": chosen_id,
            "modelLabel": chosen_label,
            "spentTodayAtomic": session.spent_atomic_this_window,
            "remainingWindowAtomic": remaining,
            "remainingWindowUsdc": _usd_plain(remaining),
            "outstandingAtomic": session.outstanding_atomic,
            "outstandingUsdc": _usd_plain(session.outstanding_atomic),
            "paused": session.paused,
            "pauseReason": session.pause_reason,
            "policyExpiresAt": session.policy_expires_at,
        }

    def send(self, user_id: str, text: str):
        # Resolve the wallet first: no wallet is a clean refusal, not a crash
        # somewhere deeper in the client.
        address = self._wallet_address(user_id)
        return self.client.send(user_id, text, wallet_address=address)


    MODELS_PER_PAGE = 6

    def models(
        self, user_id: str, *, category: str = "", query: str = "", page: int = 0
    ) -> dict[str, Any]:
        """The categories, or one page of models inside one of them."""
        chosen = self.store.get_session(user_id).model or self.default_model
        if not category and not query:
            return {
                "ok": True,
                "chosen": chosen,
                "categories": [
                    {
                        "key": c.key,
                        "label": c.label,
                        "count": len(self._catalogue().models(category=c.key)),
                    }
                    for c in self._catalogue().categories()
                ],
            }

        category = category or "all"
        total = len(self._catalogue().models(category=category, query=query))
        page = max(0, int(page))
        entries = self._catalogue().models(
            category=category,
            query=query,
            page=page,
            per_page=self.MODELS_PER_PAGE,
        )
        return {
            "ok": True,
            "chosen": chosen,
            "category": category,
            "query": query,
            "page": page,
            "total": total,
            "hasMore": (page + 1) * self.MODELS_PER_PAGE < total,
            "models": [
                {
                    "id": m.model_id,
                    "label": m.label,
                    "blurb": m.blurb,
                    "inputUsdPerMTok": m.input_usd_per_mtok,
                    "outputUsdPerMTok": m.output_usd_per_mtok,
                    "chosen": m.model_id == chosen,
                }
                for m in entries
            ],
        }

    def set_model(self, user_id: str, model_id: str) -> dict[str, Any]:
        """Switch models. Costs nothing and never touches the budget."""
        model = self._catalogue().resolve(model_id)
        self.store.set_model(user_id, model.model_id)
        return {"ok": True, "chosen": model.model_id, "label": model.label}

    def _catalogue(self):
        if self.catalogue is None:
            # No catalogue supplied: serve the built-in set. Reaching for the
            # network here would give every caller — tests included — a hidden
            # HTTP dependency it never asked for. build_chat_service_from_env
            # passes the live one explicitly.
            self.catalogue = VeniceModelCatalogue(
                fetch=lambda: (_ for _ in ()).throw(
                    RuntimeError("no model list configured")
                )
            )
        return self.catalogue

    def end(self, user_id: str) -> dict[str, Any]:
        # Leaving chat mode is a UI action. It refunds nothing and cancels
        # nothing: credit already paid for stays exactly where it is.
        session = self.store.get_session(user_id)
        return {"ok": True, "outstandingAtomic": session.outstanding_atomic}

    def _wallet_address(self, user_id: str) -> str:
        status = self.wallet_service.wallet_status(user_id)
        if not status.get("ok"):
            raise PrefundFailed(
                "You need a wallet first. Send /wallet to create one."
            )
        address = str((status.get("wallet") or {}).get("address") or "")
        if not address:
            raise PrefundFailed(
                "You need a wallet first. Send /wallet to create one."
            )
        key = address.lower()
        if key not in self.wallet_owners and len(
            self.wallet_owners
        ) >= WALLET_OWNER_CACHE_MAX:
            self.wallet_owners.pop(next(iter(self.wallet_owners)), None)
        self.wallet_owners[key] = str(user_id)
        return address


def build_chat_service_from_env(
    *,
    wallet_service: Any,
    settle: Callable[[dict[str, Any]], dict[str, Any]],
    env: Mapping[str, str] | None = None,
    transport: Callable[..., Any] | None = None,
    purchases_paused: Callable[[], bool] | None = None,
) -> ChatService | None:
    """Build the chat service, or return None when the feature is off.

    Returning None rather than a disabled object is deliberate: with the flag
    unset there is no chat service on the server at all, and the routes 404.
    """
    values = os.environ if env is None else env
    if str(values.get("SIGN402_AI_CHAT_ENABLED", "")).strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None

    pay_to = str(values.get("SIGN402_AI_CHAT_MERCHANT_PAYTO", "") or "").strip()
    if not pay_to:
        raise ValueError(
            "SIGN402_AI_CHAT_MERCHANT_PAYTO is required when AI chat is enabled"
        )

    store = ChatStore(
        Path(
            str(values.get("SIGN402_CHAT_STORE_PATH", "") or "")
            or DEFAULT_CHAT_STORE_PATH
        )
    )
    config = VeniceConfig(
        bound_pay_to=pay_to.lower(),
        network=str(values.get("SIGN402_AI_CHAT_NETWORK", "") or "eip155:8453"),
        asset=str(
            values.get("SIGN402_AI_CHAT_ASSET", "")
            or "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        ).lower(),
        # Venice quotes a flat $5 top-up, so the chunk is not ours to choose.
        chunk_atomic=_int_env(
            values, "SIGN402_AI_CHAT_PREFUND_CHUNK_ATOMIC", 5_000_000
        ),
        # Two chunks, the ratio the design spec used. One chunk exactly would
        # strand anyone holding leftover credit below Venice's usable floor:
        # dust + a whole chunk exceeds the cap, so they could never top up
        # again.
        max_outstanding_atomic=_int_env(
            values, "SIGN402_AI_CHAT_MAX_OUTSTANDING_ATOMIC", 10_000_000
        ),
        daily_cap_atomic=_int_env(
            values, "SIGN402_AI_CHAT_DEFAULT_DAILY_CAP_ATOMIC", 5_000_000
        ),
    )
    service = ChatService(
        store=store,
        client=None,
        wallet_service=wallet_service,
        daily_cap_atomic=config.daily_cap_atomic,
        default_model=config.model,
        catalogue=VeniceModelCatalogue(fetch=_fetch_venice_models),
    )
    service.client = VeniceChatClient(
        store=store,
        transport=transport or _urllib_transport,
        signer=_wallet_signer(wallet_service, service.wallet_owners),
        settle=settle,
        config=config,
        purchases_paused=purchases_paused,
    )
    return service


def _fetch_venice_models() -> dict[str, Any]:
    import urllib.request

    request = urllib.request.Request(
        f"{VENICE_BASE_URL}/models", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def _int_env(values: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(str(values.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _wallet_signer(
    wallet_service: Any, owners: "Mapping[str, str]"
) -> Callable[[str, str], str]:
    """Sign the EIP-4361 message with the user's managed wallet.

    The client works in wallet addresses; the wallet service works in Telegram
    user ids. `owners` carries that one association, which is safe to cache
    because an address belongs to exactly one user.

    The key is decrypted per signature and not retained. Venice wants a fresh
    signature roughly every five minutes, so this runs far more often than a
    payment does — which is why it must never be logged.
    """

    def sign(wallet_address: str, message: str) -> str:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        owner = owners.get(str(wallet_address).lower())
        if not owner:
            raise PrefundFailed("That wallet is not linked to this chat.")
        private_key = wallet_service.decrypt_private_key_for_future_signing(
            owner
        )
        signed = Account.sign_message(
            encode_defunct(text=message), private_key=private_key
        )
        signature = signed.signature
        # hexbytes >= 1.0 drops the 0x prefix from .hex(); Venice rejects the
        # bare form with "Invalid Sign-in-with-x signature".
        return (
            signature.to_0x_hex()
            if hasattr(signature, "to_0x_hex")
            else "0x" + signature.hex().removeprefix("0x")
        )

    return sign


def _urllib_transport(method: str, url: str, *, headers=None, json_body=None):
    import urllib.request

    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=dict(headers or {}), method=method
    )

    class _Response:
        def __init__(self, status, body, response_headers):
            self.status = status
            self._body = body
            self.headers = response_headers

        def json(self):
            try:
                return json.loads(self._body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return _Response(
                response.status, response.read(), dict(response.headers)
            )
    except urllib.error.HTTPError as error:
        return _Response(error.code, error.read(), dict(error.headers or {}))


X402_LIST_CHANGES_URL = "https://x402-list.com/api/v1/changes"
PAYTO_CHANGED = "payto_changed"
_WATCHER_STATE_KEY = "payto_watcher_last_observed_at"


class PayToWatcher:
    """Pause chat when the merchant's payout address changes.

    The x402-list feed publishes only abbreviated addresses — `0xC49e…354A` —
    in both the summary and the snapshots. Two things follow:

    - matching a bound address means comparing head and tail, not identity, so
      the merchant slug is the stronger signal and is checked first;
    - the new address is unknowable from the feed, so there is nothing to
      migrate to even if migrating silently were permitted. It is not. A fresh
      approval, showing the address seen in a live challenge, is required.

    The feed also lags, so `VeniceChatClient` treats a live challenge whose
    `payTo` disagrees with the binding as the very same event.
    """

    def __init__(
        self,
        *,
        store: ChatStore,
        fetch_changes: Callable[..., list[dict[str, Any]]],
        merchant_slug: str,
        bound_pay_to: str,
        notify: Callable[[dict[str, Any]], None],
        merchant_name: str = "Venice AI",
    ):
        self.store = store
        self.fetch_changes = fetch_changes
        self.merchant_slug = str(merchant_slug or "").strip().lower()
        self.bound_pay_to = str(bound_pay_to or "").strip().lower()
        self.notify = notify
        self.merchant_name = merchant_name

    def poll(self) -> bool:
        """Handle any new payTo change. Returns True if one was acted on."""
        try:
            changes = self.fetch_changes(change_type=PAYTO_CHANGED)
        except Exception:
            # A feed outage must never pause a paying user's chat.
            return False

        last_seen = self.store.get_watcher_state(_WATCHER_STATE_KEY)
        relevant = [
            change
            for change in changes or []
            if self._affects_us(change)
            and str(change.get("observed_at") or "") > last_seen
        ]
        if not relevant:
            return False

        newest = max(relevant, key=lambda c: str(c.get("observed_at") or ""))
        users = self.store.users_bound_to(self.bound_pay_to)
        for user_id in users:
            self.store.pause(user_id, ChatState.MERCHANT_CHANGED)

        # Once per event, covering every affected user — not once per policy,
        # and not again on the next poll.
        self.store.set_watcher_state(
            _WATCHER_STATE_KEY, str(newest.get("observed_at") or "")
        )
        self.notify(
            {
                "users": users,
                "observedAt": str(newest.get("observed_at") or ""),
                "telegramText": (
                    f"{self.merchant_name} changed its payout address. "
                    "Chat is paused until you approve the new one. "
                    "Any credit you already paid for is still yours."
                ),
            }
        )
        return True

    def _affects_us(self, change: dict[str, Any]) -> bool:
        if str(change.get("type") or "") != PAYTO_CHANGED:
            return False
        if str(change.get("slug") or "").strip().lower() == self.merchant_slug:
            return True
        removed = (change.get("summary") or {}).get("payToRemoved") or []
        return any(_abbrev_matches(self.bound_pay_to, entry) for entry in removed)


def _abbrev_matches(full: str, abbreviated: str) -> bool:
    """Does `0x2670…293f` refer to this full address?

    Head and tail only: it is all the feed gives. Weaker than an identity
    check, which is why it is the fallback and the slug is the primary key.
    """
    text = str(abbreviated or "").strip().lower()
    if "…" not in text and "..." not in text:
        return text == str(full or "").strip().lower()
    separator = "…" if "…" in text else "..."
    head, _, tail = text.partition(separator)
    target = str(full or "").strip().lower()
    if not head or not tail:
        return False
    return target.startswith(head) and target.endswith(tail)


DEFAULT_WATCHER_INTERVAL_SECONDS = 3600


def start_payto_watcher(
    *,
    store: ChatStore,
    bound_pay_to: str,
    merchant_slug: str = "venice-ai",
    interval_seconds: int = DEFAULT_WATCHER_INTERVAL_SECONDS,
    notify: Callable[[dict[str, Any]], None] | None = None,
) -> "PayToWatcher":
    """Poll the changes feed on a daemon thread.

    The pause is the protection, and it is durable in the database; the notice
    is a courtesy. A user whose chat was paused finds out at their next message
    regardless, because the paused session refuses before any payment.
    """
    watcher = PayToWatcher(
        store=store,
        fetch_changes=fetch_x402_list_changes,
        merchant_slug=merchant_slug,
        bound_pay_to=bound_pay_to,
        notify=notify or _record_payto_notice(store),
    )

    def loop() -> None:
        while True:
            try:
                watcher.poll()
            except Exception:
                # Never let a watcher failure take the gateway down with it.
                pass
            time.sleep(interval_seconds)

    thread = threading.Thread(
        target=loop, name="sign402-payto-watcher", daemon=True
    )
    thread.start()
    watcher.thread = thread
    return watcher


def _record_payto_notice(store: ChatStore) -> Callable[[dict[str, Any]], None]:
    """Keep the last notice so it can be shown rather than lost.

    The gateway has no outbound Telegram channel of its own; the plugin owns
    that. Storing the notice means it survives until something can deliver it.
    """

    def record(notice: dict[str, Any]) -> None:
        store.set_watcher_state(
            "payto_last_notice",
            json.dumps(
                {
                    "observedAt": notice.get("observedAt", ""),
                    "users": notice.get("users", []),
                    "telegramText": notice.get("telegramText", ""),
                },
                separators=(",", ":"),
            ),
        )

    return record


def fetch_x402_list_changes(
    *,
    change_type: str = PAYTO_CHANGED,
    slug: str = "",
    limit: int = 50,
    opener: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Read the public x402-list changes feed. No credentials, no user data."""
    import urllib.parse
    import urllib.request

    query = {"type": change_type, "limit": str(int(limit))}
    if slug:
        query["slug"] = slug
    url = f"{X402_LIST_CHANGES_URL}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "sign402/1.0"}
    )
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else payload
    return [entry for entry in (data or []) if isinstance(entry, dict)]


@dataclass(frozen=True)
class VeniceConfig:
    bound_pay_to: str
    network: str
    asset: str
    chunk_atomic: int
    max_outstanding_atomic: int
    daily_cap_atomic: int
    base_url: str = VENICE_BASE_URL
    model: str = DEFAULT_MODEL
    estimated_cost_atomic: int = DEFAULT_ESTIMATED_COST_ATOMIC


@dataclass(frozen=True)
class ChatResult:
    text: str
    cost_atomic: int
    prefunded: bool
    remaining_window_atomic: int
    outstanding_atomic: int
    web_footer: str = ""


class VeniceChatClient:
    def __init__(
        self,
        *,
        store: ChatStore,
        transport: Callable[..., Any],
        signer: Callable[[str, str], str],
        settle: Callable[[dict[str, Any]], dict[str, Any]],
        config: VeniceConfig,
        purchases_paused: Callable[[], bool] | None = None,
        now: Callable[[], int] | None = None,
        web_search: Any = None,
    ):
        self.store = store
        self.transport = transport
        self.signer = signer
        self.settle = settle
        self.config = config
        # None means the feature is off, and every path below is the one that
        # ran before it existed.
        self.web_search = web_search
        self.purchases_paused = purchases_paused or (lambda: False)
        self.now = now or (lambda: int(time.time()))

    # -- public ----------------------------------------------------------


    def send(
        self, user_id: str, prompt: str, *, wallet_address: str
    ) -> ChatResult:
        """Send one paid message, prefunding first if local credit is short.

        The order below is the design's, and it matters: every refusal that can
        be decided without moving funds is decided before anything is paid.
        """
        session = self.store.get_session(user_id)

        # 1. Refusals that need no network call at all.
        if session.paused:
            raise _error_for(
                session.pause_reason,
                "Chat is paused and needs to be re-approved.",
            )
        if not session.bound_pay_to or not session.policy_hash:
            # The design's rule: paid chat requires an approved policy bound to
            # the merchant. Without one there is nothing authorising a spend,
            # so refuse before any request is made.
            raise PolicyMissing(
                "Approve a daily chat budget first, then you can chat without "
                "confirming every message."
            )
        if session.policy_expired:
            # Refuse before the 402 request is even made: an expired policy
            # cannot authorise a payment, so there is nothing to ask about.
            raise PolicyExpired(
                "Your chat approval has expired. Approve a new one to continue; "
                "any credit you already paid for is still yours."
            )

        # A globally paused gateway does not even read the provider: there is
        # nothing it could do with the answer.
        if self.purchases_paused():
            raise PrefundFailed(
                "Purchases are paused right now. Try again later."
            )

        prefunded = False
        # 2. Ask Venice whether this wallet can spend. This is the integration
        #    guide's own step, and it is authoritative: our local ledger is a
        #    record of what we paid, not of what the provider will honour, and
        #    the two drift after any failure. `canConsume` is their answer.
        if not self._can_consume(
            wallet_address, fallback_atomic=session.outstanding_atomic
        ):
            self._prefund(user_id, session, wallet_address=wallet_address)
            prefunded = True

        # 3. Ask Venice. A failure after a fresh prefund is not the same as a
        #    failure on existing credit: the first means money moved and no
        #    answer came back.
        try:
            text, remaining, web_footer = self._answer(
                user_id, prompt, wallet_address=wallet_address
            )
        except ProviderUnavailable:
            if prefunded:
                self.store.pause(user_id, ChatState.RECONCILIATION_REQUIRED)
                raise ReconciliationRequired(
                    "A payment went through but the answer did not arrive. "
                    "Chat is paused and your credit is preserved."
                ) from None
            raise

        # 4. Debit what Venice actually charged.
        cost = self._debit_actual_cost(
            user_id, remaining, wallet_address=wallet_address
        )
        session = self.store.get_session(user_id)
        return ChatResult(
            text=text,
            cost_atomic=cost,
            prefunded=prefunded,
            remaining_window_atomic=self._remaining_window(session),
            outstanding_atomic=session.outstanding_atomic,
            web_footer=web_footer,
        )

    def _answer(
        self, user_id: str, prompt: str, *, wallet_address: str
    ) -> tuple[str, str | None, str]:
        """One completion, or one search and one completion.

        With `web_search` unset this is exactly `_ask` and nothing else, which
        is what keeps the feature flag honest.
        """
        if self.web_search is None:
            text, remaining = self._ask(
                user_id, prompt, wallet_address=wallet_address
            )
            return text, remaining, ""

        from .web_search import answer_with_web

        # `_ask` reports the balance header alongside the text; the web turn
        # only deals in text, so the last header seen is kept here.
        seen: dict[str, str | None] = {"remaining": None}

        def ask(text: str) -> str:
            answer, remaining = self._ask(
                user_id, text, wallet_address=wallet_address
            )
            seen["remaining"] = remaining
            return answer

        result = answer_with_web(
            ask=ask,
            search=self.web_search,
            user_id=user_id,
            message=prompt,
            wallet_address=wallet_address,
        )
        return result.text, seen["remaining"], result.footer

    # -- prefund ---------------------------------------------------------

    def _prefund(
        self, user_id: str, session, *, wallet_address: str
    ) -> None:
        if self.purchases_paused():
            raise PrefundFailed("Purchases are paused right now. Try again later.")

        chunk = self.config.chunk_atomic

        # Cap checks that need no network call.
        remaining = self._remaining_window(session)
        if remaining <= 0:
            raise WindowExhausted(
                "Today's budget is spent. It resets at 00:00 UTC."
            )
        if chunk > remaining:
            raise WindowExhausted(
                "Today's budget cannot cover the next top-up. "
                "It resets at 00:00 UTC."
            )
        if session.outstanding_atomic + chunk > self.config.max_outstanding_atomic:
            raise PrefundFailed(
                "Too much credit is already held for this chat."
            )

        try:
            claim = self.store.claim_prefund(user_id)
        except PrefundClaimUnavailable:
            raise PrefundFailed(
                "Another message is already topping up. Try again in a moment."
            ) from None

        with claim:
            # Re-check under the claim: a concurrent message may have just
            # topped up. Ask the provider again rather than our own ledger —
            # it is the same authority the decision to get here was made on.
            session = self.store.get_session(user_id)
            if self._can_consume(
                wallet_address, fallback_atomic=session.outstanding_atomic
            ):
                return

            requirement = self._top_up_challenge(wallet_address)
            self._validate_binding(user_id, requirement)

            amount = int(requirement["amount"])
            if amount > self._remaining_window(session):
                raise WindowExhausted(
                    "Today's budget cannot cover the next top-up. "
                    "It resets at 00:00 UTC."
                )
            if (
                session.outstanding_atomic + amount
                > self.config.max_outstanding_atomic
            ):
                raise PrefundFailed(
                    "Too much credit is already held for this chat."
                )

            # The buyer performs the paid POST itself: it fetches the 402,
            # signs under the approved terms, and retries. There is no separate
            # confirm step, and repeating the POST here would only earn another
            # 402.
            try:
                settlement = self.settle(requirement, user_id=user_id)
            except Exception as exc:
                # Nothing was recorded, so nothing needs unwinding. Log why:
                # without this the user sees "try again shortly" and the
                # operator sees an empty journal, which is what happened on the
                # first live attempt. Type and message only — no prompt text,
                # no key material.
                logger.warning(
                    "Venice top-up failed: %s: %s",
                    type(exc).__name__,
                    str(exc)[:200],
                )
                raise PrefundFailed(
                    "The top-up did not go through. Try again shortly."
                ) from None

            if not isinstance(settlement, dict) or not settlement.get("ok"):
                # The payment may or may not have landed. Never assume it did.
                self.store.pause(user_id, ChatState.RECONCILIATION_REQUIRED)
                raise ReconciliationRequired(
                    "A top-up could not be confirmed. Chat is paused and "
                    "nothing further will be charged."
                )

            self.store.record_prefund(user_id, amount)

    def _can_consume(self, wallet_address: str, *, fallback_atomic: int) -> bool:
        """`GET /x402/balance/{address}` — the documented pre-flight check.

        Venice answers with `canConsume`, `balanceUsd` and `minimumTopUpUsd`.
        We take `canConsume` at face value: it is the only party that knows
        what it will honour. If the call itself fails we fall back to our own
        ledger rather than blocking the user on a read.
        """
        try:
            response = self.transport(
                "GET",
                f"{self.config.base_url}/x402/balance/{wallet_address}",
                headers=self._auth_headers(wallet_address),
            )
            if response.status != 200:
                raise ValueError("balance unavailable")
            data = (response.json() or {}).get("data") or {}
            return bool(data.get("canConsume"))
        except Exception:
            return fallback_atomic >= VENICE_MINIMUM_BALANCE_ATOMIC

    def _top_up_challenge(self, wallet_address: str) -> dict[str, Any]:
        response = self.transport(
            "POST",
            f"{self.config.base_url}/x402/top-up",
            headers=self._auth_headers(wallet_address),
            json_body={},
        )
        if response.status != 402:
            raise PrefundFailed("The top-up did not go through. Try again shortly.")
        payload = response.json() or {}
        for entry in payload.get("accepts") or []:
            if str(entry.get("network", "")).startswith("eip155"):
                return entry
        raise PrefundFailed("No supported payment option was offered.")

    def _validate_binding(self, user_id: str, requirement: dict[str, Any]) -> None:
        """`payTo` is the binding. Domain and URL are never sufficient.

        Checked against the address this user approved, not the operator's
        configured default: the approval is what authorises the spend, so
        changing the env must never silently re-point an existing policy.
        """
        bound = str(
            self.store.get_session(user_id).bound_pay_to
            or self.config.bound_pay_to
            or ""
        ).strip().lower()
        offered = str(requirement.get("payTo") or "").strip().lower()
        network = str(requirement.get("network") or "").strip().lower()
        asset = str(requirement.get("asset") or "").strip().lower()

        mismatched = (
            offered != bound
            or network != str(self.config.network).strip().lower()
            or asset != str(self.config.asset).strip().lower()
        )
        if mismatched:
            self.store.pause(user_id, ChatState.MERCHANT_CHANGED)
            raise MerchantChanged(
                "Venice AI changed its payout details. "
                "Chat is paused until you approve the new ones."
            )

    # -- chat ------------------------------------------------------------

    def _ask(
        self, user_id: str, prompt: str, *, wallet_address: str
    ) -> tuple[str, str | None]:
        response = self.transport(
            "POST",
            f"{self.config.base_url}/chat/completions",
            headers=self._auth_headers(wallet_address),
            json_body={
                "model": (
                    self.store.get_session(user_id).model or self.config.model
                ),
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if response.status != 200:
            # Status and error code only. A refusal here is either "this wallet
            # has no prepaid balance" or "the signature was rejected", and
            # telling those apart without a log means guessing. Prompt text and
            # model output are never logged.
            logger.warning(
                "Venice chat refused: status=%s code=%s",
                response.status,
                _provider_error_code(response),
            )
            if response.status == 402:
                # The provider is up and answering; it wants funding. "Try
                # again" would be false — nothing changes until it is paid.
                raise ProviderOutOfFunds(
                    "This chat has no credit yet. Approve a daily budget to "
                    "top it up."
                )
            raise ProviderUnavailable(
                "The AI provider is not responding. Try again in a moment."
            )
        payload = response.json() or {}
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.warning(
                "Venice chat returned an unreadable body: status=%s",
                response.status,
            )
            raise ProviderUnavailable(
                "The AI provider returned an unreadable answer."
            ) from None
        return str(text), _header(response, BALANCE_REMAINING_HEADER)

    def _debit_actual_cost(
        self, user_id: str, remaining: str | None, *, wallet_address: str
    ) -> int:
        """Charge what the provider actually charged.

        Venice documents `X-Balance-Remaining` as optional and does not send
        it, so the balance endpoint is the real source. Estimating instead
        billed a flat $0.10 a message against a true cost of a fraction of a
        cent — thirty times high, and the ledger emptied long before the credit
        did.
        """
        before = self.store.get_session(user_id).outstanding_atomic
        reported = _to_atomic(remaining)
        if reported is None:
            reported = self._provider_balance_atomic(wallet_address)
        if reported is None:
            # Both the header and the balance call failed. Charge nothing
            # rather than invent a number: the next message reconciles.
            logger.warning("Venice reported no balance; message left unbilled")
            return 0

        self.store.reconcile_outstanding(user_id, reported)
        return max(0, before - reported)

    def _provider_balance_atomic(self, wallet_address: str) -> int | None:
        try:
            response = self.transport(
                "GET",
                f"{self.config.base_url}/x402/balance/{wallet_address}",
                headers=self._auth_headers(wallet_address),
            )
            if response.status != 200:
                return None
            data = (response.json() or {}).get("data") or {}
            return _to_atomic(str(data.get("balanceUsd")))
        except Exception:
            return None

    # -- sign-in-with-x --------------------------------------------------

    def _auth_headers(self, wallet_address: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            SIWX_HEADER: self._siwx_header(wallet_address),
        }

    def _siwx_header(self, wallet_address: str) -> str:
        """Build the EIP-4361 header Venice authenticates the wallet with.

        This is authentication, not payment: it tells Venice whose prepaid
        balance to meter against. A fresh nonce and timestamp per request flow.
        """
        issued_at = _iso8601(self.now())
        expiration = _iso8601(self.now() + 300)
        chain_id = _chain_id(self.config.network)
        nonce = _nonce()
        message = (
            "api.venice.ai wants you to sign in with your Ethereum account:\n"
            f"{wallet_address}\n\n"
            "Sign in to Venice AI\n\n"
            f"URI: {self.config.base_url}\n"
            "Version: 1\n"
            f"Chain ID: {chain_id}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at}\n"
            f"Expiration Time: {expiration}"
        )
        signature = self.signer(wallet_address, message)
        envelope = {
            "address": wallet_address,
            "message": message,
            "signature": signature,
            "timestamp": issued_at,
            "chainId": chain_id,
        }
        return base64.b64encode(
            json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

    # -- helpers ---------------------------------------------------------

    def _remaining_window(self, session) -> int:
        # The approved policy's cap wins over the configured default: raising a
        # limit has to actually take effect, and it only exists per user.
        cap = session.daily_cap_atomic or self.config.daily_cap_atomic
        return max(0, cap - session.spent_atomic_this_window)


def _error_for(state: str, message: str) -> ChatError:
    for error in (
        WindowExhausted,
        MerchantChanged,
        ProviderUnavailable,
        PrefundFailed,
        ReconciliationRequired,
        PolicyExpired,
    ):
        if error.state == state:
            return error(message)
    return PrefundFailed(message)


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None) or {}
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return str(value)
    return None


def _to_atomic(value: str | None) -> int | None:
    """Venice reports the remaining balance in dollars, not atomic units."""
    if value is None:
        return None
    try:
        return int(
            (Decimal(str(value)) * (10**USDC_DECIMALS)).to_integral_value()
        )
    except (InvalidOperation, ValueError, TypeError):
        return None


def _chain_id(network: str) -> int:
    text = str(network or "")
    if ":" in text:
        try:
            return int(text.split(":", 1)[1])
        except ValueError:
            return 0
    return 0


def _iso8601(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def _nonce() -> str:
    """A fresh nonce per request.

    Venice rejects a repeat with `X402_SIGN_IN_NONCE_REUSED`, and a prefund
    makes three signed requests back to back. Anything derived from the clock
    collides whenever two of them land in the same second, so this is random.
    """
    return secrets.token_hex(12)
