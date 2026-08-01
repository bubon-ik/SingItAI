"""Durable, narrow queue for the VPS-to-Trezor companion bridge."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from eth_utils import to_checksum_address


_USER_ID = re.compile(r"[0-9]{1,32}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")
_JOB_KINDS = frozenset({"purchase_intent", "usdc_payment"})
_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "EXPIRED"})
_MAX_JSON_BYTES = 65_536


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("broker payload is too large")
    return encoded


def _object(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored broker payload is invalid")
    return decoded


def _timestamp(value: Any, name: str) -> int:
    if type(value) is not int or not 0 < value <= (1 << 63) - 1:
        raise ValueError(f"{name} is invalid")
    return value


def _user_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if _USER_ID.fullmatch(candidate) is None:
        raise ValueError("user ID is invalid")
    return candidate


def _identifier(value: Any, name: str) -> str:
    candidate = str(value or "").strip()
    if _IDENTIFIER.fullmatch(candidate) is None:
        raise ValueError(f"{name} is invalid")
    return candidate


def _address(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("wallet address is invalid")
    try:
        address = to_checksum_address(value)
    except (TypeError, ValueError):
        raise ValueError("wallet address is invalid") from None
    if int(address[2:], 16) == 0:
        raise ValueError("wallet address is invalid")
    return address


class BrokerStore:
    """SQLite state isolated from every production Sign402 database."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        id_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ):
        self.path = Path(os.path.abspath(os.fspath(path)))
        self._token_factory = token_factory
        self._id_factory = id_factory
        self._lock = threading.RLock()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._initialize()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def __repr__(self) -> str:
        return f"BrokerStore(path={self.path!r}, credentials='<redacted>')"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _database(self):
        database = self._connect()
        try:
            with database:
                yield database
        finally:
            database.close()

    def _initialize(self) -> None:
        with self._database() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS enrollments (
                    code_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    consumed_at INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS enrollments_open_user
                    ON enrollments(user_id) WHERE consumed_at IS NULL;

                CREATE TABLE IF NOT EXISTS companions (
                    companion_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE,
                    wallet_address TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'REVOKED')),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    companion_id TEXT NOT NULL REFERENCES companions(companion_id),
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('purchase_intent', 'usdc_payment')),
                    idempotency_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('QUEUED', 'LEASED', 'SUCCEEDED', 'FAILED', 'EXPIRED')),
                    result_json TEXT,
                    error_code TEXT,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    leased_at INTEGER,
                    completed_at INTEGER,
                    UNIQUE(companion_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_claimable
                    ON jobs(companion_id, state, created_at);
                """
            )

    def create_enrollment(self, user_id: str, *, now: int, ttl_seconds: int = 600) -> str:
        owner = _user_id(user_id)
        created = _timestamp(now, "created time")
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 3600:
            raise ValueError("enrollment lifetime is invalid")
        code = str(self._token_factory() or "")
        if len(code) < 32 or len(code) > 256:
            raise ValueError("enrollment token generation failed")
        with self._lock, self._database() as database:
            existing = database.execute(
                "SELECT status FROM companions WHERE user_id = ?", (owner,)
            ).fetchone()
            if existing is not None and existing["status"] == "ACTIVE":
                raise ValueError("user already has an active companion")
            database.execute(
                "DELETE FROM enrollments WHERE user_id = ? AND consumed_at IS NULL",
                (owner,),
            )
            database.execute(
                "INSERT INTO enrollments(code_hash, user_id, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (_token_hash(code), owner, created + ttl_seconds, created),
            )
        return code

    def enroll(self, code: str, wallet_address: str, *, now: int) -> dict[str, Any]:
        if not isinstance(code, str) or not 32 <= len(code) <= 256:
            raise ValueError("enrollment code is invalid")
        address = _address(wallet_address)
        timestamp = _timestamp(now, "enrollment time")
        with self._lock, self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            enrollment = database.execute(
                "SELECT * FROM enrollments WHERE code_hash = ?",
                (_token_hash(code),),
            ).fetchone()
            if (
                enrollment is None
                or enrollment["consumed_at"] is not None
                or int(enrollment["expires_at"]) <= timestamp
            ):
                raise ValueError("enrollment code is invalid or expired")
            owner = str(enrollment["user_id"])
            existing = database.execute(
                "SELECT * FROM companions WHERE user_id = ?", (owner,)
            ).fetchone()
            if existing is not None and existing["status"] == "ACTIVE":
                raise ValueError("user already has an active companion")
            companion_id = "cmp_" + str(self._id_factory() or "")
            token = str(self._token_factory() or "")
            _identifier(companion_id, "companion ID")
            if len(token) < 32 or len(token) > 256:
                raise ValueError("companion token generation failed")
            if existing is not None:
                database.execute("DELETE FROM companions WHERE companion_id = ?", (existing["companion_id"],))
            database.execute(
                "INSERT INTO companions(companion_id, user_id, wallet_address, token_hash, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)",
                (companion_id, owner, address, _token_hash(token), timestamp, timestamp),
            )
            database.execute(
                "UPDATE enrollments SET consumed_at = ? WHERE code_hash = ?",
                (timestamp, _token_hash(code)),
            )
        return {
            "companionId": companion_id,
            "userId": owner,
            "walletAddress": address,
            "token": token,
            "createdAt": timestamp,
        }

    def _authenticated(self, database: sqlite3.Connection, token: str) -> sqlite3.Row:
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise PermissionError("invalid companion token")
        row = database.execute(
            "SELECT * FROM companions WHERE token_hash = ? AND status = 'ACTIVE'",
            (_token_hash(token),),
        ).fetchone()
        if row is None:
            raise PermissionError("invalid companion token")
        return row

    def companion(self, user_id: str) -> dict[str, Any] | None:
        owner = _user_id(user_id)
        with self._database() as database:
            row = database.execute(
                "SELECT * FROM companions WHERE user_id = ? AND status = 'ACTIVE'",
                (owner,),
            ).fetchone()
        return None if row is None else self._companion(row)

    @staticmethod
    def _companion(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "companionId": str(row["companion_id"]),
            "userId": str(row["user_id"]),
            "walletAddress": str(row["wallet_address"]),
            "status": str(row["status"]),
            "createdAt": int(row["created_at"]),
            "updatedAt": int(row["updated_at"]),
        }

    def create_job(
        self,
        *,
        user_id: str,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
        expires_at: int,
        now: int,
    ) -> dict[str, Any]:
        owner = _user_id(user_id)
        if kind not in _JOB_KINDS:
            raise ValueError("job kind is invalid")
        key = _identifier(idempotency_key, "idempotency key")
        if not isinstance(payload, dict):
            raise ValueError("job payload is invalid")
        serialized = _json(payload)
        created = _timestamp(now, "job creation time")
        expiration = _timestamp(expires_at, "job expiration")
        if expiration <= created or expiration - created > 900:
            raise ValueError("job expiration is invalid")
        job_id = "job_" + str(self._id_factory() or "")
        _identifier(job_id, "job ID")
        with self._lock, self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            companion = database.execute(
                "SELECT * FROM companions WHERE user_id = ? AND status = 'ACTIVE'",
                (owner,),
            ).fetchone()
            if companion is None:
                raise ValueError("user has no active Trezor companion")
            existing = database.execute(
                "SELECT * FROM jobs WHERE companion_id = ? AND idempotency_key = ?",
                (companion["companion_id"], key),
            ).fetchone()
            if existing is not None:
                if existing["kind"] != kind or existing["payload_json"] != serialized:
                    raise ValueError("job idempotency conflict")
                return self._job(existing)
            database.execute(
                "INSERT INTO jobs(job_id, companion_id, user_id, kind, idempotency_key, payload_json, state, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?)",
                (job_id, companion["companion_id"], owner, kind, key, serialized, created, expiration),
            )
            row = database.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job(row)

    def claim(self, token: str, *, now: int) -> dict[str, Any] | None:
        timestamp = _timestamp(now, "claim time")
        with self._lock, self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            companion = self._authenticated(database, token)
            database.execute(
                "UPDATE jobs SET state = 'EXPIRED', completed_at = ? "
                "WHERE companion_id = ? AND state = 'QUEUED' AND expires_at <= ?",
                (timestamp, companion["companion_id"], timestamp),
            )
            row = database.execute(
                "SELECT * FROM jobs WHERE companion_id = ? AND state = 'LEASED' "
                "ORDER BY created_at LIMIT 1",
                (companion["companion_id"],),
            ).fetchone()
            if row is None:
                row = database.execute(
                    "SELECT * FROM jobs WHERE companion_id = ? AND state = 'QUEUED' "
                    "AND expires_at > ? ORDER BY created_at LIMIT 1",
                    (companion["companion_id"], timestamp),
                ).fetchone()
                if row is not None:
                    database.execute(
                        "UPDATE jobs SET state = 'LEASED', leased_at = ? WHERE job_id = ? AND state = 'QUEUED'",
                        (timestamp, row["job_id"]),
                    )
                    row = database.execute(
                        "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                    ).fetchone()
            database.execute(
                "UPDATE companions SET updated_at = ? WHERE companion_id = ?",
                (timestamp, companion["companion_id"]),
            )
        return None if row is None else self._job(row)

    def finish(
        self,
        token: str,
        job_id: str,
        *,
        result: dict[str, Any] | None,
        error_code: str | None,
        now: int,
    ) -> dict[str, Any]:
        identifier = _identifier(job_id, "job ID")
        timestamp = _timestamp(now, "completion time")
        success = error_code is None
        if success:
            if not isinstance(result, dict):
                raise ValueError("job result is invalid")
            result_json = _json(result)
            code = None
        else:
            code = str(error_code or "")
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
                raise ValueError("job error code is invalid")
            result_json = None
        with self._lock, self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            companion = self._authenticated(database, token)
            row = database.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND companion_id = ?",
                (identifier, companion["companion_id"]),
            ).fetchone()
            if row is None:
                raise ValueError("job was not found")
            if row["state"] in _TERMINAL_STATES:
                expected = "SUCCEEDED" if success else "FAILED"
                if row["state"] != expected or row["result_json"] != result_json or row["error_code"] != code:
                    raise ValueError("job completion conflict")
                return self._job(row)
            if row["state"] != "LEASED":
                raise ValueError("job is not leased")
            database.execute(
                "UPDATE jobs SET state = ?, result_json = ?, error_code = ?, completed_at = ? WHERE job_id = ?",
                ("SUCCEEDED" if success else "FAILED", result_json, code, timestamp, identifier),
            )
            row = database.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
        return self._job(row)

    def job(self, job_id: str, *, now: int) -> dict[str, Any]:
        identifier = _identifier(job_id, "job ID")
        timestamp = _timestamp(now, "read time")
        with self._lock, self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("job was not found")
            if row["state"] == "QUEUED" and int(row["expires_at"]) <= timestamp:
                database.execute(
                    "UPDATE jobs SET state = 'EXPIRED', completed_at = ? WHERE job_id = ?",
                    (timestamp, identifier),
                )
                row = database.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
        return self._job(row)

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        result: dict[str, Any] = {
            "jobId": str(row["job_id"]),
            "userId": str(row["user_id"]),
            "kind": str(row["kind"]),
            "idempotencyKey": str(row["idempotency_key"]),
            "payload": _object(str(row["payload_json"])),
            "state": str(row["state"]),
            "createdAt": int(row["created_at"]),
            "expiresAt": int(row["expires_at"]),
        }
        if row["result_json"] is not None:
            result["result"] = _object(str(row["result_json"]))
        if row["error_code"] is not None:
            result["errorCode"] = str(row["error_code"])
        return result
