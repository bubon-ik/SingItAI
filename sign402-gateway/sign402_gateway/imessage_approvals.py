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


DEFAULT_IMESSAGE_APPROVAL_STORE_PATH = (
    Path.home() / ".sign402" / "imessage-approvals.db"
)
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8
PAIRING_TTL_SECONDS = 10 * 60
TEST_APPROVAL_TTL_SECONDS = 2 * 60
PURCHASE_APPROVAL_TTL_SECONDS = 2 * 60


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
                    code_digest TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER
                )
                """
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

    def send(self, *, photon_user_id: str, message: str) -> dict[str, object]:
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

    def create_pairing(self, telegram_user_id: str) -> dict[str, Any]:
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet_status = self.wallet_service.wallet_status(user_id)
        if not wallet_status.get("ok"):
            return {
                "ok": False,
                "telegramText": wallet_status.get(
                    "telegramText",
                    "No Base agent wallet yet. Send /create_wallet to create one.",
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
                WHERE telegram_user_id = ? AND status = 'active'
                """,
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO imessage_pairings (
                    telegram_user_id, code_digest, status, created_at, expires_at
                )
                VALUES (?, ?, 'active', ?, ?)
                """,
                (user_id, code_digest, now, now + PAIRING_TTL_SECONDS),
            )
            self._record_audit(
                db,
                telegram_user_id=user_id,
                event_type="pairing_created",
                status="active",
            )
        return {
            "ok": True,
            "code": code,
            "expiresInSeconds": PAIRING_TTL_SECONDS,
            "telegramText": (
                "Send this code to the Hermes iMessage line within 10 minutes:\n"
                f"{code}\n\n"
                "After it is linked, approvals will happen in iMessage."
            ),
        }

    def link_photon_sender(self, code: str, photon_user_id: str) -> dict[str, Any]:
        normalized_photon = normalize_e164(photon_user_id)
        code_value = str(code or "").strip().upper()
        if not _looks_like_pairing_code(code_value):
            return _link_failed()

        now = self._now()
        code_digest = self._digest(f"pairing:{code_value}")
        photon_digest = self._digest(f"photon:{normalized_photon}")
        encrypted_photon = self._fernet.encrypt(
            normalized_photon.encode("utf-8")
        ).decode("ascii")
        with self.store.lock, self.store._database() as db:
            self._expire_pairings(db, now)
            row = db.execute(
                """
                SELECT id, telegram_user_id
                FROM imessage_pairings
                WHERE code_digest = ? AND status = 'active' AND expires_at > ?
                """,
                (code_digest, now),
            ).fetchone()
            if row is None:
                return _link_failed()

            user_id = str(row["telegram_user_id"])
            existing_user = db.execute(
                "SELECT telegram_user_id FROM imessage_links WHERE telegram_user_id = ?",
                (user_id,),
            ).fetchone()
            existing_phone = db.execute(
                "SELECT telegram_user_id FROM imessage_links WHERE photon_digest = ?",
                (photon_digest,),
            ).fetchone()
            if existing_user is not None or existing_phone is not None:
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
                )
                return {
                    "ok": False,
                    "imessageText": (
                        "Could not link this iMessage sender. Please return to Telegram."
                    ),
                }

            db.execute(
                """
                INSERT INTO imessage_links (
                    telegram_user_id, photon_digest, encrypted_photon_user_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, photon_digest, encrypted_photon, now, now),
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
            )
        return {
            "ok": True,
            "telegramUserId": user_id,
            "imessageText": (
                "iMessage linked. Future Sign402 approvals will arrive here."
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
                    "No Base agent wallet yet. Send /create_wallet to create one.",
                ),
            }
        wallet = wallet_status.get("wallet") or {}
        wallet_address = str(wallet.get("address", "") or "")

        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            link = db.execute(
                """
                SELECT encrypted_photon_user_id
                FROM imessage_links
                WHERE telegram_user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if link is None:
                return {
                    "ok": False,
                    "telegramText": (
                        "iMessage is not linked yet. Send /connect_imessage first."
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
            encrypted_photon = str(link["encrypted_photon_user_id"])
            photon_user_id = self._fernet.decrypt(
                encrypted_photon.encode("ascii")
            ).decode("utf-8")
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

        delivery = self.notifier.send(photon_user_id=photon_user_id, message=message)
        if not delivery.get("ok"):
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
                "telegramText": "could not deliver the iMessage approval. No action was approved.",
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
            )
        return {
            "ok": True,
            "approvalId": approval_id,
            "commitmentHash": commitment_hash,
            "telegramText": (
                "Test approval sent to iMessage. Reply YES or NO there."
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
        wallet_status = self.wallet_service.wallet_status(user_id)
        if not wallet_status.get("ok"):
            return {
                "ok": False,
                "approved": False,
                "telegramText": wallet_status.get(
                    "telegramText",
                    "No Base agent wallet yet. Send /create_wallet to create one.",
                ),
            }

        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            link = db.execute(
                """
                SELECT encrypted_photon_user_id
                FROM imessage_links
                WHERE telegram_user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if link is None:
                return {
                    "ok": False,
                    "approved": False,
                    "telegramText": (
                        "iMessage is not linked yet. Send /connect_imessage first."
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
                "contextLines": [str(line) for line in context_lines],
                "createdAt": now,
                "expiresAt": expires_at,
            }
            canonical_json = _canonical_json(canonical)
            approval_id = secrets.token_urlsafe(18)
            encrypted_photon = str(link["encrypted_photon_user_id"])
            photon_user_id = self._fernet.decrypt(
                encrypted_photon.encode("ascii")
            ).decode("utf-8")
            message = _purchase_approval_message(
                context_lines=[str(line) for line in context_lines],
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
                action_type=str(action_type or "sign402_external"),
                commitment_hash=normalized_hash,
                status="pending",
            )

        delivery = self.notifier.send(photon_user_id=photon_user_id, message=message)
        if not delivery.get("ok"):
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
                "telegramText": "could not deliver the iMessage approval. No action was approved.",
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
                    "No Base agent wallet yet. Send /create_wallet to create one.",
                ),
            }
        wallet = wallet_status.get("wallet") or {}
        wallet_address = str(wallet.get("address", "") or "")

        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            link = db.execute(
                """
                SELECT encrypted_photon_user_id
                FROM imessage_links
                WHERE telegram_user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if link is None:
                return {
                    "ok": False,
                    "status": "imessage_not_linked",
                    "telegramText": (
                        "iMessage is not linked yet. Send /connect_imessage first."
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
            context_lines = _purchase_context_lines(
                tool_name=str(tool_name or "x402 resource"),
                wallet_address=wallet_address,
                resource_url=str(resource_url or ""),
                payment_requirements=payment_requirements,
            )
            encrypted_photon = str(link["encrypted_photon_user_id"])
            photon_user_id = self._fernet.decrypt(
                encrypted_photon.encode("ascii")
            ).decode("utf-8")
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

        delivery = self.notifier.send(photon_user_id=photon_user_id, message=message)
        if not delivery.get("ok"):
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
                "telegramText": "could not deliver the iMessage approval. No action was approved.",
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
            )

        return self._wait_for_approval_decision(
            approval_id=approval_id,
            telegram_user_id=user_id,
            commitment_hash=commitment_hash,
            expires_at=expires_at,
        )

    def pending_for_photon_sender(self, photon_user_id: str) -> dict[str, Any]:
        normalized_photon = normalize_e164(photon_user_id)
        photon_digest = self._digest(f"photon:{normalized_photon}")
        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            link = db.execute(
                """
                SELECT telegram_user_id
                FROM imessage_links
                WHERE photon_digest = ?
                """,
                (photon_digest,),
            ).fetchone()
            if link is None:
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
    ) -> dict[str, Any]:
        normalized_photon = normalize_e164(photon_user_id)
        expected_approval_id = str(approval_id).strip() if approval_id is not None else ""
        normalized_decision = str(decision or "").strip().upper()
        if normalized_decision not in {"YES", "NO"}:
            return {"ok": False, "imessageText": "Reply YES or NO."}
        final_status = "approved" if normalized_decision == "YES" else "denied"
        photon_digest = self._digest(f"photon:{normalized_photon}")
        now = self._now()
        with self.store.lock, self.store._database() as db:
            self._expire_approvals(db, now)
            link = db.execute(
                """
                SELECT telegram_user_id
                FROM imessage_links
                WHERE photon_digest = ?
                """,
                (photon_digest,),
            ).fetchone()
            if link is None:
                return _no_pending()
            user_id = str(link["telegram_user_id"])
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
                    "telegramText": "Purchase was not approved in iMessage.",
                }
            if now >= expires_at:
                break
            time.sleep(self.purchase_approval_poll_interval)
        return {
            "ok": False,
            "status": "timeout",
            "approvalId": approval_id,
            "commitmentHash": commitment_hash,
            "telegramText": "iMessage approval timed out. No funds were moved.",
        }

    def _now(self) -> int:
        return int(self.now())

    def _digest(self, value: str) -> str:
        return hmac.new(
            self.master_key.encode("utf-8"),
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

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
    return ImessageApprovalService(
        store=ImessageApprovalStore(
            store_path or DEFAULT_IMESSAGE_APPROVAL_STORE_PATH
        ),
        wallet_service=wallet_service,
        master_key=env.get("SIGN402_WALLET_MASTER_KEY", ""),
        notifier=HermesCliNotifier(
            hermes_cli=env.get("SIGN402_HERMES_CLI", "/home/hermes/.local/bin/hermes"),
            hermes_home=env.get("SIGN402_HERMES_HOME", "/home/hermes/.hermes"),
            timeout=float(env.get("SIGN402_HERMES_SEND_TIMEOUT", "30")),
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


def _generate_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))


def _looks_like_pairing_code(value: str) -> bool:
    return (
        len(value) == PAIRING_CODE_LENGTH
        and all(ch in PAIRING_CODE_ALPHABET for ch in value)
    )


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
    expires_lines = [
        line for line in context_lines if str(line).strip().lower().startswith("expires:")
    ]
    body_lines = [
        line for line in context_lines if not str(line).strip().lower().startswith("expires:")
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
