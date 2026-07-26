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


def _context(fields: dict[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in fields.items() if value != "")


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
        _context(fields),
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
        _context(fields),
        _safe(detail, env=env, limit=limit),
    )
