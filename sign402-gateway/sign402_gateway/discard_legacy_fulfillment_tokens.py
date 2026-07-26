from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn, TypedDict, cast

from .secure_state import SensitiveStateError, atomic_write_private_json


class LegacyFulfillmentTokenCleanupError(RuntimeError):
    pass


class CleanupReport(TypedDict):
    mode: str
    records: int
    plaintext_token_records: int
    encrypted_token_records: int
    token_fields_removed: int
    changed: bool


class _DuplicateObjectMember(ValueError):
    pass


class _NonstandardJsonNumber(ValueError):
    pass


def _object_without_duplicate_members(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateObjectMember
        result[key] = value
    return result


def _reject_nonstandard_json_number(_: str) -> NoReturn:
    raise _NonstandardJsonNumber


def _normalize_lossless_json_numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        converted = float(value)
        if (
            not math.isfinite(converted)
            or Decimal(str(converted)) != value
        ):
            raise LegacyFulfillmentTokenCleanupError(
                "purchase store numbers must be losslessly representable"
            )
        return converted
    if isinstance(value, list):
        return [
            _normalize_lossless_json_numbers(item)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _normalize_lossless_json_numbers(item)
            for key, item in value.items()
        }
    return value


def _load_purchase_store(path: Path) -> dict[str, dict[str, Any]]:
    if path.is_symlink():
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must not be a symlink"
        )
    if not path.exists():
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store does not exist"
        )
    if not path.is_file():
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must be a regular file"
        )
    try:
        serialized = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must be UTF-8 JSON"
        ) from None
    except OSError:
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store could not be read"
        ) from None
    try:
        payload = json.loads(
            serialized,
            object_pairs_hook=_object_without_duplicate_members,
            parse_float=Decimal,
            parse_constant=_reject_nonstandard_json_number,
        )
    except _DuplicateObjectMember:
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must not contain duplicate object members"
        ) from None
    except _NonstandardJsonNumber:
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must contain strict JSON numbers"
        ) from None
    except InvalidOperation:
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store numbers must be losslessly representable"
        ) from None
    except json.JSONDecodeError:
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store must contain valid JSON"
        ) from None
    payload = _normalize_lossless_json_numbers(payload)
    if not isinstance(payload, dict):
        raise LegacyFulfillmentTokenCleanupError(
            "purchase store root must be an object"
        )
    if any(not isinstance(record, dict) for record in payload.values()):
        raise LegacyFulfillmentTokenCleanupError(
            "every purchase store record must be an object"
        )
    return cast(dict[str, dict[str, Any]], payload)


def cleanup_legacy_fulfillment_tokens(
    path: Path,
    *,
    apply: bool = False,
) -> CleanupReport:
    data = _load_purchase_store(Path(path))
    plaintext_records = sum(
        "fulfillmentToken" in record for record in data.values()
    )
    encrypted_records = sum(
        "encryptedFulfillmentToken" in record for record in data.values()
    )
    token_fields = plaintext_records + encrypted_records
    changed = False
    if apply and token_fields:
        cleaned = {
            key: {
                field: value
                for field, value in record.items()
                if field not in {
                    "fulfillmentToken",
                    "encryptedFulfillmentToken",
                }
            }
            for key, record in data.items()
        }
        try:
            atomic_write_private_json(Path(path), cleaned)
        except (OSError, SensitiveStateError):
            raise LegacyFulfillmentTokenCleanupError(
                "purchase store could not be atomically updated"
            ) from None
        changed = True
    return {
        "mode": "apply" if apply else "dry-run",
        "records": len(data),
        "plaintext_token_records": plaintext_records,
        "encrypted_token_records": encrypted_records,
        "token_fields_removed": token_fields,
        "changed": changed,
    }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or discard fulfillment-token fields from a "
            "Sign402 user purchase store."
        )
    )
    parser.add_argument(
        "--path",
        required=True,
        type=Path,
        help="absolute path to user-purchases.json",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically remove token fields; default is dry run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        report = cleanup_legacy_fulfillment_tokens(
            args.path,
            apply=args.apply,
        )
    except LegacyFulfillmentTokenCleanupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
