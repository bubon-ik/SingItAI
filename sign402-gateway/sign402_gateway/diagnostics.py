"""Operator-facing diagnostics for failures the user-facing API hides.

The gateway deliberately answers with generic messages so provider output can
never leak into responses or persisted state. That safety property used to cost
us the cause entirely: every failure path raised ``from None`` and nothing was
written anywhere, so a production purchase failure was undebuggable.

These helpers keep the safety property and restore the cause on a separate
channel: the service log, with values of secret-looking environment variables
redacted first, so provider output cannot carry credentials into the journal.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import traceback
from collections.abc import Mapping
from typing import Any


DEFAULT_DETAIL_LIMIT = 4000
MIN_REDACTABLE_LENGTH = 8

_SECRET_NAME = re.compile(
    r"SECRET|PASSWORD|PRIVATE|MNEMONIC|SEED|CREDENTIAL|TOKEN|API_KEY|_KEY$",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_EVM_VALUE = re.compile(r"\b0x[a-fA-F0-9]{16,}\b")
_ESIM_VALUE = re.compile(r"(?i)\bLPA:\S+")
_QR_VALUE = re.compile(r"(?i)\b(?:qr|qrcode|qr_code)\s*[:=]\s*\S+")
_BEARER_PAIR = re.compile(
    r"(?i)(?<![\w-])("
    r"pin|code|redemption|activation|activation_code|esim|"
    r"secret|password|private_key|mnemonic|seed|credential|"
    r"api_key|access_token|refresh_token|invoice_access_token|"
    r"invoiceAccessToken|access_link|accessLink|email"
    r")\s*[:=]\s*\S+"
)
# The buyer's address also reaches us unlabelled, inside provider prose.
_EMAIL_VALUE = re.compile(r"(?i)\b[^\s@\"'<>]+@[^\s@\"'<>]+\.[^\s@\"'<>.,;]+")
_PROVIDER_DIAGNOSTIC_LIMIT = 512


def redact_secrets(text: str, *, env: Mapping[str, str] | None = None) -> str:
    """Replace secret environment values with ``<redacted:NAME>`` markers.

    Longest values are replaced first so a secret that contains another secret
    as a prefix cannot survive as a partially redacted fragment.
    """
    source = os.environ if env is None else env
    secrets = [
        (name, value.strip())
        for name, value in source.items()
        if _SECRET_NAME.search(name)
        and len(str(value).strip()) >= MIN_REDACTABLE_LENGTH
    ]
    secrets.sort(key=lambda item: len(item[1]), reverse=True)
    redacted = str(text)
    for name, value in secrets:
        redacted = redacted.replace(value, f"<redacted:{name}>")
    return redacted


def bounded(text: str, *, limit: int = DEFAULT_DETAIL_LIMIT) -> str:
    value = str(text)
    if len(value) <= int(limit):
        return value
    return value[: int(limit)] + "…(truncated)"


def _safe_provider_text(
    value: Any,
    *,
    env: Mapping[str, str] | None,
) -> str:
    if isinstance(value, (Mapping, list, tuple, set)):
        return "<redacted:non-scalar>"
    text = redact_secrets(str(value), env=env)
    text = _URL.sub("<redacted:url>", text)
    text = _EVM_VALUE.sub("<redacted:evm-value>", text)
    text = _ESIM_VALUE.sub("<redacted:esim>", text)
    text = _QR_VALUE.sub("<redacted:qr>", text)
    text = _EMAIL_VALUE.sub("<redacted:email>", text)
    text = _BEARER_PAIR.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        text,
    )
    return bounded(text, limit=_PROVIDER_DIAGNOSTIC_LIMIT)


def safe_provider_diagnostic(
    detail: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str | int]:
    """Return a strict allowlist from provider error output.

    Unparseable bodies are represented only by byte length and a fingerprint,
    so payment links, addresses, redemption data, and other bearer values
    cannot reach the journal.
    """
    raw = str(detail)
    encoded = raw.encode("utf-8")
    fingerprint = {
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"type": "unparseable", **fingerprint}
    if not isinstance(parsed, Mapping):
        return {"type": "non_object", **fingerprint}

    source: Mapping[str, Any] = parsed
    nested_error = parsed.get("error")
    if isinstance(nested_error, Mapping):
        source = {**parsed, **nested_error}

    result: dict[str, str | int] = {}
    aliases = {
        "code": ("error_code", "errorCode", "code"),
        "message": ("message", "error_message", "errorMessage"),
        "status": ("status", "status_code", "statusCode"),
        "requestId": (
            "request_id",
            "requestId",
            "trace_id",
            "traceId",
        ),
    }
    for target, keys in aliases.items():
        source_key = next((key for key in keys if source.get(key) is not None), None)
        if source_key is None:
            continue
        result[target] = _safe_provider_text(source[source_key], env=env)
    if result:
        return result
    return {"type": "no_allowlisted_fields", **fingerprint}


TRANSPORT_LOGGERS = ("httpx", "httpcore", "hpack", "mcp")


def configure_logging(*, level: str = "INFO", stream: Any | None = None) -> None:
    """Turn on service logging without leaking request URLs.

    httpx logs every request line at INFO, and the Bitrefill MCP endpoint
    carries the API key in the URL path, so the transport loggers stay at
    WARNING no matter what level the service runs at.
    """
    logging.basicConfig(
        level=str(level).upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=stream,
    )
    for name in TRANSPORT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def describe_payload_shape(
    value: Any,
    *,
    depth: int = 3,
    max_keys: int = 40,
) -> Any:
    """Describe a decoded payload by key names and value types only.

    A provider response that fails a structural check is unusable as evidence
    unless its shape is visible, but its values can be payment addresses or
    redemption codes. Field names and type names carry the diagnosis without
    carrying any of that, so only those are returned.
    """
    if isinstance(value, Mapping):
        if depth <= 0:
            return f"object({len(value)} keys)"
        shape: dict[str, Any] = {}
        items = sorted(
            ((str(key), item) for key, item in value.items()),
            key=lambda pair: pair[0],
        )
        for index, (key, item) in enumerate(items):
            if index >= int(max_keys):
                shape["…"] = f"+{len(items) - int(max_keys)} more"
                break
            shape[key] = describe_payload_shape(
                item,
                depth=depth - 1,
                max_keys=max_keys,
            )
        return shape
    if isinstance(value, (list, tuple)):
        if not value:
            return f"list[{len(value)}]"
        if depth <= 0:
            return f"list[{len(value)}]"
        return {
            f"list[{len(value)}]": describe_payload_shape(
                value[0],
                depth=depth - 1,
                max_keys=max_keys,
            )
        }
    return type(value).__name__


def _context(
    fields: dict[str, Any],
    *,
    env: Mapping[str, str] | None,
) -> str:
    return " ".join(
        f"{key}={_safe_provider_text(value, env=env)}"
        for key, value in fields.items()
        if value != ""
    )


def _safe(
    text: str,
    *,
    env: Mapping[str, str] | None,
    limit: int,
) -> str:
    return bounded(redact_secrets(text, env=env), limit=limit)


def log_swallowed_failure(
    logger: logging.Logger,
    event: str,
    exc: BaseException,
    *,
    env: Mapping[str, str] | None = None,
    limit: int = DEFAULT_DETAIL_LIMIT,
    **fields: Any,
) -> None:
    """Record the real cause of a failure the caller is about to generalize.

    The traceback is formatted here rather than passed as ``exc_info`` so the
    redaction pass also covers chained causes and frame contents.
    """
    chain = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    logger.error(
        "%s [%s]: %s: %s\n%s",
        event,
        _context(fields, env=env),
        type(exc).__name__,
        _safe(str(exc), env=env, limit=limit),
        _safe(chain, env=env, limit=limit),
    )


def log_hidden_detail(
    logger: logging.Logger,
    event: str,
    detail: str,
    *,
    env: Mapping[str, str] | None = None,
    limit: int = DEFAULT_DETAIL_LIMIT,
    **fields: Any,
) -> None:
    """Record provider output that must stay out of responses and stored state."""
    logger.error(
        "%s [%s]: %s",
        event,
        _context(fields, env=env),
        _safe(detail, env=env, limit=limit),
    )
