"""Per-user chat session state for the Venice AI chat flow.

The store holds four kinds of state per user:

- the free-message allowance, which is spent once and never refills;
- the UTC daily spend window, whose rollover is computed on read so that
  correctness never depends on a scheduler running;
- outstanding prefunded credit, which is user funds and must survive a restart;
- the pause flag and the policy binding (`payTo`) the chat runner validates
  every 402 challenge against.

A prefund counts against the daily window at the moment it is paid, not as it
is consumed. That is the conservative direction: a user can never be surprised
by a spend larger than the cap they approved.

Nothing in this module stores prompt text or model output.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

DEFAULT_CHAT_STORE_PATH = Path.home() / ".sign402" / "chat-sessions.db"
SECONDS_PER_DAY = 86_400
DEFAULT_FREE_MESSAGES = 5

# A prefund claim is released by its context manager. This timeout only covers
# the case where the process died between claiming and settling, so a crashed
# claim cannot wedge a user's chat forever. It is deliberately longer than the
# 300s `maxTimeoutSeconds` an x402 challenge allows.
PREFUND_CLAIM_TTL_SECONDS = 900

_MEMORY_PATH = ":memory:"


class PrefundClaimUnavailable(RuntimeError):
    """Another prefund is already in flight for this user."""


@dataclass(frozen=True)
class ChatSession:
    user_id: str
    window_start: int
    spent_atomic_this_window: int
    outstanding_atomic: int
    free_remaining: int
    paused: bool
    pause_reason: str
    policy_hash: str
    bound_pay_to: str
    updated_at: int
    daily_cap_atomic: int = 0
    policy_expires_at: int = 0
    policy_expired: bool = False


class ChatStore:
    def __init__(
        self,
        path: Path | str,
        *,
        now: Callable[[], int] | None = None,
        free_messages: int = DEFAULT_FREE_MESSAGES,
    ):
        self.now: Callable[[], int] = now or (lambda: int(time.time()))
        self.free_messages = _non_negative(free_messages, "free_messages")
        self.lock = threading.RLock()

        self._in_memory = str(path) == _MEMORY_PATH
        self.path = _MEMORY_PATH if self._in_memory else Path(path)
        self._memory_db: sqlite3.Connection | None = None

        if not self._in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _best_effort_chmod(self.path.parent, 0o700)
        self._init_db()
        if not self._in_memory:
            _best_effort_chmod(self.path, 0o600)

    # -- reads ---------------------------------------------------------------

    def get_session(self, user_id: str) -> ChatSession:
        user_id = _user_id(user_id)
        with self.lock, self._database() as db:
            row = self._row(db, user_id)
            if row is None:
                return self._empty_session(user_id)
            row = self._rolled_over(db, row)
            return self._to_session(row)

    # -- free tier -----------------------------------------------------------

    def consume_free_message(self, user_id: str) -> bool:
        """Spend one free message. Returns False once the allowance is gone.

        The allowance is a lifetime one, not a daily one: it never refills on
        window rollover.
        """
        user_id = _user_id(user_id)
        with self.lock, self._database() as db:
            row = self._ensure_row(db, user_id)
            if int(row["free_used"]) >= self.free_messages:
                return False
            db.execute(
                """
                UPDATE chat_sessions
                SET free_used = free_used + 1, updated_at = ?
                WHERE user_id = ? AND free_used < ?
                """,
                (self.now(), user_id, self.free_messages),
            )
            return True

    # -- money ---------------------------------------------------------------

    def record_prefund(self, user_id: str, atomic: int) -> ChatSession:
        """Record a settled prefund: it adds to both the window and the credit."""
        user_id = _user_id(user_id)
        amount = _non_negative(atomic, "atomic")
        with self.lock, self._database() as db:
            row = self._rolled_over(db, self._ensure_row(db, user_id))
            db.execute(
                """
                UPDATE chat_sessions
                SET spent_atomic = spent_atomic + ?,
                    outstanding_atomic = outstanding_atomic + ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (amount, amount, self.now(), user_id),
            )
            return self._to_session(self._row(db, user_id))

    def debit(self, user_id: str, atomic: int) -> ChatSession:
        """Consume prefunded credit. Never touches the window counter."""
        user_id = _user_id(user_id)
        amount = _non_negative(atomic, "atomic")
        with self.lock, self._database() as db:
            row = self._rolled_over(db, self._ensure_row(db, user_id))
            outstanding = int(row["outstanding_atomic"])
            if amount > outstanding:
                raise ValueError(
                    "debit exceeds outstanding credit: "
                    f"{amount} > {outstanding}"
                )
            db.execute(
                """
                UPDATE chat_sessions
                SET outstanding_atomic = outstanding_atomic - ?, updated_at = ?
                WHERE user_id = ?
                """,
                (amount, self.now(), user_id),
            )
            return self._to_session(self._row(db, user_id))

    # -- pause ---------------------------------------------------------------

    def pause(self, user_id: str, reason: str) -> ChatSession:
        return self._set_pause(user_id, True, str(reason or ""))

    def resume(self, user_id: str) -> ChatSession:
        return self._set_pause(user_id, False, "")

    # -- policy binding ------------------------------------------------------

    def bind_policy(
        self, user_id: str, *, policy_hash: str, pay_to: str
    ) -> ChatSession:
        """Bind this user's chat to one merchant address.

        `pay_to` is stored lowercased because it is compared against the
        `payTo` of a live 402 challenge, whose casing is not guaranteed.
        """
        user_id = _user_id(user_id)
        with self.lock, self._database() as db:
            self._ensure_row(db, user_id)
            db.execute(
                """
                UPDATE chat_sessions
                SET policy_hash = ?, bound_pay_to = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    str(policy_hash or "").strip(),
                    str(pay_to or "").strip().lower(),
                    self.now(),
                    user_id,
                ),
            )
            return self._to_session(self._row(db, user_id))

    def approve_policy(self, user_id: str, policy) -> ChatSession:
        """Record an approved standing policy.

        This is the only way the daily cap changes: raising a limit means a new
        approval, never a silent edit. It deliberately does not touch the spend
        already recorded in the current window — a larger cap grants more room
        from now on, it does not forgive what was spent.
        """
        user_id = _user_id(user_id)
        with self.lock, self._database() as db:
            self._ensure_row(db, user_id)
            db.execute(
                """
                UPDATE chat_sessions
                SET policy_hash = ?, bound_pay_to = ?, daily_cap_atomic = ?,
                    policy_expires_at = ?, paused = 0, pause_reason = '',
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    str(policy.policy_hash),
                    str(policy.pay_to).strip().lower(),
                    int(policy.daily_cap_atomic),
                    int(policy.expires_at),
                    self.now(),
                    user_id,
                ),
            )
            return self._to_session(self._row(db, user_id))

    def revoke_policy(self, user_id: str) -> ChatSession:
        """Drop the binding and stop the chat, keeping the credit claimable."""
        user_id = _user_id(user_id)
        with self.lock, self._database() as db:
            self._ensure_row(db, user_id)
            db.execute(
                """
                UPDATE chat_sessions
                SET policy_hash = '', bound_pay_to = '', daily_cap_atomic = 0,
                    policy_expires_at = 0, paused = 1, pause_reason = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                ("REVOKED", self.now(), user_id),
            )
            return self._to_session(self._row(db, user_id))

    def users_bound_to(self, pay_to: str) -> list[str]:
        """Every user whose chat is bound to this merchant address."""
        address = str(pay_to or "").strip().lower()
        if not address:
            return []
        with self.lock, self._database() as db:
            rows = db.execute(
                "SELECT user_id FROM chat_sessions WHERE bound_pay_to = ?",
                (address,),
            ).fetchall()
        return [str(row["user_id"]) for row in rows]

    def get_watcher_state(self, key: str) -> str:
        with self.lock, self._database() as db:
            row = db.execute(
                "SELECT value FROM chat_watcher_state WHERE key = ?",
                (str(key),),
            ).fetchone()
        return str(row["value"]) if row is not None else ""

    def set_watcher_state(self, key: str, value: str) -> None:
        """Persist watcher progress so a restart cannot replay old events."""
        with self.lock, self._database() as db:
            db.execute(
                """
                INSERT INTO chat_watcher_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                (str(key), str(value), self.now()),
            )

    def claimable_credit_atomic(self, user_id: str) -> int:
        """Prefunded-but-unconsumed value. User funds: never written off."""
        return self.get_session(user_id).outstanding_atomic

    # -- prefund claim -------------------------------------------------------

    @contextmanager
    def claim_prefund(self, user_id: str) -> Iterator[None]:
        """Hold an exclusive prefund claim on this user's row.

        Two concurrent messages must produce exactly one prefund, so a second
        claim fails immediately instead of waiting for the first to finish.
        """
        user_id = _user_id(user_id)
        now = self.now()
        with self.lock, self._database() as db:
            self._ensure_row(db, user_id)
            cursor = db.execute(
                """
                UPDATE chat_sessions
                SET prefund_claimed_at = ?
                WHERE user_id = ?
                  AND (
                      prefund_claimed_at IS NULL
                      OR prefund_claimed_at <= ?
                  )
                """,
                (now, user_id, now - PREFUND_CLAIM_TTL_SECONDS),
            )
            if cursor.rowcount != 1:
                raise PrefundClaimUnavailable(
                    "a prefund is already in flight for this user"
                )
        try:
            yield
        finally:
            with self.lock, self._database() as db:
                db.execute(
                    """
                    UPDATE chat_sessions
                    SET prefund_claimed_at = NULL
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )

    # -- internals -----------------------------------------------------------

    def _set_pause(self, user_id: str, paused: bool, reason: str) -> ChatSession:
        user_id = _user_id(user_id)
        with self.lock, self._database() as db:
            self._ensure_row(db, user_id)
            db.execute(
                """
                UPDATE chat_sessions
                SET paused = ?, pause_reason = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (1 if paused else 0, reason, self.now(), user_id),
            )
            return self._to_session(self._row(db, user_id))

    def _row(self, db: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM chat_sessions WHERE user_id = ?", (user_id,)
        ).fetchone()

    def _ensure_row(self, db: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = self._row(db, user_id)
        if row is not None:
            return row
        now = self.now()
        db.execute(
            """
            INSERT INTO chat_sessions (
                user_id, window_start, spent_atomic, outstanding_atomic,
                free_used, paused, pause_reason, policy_hash, bound_pay_to,
                daily_cap_atomic, policy_expires_at, prefund_claimed_at,
                updated_at
            )
            VALUES (?, ?, 0, 0, 0, 0, '', '', '', 0, 0, NULL, ?)
            """,
            (user_id, _window_start(now), now),
        )
        return self._row(db, user_id)

    def _rolled_over(
        self, db: sqlite3.Connection, row: sqlite3.Row
    ) -> sqlite3.Row:
        """Advance the window if the UTC day changed, zeroing only the spend.

        Outstanding credit is user funds and deliberately survives rollover.
        """
        current = _window_start(self.now())
        if int(row["window_start"]) >= current:
            return row
        db.execute(
            """
            UPDATE chat_sessions
            SET window_start = ?, spent_atomic = 0, updated_at = ?
            WHERE user_id = ?
            """,
            (current, self.now(), row["user_id"]),
        )
        return self._row(db, row["user_id"])

    def _empty_session(self, user_id: str) -> ChatSession:
        now = self.now()
        return ChatSession(
            user_id=user_id,
            window_start=_window_start(now),
            spent_atomic_this_window=0,
            outstanding_atomic=0,
            free_remaining=self.free_messages,
            paused=False,
            pause_reason="",
            policy_hash="",
            bound_pay_to="",
            updated_at=now,
        )

    def _to_session(self, row: sqlite3.Row) -> ChatSession:
        expires_at = int(row["policy_expires_at"] or 0)
        return ChatSession(
            user_id=str(row["user_id"]),
            window_start=int(row["window_start"]),
            spent_atomic_this_window=int(row["spent_atomic"]),
            outstanding_atomic=int(row["outstanding_atomic"]),
            free_remaining=max(0, self.free_messages - int(row["free_used"])),
            paused=bool(row["paused"]),
            pause_reason=str(row["pause_reason"] or ""),
            policy_hash=str(row["policy_hash"] or ""),
            bound_pay_to=str(row["bound_pay_to"] or ""),
            updated_at=int(row["updated_at"]),
            daily_cap_atomic=int(row["daily_cap_atomic"] or 0),
            policy_expires_at=expires_at,
            policy_expired=bool(expires_at) and self.now() >= expires_at,
        )

    def _init_db(self) -> None:
        with self.lock, self._database() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    user_id TEXT PRIMARY KEY,
                    window_start INTEGER NOT NULL,
                    spent_atomic INTEGER NOT NULL DEFAULT 0,
                    outstanding_atomic INTEGER NOT NULL DEFAULT 0,
                    free_used INTEGER NOT NULL DEFAULT 0,
                    paused INTEGER NOT NULL DEFAULT 0,
                    pause_reason TEXT NOT NULL DEFAULT '',
                    policy_hash TEXT NOT NULL DEFAULT '',
                    bound_pay_to TEXT NOT NULL DEFAULT '',
                    daily_cap_atomic INTEGER NOT NULL DEFAULT 0,
                    policy_expires_at INTEGER NOT NULL DEFAULT 0,
                    prefund_claimed_at INTEGER,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_watcher_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def close(self) -> None:
        """Release the in-memory connection. File-backed stores close per call."""
        with self.lock:
            if self._memory_db is not None:
                self._memory_db.close()
                self._memory_db = None

    def _connect(self) -> sqlite3.Connection:
        # The gateway serves requests on threads. File-backed stores get a
        # connection per call; the single in-memory connection is shared, and
        # every use of it is already serialised by `self.lock`.
        db = sqlite3.connect(
            str(self.path), timeout=5.0, check_same_thread=not self._in_memory
        )
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        if self._in_memory:
            # An in-memory database lives and dies with its connection, so the
            # store keeps exactly one instead of opening a fresh empty one.
            if self._memory_db is None:
                self._memory_db = self._connect()
            with self._memory_db:
                yield self._memory_db
            return
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()


def _window_start(now: int) -> int:
    return (int(now) // SECONDS_PER_DAY) * SECONDS_PER_DAY


def _user_id(value: Any) -> str:
    user_id = str(value or "").strip()
    if not user_id:
        raise ValueError("user_id is required")
    return user_id


def _non_negative(value: Any, name: str) -> int:
    amount = int(value)
    if amount < 0:
        raise ValueError(f"{name} must not be negative")
    return amount


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass
