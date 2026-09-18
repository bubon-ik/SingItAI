"""Telegram controls and purchase navigation without real network or payments."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_plugin import load_plugin, FakeClient, FakeEvent, FakeGateway, FakeAdapter

try:
    from telegram import InlineKeyboardMarkup
    from telegram.ext import Application, ApplicationHandlerStop
except ImportError:
    Application = None


class ButtonCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.now = 1
        self.sessions = self.plugin.ButtonSessions(clock=lambda: self.now, ttl=10, limit=2)
        self.source = SimpleNamespace(user_id="alice", chat_id="one")
        self.markup = self.plugin.actions([(("Alza CZ", "1"), ("Alza CZ", "2"))])

    def view(self):
        markup = self.sessions.render("alice", "one", self.markup, self.source)
        self.sessions.bind("alice", "one", markup, 42)
        return markup["inline_keyboard"][0][1]["callback_data"]

    def test_buttons_are_bound_to_user_chat_and_message_and_used_once(self):
        data = self.view()
        self.assertLessEqual(len(data.encode()), 64)
        for user, chat, message in [("bob", "one", 42), ("alice", "two", 42), ("alice", "one", 43)]:
            self.assertIsNone(self.sessions.claim(user, chat, message, data))
        self.assertEqual(self.sessions.claim("alice", "one", 42, data)[0], "2")
        self.assertIsNone(self.sessions.claim("alice", "one", 42, data))

    def test_expired_replaced_and_forged_buttons_cannot_dispatch(self):
        old = self.view()
        current = self.view()
        self.assertIsNone(self.sessions.claim("alice", "one", 42, old))
        self.assertIsNone(self.sessions.claim("alice", "one", 42, current.rsplit(":", 1)[0] + ":100"))
        self.now = 12
        self.assertIsNone(self.sessions.claim("alice", "one", 42, current))

    def test_restart_has_no_old_capabilities(self):
        old = self.view()
        self.assertIsNone(self.plugin.ButtonSessions().claim("alice", "one", 42, old))

    def test_names_and_verified_prices_are_visible_without_raw_product_ids(self):
        markup = self.plugin._product_buttons([{"name": "Alza CZ", "country": "CZ", "productId": "internal-slug"}])
        self.assertIn("Alza CZ", str(markup))
        self.assertNotIn("internal-slug", str(markup))
        labels = self.plugin._package_buttons({"currency": "CZK"}, [
            {"value": "200", "priceUsd": "99999", "displayPriceUsd": "9.44"},
            {"value": "500", "priceUsd": "99999"},
        ])
        self.assertIn("200 CZK · ≈ $9.44", str(labels))
        self.assertNotIn("99999", str(labels))


class PurchaseUiTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.client = FakeClient()
        self.plugin._client_factory = lambda: self.client
        self.gateway = FakeGateway(adapter_key="telegram")
        policy = patch.dict(os.environ, {"SIGN402_TELEGRAM_ALLOWED_USERS": "*"})
        policy.start()
        self.addCleanup(policy.stop)

    def press(self, text):
        return self.plugin.handle_pre_gateway_dispatch(event=FakeEvent(text, "1045618308"), gateway=self.gateway)

    def test_named_text_fallback_selects_the_original_product_and_package(self):
        for text in ["🛍 Shop", "Search Products", "amazon", "1. Amazon Czech Republic · CZ", "1. 10", "1"]:
            self.press(text)
        self.assertEqual(self.client.bitrefill_calls, [])
        self.assertEqual(self.plugin._BITREFILL_SESSIONS["1045618308"]["stage"], "review-purchase")
        self.press("Request approval")
        self.assertEqual(self.client.bitrefill_calls[0][2:4], ("amazon-cz", "amazon-cz-10"))
        self.press("Request approval")
        self.assertEqual(len(self.client.bitrefill_calls), 1)

    def test_back_from_review_never_starts_purchase(self):
        for text in ["🛍 Shop", "Search Products", "amazon", "1", "1", "1", "Back", "Request approval"]:
            self.press(text)
        self.assertEqual(self.client.bitrefill_calls, [])

    def test_settings_does_not_provision_wallets(self):
        self.press("⚙️ Settings")
        self.assertEqual(self.client.create_wallet_calls, [])
        self.assertIn("Settings", self.gateway.adapters["telegram"].sent[-1][1])

    def test_history_opens_receipt_without_revealing(self):
        receipt = {"id": "a" * 24, "name": "Alza <CZ>", "denomination": "200 CZK", "paid": "9.44 USDC", "network": "Base", "status": "Completed", "isBitrefill": True, "canReveal": True}
        self.client.purchases = Mock(return_value={"purchase": receipt})
        text, markup = self.plugin._purchase_screen(self.client, self.plugin.TelegramIdentity("1045618308"), "purchase", "a" * 24)
        self.assertIn("Alza &lt;CZ&gt;", text)
        self.assertIn("9.44 USDC", text)
        self.assertIn("Show code", str(markup))
        self.assertFalse(self.client.purchases.call_args.kwargs["reveal"])
        self.assertNotIn("wallet", self.client.purchases.call_args.kwargs)

    def test_switching_from_withdraw_to_shop_discards_the_old_wizard(self):
        self.plugin._WITHDRAW_SESSIONS["1045618308"] = {"step": "awaiting-address"}
        self.press("🛍 Shop")
        self.assertNotIn("1045618308", self.plugin._WITHDRAW_SESSIONS)
        self.press("Search Products")
        self.press("amazon")
        self.assertEqual(len(self.client.bitrefill_search_calls), 1)

    def test_history_is_not_shown_in_a_group(self):
        event = FakeEvent("/purchases", "1045618308")
        event.source.chat_type = "group"
        self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway)
        self.assertEqual(self.client.create_wallet_calls, [])
        self.assertIn("private chat", self.gateway.adapters["telegram"].sent[-1][1])


@unittest.skipIf(Application is None, "Install python-telegram-bot to run adapter integration tests")
class TelegramAdapterUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = load_plugin()
        self.client = FakeClient()
        self.plugin._client_factory = lambda: self.client
        self.app = Application.builder().token("123:offline-test").build()
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)), edit_message_text=AsyncMock())
        self.adapter = FakeAdapter(self.bot)
        self.adapter._app = self.app
        self.gateway = FakeGateway(adapter_key="telegram", adapter=self.adapter)
        self.source = SimpleNamespace(platform="telegram", user_id="123", chat_id="123", chat_type="dm")
        self.policy = patch.dict(os.environ, {"SIGN402_TELEGRAM_ALLOWED_USERS": "123"})
        self.policy.start()
        self.addCleanup(self.policy.stop)
        self.plugin._install_telegram_buttons(self.gateway, self.source)

    async def drain(self):
        while self.plugin._TELEGRAM_SEND_TAILS:
            await asyncio.gather(*list(self.plugin._TELEGRAM_SEND_TAILS.values()))
            await asyncio.sleep(0)

    async def test_handler_installs_once_on_real_ptb_application(self):
        self.plugin._install_telegram_buttons(self.gateway, self.source)
        self.assertEqual(len(self.app.handlers[-10]), 1)
        self.assertTrue(self.app.handlers[-10][0].pattern.match("singit:abc:1"))
        self.assertFalse(self.app.handlers[-10][0].pattern.match("ea:approve"))

    async def test_progress_edits_same_card_in_order_and_retains_inline_markup(self):
        card_source = self.plugin._operation_source(self.source)
        self.plugin._send_fixed_reply(self.gateway, card_source, "Loading…")
        self.plugin._send_fixed_reply(self.gateway, card_source, "Choose a network", reply_markup=self.plugin._wallet_network_buttons())
        await self.drain()
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_text.assert_awaited_once()
        kwargs = self.bot.edit_message_text.call_args.kwargs
        self.assertEqual(kwargs["message_id"], 42)
        self.assertIsInstance(kwargs["reply_markup"], InlineKeyboardMarkup)
        data = kwargs["reply_markup"].inline_keyboard[0][1].callback_data
        self.assertEqual(self.plugin._BUTTON_SESSIONS.claim("123", "123", 42, data)[0], "/wallet solana")

    async def test_deleted_message_falls_back_to_a_fresh_message(self):
        self.bot.edit_message_text.side_effect = RuntimeError("message to edit not found")
        source = self.plugin._operation_source(self.source)
        source._singit_card.message_id = 12
        self.plugin._send_fixed_reply(self.gateway, source, "Receipt", reply_markup=self.plugin.actions([(("Purchases", "/purchases"),)]))
        await self.drain()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(source._singit_card.message_id, 42)

    async def test_callback_rechecks_policy_and_dispatches_only_its_saved_action(self):
        markup = self.plugin._prepare_telegram_markup(self.gateway, self.source, self.plugin._wallet_network_buttons())
        self.plugin._BUTTON_SESSIONS.bind("123", "123", markup, 42)
        data = markup["inline_keyboard"][0][1]["callback_data"]
        query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=123, username="alice"),
                                message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=123, type="private")), answer=AsyncMock())
        handler = self.app.handlers[-10][0].callback
        with patch.object(self.plugin, "handle_pre_gateway_dispatch") as dispatch:
            with patch.dict(os.environ, {"SIGN402_TELEGRAM_ALLOWED_USERS": "999"}):
                with self.assertRaises(ApplicationHandlerStop):
                    await handler(SimpleNamespace(callback_query=query), None)
            dispatch.assert_not_called()
            with self.assertRaises(ApplicationHandlerStop):
                await handler(SimpleNamespace(callback_query=query), None)
            dispatch.assert_called_once()
            event = dispatch.call_args.kwargs["event"]
            self.assertEqual(event.text, "/wallet solana")
            self.assertEqual(event.source.user_id, "123")
            with self.assertRaises(ApplicationHandlerStop):
                await handler(SimpleNamespace(callback_query=query), None)
            dispatch.assert_called_once()

    async def test_navigation_after_reveal_does_not_overwrite_the_code_message(self):
        source = self.plugin._operation_source(self.source)
        source._singit_protect_card = True
        source._singit_card.message_id = 42
        markup = self.plugin._prepare_telegram_markup(self.gateway, source, self.plugin.actions([(("Purchases", "/purchases"),)]))
        self.plugin._BUTTON_SESSIONS.bind("123", "123", markup, 42)
        query = SimpleNamespace(data=markup["inline_keyboard"][0][0]["callback_data"],
                                from_user=SimpleNamespace(id=123, username="alice"),
                                message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=123, type="private")), answer=AsyncMock())
        with patch.object(self.plugin, "handle_pre_gateway_dispatch") as dispatch:
            with self.assertRaises(ApplicationHandlerStop):
                await self.app.handlers[-10][0].callback(SimpleNamespace(callback_query=query), None)
        self.assertIsNone(dispatch.call_args.kwargs["event"].source._singit_card.message_id)

    async def test_leaving_screen_during_reveal_does_not_discard_one_time_code(self):
        callbacks = []
        self.plugin._background_runner = callbacks.append
        self.plugin._start_telegram_background_operation(
            identity=self.plugin.TelegramIdentity("123"), action="command:reveal", started_text="Retrieving…",
            source=self.source, gateway=self.gateway, work=lambda _: ("Code: fixture-only", None))
        self.plugin._invalidate_telegram_operation("123")
        callbacks.pop()()
        await self.drain()
        self.assertEqual(self.bot.send_message.call_args.kwargs["text"], "Code: fixture-only")
        self.bot.edit_message_text.assert_not_awaited()
