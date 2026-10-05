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

    @staticmethod
    def resolve_followup(intent, pending):
        """Inherit missing slots only when continuing the pending task."""
        if not pending or pending[0] <= time.monotonic():
            return intent
        if intent.reply == "new_task":
            return intent
        _, previous, stage = pending
        action = intent.suggested_action if intent.action == "clarify" else intent.action
        continues = (
            stage == "alternative" and action == "gift_card"
            or stage in {"country", "confirm-intent"} and action == previous.action
        )
        if continues:
            return replace(
                intent,
                country=intent.country if intent.country in intent_router.COUNTRIES else previous.country,
                category=previous.category if intent.category == "all" and not intent.category_explicit else intent.category,
                network=previous.network if intent.network == "unspecified" else intent.network,
            )
        if (stage == "country" and intent.action == "clarify"
                and intent.suggested_action is None and intent.country in intent_router.COUNTRIES):
            return replace(previous, country=intent.country)
        # A balance request, another product task or an unsupported action must
        # never be rewritten as an answer to the previous country question.
        return intent

    @staticmethod
    def merge_slots(previous, reply):
        return replace(previous,
            country=reply.country if reply.country in intent_router.COUNTRIES else previous.country,
            category=reply.category if reply.category_explicit or reply.category != "all" else previous.category,
            category_explicit=reply.category_explicit or previous.category_explicit,
            network=previous.network if reply.network == "unspecified" else reply.network)

    def repeat_pending(self, pending, identity, source, gateway, api, send):
        expires, intent, stage = pending
        prompt = replace(intent, action="clarify", suggested_action=intent.action) if stage == "confirm-intent" else intent
        self.advance(prompt, identity, source, gateway, api, send)
        # Repeating a question must not keep abandoned context alive forever.
        with self.lock:
            current = self.pending.get(str(identity.user_id))
            if current:
                self.pending[str(identity.user_id)] = (expires, current[1], current[2])

    @staticmethod
    def wizard_owns_text(session, text, api):
        """Keep selections and private checkout input local; menus do not own every sentence."""
        stage = session.get("stage")
        browsing = {"menu", "select-category", "select-product", "select-package", "awaiting-country",
                    "awaiting-search", "loading-catalog", "loading-search", "loading-product"}
        if stage not in browsing:
            return True
        normalized = api._canonical_button_text(text)
        controls = {"back", "next", "previous", "change country", "browse catalog", "search products"}
        if normalized in controls or normalized in api._BITREFILL_CATEGORY_VALUES:
            return True
        if text.isdecimal():
            return True
        if stage == "awaiting-country" and (text.upper() in intent_router.COUNTRIES or normalized == "other"):
            return True
        return False

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
        # Private form values never go to the classifier. Browsing menus only
        # retain their actual selections, allowing a new task to interrupt them.
        if (api._chat_setup(user_id, source)
                or user_id in api._CHAT_MODEL_PENDING
                or user_id in api._WITHDRAW_SESSIONS
                or user_id in api._IMESSAGE_CONNECT_SESSIONS):
            return None
        browsing_session = api._BITREFILL_SESSIONS.get(user_id)
        if browsing_session is not None and self.wizard_owns_text(browsing_session, text, api):
            return None
        if api._canonical_button_text(text) == "back":
            return None

        def send(message, buttons=None):
            api._send_fixed_reply(gateway, source, message, reply_markup=(
                api._reply_keyboard(buttons) if buttons else api._telegram_main_menu_reply_markup()))

        def recover_browsing():
            # Starting classification cancels an older catalog fetch. If the
            # classifier fails, do not leave a cancelled loading screen active.
            if not browsing_session or api._BITREFILL_SESSIONS.get(user_id) is not browsing_session:
                return
            stage = browsing_session.get("stage")
            if stage in {"loading-catalog", "loading-search", "loading-product"}:
                previous = browsing_session.get("returnSession")
                if stage == "loading-product" and isinstance(previous, dict):
                    api._BITREFILL_SESSIONS[user_id] = previous
                else:
                    api._BITREFILL_SESSIONS[user_id] = {
                        "stage": {"loading-catalog": "select-category", "loading-search": "awaiting-search",
                                  "loading-product": "menu"}[stage],
                        "country": browsing_session.get("country", api._bitrefill_country(user_id)),
                    }

        with self.lock:
            pending = self.pending.get(user_id)
        if pending and pending[0] <= time.monotonic():
            self.clear(user_id)
            pending = None
        if pending and pending[0] > time.monotonic():
            _, intent, stage = pending
            ru = intent.language == "ru"
            if stage in {"alternative", "confirm-intent"}:
                if text.casefold() in {"yes", "да", "show gift cards", "показать подарочные карты"}:
                    api._invalidate_telegram_operation(user_id)
                    self.clear(user_id)
                    chosen = replace(intent, action="gift_card") if stage == "alternative" else intent
                    self.advance(chosen, identity, source, gateway, api, send)
                    return dict(api._SKIP_RESULT)
                elif text.casefold() in {"no", "нет"}:
                    api._invalidate_telegram_operation(user_id)
                    self.clear(user_id)
                    send("Хорошо. Напиши другую задачу или выбери действие в меню." if ru else
                         "Okay. Tell me another task or choose an action from the menu.")
                    return dict(api._SKIP_RESULT)
            elif stage == "country":
                country = text.upper()
                if country in intent_router.COUNTRIES:
                    api._invalidate_telegram_operation(user_id)
                    self.clear(user_id)
                    self.advance(replace(intent, country=country), identity, source, gateway, api, send)
                    return dict(api._SKIP_RESULT)
            elif stage == "network" and text.casefold() in {"base", "solana"}:
                api._invalidate_telegram_operation(user_id)
                self.clear(user_id)
                self.advance(replace(intent, network=text.casefold()), identity, source, gateway, api, send)
                return dict(api._SKIP_RESULT)

        context = None
        if pending:
            context = {name: getattr(pending[1], name) for name in ("action", "country", "category", "network")}
            context["stage"] = pending[2]
        elif browsing_session:
            context = {name: browsing_session.get(name) for name in ("stage", "country", "category")}
            context["action"] = "esim" if browsing_session.get("query") == "esim" else "catalog"

        now = time.monotonic()
        action = "assistant:classify:" + hashlib.sha256(text.encode()).hexdigest()[:16]
        with api._TELEGRAM_OPERATION_LOCK:
            if api._TELEGRAM_ACTIVE_OPERATIONS.get(user_id, (None, None))[1] == action:
                return dict(api._SKIP_RESULT)
        with self.lock:
            self.attempts = [(at, uid) for at, uid in self.attempts if now - at < 60]
            allowed = len(self.attempts) < 120 and sum(uid == user_id for _, uid in self.attempts) < 12
            if allowed:
                self.attempts.append((now, user_id))
        if not allowed:
            api._invalidate_telegram_operation(user_id)
            recover_browsing()
            send("Please use the menu for now, or try your message again in a minute.")
            return dict(api._SKIP_RESULT)
        generation = api._reserve_telegram_operation(user_id, action)
        if generation is None:
            return dict(api._SKIP_RESULT)

        def work():
            try:
                intent = intent_router.classify(text, context=context)
            except intent_router.RouterUnavailable:
                intent = None
            # Cancellation and new commands win over a late model response.
            with api._TELEGRAM_OPERATION_LOCK:
                if not api._finish_telegram_operation(user_id, generation):
                    return
                if intent is None:
                    recover_browsing()
                    ru = bool(re.search("[А-Яа-яЁё]", text))
                    send("Не получилось определить задачу. Выбери действие в меню — оно работает без AI-чата."
                         if ru else "I couldn't identify the task. Choose an action from the menu; no AI chat setup is needed.")
                else:
                    live_pending = pending if pending and pending[0] > time.monotonic() else None
                    requested_action = intent.suggested_action if intent.action == "clarify" else intent.action
                    # Independent wallet/unsupported tasks must not be swallowed
                    # by a low-confidence relation-to-context answer.
                    if (intent.reply != "cancel" and requested_action in {"balance", "order_status", "limits", "unsupported"}
                            and not (live_pending and live_pending[2] == "network" and requested_action == "balance")):
                        intent = replace(intent, reply="new_task")
                    elif (live_pending and intent.action not in {"clarify", "chat"}
                          and intent.reply not in {"cancel", "decline"}):
                        related = (intent.action == live_pending[1].action
                                   or live_pending[2] == "alternative" and intent.action == "gift_card")
                        intent = replace(intent, reply=None if related else "new_task")
                    if intent.reply in {"cancel", "decline"}:
                        self.clear(user_id)
                        api._BITREFILL_SESSIONS.pop(user_id, None)
                        send("Хорошо, поиск отменён. Напиши другую задачу." if intent.language == "ru" else
                             "Okay, cancelled. Tell me another task.")
                        return
                    if live_pending and intent.reply in {"accept", "continue", "unclear"}:
                        previous = live_pending[1]
                        stage = live_pending[2]
                        updated = self.merge_slots(previous, intent)
                        if ((intent.reply == "accept" and stage in {"alternative", "confirm-intent"})
                                or (intent.reply == "continue" and stage == "alternative" and requested_action == "gift_card")):
                            intent = replace(updated, action="gift_card" if stage == "alternative" else previous.action)
                        elif intent.reply == "continue" and stage == "country" and updated.country in intent_router.COUNTRIES:
                            intent = updated
                        elif intent.reply == "continue" and stage == "network" and updated.network in {"base", "solana"}:
                            intent = updated
                        else:
                            retained = updated if intent.reply == "continue" else previous
                            self.repeat_pending((live_pending[0], retained, stage), identity, source, gateway, api, send)
                            return
                    elif browsing_session and intent.reply in {"continue", "accept", "unclear"}:
                        stage = browsing_session.get("stage")
                        if intent.reply == "continue" and stage == "awaiting-search":
                            api._handle_bitrefill_search_input(identity=identity, query=text, source=source,
                                                              gateway=gateway, search_all_countries=False)
                            return
                        if intent.reply == "continue" and stage == "awaiting-country" and intent.country in intent_router.COUNTRIES:
                            api._handle_bitrefill_country_input(identity=identity, text=intent.country, source=source, gateway=gateway)
                            return
                        if (intent.reply == "continue" and stage != "awaiting-country"
                                and (intent.country in intent_router.COUNTRIES or intent.category_explicit)):
                            previous = intent_router.Intent("esim" if browsing_session.get("query") == "esim" else "gift_card",
                                country=browsing_session.get("country"), category=browsing_session.get("category", "all"))
                            intent = self.merge_slots(previous, intent)
                        else:
                            recover_browsing()
                            send("Уточни запрос или выбери вариант из текущего списка." if intent.language == "ru" else
                                 "Please clarify your request or choose an option from the current list.")
                            return
                    self.clear(user_id)
                    if browsing_session is not None:
                        api._BITREFILL_SESSIONS.pop(user_id, None)
                    intent = self.resolve_followup(intent, pending)
                    self.advance(intent, identity, source, gateway, api, send, original_text=text)

        try:
            api._run_in_background(work)
        except Exception:
            api._finish_telegram_operation(user_id, generation)
            recover_browsing()
            send("Please choose an action from the menu.")
        return dict(api._SKIP_RESULT)

    def advance(self, intent, identity, source, gateway, api, send, original_text=None):
        user_id = str(identity.user_id)
        ru = intent.language == "ru"
        if intent.action == "clarify" and intent.suggested_action:
            descriptions = {
                "esim": ("Нужен мобильный интернет через eSIM для поездки?", "Are you looking for travel internet through an eSIM?"),
                "topup": ("Нужно пополнить существующий мобильный номер?", "Do you want to top up an existing mobile number?"),
                "gift_card": ("Проверить доступные подарочные карты?", "Would you like me to check available gift cards?"),
                "balance": ("Показать баланс кошелька?", "Would you like to see your wallet balance?"),
                "order_status": ("Показать последнюю покупку?", "Would you like to see your latest purchase?"),
                "limits": ("Показать текущие лимиты расходов?", "Would you like to see your current spending limits?"),
                "food": ("Ты хочешь заказать еду или продукты?", "Are you looking to order food or groceries?"),
                "goods": ("Ты хочешь купить физический товар?", "Are you looking to buy a physical product?"),
                "travel": ("Нужно бронирование поездки или проживания?", "Are you looking to book travel or accommodation?"),
            }
            if intent.suggested_action in descriptions:
                self.remember(user_id, replace(intent, action=intent.suggested_action, suggested_action=None), "confirm-intent")
                send(descriptions[intent.suggested_action][0 if ru else 1],
                     (("Да", "Нет"), ("Back",)) if ru else (("Yes", "No"), ("Back",)))
                return
        if intent.action in {"balance", "order_status", "limits"}:
            if intent.action == "balance" and intent.network == "other":
                self.remember(user_id, intent, "network")
                send("В какой сети показать баланс? Выбери Base или Solana; также работают /balance base и /balance solana." if ru else
                     "Which network balance? Choose Base or Solana; /balance base and /balance solana also work.",
                     (("Base", "Solana"), ("Back",)))
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
