"""Telegram-native controls. No web app and no payment authority in callbacks."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass
class MessageCard:
    """One operation's message; send/edit ordering belongs to the chat queue."""

    message_id: int | None = None


def actions(rows):
    """Label/action pairs, with a text keyboard fallback for older adapters."""
    return {
        "keyboard": [[{"text": label} for label, _ in row] for row in rows],
        "resize_keyboard": True,
        "_actions": [[action for _, action in row] for row in rows],
    }


class ButtonSessions:
    """Short-lived, one-use view capabilities bound to the actual chat and user.

    The payload contains an opaque nonce and index, never user ids, provider
    tokens or arbitrary commands. Changing screen invalidates its old buttons.
    """

    def __init__(self, *, clock=time.monotonic, ttl=900, limit=4096):
        self.clock, self.ttl, self.limit = clock, ttl, limit
        self.views = {}
        self.lock = threading.Lock()

    def invalidate(self, user_id, chat_id):
        with self.lock:
            self.views.pop((str(user_id), str(chat_id)), None)

    def render(self, user_id, chat_id, markup, source):
        key = (str(user_id), str(chat_id))
        nonce = secrets.token_urlsafe(12)
        choices, rows = [], []
        for row_index, row in enumerate(markup["keyboard"]):
            buttons = []
            for column, button in enumerate(row):
                action = markup.get("_actions", [])[row_index][column]
                index = len(choices)
                choices.append(action)
                buttons.append({"text": button["text"], "callback_data": f"singit:{nonce}:{index}"})
            rows.append(buttons)
        with self.lock:
            now = self.clock()
            self.views = {k: v for k, v in self.views.items() if v["expires"] > now}
            if len(self.views) >= self.limit:
                self.views.pop(next(iter(self.views)))
            self.views[key] = {"nonce": nonce, "choices": choices, "source": source,
                               "expires": now + self.ttl, "message_id": None}
        return {"inline_keyboard": rows}

    def bind(self, user_id, chat_id, markup, message_id):
        if not markup or not markup.get("inline_keyboard") or not message_id:
            return
        data = markup["inline_keyboard"][0][0].get("callback_data", "")
        with self.lock:
            view = self.views.get((str(user_id), str(chat_id)))
            if view and data.startswith(f"singit:{view['nonce']}:"):
                view["message_id"] = message_id

    def claim(self, user_id, chat_id, message_id, data):
        key = (str(user_id), str(chat_id))
        with self.lock:
            view = self.views.get(key)
            if not view or view["expires"] <= self.clock() or view["message_id"] != message_id:
                return None
            parts = str(data).split(":")
            if len(parts) != 3 or parts[:2] != ["singit", view["nonce"]] or not parts[2].isdigit():
                return None
            index = int(parts[2])
            if index >= len(view["choices"]):
                return None
            self.views.pop(key)
            return view["choices"][index], view["source"]


def plain_markup(markup):
    return {k: v for k, v in markup.items() if not k.startswith("_")} if markup else None


def product_label(product):
    name = str(product.get("name") or "Product").strip()
    country = str(product.get("country") or "").strip()
    return f"{name[:48]} · {country}" if country else name[:56]


def package_label(product, package):
    value = str(package.get("value") or package.get("packageId") or "")
    currency = str(product.get("currency") or "").upper()
    price = str(package.get("displayPriceUsd") or "")
    amount = f"{value} {currency}".strip()
    return f"{amount} · ≈ ${price}" if price else amount
