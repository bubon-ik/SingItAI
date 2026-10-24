#!/usr/bin/env python3
"""Probes SingIt's public endpoints and Base RPC, and alerts an operator on failure.

Stdlib only, Python 3.12+. This is a read-only diagnostic: it makes no change to any running
service, and it never prints a secret (a Base RPC URL can carry an API key; it is always reduced
to scheme + host before it is shown, stored, or sent anywhere).

Checks (each has an env var and a CLI flag; defaults are sensible):
  * GET $SINGIT_ALERT_ASK_URL (--ask-url), default https://ask.singitai.app/health:
    expect HTTP 200 and JSON `{"ok": true}`.
  * GET $SINGIT_ALERT_APP_URL (--app-url), default https://app.singitai.app/app/: expect HTTP 200.
    As of this change web_api.py (the public web API / gateway process behind app.singitai.app)
    has no dedicated JSON /health route - see its do_GET/_static - so this checks the app shell
    itself, the one page every user loads and the only always-on unauthenticated route it serves.
  * $SINGIT_ALERT_URLS: extra URLs, comma-separated, each expected to answer HTTP 200.
  * $SIGN402_BASE_RPC_URL (--rpc-url), if set: POST eth_blockNumber. HTTP 429 or 5xx, a timeout, a
    connection error, or a JSON-RPC error (code -32005/-32029, or a message mentioning "limit" or
    "quota") is a failure tagged rpc_quota_or_error.
  * --journal-unit NAME (repeatable): counts 5xx/Traceback lines from a user systemd unit's last
    $SINGIT_ALERT_JOURNAL_MINUTES minutes (default 10) via `journalctl --user -u NAME --since "-N
    min" -o cat` (no sudo). Alerts above $SINGIT_ALERT_5XX_THRESHOLD (default 5). Skipped, with a
    note, if journalctl is not on PATH or the unit cannot be read.

Any 5xx, timeout, connection error, non-200 status, or ok != true is a failure.

Alert channel is chosen only by what is set in the environment - the operator picks this later:
  SINGIT_ALERT_TELEGRAM_BOT_TOKEN + SINGIT_ALERT_TELEGRAM_CHAT_ID -> a Telegram message
  SINGIT_ALERT_WEBHOOK_URL (and no Telegram token)                -> POST {"text": ...} JSON
  neither                                                         -> printed to stdout, exit 1
--dry-run prints what would be sent and changes nothing: no message is sent, no state is saved.

A failing check re-alerts at most once every $SINGIT_ALERT_REPEAT_MINUTES minutes (default 60);
once it passes again, one "recovered" message is sent. State (what is currently failing, and when
it last alerted) lives in $SINGIT_ALERT_STATE, default ~/.sign402/health-alert-state.json.

Exit code: 0 if every check passed, 1 if any failed, 2 on a usage error.

    python3 scripts/singit-health-alert.py
    python3 scripts/singit-health-alert.py --dry-run
    python3 scripts/singit-health-alert.py --rpc-url "$SIGN402_BASE_RPC_URL" --journal-unit sign402-web

Sample user-level systemd timer (every 5 minutes; no deploy, no sudo - this is documentation, it
installs nothing):

    # ~/.config/systemd/user/singit-health-alert.service
    [Service]
    Type=oneshot
    ExecStart=/usr/bin/python3 /path/to/scripts/singit-health-alert.py

    # ~/.config/systemd/user/singit-health-alert.timer
    [Timer]
    OnCalendar=*:0/5
    [Install]
    WantedBy=timers.target

    systemctl --user enable --now singit-health-alert.timer

Or a user crontab line:

    */5 * * * * /usr/bin/python3 /path/to/scripts/singit-health-alert.py >/dev/null 2>&1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

USER_AGENT = "SingIt-HealthAlert/1.0 (+https://singitai.app)"
READ_CAP = 65_536

ASK_URL_ENV, DEFAULT_ASK_URL = "SINGIT_ALERT_ASK_URL", "https://ask.singitai.app/health"
APP_URL_ENV, DEFAULT_APP_URL = "SINGIT_ALERT_APP_URL", "https://app.singitai.app/app/"
EXTRA_URLS_ENV = "SINGIT_ALERT_URLS"
RPC_URL_ENV = "SIGN402_BASE_RPC_URL"
THRESHOLD_ENV, DEFAULT_THRESHOLD = "SINGIT_ALERT_5XX_THRESHOLD", 5
JOURNAL_MINUTES_ENV, DEFAULT_JOURNAL_MINUTES = "SINGIT_ALERT_JOURNAL_MINUTES", 10
TELEGRAM_TOKEN_ENV = "SINGIT_ALERT_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ENV = "SINGIT_ALERT_TELEGRAM_CHAT_ID"
WEBHOOK_URL_ENV = "SINGIT_ALERT_WEBHOOK_URL"
STATE_ENV, DEFAULT_STATE = "SINGIT_ALERT_STATE", "~/.sign402/health-alert-state.json"
REPEAT_MINUTES_ENV, DEFAULT_REPEAT_MINUTES = "SINGIT_ALERT_REPEAT_MINUTES", 60

RPC_QUOTA_CODES = (-32005, -32029)
RPC_QUOTA_WORDS = re.compile(r"limit|quota", re.I)
LOG_FAILURE_RE = re.compile(r"(?:^|[^0-9])5\d{2}(?:[^0-9]|$)|Traceback")


def redact(url: str) -> str:
    """Scheme and host only - a Base RPC URL can carry an API key in its path or query."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}"


