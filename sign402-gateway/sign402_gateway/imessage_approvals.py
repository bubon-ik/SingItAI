from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from cryptography.fernet import Fernet

from .whatsapp_cloud import MetaWhatsAppTemplateNotifier


DEFAULT_IMESSAGE_APPROVAL_STORE_PATH = (
    Path.home() / ".sign402" / "imessage-approvals.db"
)
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8
PAIRING_TTL_SECONDS = 10 * 60
LINK_ATTEMPT_LIMIT = 10
LINK_ATTEMPT_WINDOW_SECONDS = 10 * 60
TEST_APPROVAL_TTL_SECONDS = 2 * 60
PURCHASE_APPROVAL_TTL_SECONDS = 10 * 60
APPROVAL_LINE_MAX_CHARS = 80
_CONTROL_CHARACTERS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
SUPPORTED_APPROVAL_CHANNELS = frozenset({"imessage", "whatsapp"})


class ImessageApprovalStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _best_effort_chmod(self.path.parent, 0o700)
        self._init_db()
        _best_effort_chmod(self.path, 0o600)

    def _init_db(self) -> None:
        with self._database() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS imessage_pairings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'imessage',
                    code_digest TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER
                )
                """
            )
            _ensure_column(
                db,
                "imessage_pairings",
                "channel",
                "TEXT NOT NULL DEFAULT 'imessage'",
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS imessage_links (
                    telegram_user_id TEXT PRIMARY KEY,
                    photon_digest TEXT NOT NULL UNIQUE,
                    encrypted_photon_user_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_channel_links (
                    telegram_user_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    photon_digest TEXT NOT NULL,
                    encrypted_photon_user_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (telegram_user_id, channel),
                    UNIQUE (channel, photon_digest)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_channel_preferences (
                    telegram_user_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO approval_channel_links (
                    telegram_user_id, channel, photon_digest, encrypted_photon_user_id,
                    created_at, updated_at
                )
                SELECT telegram_user_id, 'imessage', photon_digest,
                       encrypted_photon_user_id, created_at, updated_at
                FROM imessage_links
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS imessage_approvals (
                    approval_id TEXT PRIMARY KEY,
                    telegram_user_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    commitment_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    decision_at INTEGER
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS imessage_approval_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    approval_id TEXT NOT NULL DEFAULT '',
                    action_type TEXT NOT NULL DEFAULT '',
                    commitment_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
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


class HermesCliNotifier:
    def __init__(
        self,
        *,
        hermes_cli: str,
        hermes_home: str,
        runner: Callable[..., Any] = subprocess.run,
        timeout: float = 30.0,
    ):
        self.hermes_cli = str(hermes_cli or "").strip()
        self.hermes_home = str(hermes_home or "").strip()
        self.runner = runner
        self.timeout = timeout

    def send(
        self,
        *,
        photon_user_id: str,
        message: str,
        channel: str = "imessage",
    ) -> dict[str, object]:
        normalized_channel = _normalize_approval_channel(channel)
        if normalized_channel != "imessage":
            # Hermes's bundled Photon sidecar is configured for iMessage. It
            # does not become a WhatsApp Business sender merely by changing a
            # recipient label, so fail closed until a real provider is wired.
            return {"ok": False, "error": "approval_channel_not_configured"}
        if not self.hermes_cli:
            return {"ok": False, "error": "hermes_cli_not_configured"}
        home = str(Path(self.hermes_home).expanduser().parent)
        args = [
            self.hermes_cli,
            "send",
            "--to",
            f"photon:{normalize_e164(photon_user_id)}",
            str(message),
        ]
        default_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        inherited_path = os.environ.get("PATH", default_path)
        hermes_path = (
            f"{home}/.local/bin:{home}/.hermes/node/bin:{home}/.hermes/bin:"
            f"{inherited_path}"
        )
        env = {
            "HOME": home,
            "HERMES_HOME": self.hermes_home,
            "LOGNAME": os.environ.get("LOGNAME", "hermes"),
            "PATH": hermes_path,
            "USER": os.environ.get("USER", "hermes"),
        }
        for key in (
            "PHOTON_PROJECT_ID",
            "PHOTON_PROJECT_SECRET",
            "PHOTON_ALLOWED_USERS",
            "PHOTON_HOME_CHANNEL",
            "PHOTON_SIDECAR_TOKEN",
            "PHOTON_SIDECAR_PORT",
            "PHOTON_SIDECAR_BIND",
        ):
            value = os.environ.get(key)
            if value:
                env[key] = value
        try:
            completed = self.runner(
                args,
                shell=False,
                timeout=self.timeout,
                capture_output=True,
                text=True,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "error": "timeout",
                "stdout": str(getattr(exc, "stdout", "") or "")[:2000],
                "stderr": "hermes send timed out",
            }
        return {
            "ok": getattr(completed, "returncode", 1) == 0,
            "stdout": str(getattr(completed, "stdout", "") or "")[:2000],
            "stderr": str(getattr(completed, "stderr", "") or "")[:2000],
        }


class ApprovalChannelNotifier:
    def __init__(self, *, imessage_notifier, whatsapp_notifier=None):
        self.imessage_notifier = imessage_notifier
        self.whatsapp_notifier = whatsapp_notifier

    def send(
        self,
        *,
        photon_user_id: str,
        message: str,
        channel: str = "imessage",
        approval_id: str = "",
        context_lines: list[str] | None = None,
        expires_at: int = 0,
    ) -> dict[str, object]:
        approval_channel = _normalize_approval_channel(channel)
        if approval_channel == "imessage":
            return self.imessage_notifier.send(
                photon_user_id=photon_user_id,
                message=message,
                channel="imessage",
            )
        if approval_channel == "whatsapp" and self.whatsapp_notifier is not None:
            return self.whatsapp_notifier.send_approval(
                wa_id=photon_user_id,
                approval_id=approval_id,
                context_lines=list(context_lines or []),
                expires_at=expires_at,
            )
        return {"ok": False, "error": "approval_channel_not_configured"}


class ImessageApprovalService:
    def __init__(
        self,
        *,
        store: ImessageApprovalStore,
        wallet_service,
        master_key: str,
        notifier,
        now: Callable[[], int | float] | None = None,
        purchase_approval_timeout: float = 120.0,
        purchase_approval_poll_interval: float = 1.0,
    ):
        self.store = store
        self.wallet_service = wallet_service
        self.master_key = str(master_key or "")
        self.notifier = notifier
        self.now = now or time.time
        self.purchase_approval_timeout = max(1.0, float(purchase_approval_timeout))
        self.purchase_approval_poll_interval = max(
            0.2,
            float(purchase_approval_poll_interval),
        )
        self._fernet = _build_fernet(self.master_key)
        self._link_failures: dict[str, list[float]] = {}
        self._link_failures_lock = threading.Lock()

    def create_pairing(
        self,
        telegram_user_id: str,
        *,
        channel: str = "imessage",
    ) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        approval_channel = _normalize_approval_channel(channel)
        if approval_channel not in SUPPORTED_APPROVAL_CHANNELS:
            return {
                "ok": False,
                "channel": approval_channel,
                "telegramText": (
                    "WhatsApp approvals are not configured yet. "
                    "Use /connect_imessage."
                ),
            }
        channel_label = _approval_channel_label(approval_channel)
        wallet_status = self.wallet_service.wallet_status(user_id)
        if not wallet_status.get("ok"):
            return {
                "ok": False,
                "telegramText": wallet_status.get(
                    "telegramText",
                    "No Base agent wallet yet. Send /wallet to create one.",
                ),
            }

        now = self._now()
        code = _generate_pairing_code()
        code_digest = self._digest(f"pairing:{code.upper()}")
        with self.store.lock, self.store._database() as db:
            db.execute(
                """
                UPDATE imessage_pairings
                SET status = 'expired'
                WHERE telegram_user_id = ? AND channel = ? AND status = 'active'
                """,
                (user_id, approval_channel),
            )
            db.execute(
                """
                INSERT INTO imessage_pairings (
                    telegram_user_id, channel, code_digest, status, created_at, expires_at
                )
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (
                    user_id,
                    approval_channel,
                    code_digest,
                    now,
                    now + PAIRING_TTL_SECONDS,
                ),
            )
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="pairing_created",
                status="active",
                metadata={"channel": approval_channel},
            )
        return {
            "ok": True,
            "code": code,
            "channel": approval_channel,
            "expiresInSeconds": PAIRING_TTL_SECONDS,
            "telegramText": (
                f"Send this code to the Hermes {channel_label} line within 10 minutes:\n"
                f"{code}\n\n"
                f"After it is linked, approvals can happen in {channel_label}."
            ),
        }

    def link_photon_sender(self, code: str, photon_user_id: str) -> dict[str, Any]:
        return self.link_sender(code, photon_user_id, channel="imessage")

    def link_sender(
        self,
        code: str,
        sender_user_id: str,
        *,
        channel: str,
    ) -> dict[str, Any]:
        requested_channel = _normalize_approval_channel(channel)
        normalized_sender = _normalize_sender_identity(
            sender_user_id,
            requested_channel,
        )
        code_value = str(code or "").strip().upper()
        if not _looks_like_pairing_code(code_value):
            return _link_failed()

        now = self._now()
        code_digest = self._digest(f"pairing:{code_value}")
        photon_digest = self._identity_digest(requested_channel, normalized_sender)
        if self._link_attempts_exhausted(photon_digest, now):
            return _link_failed()
        encrypted_photon = self._fernet.encrypt(
            normalized_sender.encode("utf-8")
        ).decode("ascii")
        with self.store.lock, self.store._database() as db:
            self._expire_pairings(db, now)
            row = db.execute(
                """
                SELECT id, telegram_user_id, channel
                FROM imessage_pairings
                WHERE code_digest = ? AND status = 'active' AND expires_at > ?
                """,
                (code_digest, now),
            ).fetchone()
            if row is None:
                self._record_link_failure(photon_digest, now)
                return _link_failed()

            user_id = str(row["telegram_user_id"])
            approval_channel = _normalize_approval_channel(row["channel"])
            if approval_channel != requested_channel:
                self._record_link_failure(photon_digest, now)
                return _link_failed()
            channel_label = _approval_channel_label(approval_channel)
            existing_user = db.execute(
                """
                SELECT telegram_user_id
                FROM approval_channel_links
                WHERE telegram_user_id = ? AND channel = ?
                """,
                (user_id, approval_channel),
            ).fetchone()
            existing_phone = db.execute(
                """
                SELECT telegram_user_id, channel
                FROM approval_channel_links
                WHERE photon_digest = ?
                """,
                (photon_digest,),
            ).fetchall()
            phone_claimed_by_other_user = any(
                str(row["telegram_user_id"]) != user_id for row in existing_phone
            )
            if existing_user is not None or phone_claimed_by_other_user:
                db.execute(
                    """
                    UPDATE imessage_pairings
                    SET status = 'consumed', consumed_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._record_audit(
                    db,
                    telegram_user_id=user_id,
                    event_type="pairing_rejected",
                    status="conflict",
                    metadata={"channel": approval_channel},
                )
                return {
                    "ok": False,
                    "imessageText": (
                        f"This {channel_label} number is already linked. "
                        "Ask the operator to unlink it, then try again."
                    ),
                }

            db.execute(
                """
                INSERT INTO approval_channel_links (
                    telegram_user_id, channel, photon_digest, encrypted_photon_user_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id, channel)
                DO UPDATE SET
                    photon_digest = excluded.photon_digest,
                    encrypted_photon_user_id = excluded.encrypted_photon_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    approval_channel,
                    photon_digest,
                    encrypted_photon,
                    now,
                    now,
                ),
            )
            if approval_channel == "imessage":
                db.execute(
                    """
                    INSERT INTO imessage_links (
                        telegram_user_id, photon_digest, encrypted_photon_user_id,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_user_id)
                    DO UPDATE SET
                        photon_digest = excluded.photon_digest,
                        encrypted_photon_user_id = excluded.encrypted_photon_user_id,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, photon_digest, encrypted_photon, now, now),
                )
            db.execute(
                """
                INSERT INTO approval_channel_preferences (
                    telegram_user_id, channel, created_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id)
                DO UPDATE SET channel = excluded.channel, updated_at = excluded.updated_at
                """,
                (user_id, approval_channel, now, now),
            )
            db.execute(
                """
                UPDATE imessage_pairings
                SET status = 'consumed', consumed_at = ?
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="identity_linked",
                status="linked",
                metadata={"channel": approval_channel},
            )
        return {
            "ok": True,
            "telegramUserId": user_id,
            "channel": approval_channel,
            "imessageText": (
                f"{channel_label} linked. Future Sign402 approvals can arrive here."
            ),
        }

    def active_channel(self, telegram_user_id: str) -> str | None:
        user_id = _require_telegram_user_id(telegram_user_id)
        with self.store.lock, self.store._database() as db:
            return self._active_channel_from_db(db, user_id)

    def select_existing_channel(
        self,
        telegram_user_id: str,
        channel: str,
    ) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        approval_channel = _normalize_approval_channel(channel)
        now = self._now()
        with self.store.lock, self.store._database() as db:
            linked = db.execute(
                """
                SELECT 1
                FROM approval_channel_links
                WHERE telegram_user_id = ? AND channel = ?
                """,
                (user_id, approval_channel),
            ).fetchone()
            if linked is None:
                return {
                    "ok": True,
                    "selected": False,
                    "requiresPairing": True,
                    "channel": approval_channel,
                }
            db.execute(
                """
                INSERT INTO approval_channel_preferences (
                    telegram_user_id, channel, created_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id)
                DO UPDATE SET channel = excluded.channel, updated_at = excluded.updated_at
                """,
                (user_id, approval_channel, now, now),
            )
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="channel_selected",
                status="selected",
                metadata={"channel": approval_channel},
            )
        channel_label = _approval_channel_label(approval_channel)
        return {
            "ok": True,
            "selected": True,
            "requiresPairing": False,
            "channel": approval_channel,
            "telegramText": f"{channel_label} selected for Sign402 approvals.",
        }

    def unlink_photon_sender(
        self,
        *,
        telegram_user_id: str = "",
        photon_user_id: str = "",
    ) -> dict[str, Any]:
        user_id = str(telegram_user_id or "").strip()
        photon_value = str(photon_user_id or "").strip()
        if user_id:
            user_id = _require_telegram_user_id(user_id)
        if not user_id and not photon_value:
            return {
                "ok": False,
                "removed": False,
                "telegramText": "Provide a Telegram user ID or iMessage phone number to unlink.",
            }

        photon_digest = ""
        if photon_value:
            normalized_photon = normalize_e164(photon_value)
            photon_digest = self._digest(f"photon:{normalized_photon}")

        with self.store.lock, self.store._database() as db:
            if user_id:
                rows = db.execute(
                    """
                    SELECT telegram_user_id, channel
                    FROM approval_channel_links
                    WHERE telegram_user_id = ?
                    """,
                    (user_id,),
                ).fetchall()
                removed = db.execute(
                    "DELETE FROM approval_channel_links WHERE telegram_user_id = ?",
                    (user_id,),
                ).rowcount
                db.execute(
                    "DELETE FROM imessage_links WHERE telegram_user_id = ?",
                    (user_id,),
                )
                db.execute(
                    "DELETE FROM approval_channel_preferences WHERE telegram_user_id = ?",
                    (user_id,),
                )
            else:
                rows = db.execute(
                    """
                    SELECT telegram_user_id, channel
                    FROM approval_channel_links
                    WHERE photon_digest = ?
                    """,
                    (photon_digest,),
                ).fetchall()
                removed = db.execute(
                    "DELETE FROM approval_channel_links WHERE photon_digest = ?",
                    (photon_digest,),
                ).rowcount
                db.execute(
                    "DELETE FROM imessage_links WHERE photon_digest = ?",
                    (photon_digest,),
                )

            for row in rows:
                self._record_audit(
                    db,
                    telegram_user_id=str(row["telegram_user_id"]),
                    event_type="identity_unlinked",
                    status="unlinked",
                    metadata={"channel": str(row["channel"])},
                )

        return {
            "ok": True,
            "removed": bool(removed),
            "telegramText": (
                "Approval channel link removed. Run /connect_imessage to link again."
                if removed
                else "No approval channel link was found."
            ),
        }

    def create_test_approval(self, telegram_user_id: str) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet_status = self.wallet_service.wallet_status(user_id)
        if not wallet_status.get("ok"):
            return {
                "ok": False,
                "telegramText": wallet_status.get(
                    "telegramText",
                    "No Base agent wallet yet. Send /wallet to create one.",
                ),
            }
        wallet = wallet_status.get("wallet") or {}
        wallet_address = str(wallet.get("address", "") or "")

        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            links = self._linked_approval_channels(db, user_id)
            if not links:
                return {
                    "ok": False,
                    "telegramText": (
                        "No iMessage approval is linked yet. Send /connect_imessage first."
                    ),
                }

            pending = db.execute(
                """
                SELECT approval_id
                FROM imessage_approvals
                WHERE telegram_user_id = ?
                  AND status = 'pending'
                  AND expires_at > ?
                """,
                (user_id, now),
            ).fetchone()
            if pending is not None:
                return {
                    "ok": False,
                    "telegramText": (
                        "An iMessage approval is already pending. Reply YES or NO there first."
                    ),
                }

            expires_at = now + TEST_APPROVAL_TTL_SECONDS
            canonical = {
                "schemaVersion": 1,
                "actionType": "sign402_test",
                "walletAddress": wallet_address,
                "nonce": secrets.token_hex(16),
                "createdAt": now,
                "expiresAt": expires_at,
            }
            canonical_json = _canonical_json(canonical)
            commitment_hash = hashlib.sha256(
                canonical_json.encode("utf-8")
            ).hexdigest()
            approval_id = secrets.token_urlsafe(18)
            context_lines = [
                "Action: TEST APPROVAL",
                f"Wallet: {_short_address(wallet_address)}",
                "Funds: No funds will move",
            ]
            message = _approval_message(
                wallet_address=wallet_address,
                commitment_hash=commitment_hash,
            )
            db.execute(
                """
                INSERT INTO imessage_approvals (
                    approval_id, telegram_user_id, action_type, commitment_hash,
                    status, context_json, canonical_json, created_at, expires_at
                )
                VALUES (?, ?, 'sign402_test', ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    user_id,
                    commitment_hash,
                    json.dumps(context_lines, separators=(",", ":")),
                    canonical_json,
                    now,
                    expires_at,
                ),
            )
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="approval_created",
                approval_id=approval_id,
                action_type="sign402_test",
                commitment_hash=commitment_hash,
                status="pending",
            )

        deliveries = self._send_approval_to_channels(
            links=links,
            message=message,
            approval_id=approval_id,
            context_lines=context_lines,
            expires_at=expires_at,
        )
        delivered_channels = self._delivery_channels(deliveries)
        if not delivered_channels:
            with self.store.lock, self.store._database() as db:
                db.execute(
                    """
                    UPDATE imessage_approvals
                    SET status = 'delivery_failed'
                    WHERE approval_id = ? AND status = 'pending'
                    """,
                    (approval_id,),
                )
                self._record_audit(
                    db,
                    telegram_user_id=user_id,
                    event_type="approval_delivery_failed",
                    approval_id=approval_id,
                    action_type="sign402_test",
                    commitment_hash=commitment_hash,
                    status="delivery_failed",
                )
            return {
                "ok": False,
                "approvalId": approval_id,
                "telegramText": "could not deliver the approval. No action was approved.",
            }

        with self.store.lock, self.store._database() as db:
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="approval_delivered",
                approval_id=approval_id,
                action_type="sign402_test",
                commitment_hash=commitment_hash,
                status="pending",
                metadata={"channels": delivered_channels},
            )
        return {
            "ok": True,
            "approvalId": approval_id,
            "commitmentHash": commitment_hash,
            "telegramText": (
                f"Test approval sent to {_approval_channel_list_label(delivered_channels)}. "
                "Reply YES or NO there."
            ),
        }

    def request_hash_approval(
        self,
        *,
        telegram_user_id: str,
        action_type: str,
        commitment_hash: str,
        context_lines: list[str],
    ) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        normalized_hash = str(commitment_hash or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", normalized_hash):
            raise ValueError("commitment_hash must be a 64-character hex string")
        # Flatten before anything stores, hashes, or displays these lines, so
        # the recorded approval matches exactly what the approver was shown.
        safe_context_lines = _sanitize_context_lines(context_lines)
        wallet_status = self.wallet_service.wallet_status(user_id)
        if not wallet_status.get("ok"):
            return {
                "ok": False,
                "approved": False,
                "telegramText": wallet_status.get(
                    "telegramText",
                    "No Base agent wallet yet. Send /wallet to create one.",
                ),
            }

        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            links = self._linked_approval_channels(db, user_id)
            if not links:
                return {
                    "ok": False,
                    "approved": False,
                    "telegramText": (
                        "No iMessage approval is linked yet. Send /connect_imessage first."
                    ),
                }

            pending = db.execute(
                """
                SELECT approval_id
                FROM imessage_approvals
                WHERE telegram_user_id = ?
                  AND status = 'pending'
                  AND expires_at > ?
                """,
                (user_id, now),
            ).fetchone()
            if pending is not None:
                return {
                    "ok": False,
                    "approved": False,
                    "telegramText": (
                        "An iMessage approval is already pending. Reply YES or NO there first."
                    ),
                }

            expires_at = now + PURCHASE_APPROVAL_TTL_SECONDS
            canonical = {
                "schemaVersion": 1,
                "actionType": str(action_type or "sign402_external"),
                "commitmentHash": normalized_hash,
                "contextLines": list(safe_context_lines),
                "createdAt": now,
                "expiresAt": expires_at,
            }
            canonical_json = _canonical_json(canonical)
            approval_id = secrets.token_urlsafe(18)
            message = _purchase_approval_message(
                context_lines=safe_context_lines,
                commitment_hash=normalized_hash,
            )
            db.execute(
                """
                INSERT INTO imessage_approvals (
                    approval_id, telegram_user_id, action_type, commitment_hash,
                    status, context_json, canonical_json, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    user_id,
                    str(action_type or "sign402_external"),
                    normalized_hash,
                    json.dumps(safe_context_lines, separators=(",", ":")),
                    canonical_json,
                    now,
                    expires_at,
                ),
            )
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="approval_created",
                approval_id=approval_id,
                action_type=str(action_type or "sign402_external"),
                commitment_hash=normalized_hash,
                status="pending",
            )

        deliveries = self._send_approval_to_channels(
            links=links,
            message=message,
            approval_id=approval_id,
            context_lines=safe_context_lines,
            expires_at=expires_at,
        )
        delivered_channels = self._delivery_channels(deliveries)
        if not delivered_channels:
            with self.store.lock, self.store._database() as db:
                db.execute(
                    """
                    UPDATE imessage_approvals
                    SET status = 'delivery_failed'
                    WHERE approval_id = ? AND status = 'pending'
                    """,
                    (approval_id,),
                )
                self._record_audit(
                    db,
                    telegram_user_id=user_id,
                    event_type="approval_delivery_failed",
                    approval_id=approval_id,
                    action_type=str(action_type or "sign402_external"),
                    commitment_hash=normalized_hash,
                    status="delivery_failed",
                )
            return {
                "ok": False,
                "approved": False,
                "approvalId": approval_id,
                "approvedHash": "",
                "commitmentHash": normalized_hash,
                "telegramText": "could not deliver the approval. No action was approved.",
            }

        with self.store.lock, self.store._database() as db:
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="approval_delivered",
                approval_id=approval_id,
                action_type=str(action_type or "sign402_external"),
                commitment_hash=normalized_hash,
                status="pending",
                metadata={"channels": delivered_channels},
            )
        decision = self._wait_for_approval_decision(
            approval_id=approval_id,
            telegram_user_id=user_id,
            commitment_hash=normalized_hash,
            expires_at=expires_at,
        )
        approved = bool(decision.get("ok")) and decision.get("status") == "approved"
        return {
            **decision,
            "approved": approved,
            "approvedHash": normalized_hash if approved else "",
            "commitmentHash": normalized_hash,
        }

    def request_purchase_approval(
        self,
        *,
        telegram_user_id: str,
        tool_name: str,
        resource_url: str,
        payment_requirements: dict[str, Any],
        payment_context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet_status = self.wallet_service.wallet_status(user_id)
        if not wallet_status.get("ok"):
            return {
                "ok": False,
                "status": "wallet_missing",
                "telegramText": wallet_status.get(
                    "telegramText",
                    "No Base agent wallet yet. Send /wallet to create one.",
                ),
            }
        wallet = wallet_status.get("wallet") or {}
        wallet_address = str(wallet.get("address", "") or "")

        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            links = self._linked_approval_channels(db, user_id)
            if not links:
                return {
                    "ok": False,
                    "status": "approval_channel_not_linked",
                    "telegramText": (
                        "No iMessage approval is linked yet. Send /connect_imessage first."
                    ),
                }

            pending = db.execute(
                """
                SELECT approval_id
                FROM imessage_approvals
                WHERE telegram_user_id = ?
                  AND status = 'pending'
                  AND expires_at > ?
                """,
                (user_id, now),
            ).fetchone()
            if pending is not None:
                return {
                    "ok": False,
                    "status": "approval_pending",
                    "telegramText": (
                        "An iMessage approval is already pending. Reply YES or NO there first."
                    ),
                }

            expires_at = now + PURCHASE_APPROVAL_TTL_SECONDS
            canonical = {
                "schemaVersion": 1,
                "actionType": "sign402_purchase",
                "walletAddress": wallet_address,
                "toolName": str(tool_name or "x402 resource"),
                "resourceUrl": str(resource_url or ""),
                "paymentRequirements": _approval_payment_requirements(payment_requirements),
                "nonce": secrets.token_hex(16),
                "createdAt": now,
                "expiresAt": expires_at,
            }
            canonical_json = _canonical_json(canonical)
            commitment_hash = hashlib.sha256(
                canonical_json.encode("utf-8")
            ).hexdigest()
            approval_id = secrets.token_urlsafe(18)
            context_lines = _sanitize_context_lines(
                _purchase_context_lines(
                    tool_name=str(tool_name or "x402 resource"),
                    wallet_address=wallet_address,
                    resource_url=str(resource_url or ""),
                    payment_requirements=payment_requirements,
                )
            )
            message = _purchase_approval_message(
                context_lines=context_lines,
                commitment_hash=commitment_hash,
            )
            db.execute(
                """
                INSERT INTO imessage_approvals (
                    approval_id, telegram_user_id, action_type, commitment_hash,
                    status, context_json, canonical_json, created_at, expires_at
                )
                VALUES (?, ?, 'sign402_purchase', ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    user_id,
                    commitment_hash,
                    json.dumps(context_lines, separators=(",", ":")),
                    canonical_json,
                    now,
                    expires_at,
                ),
            )
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="approval_created",
                approval_id=approval_id,
                action_type="sign402_purchase",
                commitment_hash=commitment_hash,
                status="pending",
                metadata={"toolName": str(tool_name or ""), "resourceUrl": str(resource_url or "")},
            )

        deliveries = self._send_approval_to_channels(
            links=links,
            message=message,
            approval_id=approval_id,
            context_lines=context_lines,
            expires_at=expires_at,
        )
        delivered_channels = self._delivery_channels(deliveries)
        if not delivered_channels:
            with self.store.lock, self.store._database() as db:
                db.execute(
                    """
                    UPDATE imessage_approvals
                    SET status = 'delivery_failed'
                    WHERE approval_id = ? AND status = 'pending'
                    """,
                    (approval_id,),
                )
                self._record_audit(
                    db,
                    telegram_user_id=user_id,
                    event_type="approval_delivery_failed",
                    approval_id=approval_id,
                    action_type="sign402_purchase",
                    commitment_hash=commitment_hash,
                    status="delivery_failed",
                )
            return {
                "ok": False,
                "status": "delivery_failed",
                "approvalId": approval_id,
                "commitmentHash": commitment_hash,
                "telegramText": "could not deliver the approval. No action was approved.",
            }

        with self.store.lock, self.store._database() as db:
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="approval_delivered",
                approval_id=approval_id,
                action_type="sign402_purchase",
                commitment_hash=commitment_hash,
                status="pending",
                metadata={"channels": delivered_channels},
            )

        return self._wait_for_approval_decision(
            approval_id=approval_id,
            telegram_user_id=user_id,
            commitment_hash=commitment_hash,
            expires_at=expires_at,
        )

    def pending_for_photon_sender(
        self,
        photon_user_id: str,
        *,
        channel: str = "imessage",
    ) -> dict[str, Any]:
        approval_channel = _normalize_approval_channel(channel)
        try:
            normalized_sender = _normalize_sender_identity(
                photon_user_id,
                approval_channel,
            )
        except ValueError:
            return {"ok": True, "pending": False}
        photon_digest = self._identity_digest(approval_channel, normalized_sender)
        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            link = db.execute(
                """
                SELECT telegram_user_id
                FROM approval_channel_links
                WHERE channel = ? AND photon_digest = ?
                GROUP BY telegram_user_id
                ORDER BY telegram_user_id ASC
                LIMIT 1
                """,
                (approval_channel, photon_digest),
            ).fetchone()
            if link is None:
                return {"ok": True, "pending": False}
            if self._active_channel_from_db(db, str(link["telegram_user_id"])) != approval_channel:
                return {"ok": True, "pending": False}
            approval = db.execute(
                """
                SELECT approval_id, action_type, commitment_hash, expires_at
                FROM imessage_approvals
                WHERE telegram_user_id = ?
                  AND status = 'pending'
                  AND expires_at > ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (str(link["telegram_user_id"]), now),
            ).fetchone()
        if approval is None:
            return {"ok": True, "pending": False}
        return {
            "ok": True,
            "pending": True,
            "approvalId": str(approval["approval_id"]),
            "actionType": str(approval["action_type"]),
            "commitmentHash": str(approval["commitment_hash"]),
            "expiresAt": int(approval["expires_at"]),
        }

    def record_decision(
        self,
        photon_user_id: str,
        decision: str,
        *,
        approval_id: str | None = None,
        channel: str = "imessage",
    ) -> dict[str, Any]:
        approval_channel = _normalize_approval_channel(channel)
        try:
            normalized_sender = _normalize_sender_identity(
                photon_user_id,
                approval_channel,
            )
        except ValueError:
            return _no_pending()
        expected_approval_id = str(approval_id).strip() if approval_id is not None else ""
        if approval_channel == "whatsapp" and not expected_approval_id:
            return _no_pending()
        normalized_decision = str(decision or "").strip().upper()
        if normalized_decision not in {"YES", "NO"}:
            return {"ok": False, "imessageText": "Reply YES or NO."}
        final_status = "approved" if normalized_decision == "YES" else "denied"
        photon_digest = self._identity_digest(approval_channel, normalized_sender)
        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            link = db.execute(
                """
                SELECT telegram_user_id
                FROM approval_channel_links
                WHERE channel = ? AND photon_digest = ?
                GROUP BY telegram_user_id
                ORDER BY telegram_user_id ASC
                LIMIT 1
                """,
                (approval_channel, photon_digest),
            ).fetchone()
            if link is None:
                return _no_pending()
            user_id = str(link["telegram_user_id"])
            if self._active_channel_from_db(db, user_id) != approval_channel:
                return _no_pending()
            # When the caller (sidecar) echoes the approval it actually showed
            # the user, bind the decision to that exact approval so a stale YES
            # cannot approve a different, newer commitment.
            if expected_approval_id:
                approval = db.execute(
                    """
                    SELECT approval_id, action_type, commitment_hash
                    FROM imessage_approvals
                    WHERE telegram_user_id = ?
                      AND approval_id = ?
                      AND status = 'pending'
                      AND expires_at > ?
                    LIMIT 1
                    """,
                    (user_id, expected_approval_id, now),
                ).fetchone()
            else:
                approval = db.execute(
                    """
                    SELECT approval_id, action_type, commitment_hash
                    FROM imessage_approvals
                    WHERE telegram_user_id = ?
                      AND status = 'pending'
                      AND expires_at > ?
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (user_id, now),
                ).fetchone()
            if approval is None:
                return _no_pending()
            updated = db.execute(
                """
                UPDATE imessage_approvals
                SET status = ?, decision_at = ?
                WHERE approval_id = ? AND status = 'pending' AND expires_at > ?
                """,
                (final_status, now, approval["approval_id"], now),
            ).rowcount
            if updated != 1:
                return _no_pending()
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type=f"approval_{final_status}",
                approval_id=str(approval["approval_id"]),
                action_type=str(approval["action_type"]),
                commitment_hash=str(approval["commitment_hash"]),
                status=final_status,
            )
        return {
            "ok": True,
            "status": final_status,
            "imessageText": _decision_text(str(approval["action_type"]), final_status),
        }

    def _wait_for_approval_decision(
        self,
        *,
        approval_id: str,
        telegram_user_id: str,
        commitment_hash: str,
        expires_at: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.purchase_approval_timeout
        while time.monotonic() <= deadline:
            now = self._now()
            with self.store.lock, self.store._database() as db:
                self._expire_approvals(db, now)
                row = db.execute(
                    """
                    SELECT status, decision_at
                    FROM imessage_approvals
                    WHERE approval_id = ?
                    """,
                    (approval_id,),
                ).fetchone()
            status = str(row["status"]) if row is not None else "missing"
            if status == "approved":
                return {
                    "ok": True,
                    "status": "approved",
                    "approvalId": approval_id,
                    "commitmentHash": commitment_hash,
                }
            if status in {"denied", "expired", "delivery_failed", "missing"}:
                return {
                    "ok": False,
                    "status": status,
                    "approvalId": approval_id,
                    "commitmentHash": commitment_hash,
                    "telegramText": "Purchase was not approved.",
                }
            if now >= expires_at:
                break
            time.sleep(self.purchase_approval_poll_interval)
        return {
            "ok": False,
            "status": "timeout",
            "approvalId": approval_id,
            "commitmentHash": commitment_hash,
            "telegramText": "Approval timed out. No funds were moved.",
        }

    def _linked_approval_channels(
        self,
        db: sqlite3.Connection,
        telegram_user_id: str,
    ) -> list[dict[str, str]]:
        active_channel = self._active_channel_from_db(db, str(telegram_user_id))
        if active_channel is None:
            return []
        rows = db.execute(
            """
            SELECT channel, encrypted_photon_user_id
            FROM approval_channel_links
            WHERE telegram_user_id = ? AND channel = ?
            ORDER BY CASE channel WHEN 'imessage' THEN 0 WHEN 'whatsapp' THEN 1 ELSE 2 END,
                     channel
            """,
            (str(telegram_user_id), active_channel),
        ).fetchall()
        links: list[dict[str, str]] = []
        for row in rows:
            channel = _normalize_approval_channel(row["channel"])
            if channel not in SUPPORTED_APPROVAL_CHANNELS:
                # Keep historical rows for audit/unlinking, but never use a
                # transport that has not been configured on this deployment.
                continue
            encrypted_photon = str(row["encrypted_photon_user_id"])
            photon_user_id = self._fernet.decrypt(
                encrypted_photon.encode("ascii")
            ).decode("utf-8")
            links.append({"channel": channel, "photonUserId": photon_user_id})
        return links

    def _send_approval_to_channels(
        self,
        *,
        links: list[dict[str, str]],
        message: str,
        approval_id: str,
        context_lines: list[str],
        expires_at: int,
    ) -> list[dict[str, Any]]:
        deliveries: list[dict[str, Any]] = []
        for link in links:
            channel = _normalize_approval_channel(link["channel"])
            result = self.notifier.send(
                photon_user_id=str(link["photonUserId"]),
                message=message,
                channel=channel,
                approval_id=approval_id,
                context_lines=context_lines,
                expires_at=expires_at,
            )
            deliveries.append(
                {
                    "channel": channel,
                    "photonUserId": str(link["photonUserId"]),
                    "ok": bool(result.get("ok")),
                }
            )
        return deliveries

    def _delivery_channels(self, deliveries: list[dict[str, Any]]) -> list[str]:
        return [
            _normalize_approval_channel(str(delivery.get("channel") or ""))
            for delivery in deliveries
            if delivery.get("ok")
        ]

    def _now(self) -> int:
        return int(self.now())

    def _digest(self, value: str) -> str:
        return hmac.new(
            self.master_key.encode("utf-8"),
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _link_attempts_exhausted(self, photon_digest: str, now: int) -> bool:
        """True when a sender burned too many bad pairing codes recently.

        Codes have 2^40 entropy, so this guard exists to make online guessing
        wholly impractical rather than merely infeasible.
        """
        cutoff = now - LINK_ATTEMPT_WINDOW_SECONDS
        with self._link_failures_lock:
            failures = [t for t in self._link_failures.get(photon_digest, []) if t > cutoff]
            self._link_failures[photon_digest] = failures
            return len(failures) >= LINK_ATTEMPT_LIMIT

    def _record_link_failure(self, photon_digest: str, now: int) -> None:
        cutoff = now - LINK_ATTEMPT_WINDOW_SECONDS
        with self._link_failures_lock:
            failures = [t for t in self._link_failures.get(photon_digest, []) if t > cutoff]
            failures.append(now)
            self._link_failures[photon_digest] = failures

    def _identity_digest(self, channel: str, sender_user_id: str) -> str:
        approval_channel = _normalize_approval_channel(channel)
        namespace = "photon" if approval_channel == "imessage" else "whatsapp"
        return self._digest(f"{namespace}:{sender_user_id}")

    def _active_channel_from_db(
        self,
        db: sqlite3.Connection,
        telegram_user_id: str,
    ) -> str | None:
        preference = db.execute(
            """
            SELECT channel
            FROM approval_channel_preferences
            WHERE telegram_user_id = ?
            """,
            (str(telegram_user_id),),
        ).fetchone()
        if preference is not None:
            channel = _normalize_approval_channel(preference["channel"])
            if channel in SUPPORTED_APPROVAL_CHANNELS:
                return channel
        legacy = db.execute(
            """
            SELECT channel
            FROM approval_channel_links
            WHERE telegram_user_id = ?
            ORDER BY CASE channel WHEN 'imessage' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (str(telegram_user_id),),
        ).fetchone()
        if legacy is None:
            return None
        channel = _normalize_approval_channel(legacy["channel"])
        return channel if channel in SUPPORTED_APPROVAL_CHANNELS else None

    def _expire_pairings(self, db: sqlite3.Connection, now: int) -> None:
        db.execute(
            """
            UPDATE imessage_pairings
            SET status = 'expired'
            WHERE status = 'active' AND expires_at <= ?
            """,
            (now,),
        )

    def _expire_approvals(self, db: sqlite3.Connection, now: int) -> None:
        expired_rows = db.execute(
            """
            SELECT approval_id, telegram_user_id, action_type, commitment_hash
            FROM imessage_approvals
            WHERE status = 'pending' AND expires_at <= ?
            """,
            (now,),
        ).fetchall()
        db.execute(
            """
            UPDATE imessage_approvals
            SET status = 'expired'
            WHERE status = 'pending' AND expires_at <= ?
            """,
            (now,),
        )
        for row in expired_rows:
            self._record_audit(
                db,
                telegram_user_id=str(row["telegram_user_id"]),
                event_type="approval_expired",
                approval_id=str(row["approval_id"]),
                action_type=str(row["action_type"]),
                commitment_hash=str(row["commitment_hash"]),
                status="expired",
            )

    def _record_audit(
        self,
        db: sqlite3.Connection,
        *,
        telegram_user_id: str = "",
        event_type: str,
        approval_id: str = "",
        action_type: str = "",
        commitment_hash: str = "",
        status: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO imessage_approval_audit (
                telegram_user_id, event_type, approval_id, action_type,
                commitment_hash, status, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(telegram_user_id or ""),
                event_type,
                str(approval_id or ""),
                str(action_type or ""),
                str(commitment_hash or ""),
                str(status or ""),
                json.dumps(metadata or {}, separators=(",", ":")),
                self._now(),
            ),
        )


def build_imessage_approval_service_from_env(
    *,
    env: dict[str, str],
    wallet_service,
    store_path: Path | None = None,
) -> ImessageApprovalService:
    whatsapp_notifier = None
    whatsapp_access_token = str(
        env.get("SIGN402_WHATSAPP_ACCESS_TOKEN", "") or ""
    ).strip()
    whatsapp_phone_number_id = str(
        env.get("SIGN402_WHATSAPP_PHONE_NUMBER_ID", "") or ""
    ).strip()
    if whatsapp_access_token and whatsapp_phone_number_id:
        whatsapp_notifier = MetaWhatsAppTemplateNotifier(
            access_token=whatsapp_access_token,
            phone_number_id=whatsapp_phone_number_id,
            template_name=env.get(
                "SIGN402_WHATSAPP_TEMPLATE_NAME",
                "sign402_payment_approval",
            ),
            template_language=env.get("SIGN402_WHATSAPP_TEMPLATE_LANGUAGE", "en_US"),
            graph_api_version=env.get("SIGN402_WHATSAPP_GRAPH_API_VERSION", "v25.0"),
            timeout=float(env.get("SIGN402_WHATSAPP_HTTP_TIMEOUT_SECONDS", "20")),
        )
    return ImessageApprovalService(
        store=ImessageApprovalStore(
            store_path or DEFAULT_IMESSAGE_APPROVAL_STORE_PATH
        ),
        wallet_service=wallet_service,
        master_key=env.get("SIGN402_WALLET_MASTER_KEY", ""),
        notifier=ApprovalChannelNotifier(
            imessage_notifier=HermesCliNotifier(
                hermes_cli=env.get(
                    "SIGN402_HERMES_CLI",
                    "/home/hermes/.local/bin/hermes",
                ),
                hermes_home=env.get("SIGN402_HERMES_HOME", "/home/hermes/.hermes"),
                timeout=float(env.get("SIGN402_HERMES_SEND_TIMEOUT", "30")),
            ),
            whatsapp_notifier=whatsapp_notifier,
        ),
        purchase_approval_timeout=float(
            env.get("SIGN402_IMESSAGE_PURCHASE_APPROVAL_TIMEOUT", "120")
        ),
        purchase_approval_poll_interval=float(
            env.get("SIGN402_IMESSAGE_PURCHASE_APPROVAL_POLL_INTERVAL", "1")
        ),
    )


def normalize_e164(value: str) -> str:
    raw = str(value or "").strip()
    if "ext" in raw.lower():
        raise ValueError("photonUserId must be E.164")
    if raw.startswith("+"):
        candidate = "+" + "".join(ch for ch in raw[1:] if ch.isdigit())
    else:
        raise ValueError("photonUserId must be E.164")
    digits = candidate[1:]
    if not digits or digits[0] == "0" or len(digits) < 8 or len(digits) > 15:
        raise ValueError("photonUserId must be E.164")
    return candidate


def _ensure_column(
    db: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    existing = {
        str(row["name"])
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _build_fernet(master_key: str) -> Fernet:
    try:
        return Fernet(str(master_key or "").encode("ascii"))
    except Exception as exc:
        raise ValueError("SIGN402_WALLET_MASTER_KEY must be a valid Fernet key") from exc


def _require_telegram_user_id(telegram_user_id: str) -> str:
    user_id = str(telegram_user_id or "").strip()
    if not user_id:
        raise ValueError("telegramUserId is required")
    return user_id


def _normalize_approval_channel(channel: str) -> str:
    normalized = str(channel or "imessage").strip().lower().replace("-", "_")
    aliases = {
        "ios": "imessage",
        "i_message": "imessage",
        "sms": "imessage",
        "wa": "whatsapp",
        "whats_app": "whatsapp",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"imessage", "whatsapp"}:
        raise ValueError("approval channel must be imessage or whatsapp")
    return normalized


def _normalize_sender_identity(sender_user_id: str, channel: str) -> str:
    approval_channel = _normalize_approval_channel(channel)
    if approval_channel == "imessage":
        return normalize_e164(sender_user_id)
    wa_id = str(sender_user_id or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{4,19}", wa_id):
        raise ValueError("WhatsApp user ID must contain 5 to 20 digits")
    return wa_id


def _approval_channel_label(channel: str) -> str:
    return "WhatsApp" if _normalize_approval_channel(channel) == "whatsapp" else "iMessage"


def _approval_channel_list_label(channels: list[str]) -> str:
    labels = [_approval_channel_label(channel) for channel in channels]
    if not labels:
        return "iMessage"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _generate_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def _looks_like_pairing_code(value: str) -> bool:
    return (
        len(value) == PAIRING_CODE_LENGTH
        and all(ch in PAIRING_CODE_ALPHABET for ch in value)
    )


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sanitize_context_line(value: Any) -> str:
    """Flatten one approval line so its content cannot forge extra lines.

    Approval text is newline-joined before it reaches the approver, and some
    lines carry provider-controlled data (a Bitrefill product name). A line
    break surviving from there would let that data inject its own "Total:"
    above the real one, so the human's YES would be bound to terms they never
    saw. Whitespace is collapsed and control characters are dropped; Python's
    ``str.split`` already treats U+2028/U+2029/NEL as whitespace.
    """
    text = _CONTROL_CHARACTERS_RE.sub(" ", str(value if value is not None else ""))
    return " ".join(text.split())[:APPROVAL_LINE_MAX_CHARS]


def _sanitize_context_lines(lines: Any) -> list[str]:
    if not isinstance(lines, (list, tuple)):
        return []
    sanitized = (_sanitize_context_line(line) for line in lines)
    return [line for line in sanitized if line]


def _short_address(address: str) -> str:
    if len(address) >= 12:
        return f"{address[:6]}...{address[-4:]}"
    return address


def _approval_message(*, wallet_address: str, commitment_hash: str) -> str:
    return "\n".join(
        [
            "Action: TEST APPROVAL",
            f"Wallet: {_short_address(wallet_address)}",
            "Funds: No funds will move",
            "Expires: 2 minutes",
            f"Hash: {commitment_hash[:8]}",
            "",
            "Reply YES or NO.",
        ]
    )


def _approval_payment_requirements(requirement: dict[str, Any]) -> dict[str, Any]:
    return {
        "scheme": str(requirement.get("scheme") or ""),
        "network": str(requirement.get("network") or requirement.get("x402Network") or ""),
        "asset": str(requirement.get("asset") or ""),
        "amountAtomic": str(requirement.get("amountAtomic") or requirement.get("amount") or ""),
        "receiver": str(requirement.get("receiver") or requirement.get("payTo") or ""),
        "resource": str(requirement.get("resource") or ""),
        "paymentIntent": str(requirement.get("paymentIntent") or ""),
        "purpose": str(requirement.get("purpose") or ""),
    }


def _purchase_context_lines(
    *,
    tool_name: str,
    wallet_address: str,
    resource_url: str,
    payment_requirements: dict[str, Any],
) -> list[str]:
    return [
        f"Action: BUY {str(tool_name or 'x402 resource').upper()}",
        f"Cost: {_display_amount(payment_requirements)}",
        f"Wallet: {_short_address(wallet_address)}",
        f"To: {_short_address(str(payment_requirements.get('receiver') or ''))}",
        f"Resource: {_short_resource(resource_url)}",
    ]


def _purchase_approval_message(
    *,
    context_lines: list[str],
    commitment_hash: str,
) -> str:
    # Sanitize here too: this is the last step before the approver reads the
    # text, so a caller that forgets to flatten still cannot forge a line.
    safe_lines = _sanitize_context_lines(context_lines)
    expires_lines = [
        line for line in safe_lines if line.strip().lower().startswith("expires:")
    ]
    body_lines = [
        line for line in safe_lines if not line.strip().lower().startswith("expires:")
    ]
    expires_line = expires_lines[0] if expires_lines else "Expires: 2 minutes"
    return "\n".join(
        [
            *body_lines,
            expires_line,
            f"Hash: {commitment_hash[:8]}",
            "",
            "Reply YES or NO.",
        ]
    )


def _display_amount(requirement: dict[str, Any]) -> str:
    amount_atomic = str(requirement.get("amountAtomic") or requirement.get("amount") or "0")
    asset = str(requirement.get("asset") or "").lower()
    try:
        atomic = int(amount_atomic)
    except ValueError:
        return amount_atomic
    if asset == "0x833589fcD6edb6e08f4c7c32d4f71b54bda02913".lower():
        whole = atomic // 1_000_000
        fractional = str(atomic % 1_000_000).rjust(6, "0").rstrip("0")
        value = str(whole) if not fractional else f"{whole}.{fractional}"
        return f"{value} USDC"
    return amount_atomic


def _short_resource(resource_url: str) -> str:
    text = str(resource_url or "").removeprefix("https://").removeprefix("http://")
    return text[:64] + "..." if len(text) > 67 else text


def _decision_text(action_type: str, final_status: str) -> str:
    if action_type == "sign402_purchase":
        return f"Sign402 payment {final_status}."
    return f"Sign402 test approval {final_status}."


def _link_failed() -> dict[str, Any]:
    return {
        "ok": False,
        "imessageText": (
            "This pairing code is invalid or expired. Please request a new code in Telegram."
        ),
    }


def _no_pending() -> dict[str, Any]:
    return {"ok": False, "imessageText": "No pending approval."}


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass
