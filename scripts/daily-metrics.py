#!/usr/bin/env python3
"""A day's SingIt metrics, read straight from the gateway's SQLite stores.

Read-only: every database is opened with `file:PATH?mode=ro` (see
`scripts/sign402-operator.py`'s `_connect_readonly`), so this can run on the live VPS beside the
gateway without risking its state. A missing database, or a table that is not there yet on an
older deploy, is reported as `null` rather than crashing the whole run. Output is aggregate
counts and sums only — no wallet address or account id is ever printed.

    python scripts/daily-metrics.py --day 2026-10-17
    python scripts/daily-metrics.py --out ~/.sign402/metrics          # writes DIR/YYYY-MM-DD.json
    python scripts/daily-metrics.py --text                           # a ~10-line summary too

The day defaults to yesterday in Europe/Prague (the cron runs at 00:10 there); `--day` overrides
it. DB paths default to `$SIGN402_STATE_DIR/<file>` (falling back to `~/.sign402/<file>`), each
overridable by the same environment variable the gateway itself reads, or by a CLI flag. The
Telegram Bitrefill orders database has no default path at all — it lives outside the state dir
on deployments that have it, and is only read when `--bitrefill-orders-db` or
`SIGN402_BITREFILL_COMMERCE_STORE_PATH` names it; otherwise its section is reported as `null`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ_NAME = "Europe/Prague"
# solana_allowance.py's details for a Solana op that was never signed (expire_stale_prepared and
# supersede_prepared); keep in sync.
SOLANA_UNSIGNED_DETAILS = ("Not signed in time.", "Replaced by a newer request.")

STATE_DIR_ENV = "SIGN402_STATE_DIR"
DEFAULT_STATE_DIR = "~/.sign402"

WEB_DB_ENV = "SIGN402_WEB_DB"
WEB_DB_FILENAME = "web.db"
ALLOWANCE_DB_ENV = "SIGN402_ALLOWANCE_DB"
ALLOWANCE_DB_FILENAME = "allowance.db"
SOLANA_ALLOWANCE_DB_ENV = "SIGN402_SOLANA_ALLOWANCE_DB"
SOLANA_ALLOWANCE_DB_FILENAME = "solana-allowance.db"
USER_WALLET_DB_ENV = "SIGN402_USER_WALLET_STORE_PATH"
USER_WALLET_DB_FILENAME = "user-wallets.db"
# No filename/default here on purpose: this database lives outside the state dir (a separate
# demo-dashboard host path on deployments that have it at all) and must be named explicitly.
BITREFILL_ORDERS_DB_ENV = "SIGN402_BITREFILL_COMMERCE_STORE_PATH"

# Known Telegram Bitrefill order states, grouped the way an operator reads them; anything not
# listed here (a future state, or a typo) lands in "other" rather than being dropped or crashing.
BITREFILL_STATE_BUCKETS = {
    "DELIVERED": "delivered",
    "USER_REJECTED": "user_rejected",
    "QUOTED": "pending",
    "FULFILLING": "pending",
    "RECONCILIATION_REQUIRED": "needs_reconciliation",
    "FULFILLMENT_FAILED": "failed",
    "REFUNDED": "failed",
    "QUOTE_EXPIRED": "expired",
}


def day_window(day: datetime.date, tz_name: str = TZ_NAME) -> tuple[int, int]:
    """Unix seconds [start, end) for one local midnight-to-midnight day."""
    tz = ZoneInfo(tz_name)
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + datetime.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def yesterday(tz_name: str = TZ_NAME) -> datetime.date:
    return (datetime.datetime.now(ZoneInfo(tz_name)) - datetime.timedelta(days=1)).date()


class _ReadonlyConnection:
    """Same shape as `sign402-operator.py`'s `_connect_readonly`: None for a missing file."""

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

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.db is not None:
            self.db.close()


def _connect_readonly(path: Path) -> _ReadonlyConnection:
    return _ReadonlyConnection(path)


def _scalar(db: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    """A query's first value, or None if the table it needs is not there."""
    try:
        row = db.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row is not None else None


def _rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row] | None:
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return None


def _usd(atomic: Any) -> str | None:
    if atomic is None:
        return None
    return f"{int(atomic) / 1_000_000:.2f}"


def _merge_counts(a: dict[str, int] | None, b: dict[str, int] | None) -> dict[str, int] | None:
    if a is None and b is None:
        return None
    out = dict(a or {})
    for key, value in (b or {}).items():
        out[key] = out.get(key, 0) + value
    return out


def _add_count(a: int | None, b: int | None) -> int | None:
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


# A settlement resource that is a balance top-up, not a purchase (web.db's own `credit_topups`
# already counts it; counting it again here would double it). Matches
# CREDIT_URL in web_balance.py ("https://ask.singitai.app/v1/credit").
_SETTLEMENT_CATEGORY_SQL = """
    CASE WHEN resource LIKE 'bitrefill:invoice:%' THEN 'bitrefill'
         WHEN resource LIKE '%/v1/credit' THEN 'credit_topup'
         WHEN resource LIKE '%/chat/completions/metered' OR resource LIKE '%/chat/completions' THEN 'chat_answer'
         ELSE 'data_tool' END
"""


def web_metrics(path: Path, s: int, e: int) -> dict[str, Any] | None:
    with _connect_readonly(path) as db:
        if db is None:
            return None
        out: dict[str, Any] = {}
        new_rows = _rows(db, "SELECT account_id FROM accounts WHERE created_at >= ? AND created_at < ?", (s, e))
        if new_rows is None:
            out["new_accounts"] = None
        else:
            out["new_accounts"] = {"base": 0, "solana": 0}
            for row in new_rows:
                chain = "solana" if str(row["account_id"]).startswith("solana:") else "base"
                out["new_accounts"][chain] += 1
        out["total_accounts"] = _scalar(db, "SELECT COUNT(*) FROM accounts")
        # `sessions` rows are deleted once they expire, so counting them is only ever a lower
        # bound for "today", not a reliable daily figure — prefer the durable `events` table.
        signed_in_from_events = _scalar(
            db, "SELECT COUNT(DISTINCT uid) FROM events WHERE name = 'sign_in' AND at >= ? AND at < ?", (s, e))
        out["signed_in_today"] = signed_in_from_events
        out["signed_in_today_source"] = "events" if signed_in_from_events is not None else "unavailable"
        out["sessions_rows_today_unreliable"] = _scalar(
            db, "SELECT COUNT(DISTINCT account_id) FROM sessions WHERE created_at >= ? AND created_at < ?", (s, e))
        out["chat_answers"] = _scalar(
            db, "SELECT COUNT(*) FROM chat_usage WHERE created_at >= ? AND created_at < ?", (s, e))
        out["chat_payers"] = _scalar(
            db, "SELECT COUNT(DISTINCT account_id) FROM chat_usage WHERE created_at >= ? AND created_at < ?", (s, e))
        chat_cost = _scalar(
            db, "SELECT SUM(cost_atomic) FROM chat_usage WHERE created_at >= ? AND created_at < ?", (s, e))
        out["chat_cost_usd"] = _usd(chat_cost)
        topup_rows = _rows(
            db, "SELECT state, COUNT(*) n, SUM(COALESCE(paid_atomic, amount_atomic)) usd FROM credit_topups"
                " WHERE created_at >= ? AND created_at < ? GROUP BY state", (s, e))
        if topup_rows is None:
            out["topups"] = None
            out["topups_credited_usd"] = None
        else:
            out["topups"] = {row["state"]: {"count": row["n"], "usd": _usd(row["usd"])} for row in topup_rows}
            credited = out["topups"].get("credited")
            out["topups_credited_usd"] = credited["usd"] if credited else "0.00"
        out["api_keys_created"] = _scalar(
            db, "SELECT COUNT(*) FROM api_keys WHERE created_at >= ? AND created_at < ?", (s, e))
        event_rows = _rows(
            db, "SELECT name, COUNT(*) n FROM events WHERE at >= ? AND at < ? GROUP BY name", (s, e))
        out["events"] = {row["name"]: row["n"] for row in event_rows} if event_rows is not None else None
        pause_props = _rows(
            db, "SELECT props_json FROM events WHERE name = 'pause' AND at >= ? AND at < ?", (s, e))
        if pause_props is None:
            out["_pause_triggers"] = None
        else:
            triggers: dict[str, int] = {}
            for row in pause_props:
                try:
                    trigger = json.loads(row["props_json"]).get("trigger", "unknown")
                except (ValueError, TypeError, AttributeError):
                    trigger = "unknown"
                triggers[trigger] = triggers.get(trigger, 0) + 1
            out["_pause_triggers"] = triggers
        return out