def _http_check(name: str, url: str, *, timeout: float, require_ok: bool) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, body = response.status, response.read(READ_CAP)
    except urllib.error.HTTPError as error:
        return {"name": name, "ok": False, "message": f"HTTP {error.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"name": name, "ok": False, "message": f"unreachable ({type(error).__name__})"}
    if status != 200:
        return {"name": name, "ok": False, "message": f"HTTP {status}"}
    if require_ok:
        try:
            payload = json.loads(body)
        except ValueError:
            return {"name": name, "ok": False, "message": "200 but the body is not JSON"}
        ok_field = payload.get("ok") if isinstance(payload, dict) else None
        if ok_field is not True:
            return {"name": name, "ok": False, "message": f"200 but ok={ok_field!r}"}
    return {"name": name, "ok": True, "message": "ok"}


def check_rpc(url: str, *, timeout: float) -> dict[str, Any]:
    name, host = "rpc", redact(url)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}).encode()
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read(READ_CAP)
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read(READ_CAP) or b""
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"name": name, "ok": False, "message": f"rpc_quota_or_error: {host} unreachable ({type(error).__name__})"}
    if status == 429 or status >= 500:
        return {"name": name, "ok": False, "message": f"rpc_quota_or_error: {host} returned HTTP {status}"}
    if status != 200:  # 401/403 (a revoked or over-quota key on some providers) is a failure too
        return {"name": name, "ok": False, "message": f"rpc_error: {host} returned HTTP {status}"}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"name": name, "ok": False, "message": f"rpc_quota_or_error: {host} returned a non-JSON body"}
    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        text = str((error.get("message") if isinstance(error, dict) else error) or "")
        tag = "rpc_quota_or_error" if code in RPC_QUOTA_CODES or RPC_QUOTA_WORDS.search(text) else "rpc_error"
        return {"name": name, "ok": False, "message": f"{tag}: {host}: {text[:200]}"}
    result = payload.get("result") if isinstance(payload, dict) else None
    if not (isinstance(result, str) and re.fullmatch(r"0x[0-9a-fA-F]+", result)):
        return {"name": name, "ok": False, "message": f"rpc_error: {host} answered without a block number"}
    return {"name": name, "ok": True, "message": "ok"}


