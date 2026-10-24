import datetime
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "daily-metrics.py"


def load_daily_metrics():
    spec = importlib.util.spec_from_file_location("daily_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# A fixed day with no DST ambiguity, Europe/Prague is UTC+1 (CET) in October.
DAY = datetime.date(2026, 10, 17)


class DayWindowTests(unittest.TestCase):
    def test_window_matches_prague_midnight_cet(self):
        dm = load_daily_metrics()
        # DST ends the last Sunday of October (2026-10-25); December is unambiguously CET.
        s, e = dm.day_window(datetime.date(2026, 12, 1))
        # 2026-12-01 00:00 CET == 2026-11-30 23:00 UTC.
        self.assertEqual(datetime.datetime.fromtimestamp(s, tz=datetime.timezone.utc),
                         datetime.datetime(2026, 11, 30, 23, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(e - s, 86400)

    def test_window_matches_prague_midnight_cest_in_summer(self):
        dm = load_daily_metrics()
        s, e = dm.day_window(datetime.date(2026, 7, 1))
        # Summer: Europe/Prague is UTC+2 (CEST).
        self.assertEqual(datetime.datetime.fromtimestamp(s, tz=datetime.timezone.utc),
                         datetime.datetime(2026, 6, 30, 22, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(e - s, 86400)


class DailyMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dm = load_daily_metrics()
        self.s, self.e = self.dm.day_window(DAY)

    def missing_paths(self, **overrides):
        paths = {
            "web_db": self.root / "web.db", "allowance_db": self.root / "allowance.db",
            "solana_db": self.root / "solana-allowance.db", "user_wallet_db": self.root / "user-wallets.db",
            "bitrefill_orders_db": self.root / "bitrefill-orders.sqlite3",
        }
        paths.update(overrides)
        return paths

    def seed_web(self, with_events=True, with_chat_usage=True):
        path = self.root / "web.db"
        db = sqlite3.connect(path)
        with db:
            db.executescript("""
                CREATE TABLE accounts (account_id TEXT PRIMARY KEY, owner_address TEXT, telegram_user_id TEXT,
                    created_at INTEGER, utm_json TEXT);
                CREATE TABLE sessions (session_hash TEXT PRIMARY KEY, account_id TEXT, created_at INTEGER);
                CREATE TABLE api_keys (key_id TEXT PRIMARY KEY, account_id TEXT, created_at INTEGER);
                CREATE TABLE credit_topups (topup_id TEXT PRIMARY KEY, account_id TEXT, amount_atomic INTEGER,
                    state TEXT, paid_atomic INTEGER, created_at INTEGER, updated_at INTEGER);
            """)
            if with_chat_usage:
                db.execute("""CREATE TABLE chat_usage (account_id TEXT, chat_id TEXT, model TEXT, model_label TEXT,
                    prompt_tokens INTEGER, completion_tokens INTEGER, cost_atomic INTEGER, created_at INTEGER)""")
                db.execute("INSERT INTO chat_usage VALUES ('wallet:AAA', 'c1', 'gpt', 'GPT', 10, 20, 1500000, ?)",
                          (self.s + 10,))
                db.execute("INSERT INTO chat_usage VALUES ('wallet:AAA', 'c2', 'gpt', 'GPT', 5, 5, 500000, ?)",
                          (self.s + 20,))
            if with_events:
                db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, at INTEGER, name TEXT, uid TEXT,"
                          " props_json TEXT)")
                db.execute("INSERT INTO events(at, name, uid, props_json) VALUES (?, 'sign_in', 'u1', '{}')",
                          (self.s + 1,))
                db.execute("INSERT INTO events(at, name, uid, props_json) VALUES (?, 'pause', 'u1', ?)",
                          (self.s + 2, json.dumps({"trigger": "watcher_auto_pause"})))
                db.execute("INSERT INTO events(at, name, uid, props_json) VALUES (?, 'pause', 'u2', ?)",
                          (self.s + 3, json.dumps({"trigger": "user"})))
            db.execute("INSERT INTO accounts VALUES ('wallet:AAA', '0xAAA', NULL, ?, NULL)", (self.s + 5,))
            db.execute("INSERT INTO accounts VALUES ('solana:BBB', 'BBB', NULL, ?, NULL)", (self.s + 6,))
            db.execute("INSERT INTO accounts VALUES ('wallet:OLD', '0xOLD', NULL, ?, NULL)", (self.s - 1000,))
            db.execute("INSERT INTO sessions VALUES ('h1', 'wallet:AAA', ?)", (self.s + 7,))
            db.execute("INSERT INTO api_keys VALUES ('key1', 'wallet:AAA', ?)", (self.s + 8,))
            db.execute("INSERT INTO credit_topups VALUES ('t1', 'wallet:AAA', 2000000, 'credited', 2000000,"
                      " ?, ?)", (self.s + 9, self.s + 9))
            db.execute("INSERT INTO credit_topups VALUES ('t2', 'wallet:AAA', 500000, 'failed', NULL, ?, ?)",
                      (self.s + 9, self.s + 9))
        db.close()
        return path

    def seed_allowance(self):
        path = self.root / "allowance.db"
        db = sqlite3.connect(path)
        with db:
            db.executescript("""
                CREATE TABLE limiters (limiter_address TEXT PRIMARY KEY, user_id TEXT, status TEXT, created_at INTEGER);
                CREATE TABLE operations (op_id TEXT PRIMARY KEY, user_id TEXT, kind TEXT, state TEXT,
                    amount INTEGER, created_at INTEGER);
                CREATE TABLE settlements (tx_hash TEXT, log_index TEXT, user_id TEXT, pay_to TEXT, amount INTEGER,
                    resource TEXT, created_at INTEGER, PRIMARY KEY (tx_hash, log_index));
                CREATE TABLE alerts (alert_id INTEGER PRIMARY KEY, user_id TEXT, severity TEXT, text TEXT,
                    created_at INTEGER);
                CREATE TABLE agent_outflows (tx_hash TEXT, log_index TEXT, user_id TEXT, recipient TEXT,
                    amount INTEGER, first_seen INTEGER, resolved INTEGER, PRIMARY KEY (tx_hash, log_index));
            """)
            db.execute("INSERT INTO limiters VALUES ('0xL1', 'wallet:AAA', 'ACTIVE', ?)", (self.s + 1,))
            db.execute("INSERT INTO operations VALUES ('op1', 'wallet:AAA', 'GRANT', 'DONE', 1000000, ?)",
                      (self.s + 2,))
            db.execute("INSERT INTO operations VALUES ('op2', 'wallet:AAA', 'GRANT', 'FAILED', 0, ?)", (self.s + 3,))
            db.execute("INSERT INTO operations VALUES ('op3', 'wallet:AAA', 'REVOKE', 'DONE', 0, ?)", (self.s + 4,))
            db.execute("INSERT INTO operations VALUES ('op4', 'wallet:AAA', 'PAUSE', 'DONE', 0, ?)", (self.s + 5,))
            db.execute("INSERT INTO settlements VALUES ('0xtx1', '0', 'wallet:AAA', '0xpay', 3000000,"
                      " 'bitrefill:invoice:1', ?)", (self.s + 6,))
            db.execute("INSERT INTO settlements VALUES ('0xtx2', '0', 'wallet:AAA', '0xpay',"
                      " 400000, 'https://ask.singitai.app/v1/chat/completions/metered', ?)", (self.s + 7,))
            # A balance top-up settlement: must be excluded from purchases/spenders/volume, and
            # reported under its own credit_topup bucket (it would otherwise double-count with
            # web.db's own credit_topups table).
            db.execute("INSERT INTO settlements VALUES ('0xtx3', '0', 'wallet:AAA', '0xpay',"
                      " 250000, 'https://ask.singitai.app/v1/credit', ?)", (self.s + 7,))
            # Non-metered chat completions count as chat_answer, same as the metered resource.
            db.execute("INSERT INTO settlements VALUES ('0xtx4', '0', 'wallet:AAA', '0xpay',"
                      " 100000, 'https://ask.singitai.app/v1/chat/completions', ?)", (self.s + 8,))
            # A legacy Telegram-lane id (no "wallet:" prefix) buying a data/tool resource.
            db.execute("INSERT INTO settlements VALUES ('0xtx5', '0', '123456789', '0xpay',"
                      " 50000, 'https://api.exa.ai/search', ?)", (self.s + 9,))
            db.execute("INSERT INTO alerts VALUES (1, 'wallet:AAA', 'ALARM', 'paused', ?)", (self.s + 5,))
            db.execute("INSERT INTO agent_outflows VALUES ('0xo1', '0', 'wallet:AAA', '0xr', 1, ?, 0)", (self.s,))
        db.close()
        return path

    def seed_solana(self):
        path = self.root / "solana-allowance.db"
        db = sqlite3.connect(path)
        with db:
            db.executescript("""
                CREATE TABLE limits (account TEXT PRIMARY KEY, owner TEXT, daily_cap INTEGER, per_purchase_cap INTEGER,
                    expiry INTEGER, created_at INTEGER, updated_at INTEGER);
                CREATE TABLE ops (op_id TEXT PRIMARY KEY, account TEXT, kind TEXT, amount INTEGER, state TEXT,
                    created_at INTEGER);
                CREATE TABLE spends (id INTEGER PRIMARY KEY, account TEXT, amount INTEGER, purpose TEXT,
                    pull_tx TEXT, created_at INTEGER);
            """)
            db.execute("INSERT INTO limits VALUES ('solana:BBB', 'BBB', 1, 1, ?, ?, ?)",
                      (self.s + 100000, self.s + 1, self.s + 1))
            db.execute("INSERT INTO ops VALUES ('sop1', 'solana:BBB', 'GRANT', 500000, 'DONE', ?)", (self.s + 2,))
            db.execute("INSERT INTO spends VALUES (1, 'solana:BBB', 700000, 'Exa web search', 'tx1', ?)",
                      (self.s + 3,))
            db.execute("INSERT INTO spends VALUES (2, 'solana:BBB', 100000, 'Exa web search', NULL, ?)",
                      (self.e - 10000,))  # unconfirmed, older than an hour before window end
            # BBB did grant (sop1 DONE), so these PREPARED rows are retries (SI-45), not an
            # abandoned setup: a closed/rejected wallet prompt or an expired blockhash, not
            # someone who never came back.
            db.execute("INSERT INTO ops VALUES ('sop2', 'solana:BBB', 'GRANT', 200000, 'PREPARED', ?)",
                      (self.e - 10000,))  # today
            db.execute("INSERT INTO ops VALUES ('sop3', 'solana:BBB', 'GRANT', 300000, 'PREPARED', ?)",
                      (self.s - 100000,))  # an earlier day: counts toward "total", not "today"
            db.execute("INSERT INTO ops VALUES ('sop4', 'solana:BBB', 'GRANT', 400000, 'PREPARED', ?)",
                      (self.e - 100,))  # today
            # CCC never granted at all: its limits are old enough (today) to call it abandoned.
            db.execute("INSERT INTO limits VALUES ('solana:CCC', 'CCC', 1, 1, ?, ?, ?)",
                      (self.s + 100000, self.e - 10000, self.e - 10000))
            # DDD never granted either, but its limits are from an earlier day: counts toward
            # "total", not "today".
            db.execute("INSERT INTO limits VALUES ('solana:DDD', 'DDD', 1, 1, ?, ?, ?)",
                      (self.s + 100000, self.s - 200000, self.s - 200000))
            # EEE set limits too recently (within the last hour before the window ends) to call
            # it abandoned yet.
            db.execute("INSERT INTO limits VALUES ('solana:EEE', 'EEE', 1, 1, ?, ?, ?)",
                      (self.s + 100000, self.e - 100, self.e - 100))
        db.close()
        return path

    def seed_telegram(self):
        path = self.root / "user-wallets.db"
        db = sqlite3.connect(path)
        with db:
            db.executescript("""
                CREATE TABLE user_wallets (telegram_user_id TEXT PRIMARY KEY, chain TEXT, created_at INTEGER);
                CREATE TABLE solana_user_wallets (telegram_user_id TEXT PRIMARY KEY, chain TEXT, created_at INTEGER);
            """)
            db.execute("INSERT INTO user_wallets VALUES ('111', 'base', ?)", (self.s + 1,))
            db.execute("INSERT INTO solana_user_wallets VALUES ('222', 'solana', ?)", (self.s + 1,))
        db.close()
        return path

    def seed_bitrefill_orders(self):
        path = self.root / "bitrefill-orders.sqlite3"
        db = sqlite3.connect(path)
        with db:
            db.executescript("""
                CREATE TABLE bitrefill_orders (quote_id TEXT PRIMARY KEY, state TEXT, quote_json TEXT,
                    metadata_json TEXT DEFAULT '{}', created_at INTEGER DEFAULT 0, updated_at INTEGER);
            """)
            db.execute("INSERT INTO bitrefill_orders VALUES ('q1', 'DELIVERED', '{}', '{}', 0, ?)", (self.s + 1,))
            db.execute("INSERT INTO bitrefill_orders VALUES ('q2', 'USER_REJECTED', '{}', '{}', 0, ?)", (self.s + 1,))
            db.execute("INSERT INTO bitrefill_orders VALUES ('q3', 'QUOTED', '{}', '{}', 0, ?)", (self.s + 1,))
            db.execute("INSERT INTO bitrefill_orders VALUES ('q4', 'FULFILLING', '{}', '{}', 0, ?)", (self.s + 1,))
            db.execute("INSERT INTO bitrefill_orders VALUES ('q5', 'RECONCILIATION_REQUIRED', '{}', '{}', 0, ?)",
                      (self.s + 1,))
            db.execute("INSERT INTO bitrefill_orders VALUES ('q6', 'FULFILLMENT_FAILED', '{}', '{}', 0, ?)",
                      (self.s + 1,))
            db.execute("INSERT INTO bitrefill_orders VALUES ('q7', 'REFUNDED', '{}', '{}', 0, ?)", (self.s + 1,))
            db.execute("INSERT INTO bitrefill_orders VALUES ('q8', 'QUOTE_EXPIRED', '{}', '{}', 0, ?)", (self.s + 1,))
            # A future/unknown state (a typo, or a state added after this script was last updated).
            db.execute("INSERT INTO bitrefill_orders VALUES ('q9', 'SOMETHING_NEW', '{}', '{}', 0, ?)", (self.s + 1,))
        db.close()
        return path

    def test_all_databases_missing_is_reported_gracefully_not_a_crash(self):
        report = self.dm.build_report(DAY, **self.missing_paths())
        self.assertIsNone(report["total_accounts"])
        self.assertIsNone(report["new_accounts"])
        self.assertIsNone(report["purchases"])
        self.assertIsNone(report["detail"]["web"])
        self.assertIsNone(report["bitrefill_orders"])
        self.assertIsNone(report["abandoned_solana_setups"])
        self.assertEqual(report["signed_in_today_source"], "unavailable")
        self.dm.text_summary(report)  # must not raise either

    def test_web_metrics(self):
        web = self.seed_web()
        report = self.dm.build_report(DAY, **self.missing_paths(web_db=web))
        self.assertEqual(report["new_accounts"], {"base": 1, "solana": 1})
        self.assertEqual(report["total_accounts"], 3)
        self.assertEqual(report["chat_answers"], 2)
        self.assertEqual(report["chat_cost_usd"], "2.00")
        self.assertEqual(report["topups_credited_usd"], "2.00")
        self.assertEqual(report["errors"]["topups_failed"], 1)
        self.assertEqual(report["events"], {"sign_in": 1, "pause": 2})
        self.assertEqual(report["pauses"], {"user": 1, "watcher": 1})
        # signed_in_today is derived from events (sign_in, distinct uid), not the pruned sessions table.
        self.assertEqual(report["signed_in_today"], 1)
        self.assertEqual(report["signed_in_today_source"], "events")
        self.assertEqual(report["sessions_rows_today_unreliable"], 1)

    def test_signed_in_today_is_unavailable_without_an_events_table(self):
        web = self.seed_web(with_events=False)
        report = self.dm.build_report(DAY, **self.missing_paths(web_db=web))
        self.assertIsNone(report["signed_in_today"])
        self.assertEqual(report["signed_in_today_source"], "unavailable")
        # The sessions table is still there and still reported, just labelled unreliable.
        self.assertEqual(report["sessions_rows_today_unreliable"], 1)

    def test_missing_table_in_an_existing_db_is_null_not_a_crash(self):
        web = self.seed_web(with_chat_usage=False, with_events=False)
        report = self.dm.build_report(DAY, **self.missing_paths(web_db=web))
        self.assertIsNone(report["chat_answers"])
        self.assertIsNone(report["chat_cost_usd"])
        self.assertIsNone(report["events"])
        self.assertEqual(report["total_accounts"], 3)  # accounts table is still there

    def test_base_allowance_metrics(self):
        allowance = self.seed_allowance()
        report = self.dm.build_report(DAY, **self.missing_paths(allowance_db=allowance))
        self.assertEqual(report["limits_set"], {"base": 1})
        self.assertEqual(report["approvals_done"], 1)
        self.assertEqual(report["approvals_failed"], 1)
        self.assertEqual(report["revokes"], 1)
        self.assertEqual(report["pauses"], {"user": 0, "watcher": 1})
        self.assertEqual(report["alarms"], 1)
        self.assertEqual(report["detail"]["base_allowance"]["stuck_outflows"], 1)
        self.assertEqual(report["errors"]["ops_failed"], 1)
        # bitrefill(wallet:AAA) + chat metered(wallet:AAA) + chat plain(wallet:AAA) + data_tool(telegram id):
        # 2 distinct spenders, 2 distinct first-purchase days — the /v1/credit settlement counts
        # toward neither.
        self.assertEqual(report["active_spenders"], 2)
        self.assertEqual(report["first_purchases"], 2)
        self.assertEqual(report["purchases"], {"bitrefill": 1, "chat_answer": 2, "data_tool": 1})
        self.assertNotIn("credit_topup", report["purchases"])
        self.assertEqual(report["usdc_volume"]["base"], "3.55")  # excludes the 0.25 credit settlement
        self.assertEqual(report["credit_topup_settlements"], {"count": 1, "buyers": 1, "usd": "0.25"})
        self.assertEqual(report["settlements_by_lane"],
                         {"web": {"count": 3, "buyers": 1, "usd": "3.50"},
                          "telegram": {"count": 1, "buyers": 1, "usd": "0.05"}})

    def test_unsigned_solana_expiries_are_not_counted_as_errors(self):
        path = self.root / "solana-allowance.db"
        db = sqlite3.connect(path)
        with db:
            db.executescript("""
                CREATE TABLE limits (account TEXT PRIMARY KEY, owner TEXT, daily_cap INTEGER, per_purchase_cap INTEGER,
                    expiry INTEGER, created_at INTEGER, updated_at INTEGER);
                CREATE TABLE ops (op_id TEXT PRIMARY KEY, account TEXT, kind TEXT, amount INTEGER, state TEXT,
                    created_at INTEGER, detail TEXT NOT NULL DEFAULT '');
                CREATE TABLE spends (id INTEGER PRIMARY KEY, account TEXT, amount INTEGER, purpose TEXT,
                    pull_tx TEXT, created_at INTEGER);
            """)
            for i, detail in enumerate(self.dm.SOLANA_UNSIGNED_DETAILS):
                db.execute("INSERT INTO ops VALUES (?, 'solana:BBB', 'GRANT', 1, 'EXPIRED', ?, ?)",
                           (f"u{i}", self.s + 5, detail))
            db.execute("INSERT INTO ops VALUES ('late', 'solana:BBB', 'GRANT', 1, 'EXPIRED', ?, "
                       "'Too late for this transaction; prepare it again.')", (self.s + 6,))
        db.close()
        report = self.dm.build_report(DAY, **self.missing_paths(solana_db=path))
        self.assertEqual(report["errors"]["ops_expired"], 1)  # only the signed-but-too-late one

    def test_solana_allowance_metrics(self):
        solana = self.seed_solana()
        report = self.dm.build_report(DAY, **self.missing_paths(solana_db=solana))
        self.assertEqual(report["limits_set"], {"solana": 3})  # BBB, CCC, EEE; DDD is from an earlier day
        self.assertEqual(report["approvals_done"], 1)
        self.assertEqual(report["usdc_volume"]["solana"], "0.70")
        self.assertEqual(report["purchases"], {"data_tool": 1})
        self.assertEqual(report["errors"]["stuck_reservations"], 1)
        detail = report["detail"]["solana_allowance"]
        self.assertEqual(detail["accounts_with_limits"], 4)  # BBB, CCC, DDD, EEE
        self.assertEqual(detail["accounts_granted"], 1)  # BBB only (sop1 DONE)
        # CCC (today, old enough) and DDD (an earlier day) never granted; EEE is too recent to
        # call abandoned yet, and BBB did grant (its PREPARED rows are retries, not abandonment).
        self.assertEqual(detail["abandoned_setups"], {"today": 1, "total": 2})
        self.assertEqual(report["abandoned_solana_setups"], detail["abandoned_setups"])
        # sop2 and sop4 (today) plus sop3 (an earlier day) are all retries from BBB, which did
        # eventually grant.
        self.assertEqual(detail["grant_retries"], {"today": 2, "total": 3})

    def test_telegram_metrics(self):
        telegram = self.seed_telegram()
        report = self.dm.build_report(DAY, **self.missing_paths(user_wallet_db=telegram))
        self.assertEqual(report["telegram_new_wallets"], 2)

    def test_bitrefill_orders_classifies_known_states_and_puts_the_rest_in_other(self):
        bitrefill = self.seed_bitrefill_orders()
        report = self.dm.build_report(DAY, **self.missing_paths(bitrefill_orders_db=bitrefill))
        orders = report["bitrefill_orders"]
        self.assertEqual(orders["by_bucket"],
                         {"delivered": 1, "user_rejected": 1, "pending": 2, "needs_reconciliation": 1,
                          "failed": 2, "expired": 1, "other": 1})
        self.assertEqual(orders["by_state"]["SOMETHING_NEW"], 1)
        self.assertEqual(report["errors"]["bitrefill_refunded"], 1)

    def test_bitrefill_orders_db_not_given_is_skipped_not_a_default_path(self):
        # No default path exists for this DB; omitting it must not try to read some assumed
        # location (and must not crash).
        report = self.dm.build_report(DAY, **self.missing_paths(bitrefill_orders_db=None))
        self.assertIsNone(report["bitrefill_orders"])
        self.assertIsNone(report["errors"]["bitrefill_refunded"])

    def test_all_sources_together(self):
        paths = self.missing_paths(web_db=self.seed_web(), allowance_db=self.seed_allowance(),
                                    solana_db=self.seed_solana(), user_wallet_db=self.seed_telegram())
        report = self.dm.build_report(DAY, **paths)
        self.assertEqual(report["new_accounts"], {"base": 1, "solana": 1})
        self.assertEqual(report["limits_set"], {"base": 1, "solana": 3})
        self.assertEqual(report["telegram_new_wallets"], 2)
        json.dumps(report)  # the whole thing must be JSON-serializable

    def test_databases_are_opened_read_only_and_left_unchanged(self):
        web_path = self.seed_web()
        allowance_path = self.seed_allowance()
        before = {
            path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in (web_path, allowance_path)
        }
        # A write through the read-only connection must fail, proving mode=ro actually took.
        with self.dm._connect_readonly(web_path) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO accounts VALUES ('wallet:NEW', '0xNEW', NULL, 0, NULL)")

        self.dm.build_report(DAY, **self.missing_paths(web_db=web_path, allowance_db=allowance_path))

        after = {
            path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in (web_path, allowance_path)
        }
        self.assertEqual(before, after)

    def test_cli_writes_json_file_named_by_day_and_prints_it(self):
        web = self.seed_web()
        out_dir = self.root / "out"
        captured = io.StringIO()
        with redirect_stdout(captured):
            exit_code = self.dm.main(
                ["--day", DAY.isoformat(), "--out", str(out_dir),
                 "--web-db", str(web), "--allowance-db", str(self.root / "missing-allowance.db"),
                 "--solana-db", str(self.root / "missing-solana.db"),
                 "--user-wallet-db", str(self.root / "missing-uw.db"),
                 "--bitrefill-orders-db", str(self.root / "missing-br.sqlite3")])
        self.assertEqual(exit_code, 0)
        written = out_dir / f"{DAY.isoformat()}.json"
        self.assertTrue(written.is_file())
        saved = json.loads(written.read_text())
        self.assertEqual(saved["day"], DAY.isoformat())
        self.assertEqual(saved["total_accounts"], 3)
        printed = json.loads(captured.getvalue())
        self.assertEqual(printed["day"], DAY.isoformat())

    def test_state_dir_resolves_default_db_paths(self):
        state_dir = self.root / "state"
        state_dir.mkdir()
        self.s, self.e = self.dm.day_window(DAY)  # recompute against a path under `state_dir`, not `self.root`
        db = sqlite3.connect(state_dir / "web.db")
        with db:
            db.execute("CREATE TABLE accounts (account_id TEXT PRIMARY KEY, owner_address TEXT,"
                      " telegram_user_id TEXT, created_at INTEGER, utm_json TEXT)")
        db.close()
        captured = io.StringIO()
        with redirect_stdout(captured):
            exit_code = self.dm.main(["--day", DAY.isoformat(), "--state-dir", str(state_dir)])
        self.assertEqual(exit_code, 0)
        printed = json.loads(captured.getvalue())
        self.assertEqual(printed["total_accounts"], 0)  # the table exists (empty), proving the path resolved

    def test_bitrefill_orders_db_has_no_default_from_the_cli_either(self):
        web = self.seed_web()
        captured = io.StringIO()
        with redirect_stdout(captured):
            exit_code = self.dm.main(
                ["--day", DAY.isoformat(), "--web-db", str(web),
                 "--allowance-db", str(self.root / "missing-allowance.db"),
                 "--solana-db", str(self.root / "missing-solana.db"),
                 "--user-wallet-db", str(self.root / "missing-uw.db")])
        self.assertEqual(exit_code, 0)
        printed = json.loads(captured.getvalue())
        self.assertIsNone(printed["bitrefill_orders"])

    def test_default_day_env_overrides_and_missing_dbs_all_work_together(self):
        web = self.seed_web()
        report = self.dm.build_report(
            DAY, web_db=web, allowance_db=self.root / "nope1.db", solana_db=self.root / "nope2.db",
            user_wallet_db=self.root / "nope3.db", bitrefill_orders_db=self.root / "nope4.db")
        self.assertEqual(report["day"], DAY.isoformat())
        self.assertEqual(report["timezone"], "Europe/Prague")


if __name__ == "__main__":
    unittest.main()