def base_allowance_metrics(path: Path, s: int, e: int) -> dict[str, Any] | None:
    with _connect_readonly(path) as db:
        if db is None:
            return None
        out: dict[str, Any] = {}
        out["limits_set"] = _scalar(
            db, "SELECT COUNT(*) FROM limiters WHERE created_at >= ? AND created_at < ? AND status <> 'REJECTED'",
            (s, e))
        # operations.kind is only ever GRANT, REVOKE or PAUSE on Base (unlike Solana's ops table,
        # which also has FUND_GAS) — see the CHECK constraint OPERATIONS_SQL builds in agent_allowance.py.
        op_rows = _rows(
            db, "SELECT kind, state, COUNT(*) n FROM operations WHERE created_at >= ? AND created_at < ?"
                " GROUP BY kind, state", (s, e))
        out["_operations"] = op_rows  # consumed by the caller; not part of the public summary
        purchase_rows = _rows(
            db, f"SELECT {_SETTLEMENT_CATEGORY_SQL} cat, COUNT(*) n, COUNT(DISTINCT user_id) buyers, SUM(amount) usd"
                " FROM settlements WHERE created_at >= ? AND created_at < ? GROUP BY cat", (s, e))
        if purchase_rows is None:
            out["purchases"] = None
            out["usdc_volume_base"] = None
        else:
            out["purchases"] = {row["cat"]: {"count": row["n"], "buyers": row["buyers"], "usd": _usd(row["usd"])}
                                for row in purchase_rows}
            # Credit top-ups are not purchases (and web.db's credit_topups already counts them).
            out["usdc_volume_base"] = _usd(
                sum(int(row["usd"] or 0) for row in purchase_rows if row["cat"] != "credit_topup"))
        out["active_spenders"] = _scalar(
            db, "SELECT COUNT(DISTINCT user_id) FROM settlements WHERE created_at >= ? AND created_at < ?"
                " AND resource NOT LIKE '%/v1/credit'", (s, e))
        out["first_purchases"] = _scalar(
            db, "SELECT COUNT(*) FROM (SELECT user_id, MIN(created_at) f FROM settlements"
                " WHERE resource NOT LIKE '%/v1/credit' GROUP BY user_id) WHERE f >= ? AND f < ?", (s, e))
        out["repeat_buyers"] = _scalar(
            db, "SELECT COUNT(*) FROM (SELECT user_id FROM settlements WHERE resource NOT LIKE '%/v1/credit'"
                " GROUP BY user_id HAVING COUNT(*) >= 2 AND MAX(created_at) >= ?)", (s,))
        # allowance.db's user_id is `wallet:0x…` for the web lane, matching web.db's accounts
        # exactly; a bare numeric id (no "wallet:" prefix) is a legacy Telegram-bot user.
        lane_rows = _rows(
            db, "SELECT CASE WHEN user_id LIKE 'wallet:%' THEN 'web' ELSE 'telegram' END lane,"
                " COUNT(*) n, COUNT(DISTINCT user_id) buyers, SUM(amount) usd FROM settlements"
                " WHERE created_at >= ? AND created_at < ? AND resource NOT LIKE '%/v1/credit' GROUP BY lane",
            (s, e))
        out["settlements_by_lane"] = (
            {row["lane"]: {"count": row["n"], "buyers": row["buyers"], "usd": _usd(row["usd"])}
             for row in lane_rows} if lane_rows is not None else None
        )
        alarm_rows = _rows(
            db, "SELECT severity, COUNT(*) n FROM alerts WHERE created_at >= ? AND created_at < ? GROUP BY severity",
            (s, e))
        out["alerts"] = {row["severity"]: row["n"] for row in alarm_rows} if alarm_rows is not None else None
        out["stuck_outflows"] = _scalar(db, "SELECT COUNT(*) FROM agent_outflows WHERE resolved = 0")
        return out


