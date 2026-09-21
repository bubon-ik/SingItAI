"""Natural-language entry to existing workflows, without granting tool authority."""

from __future__ import annotations

import re
import hashlib
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

from . import intent_router


class Assistant:
    def __init__(self):
        self.pending = {}
        self.attempts = []
        self.lock = threading.RLock()

    def clear(self, user_id):
        with self.lock:
            self.pending.pop(str(user_id), None)

    def remember(self, user_id, intent, stage):
        with self.lock:
            if len(self.pending) >= 4096 and user_id not in self.pending:
                self.pending.pop(next(iter(self.pending)))
            self.pending[user_id] = (time.monotonic() + 900, intent, stage)

    def handle(self, *, event, source, gateway, api):
        if not intent_router.enabled() or not api._is_telegram_source(source):
            return None
        if getattr(source, "chat_type", "dm") not in {"dm", "private"}:
            return None
        identity = api._identity_from_telegram_source(source)
        text = str(getattr(event, "text", "") or "").strip()
        if identity is None or not text or text.startswith("/"):
            return None
        user_id = str(identity.user_id)
        # Existing wizards own their replies even when they do not recognize them.
        if (api._chat_setup(user_id, source)
                or user_id in api._CHAT_MODEL_PENDING or user_id in api._BITREFILL_SESSIONS
                or user_id in api._WITHDRAW_SESSIONS
                or user_id in api._IMESSAGE_CONNECT_SESSIONS):
            return None

        def send(message, buttons=None):
            api._send_fixed_reply(gateway, source, message, reply_markup=(
                api._reply_keyboard(buttons) if buttons else api._telegram_main_menu_reply_markup()))

        with self.lock:
            pending = self.pending.pop(user_id, None)
        country_intent = None
        if pending and pending[0] > time.monotonic():
            _, intent, stage = pending
            ru = intent.language == "ru"
            if stage == "alternative":
                if text.casefold() in {"yes", "да", "show gift cards", "показать подарочные карты"}:
                    self.advance(replace(intent, action="gift_card"), identity, source, gateway, api, send)
                elif text.casefold() in {"no", "нет"}:
                    send("Хорошо. Напиши другую задачу или выбери действие в меню." if ru else
                         "Okay. Tell me another task or choose an action from the menu.")
                else:
                    # A different task is a new request, not consent to the alternative.
                    pending = None
                if pending:
                    return dict(api._SKIP_RESULT)
            elif stage == "country":
                country = text.upper()
                if country in intent_router.COUNTRIES:
                    self.advance(replace(intent, country=country), identity, source, gateway, api, send)
                    return dict(api._SKIP_RESULT)
                else:
                    country_intent = intent

        now = time.monotonic()
        with self.lock:
            self.attempts = [(at, uid) for at, uid in self.attempts if now - at < 60]
            allowed = len(self.attempts) < 120 and sum(uid == user_id for _, uid in self.attempts) < 12
            if allowed:
                self.attempts.append((now, user_id))
        if not allowed:
            send("Please use the menu for now, or try your message again in a minute.")
            return dict(api._SKIP_RESULT)
        action = "assistant:classify:" + hashlib.sha256(text.encode()).hexdigest()[:16]
        generation = api._reserve_telegram_operation(user_id, action)
        if generation is None:
            return dict(api._SKIP_RESULT)

        def work():
            try:
                intent = intent_router.classify(text)
            except intent_router.RouterUnavailable:
                intent = None
            # Cancellation and new commands win over a late model response.
            with api._TELEGRAM_OPERATION_LOCK:
                if not api._finish_telegram_operation(user_id, generation):
                    return
                if intent is None:
                    if country_intent is not None:
                        self.remember(user_id, country_intent, "country")
                    ru = bool(re.search("[А-Яа-яЁё]", text))
                    send("Не получилось определить задачу. Выбери действие в меню — оно работает без AI-чата."
                         if ru else "I couldn't identify the task. Choose an action from the menu; no AI chat setup is needed.")
                else:
                    if country_intent is not None:
                        intent = replace(country_intent, country=intent.country)
                    self.advance(intent, identity, source, gateway, api, send, original_text=text)

        try:
            api._run_in_background(work)
        except Exception:
            api._finish_telegram_operation(user_id, generation)
            send("Please choose an action from the menu.")
        return dict(api._SKIP_RESULT)

    def advance(self, intent, identity, source, gateway, api, send, original_text=None):
        user_id = str(identity.user_id)
        ru = intent.language == "ru"
        if intent.action in {"balance", "order_status", "limits"}:
            if intent.action == "balance" and intent.network == "other":
                send("Какой баланс показать? Используй /balance base или /balance solana." if ru else
                     "Which balance? Use /balance base or /balance solana.")
                return
            command = {"balance": "balance", "order_status": "last-purchase", "limits": "limits"}[intent.action]
            args = intent.network if intent.action == "balance" and intent.network in {"base", "solana"} else ""
            api._handle_telegram_public_command_request(command=command, args=args, source=source, gateway=gateway)
            return
        if intent.action in {"food", "goods", "travel"}:
            category = {"food": "food", "goods": "shopping", "travel": "travel"}[intent.action]
            intent = replace(intent, category=category)
            self.remember(user_id, intent, "alternative")
            service = ({"food": "доставку еды и продуктов", "goods": "заказы физических товаров",
                        "travel": "бронирования"} if ru else
                       {"food": "food or grocery deliveries", "goods": "physical-goods orders",
                        "travel": "bookings"})[intent.action]
            send(
                f"Я пока не могу оформлять {service}. "
                "Могу проверить подарочные карты по этой категории. Картой нужно будет воспользоваться "
                "у продавца и оформить заказ самостоятельно. Проверить доступные карты?" if ru else
                f"I can't place {service} yet. I can check "
                "gift cards in this category. You would redeem the card and place the order with "
                "the merchant yourself. Would you like me to check available cards?",
                (("Показать подарочные карты", "Нет"), ("Back",)) if ru else
                (("Show gift cards", "No"), ("Back",)))
            return
        if intent.action in {"esim", "topup", "gift_card"}:
            if intent.country not in intent_router.COUNTRIES:
                self.remember(user_id, intent, "country")
                send("В какой стране будешь пользоваться покупкой? Напиши название или выбери страну." if ru else
                     "Which country will you use it in? Type its name or choose a country.",
                     (("CZ", "DE", "US"), ("PL", "UA", "GB"), ("Back",)))
                return
            # Catalog browsing does not choose or change the payment network.
            api._BITREFILL_USER_COUNTRIES[user_id] = intent.country
            if intent.network in {"solana", "other"}:
                send("Могу показать каталог. Оплата в запрошенной сети через этого бота пока не подтверждена; "
                     "поиск ничего не оплачивает." if ru else
                     "I can show the catalog. Payment on your requested network is not confirmed as available "
                     "in this bot; browsing does not pay for anything.")
                # Do not silently drop an explicit unsupported payment-network request
                # into the existing Base purchase wizard.
                send("Для продолжения выбери поддерживаемый способ покупки через меню." if ru else
                     "To continue, choose a supported purchase method from the menu.")
                return
            if intent.action == "esim":
                send("Проверю eSIM для этой страны. В условиях пакета нужно проверить покрытие, срок и объём данных."
                     if ru else "I'll check eSIMs for that country. Check each package's coverage, validity and data allowance.")
                api._handle_bitrefill_search_input(identity=identity, query="esim", source=source,
                                                  gateway=gateway, search_all_countries=False)
            else:
                if intent.action == "gift_card":
                    send("Покажу доступные карты. Это покупка карты, а не оформление заказа у продавца." if ru else
                         "I'll show available cards. Buying a card does not place an order with the merchant.")
                api._send_bitrefill_catalog_page(identity=identity,
                    category="mobile" if intent.action == "topup" else intent.category,
                    start=0, source=source, gateway=gateway)
            return
        if intent.action == "chat":
            if original_text and api._ai_chat_enabled():
                api._handle_telegram_chat_message(
                    event=SimpleNamespace(text=original_text), source=source, gateway=gateway)
                return
            send("Для разговора выбери Chat. Для действий я могу найти eSIM, пополнение или подарочную карту, "
                 "показать баланс и последний заказ." if ru else
                 "Choose Chat for a conversation. I can help find eSIMs, mobile top-ups and gift cards, "
                 "or show your balance and latest purchase.")
        else:
            send("Уточни задачу: найти eSIM, пополнить телефон, подобрать подарочную карту, показать баланс "
                 "или последний заказ? Другие действия доступны только через подключённые функции в меню." if ru else
                 "Would you like an eSIM, a mobile top-up, a gift card, your balance or your last purchase? "
                 "Other actions require a supported feature in the menu.")
