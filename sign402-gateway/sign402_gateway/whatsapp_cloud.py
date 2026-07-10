from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_MAX_RESPONSE_BYTES = 64 * 1024
_WA_ID_PATTERN = re.compile(r"[1-9][0-9]{4,19}")
_PHONE_NUMBER_ID_PATTERN = re.compile(r"[0-9]{6,32}")
_APPROVAL_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}")
_TEMPLATE_NAME_PATTERN = re.compile(r"[a-z0-9_]{1,512}")
_LANGUAGE_PATTERN = re.compile(r"[A-Za-z]{2,3}(?:_[A-Za-z]{2})?")
_GRAPH_VERSION_PATTERN = re.compile(r"v[0-9]{1,3}\.[0-9]{1,2}")


class MetaWhatsAppTemplateNotifier:
    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        template_name: str = "sign402_payment_approval",
        template_language: str = "en_US",
        graph_api_version: str = "v25.0",
        opener: Callable[..., Any] = urlopen,
        timeout: float = 20.0,
    ):
        self.access_token = str(access_token or "").strip()
        self.phone_number_id = str(phone_number_id or "").strip()
        self.template_name = str(template_name or "").strip()
        self.template_language = str(template_language or "").strip()
        self.graph_api_version = str(graph_api_version or "").strip()
        self.opener = opener
        self.timeout = max(1.0, float(timeout))

    def send_approval(
        self,
        *,
        wa_id: str,
        approval_id: str,
        context_lines: list[str],
        expires_at: int,
    ) -> dict[str, object]:
        normalized_wa_id = str(wa_id or "").strip()
        normalized_approval_id = str(approval_id or "").strip()
        safe_context = _safe_context(context_lines)
        try:
            expiry_value = int(expires_at)
        except (TypeError, ValueError):
            expiry_value = 0
        if not self._configured() or not (
            _WA_ID_PATTERN.fullmatch(normalized_wa_id)
            and _APPROVAL_ID_PATTERN.fullmatch(normalized_approval_id)
            and safe_context
            and expiry_value > 0
        ):
            return {"ok": False, "error": "invalid_request"}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalized_wa_id,
            "type": "template",
            "template": {
                "name": self.template_name,
                "language": {"code": self.template_language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": safe_context},
                            {"type": "text", "text": normalized_approval_id[:24]},
                            {"type": "text", "text": _format_expiry(expiry_value)},
                        ],
                    },
                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": "0",
                        "parameters": [
                            {
                                "type": "payload",
                                "payload": f"sign402:approve:{normalized_approval_id}",
                            }
                        ],
                    },
                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": "1",
                        "parameters": [
                            {
                                "type": "payload",
                                "payload": f"sign402:reject:{normalized_approval_id}",
                            }
                        ],
                    },
                ],
            },
        }
        request = Request(
            (
                f"https://graph.facebook.com/{self.graph_api_version}/"
                f"{self.phone_number_id}/messages"
            ),
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            response = self.opener(request, timeout=self.timeout)
        except HTTPError as exc:
            exc.close()
            return {"ok": False, "error": "http_error"}
        except (URLError, TimeoutError, OSError):
            return {"ok": False, "error": "transport_error"}

        try:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if len(raw) > _MAX_RESPONSE_BYTES:
            return {"ok": False, "error": "invalid_response"}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "error": "invalid_response"}
        messages = parsed.get("messages") if isinstance(parsed, dict) else None
        first = messages[0] if isinstance(messages, list) and messages else None
        message_id = str(first.get("id") or "").strip() if isinstance(first, dict) else ""
        if not message_id or len(message_id) > 512:
            return {"ok": False, "error": "invalid_response"}
        return {"ok": True, "messageId": message_id}

    def _configured(self) -> bool:
        return bool(
            self.access_token
            and _PHONE_NUMBER_ID_PATTERN.fullmatch(self.phone_number_id)
            and _TEMPLATE_NAME_PATTERN.fullmatch(self.template_name)
            and _LANGUAGE_PATTERN.fullmatch(self.template_language)
            and _GRAPH_VERSION_PATTERN.fullmatch(self.graph_api_version)
        )


def _safe_context(context_lines: list[str]) -> str:
    if not isinstance(context_lines, list):
        return ""
    lines = []
    for value in context_lines[:8]:
        line = " ".join(str(value or "").split()).strip()
        if line:
            lines.append(line[:160])
    return "\n".join(lines)[:960]


def _format_expiry(expires_at: int) -> str:
    return datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