def solana_allowance_metrics(path: Path, s: int, e: int) -> dict[str, Any] | None:
    with _connect_readonly(path) as db:
        if db is None:
            return None
        out: dict[str, Any] = {}
        out["limits_set"] = _scalar(db, "SELECT COUNT(*) FROM limits WHERE created_at >= ? AND created_at < ?", (s, e))
        op_rows = _rows(
            # A prompt the owner never signed (superseded by a retry, or left until its blockhash died) is
            # EXPIRED in the store but is a retry, not an error: report it as UNSIGNED so errors.ops_expired
            # keeps meaning "signed but too late" (SI-45, Codex review).
            db, "SELECT kind, CASE WHEN state = 'EXPIRED' AND detail IN (?, ?) THEN 'UNSIGNED' ELSE state END st,"
                " COUNT(*) n FROM ops WHERE created_at >= ? AND created_at < ? GROUP BY kind, st",
            (*SOLANA_UNSIGNED_DETAILS, s, e))
        if op_rows is None:  # an older store without ops.detail: plain states, as before
            op_rows = _rows(db, "SELECT kind, state st, COUNT(*) n FROM ops WHERE created_at >= ? AND created_at < ?"
                                " GROUP BY kind, state", (s, e))
        op_rows = [{"kind": r["kind"], "state": r["st"], "n": r["n"]} for r in op_rows] if op_rows else op_rows
        out["_ops"] = op_rows
        settled = _rows(
            db, "SELECT COUNT(*) n, COUNT(DISTINCT account) accounts, SUM(amount) usd FROM spends"
                " WHERE created_at >= ? AND created_at < ? AND pull_tx IS NOT NULL", (s, e))
        if settled is None or settled[0]["n"] is None:
            out["purchases"] = None
            out["usdc_volume_solana"] = None
            out["active_spenders"] = None
        else:
            row = settled[0]
            out["purchases"] = {"count": row["n"], "buyers": row["accounts"], "usd": _usd(row["usd"])}
            out["usdc_volume_solana"] = _usd(row["usd"])
            out["active_spenders"] = row["accounts"]
        out["first_purchases"] = _scalar(
            db, "SELECT COUNT(*) FROM (SELECT account, MIN(created_at) f FROM spends WHERE pull_tx IS NOT NULL"
                " GROUP BY account) WHERE f >= ? AND f < ?", (s, e))
        out["repeat_buyers"] = _scalar(
            db, "SELECT COUNT(*) FROM (SELECT account FROM spends WHERE pull_tx IS NOT NULL GROUP BY account"
                " HAVING COUNT(*) >= 2 AND MAX(created_at) >= ?)", (s,))
        out["stuck_reservations"] = _scalar(
            db, "SELECT COUNT(*) FROM spends WHERE pull_tx IS NULL AND created_at < ?", (e - 3600,))
        # Counting PREPARED GRANT op rows (the old "abandoned_setups") is misleading: a wallet
        # prompt that is closed/rejected/expires and retried leaves several PREPARED rows for one
        # account that *did* grant a moment later (SI-45). Account-level numbers instead:
        out["accounts_with_limits"] = _scalar(db, "SELECT COUNT(*) FROM limits")
        out["accounts_granted"] = _scalar(
            db, "SELECT COUNT(DISTINCT account) FROM ops WHERE kind = 'GRANT' AND state = 'DONE'")
        # An account whose limits are over an hour old and that never completed a GRANT: the
        # owner never came back to sign the wallet transaction at all.
        out["abandoned_setups"] = {
            "today": _scalar(
                db, "SELECT COUNT(*) FROM limits l WHERE l.created_at >= ? AND l.created_at < ? AND l.created_at < ?"
                    " AND NOT EXISTS (SELECT 1 FROM ops o WHERE o.account = l.account AND o.kind = 'GRANT' AND o.state = 'DONE')",
                (s, e, e - 3600)),
            "total": _scalar(
                db, "SELECT COUNT(*) FROM limits l WHERE l.created_at < ?"
                    " AND NOT EXISTS (SELECT 1 FROM ops o WHERE o.account = l.account AND o.kind = 'GRANT' AND o.state = 'DONE')",
                (e - 3600,)),
        }
        # PREPARED/EXPIRED GRANT rows from an account that did eventually grant: these are the
        # retries findings.md identified, not abandonment.
        out["grant_retries"] = {
            "today": _scalar(
                db, "SELECT COUNT(*) FROM ops o WHERE o.kind = 'GRANT' AND o.state IN ('PREPARED', 'EXPIRED')"
                    " AND o.created_at >= ? AND o.created_at < ?"
                    " AND EXISTS (SELECT 1 FROM ops d WHERE d.account = o.account AND d.kind = 'GRANT' AND d.state = 'DONE')",
                (s, e)),
            "total": _scalar(
                db, "SELECT COUNT(*) FROM ops o WHERE o.kind = 'GRANT' AND o.state IN ('PREPARED', 'EXPIRED')"
                    " AND EXISTS (SELECT 1 FROM ops d WHERE d.account = o.account AND d.kind = 'GRANT' AND d.state = 'DONE')"),
        }
        return out


