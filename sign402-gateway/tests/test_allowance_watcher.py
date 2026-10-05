import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from eth_utils import to_checksum_address

from sign402_gateway import allowance_watcher as aw
from sign402_gateway.agent_allowance import USDC, TRANSFER_TOPIC, AllowanceError, AllowanceStore, _word

USER = "4242"
LIMITER = to_checksum_address("0x" + "11" * 20)
AGENT = to_checksum_address("0x" + "22" * 20)
OWNER = to_checksum_address("0x" + "33" * 20)
STRANGER = to_checksum_address("0x" + "44" * 20)


class ChainLogs:
    def __init__(self):
        self.block = 100
        self.logs = []
        self.max_range = None
        self.queries = []

    def call(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.block + aw.HEAD_MARGIN_BLOCKS)  # the chain has moved past the last event
        if method == "eth_getTransactionReceipt":
            return {"blockNumber": hex(90)}
        if method == "eth_getLogs":
            query = params[0]
            low, high = int(query["fromBlock"], 16), int(query["toBlock"], 16)
            self.queries.append((low, high))
            if self.max_range is not None and high - low + 1 > self.max_range:
                raise AllowanceError("Base refused eth_getLogs: block range is too wide")
            return [log for log in self.logs
                    if log["address"].lower() == query["address"].lower()
                    and log["topics"][:len(query["topics"])] == query["topics"]
                    and low <= int(log["blockNumber"], 16) <= high]
        raise AssertionError(method)

    def spent(self, payee, amount):
        self.block += 1
        self.logs.append({"address": LIMITER, "blockNumber": hex(self.block), "transactionHash": "0x" + format(self.block, "064x"),
                          "topics": [aw.SPENT_TOPIC, "0x" + "00" * 32, "0x" + _word(payee)],
                          "data": "0x" + format(amount, "064x") + format(amount, "064x")})

    def transfer(self, frm, to, amount):
        self.block += 1
        tx = "0x" + format(self.block, "064x")
        self.logs.append({"address": USDC, "blockNumber": hex(self.block), "transactionHash": tx, "logIndex": "0x0",
                          "topics": [TRANSFER_TOPIC, "0x" + _word(frm), "0x" + _word(to)], "data": hex(amount)})
        return tx


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AllowanceStore(Path(self.tmp.name) / "allowance.db")
        self.store.record_limiter({
            "limiter_address": LIMITER, "user_id": USER, "owner_address": OWNER, "agent_address": AGENT,
            "guardian_address": STRANGER, "daily_cap": 500_000, "per_purchase_cap": 300_000, "expiry": 2_000_000_000,
            "deploy_tx": "0x" + "aa" * 32, "status": "ACTIVE", "source": "exact_match", "created_at": 1,
        })
        self.chain = ChainLogs()
        self.service = Mock(store=self.store, evm=self.chain)
        self.sent = []
        self.clock = [1_800_000_000]
        self.watcher = aw.AllowanceWatcher(self.service, notify=lambda u, t: self.sent.append((u, t)),
                                           max_spends_per_hour=2, now=lambda: self.clock[0])

    def alerts(self):
        return [(a["severity"], a["text"]) for a in reversed(self.store.recent_alerts(USER, 50))]

    def test_a_spend_to_the_agent_is_reported_and_nothing_else(self):
        self.chain.spent(AGENT, 200_000)
        self.watcher.run_once()
        self.assertEqual(self.alerts()[0][0], "INFO")
        self.assertIn("0.2 USDC moved from your Trezor to your agent", self.sent[0][1])
        self.service.pause.assert_not_called()

    def test_a_spend_to_anyone_else_pauses_at_once(self):
        self.chain.spent(STRANGER, 300_000)
        self.watcher.run_once()
        self.service.pause.assert_called_once_with(USER)
        severity, text = self.alerts()[0]
        self.assertEqual(severity, "ALARM")
        self.assertIn(f"went to {STRANGER}, not to your agent", text)
        self.assertTrue(self.sent[0][1].startswith("⚠️"))

    def test_too_many_spends_in_an_hour_pause(self):
        for _ in range(3):
            self.chain.spent(AGENT, 1_000)
        self.watcher.run_once()
        self.service.pause.assert_called_once_with(USER)
        self.assertIn("3 spends through your limiter in the last hour", self.alerts()[-1][1])

    def test_a_pass_never_reports_the_same_event_twice(self):
        self.chain.spent(AGENT, 200_000)
        self.watcher.run_once()
        self.watcher.run_once()
        self.assertEqual(len(self.alerts()), 1)

    def test_agent_outflows_are_matched_returned_or_raised(self):
        returned = self.chain.transfer(AGENT, OWNER, 50_000)
        paid = self.chain.transfer(AGENT, STRANGER, 5_000)
        stolen = self.chain.transfer(AGENT, STRANGER, 190_000)
        self.store.count_settlement(paid, "0x0", USER, STRANGER, 5_000, "https://seller", 1)

        self.watcher.run_once()
        self.service.pause.assert_not_called()
        self.assertEqual([o["tx_hash"] for o in self.store.open_outflows(USER)], [stolen])

        self.clock[0] += aw.OUTFLOW_GRACE_SECONDS + 1
        self.watcher.run_once()
        self.service.pause.assert_called_once_with(USER)
        self.assertIn("0.19 USDC left your agent", self.alerts()[-1][1])
        self.assertNotIn(returned, " ".join(t for _, t in self.alerts()))

    def test_a_settlement_counted_late_is_not_an_alarm(self):
        paid = self.chain.transfer(AGENT, STRANGER, 5_000)
        self.watcher.run_once()
        self.store.count_settlement(paid, "0x0", USER, STRANGER, 5_000, "https://seller", 1)
        self.clock[0] += aw.OUTFLOW_GRACE_SECONDS + 1
        self.watcher.run_once()
        self.service.pause.assert_not_called()

    def test_a_node_that_allows_only_ten_blocks_is_read_ten_at_a_time(self):
        self.chain.max_range = aw.MIN_BLOCK_RANGE
        self.chain.block = 150  # sixty blocks since the deployment at 90
        self.chain.spent(AGENT, 200_000)
        self.watcher.run_once()
        self.assertIn("0.2 USDC moved from your Trezor to your agent", self.sent[0][1])
        read = [q for q in self.chain.queries if q[1] - q[0] + 1 <= aw.MIN_BLOCK_RANGE]
        self.assertEqual(read[0][0], 90)
        self.assertTrue(all(b[0] == a[1] + 1 for a, b in zip(read, read[1:]) if b[0] > a[0]))
        self.watcher.run_once()
        self.assertEqual(len(self.alerts()), 1)

    def test_a_node_that_refuses_even_the_smallest_range_skips_the_pass_without_moving_on(self):
        self.chain.max_range = 1
        self.chain.spent(AGENT, 200_000)
        self.watcher.run_once()
        self.assertEqual(self.sent, [])
        self.chain.max_range = None
        self.watcher.run_once()
        self.assertIn("0.2 USDC moved", self.sent[0][1])

    def test_a_failed_pause_says_to_revoke_now(self):
        self.service.pause.side_effect = RuntimeError("guardian has no gas")
        self.chain.spent(STRANGER, 300_000)
        self.watcher.run_once()
        self.assertIn("Revoke from your Trezor now", self.alerts()[0][1])


class TelegramNotifierTests(unittest.TestCase):
    def test_a_failed_notice_never_logs_the_token(self):
        notifier = aw.TelegramNotifier("123456:SECRET-TOKEN")
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertLogs("sign402_gateway.allowance_watcher", level="WARNING") as logs:
                notifier("4242", "hello")
        self.assertNotIn("SECRET-TOKEN", "\n".join(logs.output))
