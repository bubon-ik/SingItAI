import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


STATE_ORDER = {
    "QUOTED": 10,
    "FIREFLY_APPROVED": 20,
    "USER_APPROVED": 20,
    "SINGIT_AUTHORIZED": 30,
    "SINGIT_SETTLED": 35,
    "FULFILLING": 40,
    "BITREFILL_PURCHASED": 50,
    "DELIVERED": 70,
    "QUOTE_EXPIRED": 900,
    "FIREFLY_REJECTED": 901,
    "USER_REJECTED": 901,
    "FULFILLMENT_FAILED": 902,
    "RECONCILIATION_REQUIRED": 903,
    "REFUND_REQUIRED": 904,
}


class BitrefillCommerceStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def save_quote(self, quote: dict[str, Any]) -> None:
        quote_id = str(quote["quoteId"])
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO bitrefill_orders (quote_id, state, quote_json, updated_at)
                VALUES (?, 'QUOTED', ?, ?)
                ON CONFLICT(quote_id) DO NOTHING
                """,
                (quote_id, _dumps(quote), int(time.time())),
            )

    def get_quote(self, quote_id: str) -> dict[str, Any]:
        with self.lock, self._database() as db:
            row = db.execute(
                """
                SELECT quote_id, state, quote_json, metadata_json
                FROM bitrefill_orders
                WHERE quote_id = ?
                """,
                (quote_id,),
            ).fetchone()
        if row is None:
            raise ValueError("quote not found")
        return {
            "quoteId": row["quote_id"],
            "state": row["state"],
            "quote": json.loads(row["quote_json"]),
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }

    def advance_state(
        self,
        quote_id: str,
        new_state: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if new_state not in STATE_ORDER:
            raise ValueError(f"unknown order state: {new_state}")
        with self.lock, self._database() as db:
            row = db.execute(
                "SELECT state, metadata_json FROM bitrefill_orders WHERE quote_id = ?",
                (quote_id,),
            ).fetchone()
            if row is None:
                raise ValueError("quote not found")
            old_state = str(row["state"])
            if STATE_ORDER[new_state] < STATE_ORDER[old_state]:
                raise ValueError("cannot move order state backward")
            merged = json.loads(row["metadata_json"] or "{}")
            merged.update(metadata or {})
            db.execute(
                """
                UPDATE bitrefill_orders
                SET state = ?, metadata_json = ?, updated_at = ?
                WHERE quote_id = ?
                """,
                (new_state, _dumps(merged), int(time.time()), quote_id),
            )

    def checkpoint(self, quote_id: str, metadata: dict[str, Any]) -> None:
        with self.lock, self._database() as db:
            row = db.execute(
                "SELECT metadata_json FROM bitrefill_orders WHERE quote_id = ?",
                (quote_id,),
            ).fetchone()
            if row is None:
                raise ValueError("quote not found")
            merged = json.loads(row["metadata_json"] or "{}")
            merged.update(metadata)
            db.execute(
                """
                UPDATE bitrefill_orders
                SET metadata_json = ?, updated_at = ?
                WHERE quote_id = ?
                """,
                (_dumps(merged), int(time.time()), quote_id),
            )

    def try_mark_fulfilling(self, quote_id: str) -> bool:
        # Claim the order with a single conditional UPDATE so the check-and-set
        # is atomic at the database level. A shared threading.Lock only
        # serializes within one process; the UPDATE...WHERE guarded by rowcount
        # keeps concurrent processes from double-fulfilling the same order.
        claimable_states = [
            state
            for state, order in STATE_ORDER.items()
            if order < STATE_ORDER["BITREFILL_PURCHASED"] and state != "FULFILLING"
        ]
        placeholders = ",".join("?" for _ in claimable_states)
        with self.lock, self._database() as db:
            cursor = db.execute(
                f"""
                UPDATE bitrefill_orders
                SET state = 'FULFILLING', updated_at = ?
                WHERE quote_id = ? AND state IN ({placeholders})
                """,
                (int(time.time()), quote_id, *claimable_states),
            )
            if cursor.rowcount == 1:
                return True
            row = db.execute(
                "SELECT 1 FROM bitrefill_orders WHERE quote_id = ?",
                (quote_id,),
            ).fetchone()
            if row is None:
                raise ValueError("quote not found")
            return False

    def _init_db(self) -> None:
        with self._database() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bitrefill_orders (
                    quote_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    quote_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        # timeout makes a connection wait (rather than immediately erroring)
        # when another process holds the write lock, so concurrent claims
        # serialize instead of raising "database is locked".
        db = sqlite3.connect(str(self.path), timeout=5.0)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