def telegram_metrics(path: Path, s: int, e: int) -> dict[str, Any] | None:
    with _connect_readonly(path) as db:
        if db is None:
            return None
        return {
            "new_wallets": (_scalar(db, "SELECT COUNT(*) FROM user_wallets WHERE created_at >= ? AND created_at < ?",
                                    (s, e)) or 0)
            + (_scalar(db, "SELECT COUNT(*) FROM solana_user_wallets WHERE created_at >= ? AND created_at < ?",
                      (s, e)) or 0),
            "total_wallets": _scalar(db, "SELECT COUNT(*) FROM user_wallets"),
            "total_solana_wallets": _scalar(db, "SELECT COUNT(*) FROM solana_user_wallets"),
        }


def bitrefill_orders_metrics(path: Path | None, s: int, e: int) -> dict[str, Any] | None:
    if path is None:
        return None
    with _connect_readonly(path) as db:
        if db is None:
            return None
        rows = _rows(
            db, "SELECT state, COUNT(*) n FROM bitrefill_orders WHERE updated_at >= ? AND updated_at < ?"
                " GROUP BY state", (s, e))
        if rows is None:
            return None
        by_state = {row["state"]: row["n"] for row in rows}
        by_bucket: dict[str, int] = {}
        for state, n in by_state.items():
            bucket = BITREFILL_STATE_BUCKETS.get(state, "other")
            by_bucket[bucket] = by_bucket.get(bucket, 0) + n
        return {"by_state": by_state, "by_bucket": by_bucket}


def _op_state_counts(rows: list[sqlite3.Row] | None) -> dict[str, dict[str, int]] | None:
    if rows is None:
        return None
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        out.setdefault(row["kind"], {})[row["state"]] = row["n"]
    return out