def check_journal(unit: str, *, minutes: int, threshold: int) -> dict[str, Any]:
    name = f"journal:{unit}"
    if shutil.which("journalctl") is None:
        return {"name": name, "ok": True, "skipped": True, "message": "journalctl is not available; skipped"}
    try:
        result = subprocess.run(
            ["journalctl", "--user", "-u", unit, "--since", f"-{minutes} min", "-o", "cat"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {"name": name, "ok": True, "skipped": True, "message": "journalctl could not be run; skipped"}
    if result.returncode != 0:
        return {"name": name, "ok": True, "skipped": True,
                "message": f"journalctl exited {result.returncode}; skipped"}
    hits = sum(1 for line in result.stdout.splitlines() if LOG_FAILURE_RE.search(line))
    ok = hits <= threshold
    return {"name": name, "ok": ok,
            "message": f"{hits} 5xx/Traceback line(s) in the last {minutes} min (threshold {threshold})"}


def run_checks(*, ask_url: str, app_url: str, extra_urls: list[str], rpc_url: str | None,
               journal_units: list[str], journal_minutes: int, threshold: int,
               timeout: float) -> list[dict[str, Any]]:
    results = [
        _http_check("ask", ask_url, timeout=timeout, require_ok=True),
        _http_check("app", app_url, timeout=timeout, require_ok=False),
    ]
    results += [_http_check(f"extra:{url}", url, timeout=timeout, require_ok=False) for url in extra_urls]
    if rpc_url:
        results.append(check_rpc(rpc_url, timeout=timeout))
    results += [check_journal(unit, minutes=journal_minutes, threshold=threshold) for unit in journal_units]
    return results


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def triage(results: list[dict[str, Any]], state: dict[str, Any], *, now: float,
          repeat_seconds: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """What to alert about and what recovered, plus the state to save next. Does not mutate `state`."""
    to_alert: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    next_state = dict(state)
    for result in results:
        if result.get("skipped"):
            continue
        name = result["name"]
        previous = state.get(name) or {}
        if result["ok"]:
            if previous.get("failing"):
                recovered.append(result)
            next_state[name] = {"failing": False, "last_alert": None}
            continue
        last_alert = previous.get("last_alert")
        if last_alert is None or now - last_alert >= repeat_seconds:
            to_alert.append(result)
            next_state[name] = {"failing": True, "last_alert": now}
        else:
            next_state[name] = {"failing": True, "last_alert": last_alert}
    return to_alert, recovered, next_state


def format_message(to_alert: list[dict[str, Any]], recovered: list[dict[str, Any]]) -> str:
    lines = []
    if to_alert:
        lines.append("SingIt health alert:")
        lines += [f"- {r['name']}: {r['message']}" for r in to_alert]
    if recovered:
        lines.append("Recovered:")
        lines += [f"- {r['name']}: ok" for r in recovered]
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str, *, timeout: float) -> None:
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def send_webhook(url: str, text: str, *, timeout: float) -> None:
    body = json.dumps({"text": text}).encode()
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def deliver(message: str, *, env: dict[str, str], timeout: float) -> str | None:
    """Sends `message` over whichever channel is configured; returns a failure note, or None."""
    token, chat_id = env.get(TELEGRAM_TOKEN_ENV), env.get(TELEGRAM_CHAT_ENV)
    webhook = env.get(WEBHOOK_URL_ENV)
    try:
        if token and chat_id:
            send_telegram(token, chat_id, message, timeout=timeout)
        elif webhook:
            send_webhook(webhook, message, timeout=timeout)
        else:
            print(message)
            return None
    except Exception as exc:  # a failed alert must not raise or leak the token/URL
        return f"could not deliver the alert ({type(exc).__name__})"
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ask-url", default=None, help=f"default: ${ASK_URL_ENV} or {DEFAULT_ASK_URL}")
    parser.add_argument("--app-url", default=None, help=f"default: ${APP_URL_ENV} or {DEFAULT_APP_URL}")
    parser.add_argument("--rpc-url", default=None, help=f"default: ${RPC_URL_ENV}; skipped if neither is set")
    parser.add_argument("--journal-unit", action="append", default=[], metavar="NAME", dest="journal_units",
                        help="a user systemd unit to scan for 5xx/Traceback lines; repeatable")
    parser.add_argument("--journal-minutes", type=int, default=None,
                        help=f"default: ${JOURNAL_MINUTES_ENV} or {DEFAULT_JOURNAL_MINUTES}")
    parser.add_argument("--threshold", type=int, default=None, help=f"default: ${THRESHOLD_ENV} or {DEFAULT_THRESHOLD}")
    parser.add_argument("--repeat-minutes", type=int, default=None,
                        help=f"default: ${REPEAT_MINUTES_ENV} or {DEFAULT_REPEAT_MINUTES}")
    parser.add_argument("--state", default=None, help=f"default: ${STATE_ENV} or {DEFAULT_STATE}")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout per check, seconds (default 10)")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent; send and save nothing")
    return parser


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None,
        now: Callable[[], float] = time.time) -> int:
    values = os.environ if env is None else env
    args = _build_parser().parse_args(argv)

    ask_url = args.ask_url or values.get(ASK_URL_ENV) or DEFAULT_ASK_URL
    app_url = args.app_url or values.get(APP_URL_ENV) or DEFAULT_APP_URL
    extra_urls = [u.strip() for u in (values.get(EXTRA_URLS_ENV) or "").split(",") if u.strip()]
    rpc_url = args.rpc_url or values.get(RPC_URL_ENV) or None
    journal_minutes = args.journal_minutes if args.journal_minutes is not None else \
        _int_env(values, JOURNAL_MINUTES_ENV, DEFAULT_JOURNAL_MINUTES)
    threshold = args.threshold if args.threshold is not None else _int_env(values, THRESHOLD_ENV, DEFAULT_THRESHOLD)
    repeat_minutes = args.repeat_minutes if args.repeat_minutes is not None else \
        _int_env(values, REPEAT_MINUTES_ENV, DEFAULT_REPEAT_MINUTES)
    state_path = Path(args.state or values.get(STATE_ENV) or DEFAULT_STATE).expanduser()

    results = run_checks(ask_url=ask_url, app_url=app_url, extra_urls=extra_urls, rpc_url=rpc_url,
                         journal_units=args.journal_units, journal_minutes=journal_minutes,
                         threshold=threshold, timeout=args.timeout)
    any_failing = any(not r["ok"] for r in results if not r.get("skipped"))

    state = load_state(state_path)
    to_alert, recovered, next_state = triage(results, state, now=now(), repeat_seconds=repeat_minutes * 60)
    message = format_message(to_alert, recovered)

    if message:
        if args.dry_run:
            print(f"[dry-run] would send:\n{message}")
        else:
            note = deliver(message, env=values, timeout=args.timeout)
            if note:
                print(note)
                # Not delivered: keep the previous state so the next run tries again, instead of
                # recording an alert (or a recovery) nobody received.
                next_state = state
    if not args.dry_run:
        save_state(state_path, next_state)

    for result in results:
        if result.get("skipped"):
            print(f"skip: {result['name']}: {result['message']}")
    return 1 if any_failing else 0


def _int_env(values: dict[str, str], name: str, default: int) -> int:
    try:
        return int(values[name])
    except (KeyError, ValueError, TypeError):
        return default


if __name__ == "__main__":
    sys.exit(main())
