from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SensitiveStateError(RuntimeError):
    pass


class SensitiveStateConfigurationError(SensitiveStateError):
    pass


class SensitiveStateDecryptionError(SensitiveStateError):
    pass


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SensitiveStateError("sensitive state directory must not be a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    if _mode(path) != 0o700:
        raise SensitiveStateError("sensitive state directory is not mode 0700")


def ensure_private_file(path: Path) -> None:
    if path.is_symlink():
        raise SensitiveStateError("sensitive state file must not be a symlink")
    if not path.exists():
        return
    os.chmod(path, 0o600)
    if _mode(path) != 0o600:
        raise SensitiveStateError("sensitive state file is not mode 0600")


def atomic_write_private_json(
    path: Path,
    payload: Mapping[str, Any],
) -> None:
    serialized = (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    ensure_private_directory(path.parent)
    ensure_private_file(path)
    fd = -1
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_private_file(temp_path)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


class SensitiveStateCipher:
    def __init__(self, master_key: str):
        try:
            self._fernet = Fernet(str(master_key or "").encode("ascii"))
        except (TypeError, UnicodeEncodeError, ValueError):
            raise SensitiveStateConfigurationError(
                "SIGN402_WALLET_MASTER_KEY must be a valid Fernet key"
            ) from None

    def encrypt_text(self, value: str) -> str:
        return self._fernet.encrypt(str(value).encode("utf-8")).decode("ascii")

    def decrypt_text(self, value: str) -> str:
        try:
            return self._fernet.decrypt(str(value).encode("ascii")).decode("utf-8")
        except (
            InvalidToken,
            UnicodeDecodeError,
            UnicodeEncodeError,
            ValueError,
        ):
            raise SensitiveStateDecryptionError(
                "encrypted sensitive state could not be decrypted"
            ) from None

    def encrypt_json(self, value: Mapping[str, Any]) -> str:
        serialized = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return self.encrypt_text(serialized)

    def decrypt_json(self, value: str) -> dict[str, Any]:
        try:
            decoded = json.loads(self.decrypt_text(value))
        except (json.JSONDecodeError, SensitiveStateDecryptionError):
            raise SensitiveStateDecryptionError(
                "encrypted sensitive state could not be decrypted"
            ) from None
        if not isinstance(decoded, dict):
            raise SensitiveStateDecryptionError(
                "encrypted sensitive state could not be decrypted"
            ) from None
        return decoded