def _ops_total(by_kind: dict[str, dict[str, int]] | None, kinds: tuple[str, ...], state: str) -> int | None:
    if by_kind is None:
        return None
    return sum(by_kind.get(kind, {}).get(state, 0) for kind in kinds)


# Base's `operations.kind` CHECK constraint only allows these three; Solana's `ops.kind` also
# has FUND_GAS (a SOL transfer to pay the agent's own network fees, not an allowance operation).
BASE_OP_KINDS = ("GRANT", "REVOKE", "PAUSE")
SOLANA_OP_KINDS = ("GRANT", "REVOKE", "FUND_GAS")


def build_report(day: datetime.date, *, web_db: Path, allowance_db: Path, solana_db: Path,
                 user_wallet_db: Path, bitrefill_orders_db: Path | None) -> dict[str, Any]:
    s, e = day_window(day)
    web = web_metrics(web_db, s, e)
    base = base_allowance_metrics(allowance_db, s, e)
    solana = solana_allowance_metrics(solana_db, s, e)
    telegram = telegram_metrics(user_wallet_db, s, e)
    bitrefill_orders = bitrefill_orders_metrics(bitrefill_orders_db, s, e)

    base_ops = _op_state_counts(base.pop("_operations") if base is not None else None)
    solana_ops = _op_state_counts(solana.pop("_ops") if solana is not None else None)

    approvals_done = (_ops_total(base_ops, ("GRANT",), "DONE") or 0) + (_ops_total(solana_ops, ("GRANT",), "DONE") or 0) \
        if base_ops is not None or solana_ops is not None else None
    approvals_failed = (_ops_total(base_ops, ("GRANT",), "FAILED") or 0) + (_ops_total(solana_ops, ("GRANT",), "FAILED") or 0) \
        if base_ops is not None or solana_ops is not None else None
    revokes = (_ops_total(base_ops, ("REVOKE",), "DONE") or 0) + (_ops_total(solana_ops, ("REVOKE",), "DONE") or 0) \
        if base_ops is not None or solana_ops is not None else None
    pauses_total = _ops_total(base_ops, ("PAUSE",), "DONE")  # Solana has no PAUSE kind

    pause_triggers = web.pop("_pause_triggers", None) if web is not None else None
    # The legacy `operations` table has no "who triggered this" column; the new `events` table
    # does (trigger=user|watcher_auto_pause). Prefer it once there is any `pause` event in this
    # day's window, and fall back to a watcher-count estimate from `alerts` otherwise (e.g. for
    # a day before analytics was turned on).
    if pause_triggers:
        pause_by_trigger = {"user": pause_triggers.get("user", 0), "watcher": pause_triggers.get("watcher_auto_pause", 0)}
    elif pauses_total is not None:
        alarms = ((base or {}).get("alerts") or {}).get("ALARM", 0) if base is not None else 0
        watcher = min(alarms, pauses_total)
        pause_by_trigger = {"user": max(0, pauses_total - watcher), "watcher": watcher}
    else:
        pause_by_trigger = None

    ops_failed = None
    ops_expired = None
    ops_uncertain = None
    if base_ops is not None or solana_ops is not None:
        ops_failed = (_ops_total(base_ops, BASE_OP_KINDS, "FAILED") or 0) + (_ops_total(solana_ops, SOLANA_OP_KINDS, "FAILED") or 0)
        ops_expired = (_ops_total(base_ops, BASE_OP_KINDS, "EXPIRED") or 0) + (_ops_total(solana_ops, SOLANA_OP_KINDS, "EXPIRED") or 0)
        ops_uncertain = _ops_total(solana_ops, SOLANA_OP_KINDS, "UNCERTAIN")  # Base has no UNCERTAIN state

    purchases = _merge_counts(
        {k: v["count"] for k, v in (base or {}).get("purchases", {}).items() if k != "credit_topup"}
        if base and base.get("purchases") else None,
        {"data_tool": (solana or {}).get("purchases", {}).get("count")} if solana and solana.get("purchases") else None,
    )

    report: dict[str, Any] = {
        "day": day.isoformat(),
        "timezone": TZ_NAME,
        "window": {"start": s, "end": e},
        "new_accounts": (web or {}).get("new_accounts"),
        "total_accounts": (web or {}).get("total_accounts"),
        "signed_in_today": (web or {}).get("signed_in_today"),
        "signed_in_today_source": (web or {}).get("signed_in_today_source", "unavailable"),
        "sessions_rows_today_unreliable": (web or {}).get("sessions_rows_today_unreliable"),
        "limits_set": _merge_counts(
            {"base": (base or {}).get("limits_set")} if base else None,
            {"solana": (solana or {}).get("limits_set")} if solana else None,
        ),
        "approvals_done": approvals_done,
        "approvals_failed": approvals_failed,
        "revokes": revokes,
        "pauses": pause_by_trigger,
        "active_spenders": _add_count((base or {}).get("active_spenders") if base else None,
                                      (solana or {}).get("active_spenders") if solana else None),
        "first_purchases": _add_count((base or {}).get("first_purchases") if base else None,
                                      (solana or {}).get("first_purchases") if solana else None),
        "repeat_buyers": _add_count((base or {}).get("repeat_buyers") if base else None,
                                    (solana or {}).get("repeat_buyers") if solana else None),
        "purchases": purchases,
        "credit_topup_settlements": ((base or {}).get("purchases") or {}).get("credit_topup") if base else None,
        "settlements_by_lane": (base or {}).get("settlements_by_lane") if base else None,
        "usdc_volume": {
            "base": (base or {}).get("usdc_volume_base") if base else None,
            "solana": (solana or {}).get("usdc_volume_solana") if solana else None,
        },
        "chat_answers": (web or {}).get("chat_answers"),
        "chat_cost_usd": (web or {}).get("chat_cost_usd"),
        "topups_credited_usd": (web or {}).get("topups_credited_usd"),
        "abandoned_solana_setups": (solana or {}).get("abandoned_setups") if solana else None,
        "errors": {
            "ops_failed": ops_failed,
            "ops_expired": ops_expired,
            "ops_uncertain": ops_uncertain,
            "topups_failed": ((web or {}).get("topups") or {}).get("failed", {}).get("count") if web and web.get("topups") else None,
            "bitrefill_refunded": (bitrefill_orders or {}).get("by_state", {}).get("REFUNDED") if bitrefill_orders else None,
            "stuck_reservations": (solana or {}).get("stuck_reservations") if solana else None,
        },
        "alarms": ((base or {}).get("alerts") or {}).get("ALARM") if base else None,
        "telegram_new_wallets": (telegram or {}).get("new_wallets") if telegram else None,
        "bitrefill_orders": bitrefill_orders,
        "events": (web or {}).get("events") if web else None,
        "detail": {
            "web": web, "base_allowance": base, "solana_allowance": solana,
            "telegram": telegram, "bitrefill_orders": bitrefill_orders,
        },
    }
    return report


