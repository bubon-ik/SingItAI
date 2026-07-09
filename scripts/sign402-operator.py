#!/usr/bin/env python3
"""Operator diagnostics for the Sign402 hosted Telegram bot.

The tool is intentionally local-first: it reads SQLite stores directly for
diagnostics, redacts private material, and sends mutating iMessage unlink
requests through the authenticated localhost gateway endpoint.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_GATEWAY_URL = "http://127.0.0.1:8099"
DEFAULT_USER_WALLET_DB = Path.home() / ".sign402" / "user-wallets.db"
DEFAULT_IMESSAGE_DB = Path.home() / ".sign402" / "imessage-approvals.db"
DEFAULT_BANKR_LLM_DB = Path.home() / ".sign402" / "bankr-llm.db"
DEFAULT_SPEND_LIMITS_JSON = Path.home() / ".sign402" / "user-spend-limits.json"
DEFAULT_BITREFILL_DB = (
    Path.home() / "apps" / "sign402" / "demo-dashboard" / "bitrefill-orders.sqlite3"
)

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


@dataclasses.dataclass(frozen=True)
class OperatorConfig:
    user_wallet_db: Path = DEFAULT_USER_WALLET_DB
    imessage_db: Path = DEFAULT_IMESSAGE_DB
    bankr_llm_db: Path = DEFAULT_BANKR_LLM_DB
    bitrefill_db: Path = DEFAULT_BITREFILL_DB
    spend_limits_json: Path = DEFAULT_SPEND_LIMITS_JSON
    master_key: str = ""
    gateway_url: str = DEFAULT_GATEWAY_URL
    photon_api_token: str = ""


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = load_config()

    try:
        if args.command == "user":
            print(build_user_report(config, args.telegram_id))
        elif args.command == "find-imessage":
            print(find_imessage_report(config, args.phone))
        elif args.command == "pending":
            print(build_pending_report(config, args.telegram_id))
        elif args.command == "last-purchase":
            print(build_last_purchase_report(config, args.telegram_id))
        elif args.command == "unlink-imessage":
            result = unlink_imessage(
                config,
                telegram_id=args.telegram_id or "",
                phone=args.phone or "",
            )
            print(result.get("telegramText") or json.dumps(result, sort_keys=True))
        else:
            raise SystemExit("unknown command")
    except SystemExit:
        raise
    except Exception as exc:
        print(f"operator error: {exc}", file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and repair local Sign402 user state."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    user = subparsers.add_parser("user", help="Show one Telegram user's state")
    user.add_argument("--telegram-id", required=True)

    find_imessage = subparsers.add_parser(
        "find-imessage",
        help="Find Telegram user linked to an iMessage phone number",
    )
    find_imessage.add_argument("phone")

    pending = subparsers.add_parser("pending", help="Show pending iMessage approval")
    pending.add_argument("--telegram-id", required=True)

    last_purchase = subparsers.add_parser(
        "last-purchase",
        help="Show latest Bankr/Bitrefill purchase state",
    )
    last_purchase.add_argument("--telegram-id", required=True)

    unlink = subparsers.add_parser(
        "unlink-imessage",
        help="Unlink iMessage approvals through the local gateway",
    )
    unlink.add_argument("--telegram-id", default="")
    unlink.add_argument("--phone", default="")
    return parser


def load_config(env: dict[str, str] | None = None) -> OperatorConfig:
    values = _load_env_values(env or os.environ)
    return OperatorConfig(
        user_wallet_db=Path(
            values.get("SIGN402_USER_WALLET_STORE_PATH") or DEFAULT_USER_WALLET_DB
        ),
        imessage_db=Path(
            values.get("SIGN402_IMESSAGE_APPROVAL_STORE_PATH") or DEFAULT_IMESSAGE_DB
        ),
        bankr_llm_db=Path(
            values.get("SIGN402_BANKR_LLM_STORE_PATH") or DEFAULT_BANKR_LLM_DB
        ),
        bitrefill_db=Path(
            values.get("SIGN402_BITREFILL_COMMERCE_STORE_PATH") or DEFAULT_BITREFILL_DB
        ),
        spend_limits_json=Path(
            values.get("SIGN402_USER_SPEND_LIMIT_STORE_PATH")
            or DEFAULT_SPEND_LIMITS_JSON
        ),
        master_key=str(values.get("SIGN402_WALLET_MASTER_KEY") or ""),
        gateway_url=str(values.get("SIGN402_GATEWAY_URL") or DEFAULT_GATEWAY_URL),
        photon_api_token=str(values.get("SIGN402_PHOTON_API_TOKEN") or ""),
    )


def _load_env_values(base_env: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (
        Path.home() / ".hermes" / ".env",
        Path("/etc/sign402-gateway.env"),
        Path.cwd() / "sign402-gateway" / ".env",
    ):
        values.update(_parse_env_file(path))
    values.update({key: str(value) for key, value in base_env.items()})
    return values


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("Environment="):
            line = line.removeprefix("Environment=")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        result[key] = value.strip().strip('"').strip("'")
    return result


def build_user_report(config: OperatorConfig, telegram_id: str) -> str:
    user_id = str(telegram_id).strip()
    wallet = _wallet_for_user(config.user_wallet_db, user_id)
    imessage = _imessage_for_user(config, user_id)
    pending = _pending_for_user(config.imessage_db, user_id)
    limits = _limits_for_user(config.spend_limits_json, user_id)
    bankr = _latest_bankr_for_user(config.bankr_llm_db, user_id)
    bitrefill = _latest_bitrefill_for_user(config.bitrefill_db, user_id)

    username = _display_username(wallet.get("telegram_username", "") if wallet else "")
    lines = [f"Telegram: {user_id}{username}"]
    if wallet:
        lines.append(f"Wallet: {wallet['wallet_address']}")
        lines.append(f"Wallet status: {wallet['status']} ({wallet['chain']})")
    else:
        lines.append("Wallet: missing")

    lines.append(_format_imessage_line(imessage))
    lines.append(_format_pending_line(pending))
    lines.append(_format_limits_line(limits))
    lines.append(_format_bankr_line(bankr))
    lines.append(_format_bitrefill_line(bitrefill))
    return "\n".join(lines)


def find_imessage_report(config: OperatorConfig, phone: str) -> str:
    normalized = normalize_e164(phone)
    row = _imessage_for_phone(config, normalized)
    lines = [f"iMessage: {normalized}"]
    if row:
        lines.append(f"Telegram: {row['telegram_user_id']}")
        lines.append(f"Linked at: {_format_time(row['created_at'])}")
    else:
        lines.append("Telegram: not linked")
    return "\n".join(lines)


def build_pending_report(config: OperatorConfig, telegram_id: str) -> str:
    pending = _pending_for_user(config.imessage_db, str(telegram_id).strip())
    return _format_pending_line(pending)


def build_last_purchase_report(config: OperatorConfig, telegram_id: str) -> str:
    user_id = str(telegram_id).strip()
    return "\n".join(
        [
            _format_bankr_line(_latest_bankr_for_user(config.bankr_llm_db, user_id)),
            _format_bitrefill_line(
                _latest_bitrefill_for_user(config.bitrefill_db, user_id)
            ),
        ]
    )


def unlink_imessage(
    config: OperatorConfig,
    *,
    telegram_id: str = "",
    phone: str = "",
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> dict[str, Any]:
    user_id = str(telegram_id or "").strip()
    phone_value = str(phone or "").strip()
    if not user_id and not phone_value:
        raise SystemExit("Provide a Telegram ID or phone to unlink.")
    if not config.photon_api_token:
        raise SystemExit("SIGN402_PHOTON_API_TOKEN is required for unlink.")

    payload: dict[str, Any] = {}
    if user_id:
        payload["telegramUserId"] = user_id
    if phone_value:
        payload["photonUserId"] = normalize_e164(phone_value)

    request = urllib.request.Request(
        f"{config.gateway_url.rstrip('/')}/agent/imessage/unlink",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.photon_api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        response = opener(request, timeout=timeout)
        body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        raise SystemExit(_safe_gateway_error(body) or f"gateway rejected unlink: {exc.code}") from None
    finally:
        close = locals().get("response", None)
        if close is not None and callable(getattr(close, "close", None)):
            close.close()
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("gateway returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("gateway returned invalid JSON")
    return parsed


def normalize_e164(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("phone is required")
    if text.startswith("+"):
        compact = "+" + re.sub(r"\D", "", text[1:])
    else:
        compact = re.sub(r"\D", "", text)
    if not compact.startswith("+"):
        compact = "+" + compact
    if not E164_RE.fullmatch(compact):
        raise ValueError("phone must be E.164, for example +420773173967")
    return compact


def _wallet_for_user(path: Path, user_id: str) -> dict[str, Any] | None:
    with _connect_readonly(path) as db:
        if db is None:
            return None
        row = db.execute(
            """
            SELECT telegram_user_id, telegram_username, chain, wallet_address, status,
                   created_at, updated_at
            FROM user_wallets
            WHERE telegram_user_id = ?
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def _imessage_for_user(config: OperatorConfig, user_id: str) -> dict[str, Any] | None:
    with _connect_readonly(config.imessage_db) as db:
        if db is None:
            return None
        row = db.execute(
            """
            SELECT telegram_user_id, encrypted_photon_user_id, created_at, updated_at
            FROM imessage_links
            WHERE telegram_user_id = ?
            """,
            (user_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["phone"] = _decrypt_phone(config.master_key, result["encrypted_photon_user_id"])
    result.pop("encrypted_photon_user_id", None)
    return result


def _imessage_for_phone(config: OperatorConfig, phone: str) -> dict[str, Any] | None:
    if not config.master_key:
        raise SystemExit("SIGN402_WALLET_MASTER_KEY is required to search iMessage links.")
    digest = hmac.new(
        config.master_key.encode("utf-8"),
        f"photon:{phone}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    with _connect_readonly(config.imessage_db) as db:
        if db is None:
            return None
        row = db.execute(
            """
            SELECT telegram_user_id, created_at, updated_at
            FROM imessage_links
            WHERE photon_digest = ?
            """,
            (digest,),
        ).fetchone()
    return dict(row) if row else None


def _pending_for_user(path: Path, user_id: str) -> dict[str, Any] | None:
    now = int(time.time())
    with _connect_readonly(path) as db:
        if db is None:
            return None
        row = db.execute(
            """
            SELECT approval_id, action_type, status, expires_at, created_at
            FROM imessage_approvals
            WHERE telegram_user_id = ?
              AND status = 'pending'
              AND expires_at > ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (user_id, now),
        ).fetchone()
    return dict(row) if row else None


def _limits_for_user(path: Path, user_id: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    limits = payload.get("limits") if isinstance(payload, dict) else None
    user_limits = limits.get(user_id) if isinstance(limits, dict) else None
    return dict(user_limits) if isinstance(user_limits, dict) else None


def _latest_bankr_for_user(path: Path, user_id: str) -> dict[str, Any] | None:
    with _connect_readonly(path) as db:
        if db is None:
            return None
        row = db.execute(
            """
            SELECT purchase_id, amount_usd, state, email, bankr_wallet_address,
                   api_key_fingerprint, transfer_hash, error_code, error_message,
                   created_at, updated_at
            FROM bankr_llm_purchases
            WHERE telegram_user_id = ?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def _latest_bitrefill_for_user(path: Path, user_id: str) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    with _connect_readonly(path) as db:
        if db is None:
            return None
        rows = db.execute(
            """
            SELECT quote_id, state, quote_json, metadata_json, created_at, updated_at
            FROM bitrefill_orders
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 200
            """
        ).fetchall()
    for row in rows:
        item = dict(row)
        quote = _loads_json_dict(item.get("quote_json"))
        metadata = _loads_json_dict(item.get("metadata_json"))
        if _json_contains_user_id(quote, user_id) or _json_contains_user_id(
            metadata,
            user_id,
        ):
            item["quote"] = quote
            item["metadata"] = metadata
            latest = item
            break
    return latest


class _ReadonlyConnection:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.db: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        uri = "file:" + urllib.parse.quote(str(self.path.resolve())) + "?mode=ro"
        self.db = sqlite3.connect(uri, uri=True, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.db is not None:
            self.db.close()


def _connect_readonly(path: Path) -> _ReadonlyConnection:
    return _ReadonlyConnection(path)


def _decrypt_phone(master_key: str, encrypted_value: str) -> str:
    if not master_key:
        return "linked"
    try:
        return Fernet(master_key.encode("ascii")).decrypt(
            str(encrypted_value).encode("ascii")
        ).decode("utf-8")
    except (ValueError, InvalidToken, UnicodeDecodeError):
        return "linked"


def _display_username(username: str) -> str:
    value = str(username or "").strip()
    return f" (@{value})" if value else ""


def _format_imessage_line(row: dict[str, Any] | None) -> str:
    if not row:
        return "iMessage: not linked"
    phone = str(row.get("phone") or "linked")
    return f"iMessage: linked {phone}"


def _format_pending_line(row: dict[str, Any] | None) -> str:
    if not row:
        return "Pending approval: none"
    return (
        f"Pending approval: {row['approval_id']} {row['action_type']} "
        f"expires {_format_time(row['expires_at'])}"
    )


def _format_limits_line(row: dict[str, Any] | None) -> str:
    if not row:
        return "Limits: default/operator"
    return (
        "Limits: "
        f"max {_format_usdc_atomic(row.get('maxPerTxAtomic'))} USDC / "
        f"day {_format_usdc_atomic(row.get('dailyCapAtomic'))} USDC"
    )


def _format_bankr_line(row: dict[str, Any] | None) -> str:
    if not row:
        return "Bankr LLM: no purchases"
    suffix = ""
    if row.get("api_key_fingerprint"):
        suffix = f" key {row['api_key_fingerprint']}"
    if row.get("error_code"):
        suffix = f" error {row['error_code']}"
    return f"Bankr LLM: {row['state']} ${row['amount_usd']}{suffix}"


def _format_bitrefill_line(row: dict[str, Any] | None) -> str:
    if not row:
        return "Bitrefill: no purchases"
    quote = row.get("quote") if isinstance(row.get("quote"), dict) else {}
    product = str(
        quote.get("productName")
        or quote.get("name")
        or quote.get("productId")
        or row.get("quote_id")
    )
    price = str(quote.get("priceUsd") or quote.get("amountUsd") or "").strip()
    price_text = f" ${price}" if price else ""
    return f"Bitrefill: {row['state']} {product}{price_text}"


def _format_usdc_atomic(value: Any) -> str:
    try:
        atomic = int(value)
    except (TypeError, ValueError):
        return "default"
    whole = atomic // 1_000_000
    fractional = str(atomic % 1_000_000).rjust(6, "0").rstrip("0")
    return str(whole) if not fractional else f"{whole}.{fractional}"


def _format_time(value: Any) -> str:
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(epoch))


def _loads_json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_contains_user_id(value: Any, user_id: str) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"telegramUserId", "telegram_user_id", "userId"} and str(item) == user_id:
                return True
            if _json_contains_user_id(item, user_id):
                return True
    if isinstance(value, list):
        return any(_json_contains_user_id(item, user_id) for item in value)
    return False


def _safe_gateway_error(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("telegramText", "imessageText", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
