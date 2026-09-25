"""The allowance watcher: reads the chain, reports spends, pauses on anomalies.

Design: docs/trezor-allowance-v1.md, phase 5. Run beside the gateway:

    python -m sign402_gateway.allowance_watcher

It reads what happened on Base, not what the gateway says happened. The gateway
only ever spends through a user's limiter to that user's own agent — x402 and
Bitrefill alike — so any `Spent` to another address means the agent key is being
used by someone else. That rule needs nothing from this server to be true, and
it pauses the limiter at once through the guardian.

Rules:

1. A `Spent` to anyone but the user's agent: pause, alarm.
2. More spends in an hour than SIGN402_ALLOWANCE_WATCH_MAX_SPENDS_PER_HOUR: pause, alarm.
3. A USDC transfer out of the agent that is neither a settlement the gateway
   counted nor a return to the owner, still unexplained after OUTFLOW_GRACE_SECONDS:
   pause, alarm. This one trusts the gateway's settlement records, so it is weaker
   than rule 1; a watcher on another host with its own records is stronger.
4. Every other spend is reported, so the owner sees their money move.

Alerts are stored (and shown by /allowance) and, with
SIGN402_ALLOWANCE_TELEGRAM_BOT_TOKEN, also sent to the user in Telegram.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from decimal import Decimal
from typing import Any, Callable, Mapping

from eth_utils import keccak, to_checksum_address

from .agent_allowance import USDC, USER_AGENT, AllowanceError, AllowanceService, TRANSFER_TOPIC, _word

logger = logging.getLogger(__name__)

SPENT_TOPIC = "0x" + keccak(text="Spent(bytes32,address,uint256,uint256)").hex()
MAX_SPENDS_ENV = "SIGN402_ALLOWANCE_WATCH_MAX_SPENDS_PER_HOUR"
INTERVAL_ENV = "SIGN402_ALLOWANCE_WATCH_INTERVAL_SECONDS"
BOT_TOKEN_ENV = "SIGN402_ALLOWANCE_TELEGRAM_BOT_TOKEN"
DEFAULT_MAX_SPENDS = 20
DEFAULT_INTERVAL = 30
OUTFLOW_GRACE_SECONDS = 600
MAX_BLOCK_RANGE = 2_000
MIN_BLOCK_RANGE = 10       # what the smallest paid-RPC plans allow for eth_getLogs
MAX_CHUNKS_PER_PASS = 200  # enough to catch up a day at the smallest range
HEAD_MARGIN_BLOCKS = 2     # load-balanced nodes lag the newest block by one or two


def _usdc(atomic: int) -> str:
    return f"{Decimal(atomic) / Decimal(1_000_000):.6f}".rstrip("0").rstrip(".") + " USDC"


def _address_topic(topic: str) -> str:
    return to_checksum_address("0x" + topic[-40:])


class TelegramNotifier:
    """Direct Bot API messages; the token never reaches a log line."""

    def __init__(self, bot_token: str):
        self._token = bot_token

    def __call__(self, user_id: str, text: str) -> None:
        if not str(user_id).isdigit():
            return  # a web account without Telegram: the alert is stored and shown on the page
        body = json.dumps({"chat_id": user_id, "text": text, "disable_web_page_preview": True}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage", data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except Exception as exc:  # a failed notice must not stop the watcher
            logger.warning("allowance watcher: Telegram notice failed (%s)", type(exc).__name__)


class AllowanceWatcher:
    def __init__(
        self,
        service: AllowanceService,
        *,
        notify: Callable[[str, str], None] | None = None,
        max_spends_per_hour: int = DEFAULT_MAX_SPENDS,
        now: Callable[[], float] = time.time,
    ):
        self.service = service
        self.store = service.store
        self.evm = service.evm
        self.notify = notify
        self.max_spends_per_hour = max_spends_per_hour
        self.now = now
        self._recent_spends: dict[str, list[float]] = {}
        self.block_range = MAX_BLOCK_RANGE

    # -- one pass over every active limiter --

    def run_once(self) -> None:
        latest = int(self.evm.call("eth_blockNumber", []), 16) - HEAD_MARGIN_BLOCKS
        for limiter in self.store.active_limiters():
            try:
                self._watch_limiter(limiter, latest)
                self._watch_agent(limiter, latest)
            except AllowanceError as exc:
                logger.warning("allowance watcher: %s skipped this pass: %s", limiter["limiter_address"], exc)

    def _range(self, subject: str, deploy_tx: str, latest: int) -> tuple[int, int] | None:
        start = self.store.cursor(subject)
        if start is None:
            receipt = self.evm.call("eth_getTransactionReceipt", [deploy_tx]) or {}
            start = int(receipt.get("blockNumber", hex(latest)), 16)
        else:
            start += 1
        if start > latest:
            return None
        return start, min(latest, start + self.block_range - 1)

    def _chunks(self, subject: str, deploy_tx: str, latest: int, query: Mapping[str, Any]):
        """(logs, last block) from this subject's cursor up to `latest`, one range at a time.

        A node that refuses the range gets a smaller one: free RPC plans allow
        as few as MIN_BLOCK_RANGE blocks per eth_getLogs. The caller moves the
        cursor after handling each chunk, so nothing is skipped or seen twice.
        """
        for _ in range(MAX_CHUNKS_PER_PASS):
            span = self._range(subject, deploy_tx, latest)
            if span is None:
                return
            try:
                logs = self.evm.call("eth_getLogs", [{**query, "fromBlock": hex(span[0]), "toBlock": hex(span[1])}])
            except AllowanceError as exc:
                if self.block_range <= MIN_BLOCK_RANGE:
                    raise
                self.block_range = max(MIN_BLOCK_RANGE, self.block_range // 4)
                logger.info("allowance watcher: %s; reading %s blocks at a time", exc, self.block_range)
                continue
            yield logs or [], span[1]

    def _watch_limiter(self, row: Mapping[str, Any], latest: int) -> None:
        limiter = row["limiter_address"]
        subject = f"spent:{limiter}"
        for logs, end in self._chunks(subject, row["deploy_tx"], latest, {"address": limiter, "topics": [SPENT_TOPIC]}):
            self._report_spends(row, logs)
            self.store.set_cursor(subject, end)

    def _report_spends(self, row: Mapping[str, Any], logs: list) -> None:
        limiter, user, agent = row["limiter_address"], row["user_id"], row["agent_address"]
        for entry in logs:
            payee = _address_topic(entry["topics"][2])
            amount = int(entry["data"][2:66], 16)
            tx = entry["transactionHash"]
            if payee != to_checksum_address(agent):
                self._alarm(user, (
                    f"A spend of {_usdc(amount)} from your Trezor went to {payee}, not to your agent "
                    f"(tx {tx}). The gateway never does that: your limiter has been paused. "
                    "Revoke the allowance from your Trezor with /allowance_revoke."
                ))
                continue
            self._inform(user, f"{_usdc(amount)} moved from your Trezor to your agent through the limiter (tx {tx}).")
            spends = [t for t in self._recent_spends.get(limiter, []) if t > self.now() - 3600] + [self.now()]
            self._recent_spends[limiter] = spends
            if len(spends) > self.max_spends_per_hour:
                self._alarm(user, (
                    f"{len(spends)} spends through your limiter in the last hour, more than the "
                    f"{self.max_spends_per_hour} expected: it has been paused. Check /allowance."
                ))

    def _watch_agent(self, row: Mapping[str, Any], latest: int) -> None:
        user, agent, owner = row["user_id"], row["agent_address"], row["owner_address"]
        subject = f"agent:{agent}"
        query = {"address": USDC, "topics": [TRANSFER_TOPIC, "0x" + _word(agent)]}
        for logs, end in self._chunks(subject, row["deploy_tx"], latest, query):
            for entry in logs:
                recipient = _address_topic(entry["topics"][2])
                if recipient == to_checksum_address(owner):
                    continue  # the float returned to its owner
                self.store.note_outflow(entry["transactionHash"], str(entry.get("logIndex", "0x0")), user,
                                        recipient, int(entry["data"], 16), int(self.now()))
            self.store.set_cursor(subject, end)
        counted = self.store.counted_settlements(user)
        for outflow in self.store.open_outflows(user):
            if (outflow["tx_hash"], outflow["log_index"]) in counted:
                self.store.resolve_outflow(outflow["tx_hash"], outflow["log_index"])
            elif self.now() - outflow["first_seen"] > OUTFLOW_GRACE_SECONDS:
                self.store.resolve_outflow(outflow["tx_hash"], outflow["log_index"])
                self._alarm(user, (
                    f"{_usdc(outflow['amount'])} left your agent to {outflow['recipient']} (tx {outflow['tx_hash']}) "
                    "and matches no purchase: your limiter has been paused. Check /allowance."
                ))

    # -- reporting --

    def _inform(self, user: str, text: str) -> None:
        self.store.add_alert(user, "INFO", text, int(self.now()))
        if self.notify:
            self.notify(user, text)

    def _alarm(self, user: str, text: str) -> None:
        try:
            self.service.pause(user)
        except Exception as exc:
            text += f" (The pause itself failed: {exc}. Revoke from your Trezor now.)"
            logger.error("allowance watcher: pause for %s failed: %s", user, exc)
        self.store.add_alert(user, "ALARM", text, int(self.now()))
        logger.warning("allowance watcher: ALARM for %s", user)
        if self.notify:
            self.notify(user, "⚠️ " + text)


def main() -> int:
    from .agent_allowance import build_allowance_service_from_env
    from .keyring import load_master_key

    logging.basicConfig(level=logging.INFO)
    values = os.environ
    service = build_allowance_service_from_env(load_master_key())
    if service is None:
        logger.error("allowance watcher: the lane is off (SIGN402_ALLOWANCE_ENABLED != 1)")
        return 1
    token = str(values.get(BOT_TOKEN_ENV, "")).strip()
    notify: Callable[[str, str], None] | None = TelegramNotifier(token) if token else None
    if notify is not None and values.get("SIGN402_WEB_ENABLED") == "1":
        from pathlib import Path

        from .web_accounts import DEFAULT_WEB_DB, WEB_DB_ENV, WebAccountStore

        accounts = WebAccountStore(Path(str(values.get(WEB_DB_ENV, "") or DEFAULT_WEB_DB)).expanduser())
        telegram = notify
        # A web account's notices go to the Telegram chat linked to it, if any.
        notify = lambda user, text: telegram(accounts.telegram_for(user) or user, text)  # noqa: E731
    watcher = AllowanceWatcher(
        service,
        notify=notify,
        max_spends_per_hour=int(values.get(MAX_SPENDS_ENV, DEFAULT_MAX_SPENDS)),
    )
    interval = int(values.get(INTERVAL_ENV, DEFAULT_INTERVAL))
    logger.info("allowance watcher: watching every %ss", interval)
    while True:
        try:
            watcher.run_once()
        except Exception:
            logger.exception("allowance watcher: pass failed")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