def text_summary(report: dict[str, Any]) -> str:
    new_accounts = report["new_accounts"] or {}
    usdc = report["usdc_volume"] or {}
    errors = report["errors"] or {}
    solana = (report.get("detail") or {}).get("solana_allowance") or {}
    lines = [
        f"SingIt daily metrics — {report['day']} ({report['timezone']})",
        f"New accounts: base {new_accounts.get('base', '—')}, solana {new_accounts.get('solana', '—')}"
        f" (total {report['total_accounts']})",
        f"Signed in today: {report['signed_in_today']} (source: {report['signed_in_today_source']})",
        f"Limits set: {report['limits_set']}",
        f"Approvals: {report['approvals_done']} done, {report['approvals_failed']} failed; revokes {report['revokes']}",
        f"Pauses: {report['pauses']}",
        f"Active spenders: {report['active_spenders']} (first purchases {report['first_purchases']},"
        f" repeat buyers {report['repeat_buyers']})",
        f"Purchases by category: {report['purchases']} (credit top-ups via settlement: {report['credit_topup_settlements']})",
        f"USDC volume — base {usdc.get('base')}, solana {usdc.get('solana')}",
        f"Chat answers: {report['chat_answers']} (${report['chat_cost_usd']})",
        f"Top-ups credited: ${report['topups_credited_usd']}",
        f"Solana setups: {solana.get('accounts_with_limits', '—')} with limits,"
        f" {solana.get('accounts_granted', '—')} granted, abandoned {report['abandoned_solana_setups']},"
        f" retries {solana.get('grant_retries', '—')}",
        f"Errors: {errors}",
        f"Alarms: {report['alarms']}; Telegram new wallets: {report['telegram_new_wallets']}",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", help="YYYY-MM-DD, Europe/Prague; default: yesterday there")
    parser.add_argument("--out", help="directory to also write DIR/YYYY-MM-DD.json into (created if missing)")
    parser.add_argument("--text", action="store_true", help="print a short text summary instead of JSON")
    parser.add_argument("--state-dir", default=None,
                        help=f"default: ${STATE_DIR_ENV} or {DEFAULT_STATE_DIR}; base dir for the four DBs below")
    parser.add_argument("--web-db", default=None, help=f"default: ${WEB_DB_ENV} or <state-dir>/{WEB_DB_FILENAME}")
    parser.add_argument("--allowance-db", default=None,
                        help=f"default: ${ALLOWANCE_DB_ENV} or <state-dir>/{ALLOWANCE_DB_FILENAME}")
    parser.add_argument("--solana-db", default=None,
                        help=f"default: ${SOLANA_ALLOWANCE_DB_ENV} or <state-dir>/{SOLANA_ALLOWANCE_DB_FILENAME}")
    parser.add_argument("--user-wallet-db", default=None,
                        help=f"default: ${USER_WALLET_DB_ENV} or <state-dir>/{USER_WALLET_DB_FILENAME}")
    parser.add_argument("--bitrefill-orders-db", default=None,
                        help=f"no default; only read if given or ${BITREFILL_ORDERS_DB_ENV} is set")
    return parser


def _state_dir(cli_value: str | None, env: dict[str, str]) -> Path:
    return Path(cli_value or env.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR).expanduser()


def _db_path(cli_value: str | None, env: dict[str, str], env_name: str, state_dir: Path, filename: str) -> Path:
    return Path(cli_value or env.get(env_name) or (state_dir / filename)).expanduser()


def _optional_db_path(cli_value: str | None, env: dict[str, str], env_name: str) -> Path | None:
    value = cli_value or env.get(env_name)
    return Path(value).expanduser() if value else None


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    values = os.environ if env is None else env
    args = _build_parser().parse_args(argv)
    day = datetime.date.fromisoformat(args.day) if args.day else yesterday()
    state_dir = _state_dir(args.state_dir, values)

    report = build_report(
        day,
        web_db=_db_path(args.web_db, values, WEB_DB_ENV, state_dir, WEB_DB_FILENAME),
        allowance_db=_db_path(args.allowance_db, values, ALLOWANCE_DB_ENV, state_dir, ALLOWANCE_DB_FILENAME),
        solana_db=_db_path(args.solana_db, values, SOLANA_ALLOWANCE_DB_ENV, state_dir, SOLANA_ALLOWANCE_DB_FILENAME),
        user_wallet_db=_db_path(args.user_wallet_db, values, USER_WALLET_DB_ENV, state_dir, USER_WALLET_DB_FILENAME),
        bitrefill_orders_db=_optional_db_path(args.bitrefill_orders_db, values, BITREFILL_ORDERS_DB_ENV),
    )

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        out_dir = Path(args.out).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{day.isoformat()}.json").write_text(text + "\n")
    print(text_summary(report) if args.text else text)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
