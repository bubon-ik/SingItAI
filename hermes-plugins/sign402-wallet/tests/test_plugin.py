import asyncio
import importlib.util
import io
import json
import logging
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "sign402_wallet_plugin_test"


def load_plugin():
    for name in tuple(sys.modules):
        if name == PACKAGE_NAME or name.startswith(f"{PACKAGE_NAME}."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load plugin package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class FakePlatform:
    value: str = "telegram"


@dataclass
class FakeSource:
    platform: FakePlatform
    user_id: str
    user_name: str | None = None
    chat_id: str = "chat-1"


class FakeEvent:
    def __init__(
        self,
        command: str,
        user_id: str,
        username: str | None = None,
        platform: str = "telegram",
        chat_id: str = "chat-1",
    ):
        self.text = command
        self.source = FakeSource(FakePlatform(platform), user_id, username, chat_id)

    def get_command(self):
        if not self.text.startswith("/"):
            return None
        return self.text[1:].split(maxsplit=1)[0]


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.commands = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description=""):
        self.commands[name] = {
            "handler": handler,
            "description": description,
        }


class FakeClient:
    def __init__(self, result="gateway telegram text", error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.imessage_results = {}
        self.imessage_calls = []
        self.execute_tokens = []
        self.paid_tool_calls = []
        self.paid_tool_tokens = []
        self.paid_tool_result = "Crypto News unlocked."
        self.bitrefill_calls = []
        self.bitrefill_result = "Bitrefill delivered."
        self.bitrefill_search_calls = []
        self.bitrefill_product_calls = []
        self.bitrefill_search_result = {
            "ok": True,
            "products": [
                {
                    "productId": "amazon-cz",
                    "name": "Amazon Czech Republic",
                    "country": "CZ",
                    "category": "gift_card",
                }
            ],
        }
        self.bitrefill_product_result = {
            "ok": True,
            "productId": "amazon-cz",
            "name": "Amazon Czech Republic",
            "country": "CZ",
            "requiredRecipientFields": [],
            "packages": [
                {"packageId": "amazon-cz-10", "value": "10", "priceUsd": "10.00"},
                {"packageId": "amazon-cz-25", "value": "25", "priceUsd": "25.00"},
            ],
        }
        self.access_token = "user-access-token"
        self.create_wallet_calls = []
        self.limits_calls = []
        self.limits_result = "Current spending limits."
        self.llm_calls = []
        self.llm_results = {}

    def create_wallet(self, identity):
        self.create_wallet_calls.append(identity.user_id)
        if self.error:
            raise self.error
        return {"telegramText": self.result, "accessToken": self.access_token}

    def execute(self, operation, identity, *, user_access_token=None):
        self.calls.append((operation, identity))
        self.execute_tokens.append((operation, user_access_token))
        if self.error:
            raise self.error
        return self.result

    def execute_imessage(self, operation, payload):
        self.imessage_calls.append((operation, payload))
        if self.error:
            raise self.error
        return self.imessage_results.get(operation, {"ok": True})

    def execute_paid_tool(self, tool, identity, *, user_access_token=None):
        self.paid_tool_calls.append((tool, identity.user_id, identity.username))
        self.paid_tool_tokens.append(user_access_token)
        if self.error:
            raise self.error
        return self.paid_tool_result

    def execute_bitrefill_purchase(
        self,
        identity,
        *,
        product_id,
        package_id,
        country="US",
        recipient=None,
        user_access_token=None,
    ):
        self.bitrefill_calls.append(
            (
                identity.user_id,
                identity.username,
                product_id,
                package_id,
                country,
                recipient or {},
                user_access_token,
            )
        )
        if self.error:
            raise self.error
        return self.bitrefill_result

    def search_bitrefill_products(self, *, query, country, include_test_products=False):
        self.bitrefill_search_calls.append((query, country, include_test_products))
        if self.error:
            raise self.error
        return self.bitrefill_search_result

    def get_bitrefill_product(self, *, product_id, country):
        self.bitrefill_product_calls.append((product_id, country))
        if self.error:
            raise self.error
        return self.bitrefill_product_result

    def execute_spending_limits(self, identity, *, max_per_tx_usdc=None, daily_cap_usdc=None):
        self.limits_calls.append(
            (identity.user_id, identity.username, max_per_tx_usdc, daily_cap_usdc)
        )
        if self.error:
            raise self.error
        return self.limits_result

    def execute_llm(
        self,
        operation,
        identity,
        *,
        payload=None,
        user_access_token,
    ):
        self.llm_calls.append(
            {
                "operation": operation,
                "user_id": identity.user_id,
                "username": identity.username,
                "payload": dict(payload or {}),
                "user_access_token": user_access_token,
            }
        )
        if self.error:
            raise self.error
        return self.llm_results.get(
            operation,
            {"ok": True, "telegramText": "Bankr LLM request complete."},
        )


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))


class FakeTelegramResponse:
    def __init__(self):
        self.closed = False

    def read(self):
        return b'{"ok":true}'

    def close(self):
        self.closed = True


class FakePairingStore:
    def __init__(self):
        self.generated = []
        self.approved = []

    def generate_code(self, platform, user_id, user_name=""):
        self.generated.append((platform, user_id, user_name))
        return "HERMES1"

    def approve_code(self, platform, code):
        self.approved.append((platform, code))
        return {"user_id": "+15551234567", "user_name": "Photon User"}


class FakeGateway:
    def __init__(self, adapter_key="photon"):
        self.adapters = {adapter_key: FakeAdapter()}
        self.pairing_store = FakePairingStore()


class PaidToolIntentTests(unittest.TestCase):
    def _intent(self, text):
        plugin = load_plugin()
        event = FakeEvent(text, "1045618308")
        return plugin._telegram_paid_tool_intent(event, event.source)

    def test_affirmative_requests_trigger_purchase(self):
        self.assertEqual(self._intent("buy crypto news"), "news")
        self.assertEqual(self._intent("Can u buy a cryptonews with Firefly?"), "news")

    def test_negations_and_questions_do_not_trigger_purchase(self):
        self.assertIsNone(self._intent("why did you buy crypto news?"))
        self.assertIsNone(self._intent("don't buy crypto news for me"))
        self.assertIsNone(self._intent("i didn't buy crypto news"))
        self.assertIsNone(self._intent("please cancel the crypto news buy"))
        self.assertIsNone(self._intent("do not buy crypto news"))


class PluginRegistrationTests(unittest.TestCase):
    def test_registers_dispatch_hook_and_wallet_commands(self):
        plugin = load_plugin()
        context = FakeContext()

        plugin.register(context)

        self.assertEqual(set(context.hooks), {"pre_gateway_dispatch"})
        self.assertEqual(
            set(context.commands),
            {
                "start",
                "help",
                "wallet",
                "balance",
                "last-purchase",
                "limits",
                "set-limits",
                "connect-imessage",
                "bitrefill",
                "llm-buy",
                "llm-terms",
                "llm-code",
                "llm-credits",
            },
        )
        for command in context.commands.values():
            self.assertTrue(command["description"])

    def test_register_configures_public_telegram_command_menu(self):
        plugin = load_plugin()
        context = FakeContext()
        requests = []
        callbacks = []

        def fake_opener(request, timeout):
            requests.append((request, timeout))
            return FakeTelegramResponse()

        plugin._telegram_api_opener = fake_opener
        plugin._background_runner = callbacks.append
        plugin._sleep = lambda _delay: None
        plugin._TELEGRAM_COMMAND_MENU_REFRESH_DELAYS_SECONDS = (0,)

        with patch.dict(plugin.os.environ, {"TELEGRAM_BOT_TOKEN": "telegram-token"}):
            plugin.register(context)
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()

        self.assertEqual(len(requests), 2)
        request, timeout = requests[0]
        self.assertEqual(timeout, plugin._TELEGRAM_COMMAND_MENU_TIMEOUT_SECONDS)
        self.assertEqual(
            request.full_url,
            "https://api.telegram.org/bottelegram-token/setMyCommands",
        )
        payload = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(
            json.loads(payload["commands"][0]),
            list(plugin._TELEGRAM_PUBLIC_COMMAND_MENU),
        )
        private_payload = parse_qs(requests[1][0].data.decode("utf-8"))
        self.assertEqual(
            json.loads(private_payload["commands"][0]),
            list(plugin._TELEGRAM_PUBLIC_COMMAND_MENU),
        )
        self.assertEqual(
            json.loads(private_payload["scope"][0]),
            {"type": "all_private_chats"},
        )

    def test_public_telegram_command_menu_is_pilot_facing(self):
        plugin = load_plugin()

        commands = [item["command"] for item in plugin._TELEGRAM_PUBLIC_COMMAND_MENU]

        self.assertEqual(
            commands,
            [
                "start",
                "help",
                "wallet",
                "balance",
                "connect_imessage",
                "limits",
                "bitrefill",
                "last_purchase",
                "llm_buy",
                "llm_credits",
            ],
        )
        self.assertNotIn("create_wallet", commands)
        self.assertNotIn("set_limits", commands)
        self.assertNotIn("test_approval", commands)
        self.assertNotIn("llm_terms", commands)
        self.assertNotIn("llm_code", commands)

    def test_command_uses_bound_identity_and_ignores_raw_arguments(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/wallet telegramUserId=999",
                user_id="1045618308",
                username="AlpskyKnedlik",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", "gateway telegram text")],
        )
        self.assertEqual(len(client.calls), 1)
        operation, identity = client.calls[0]
        self.assertEqual(operation, "create-wallet")
        self.assertEqual(identity.user_id, "1045618308")
        self.assertEqual(identity.username, "AlpskyKnedlik")

    def test_public_command_handler_without_pre_dispatch_rejects(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)

        result = asyncio.run(context.commands["wallet"]["handler"](""))

        self.assertIn("Telegram", result)
        self.assertEqual(client.calls, [])

    def test_balance_command_sends_per_user_access_token(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/balance",
                user_id="1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertIn(("balance", "user-access-token"), client.execute_tokens)

    def test_last_purchase_command_uses_trusted_telegram_identity(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient(result="Code: SECRET-CODE")
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/last_purchase",
                user_id="1045618308",
                username="AlpskyKnedlik",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", "Code: SECRET-CODE")],
        )
        self.assertIn(("last-purchase", "user-access-token"), client.execute_tokens)

    def test_gateway_error_returns_only_safe_user_message(self):
        plugin = load_plugin()
        context = FakeContext()
        safe_message = "Wallet service is temporarily unavailable."
        client = FakeClient(error=plugin.GatewayClientError(safe_message))
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/balance",
                user_id="1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", safe_message)],
        )

    def test_registers_imessage_commands(self):
        plugin = load_plugin()
        context = FakeContext()

        plugin.register(context)

        self.assertIn("connect-imessage", context.commands)
        self.assertNotIn("test-approval", context.commands)
        self.assertNotIn("buy-crypto-news", context.commands)

    def test_connect_imessage_uses_trusted_telegram_identity(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["connect-imessage"] = {
            "telegramText": "Send ABCDEFGH to iMessage"
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/connect_imessage telegramUserId=999",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", "Send ABCDEFGH to iMessage")],
        )
        self.assertEqual(
            client.imessage_calls,
            [
                (
                    "connect-imessage",
                    {"telegramUserId": "1045618308"},
                )
            ],
        )

    def test_start_creates_wallet_and_returns_onboarding_text(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient(result="Your Base agent wallet:\n0xabc")
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/start telegramUserId=999",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        text = gateway.adapters["telegram"].sent[0][1]

        self.assertIn("Welcome to Sign402.", text)
        self.assertIn("Your Base agent wallet:\n0xabc", text)
        self.assertIn("/balance", text)
        self.assertIn("/connect_imessage", text)
        self.assertIn("/limits", text)
        self.assertEqual(len(client.calls), 1)
        operation, identity = client.calls[0]
        self.assertEqual(operation, "create-wallet")
        self.assertEqual(identity.user_id, "1045618308")

    def test_start_is_answered_in_pre_dispatch(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient(result="Your Base agent wallet:\n0xabc")
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/start",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(client.calls[0][0], "create-wallet")
        self.assertIn("Welcome to Sign402.", gateway.adapters["telegram"].sent[0][1])

    def test_help_is_answered_with_pilot_commands(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/help",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        text = gateway.adapters["telegram"].sent[0][1]
        self.assertIn("Sign402 commands", text)
        self.assertIn("/wallet", text)
        self.assertIn("/connect_imessage", text)
        self.assertIn("/bitrefill", text)
        self.assertIn("/llm_buy", text)
        self.assertNotIn("/llm_terms", text)
        self.assertNotIn("/llm_code", text)

    def test_reply_keyboard_labels_are_treated_as_commands(self):
        plugin = load_plugin()
        self.assertEqual(
            plugin._telegram_public_command(
                FakeEvent("Wallet", "1045618308"),
                FakeEvent("Wallet", "1045618308").source,
            ),
            "wallet",
        )
        self.assertEqual(
            plugin._telegram_public_command(
                FakeEvent("Buy LLM Credits", "1045618308"),
                FakeEvent("Buy LLM Credits", "1045618308").source,
            ),
            "llm-buy",
        )
        self.assertEqual(
            plugin._telegram_public_command(
                FakeEvent("Connect iMessage", "1045618308"),
                FakeEvent("Connect iMessage", "1045618308").source,
            ),
            "connect-imessage",
        )

    def test_help_direct_reply_includes_reply_keyboard(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")
        requests = []

        def fake_opener(request, timeout):
            requests.append((request, timeout))
            return FakeTelegramResponse()

        plugin._telegram_api_opener = fake_opener

        with patch.dict(plugin.os.environ, {"TELEGRAM_BOT_TOKEN": "telegram-token"}):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "/help",
                    "1045618308",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(gateway.adapters["telegram"].sent, [])
        payload = parse_qs(requests[0][0].data.decode("utf-8"))
        reply_markup = json.loads(payload["reply_markup"][0])
        self.assertTrue(reply_markup["resize_keyboard"])
        self.assertEqual(
            reply_markup["keyboard"],
            [
                [{"text": "Wallet"}, {"text": "Balance"}],
                [{"text": "Connect iMessage"}, {"text": "Limits"}],
                [{"text": "Buy Bitrefill"}, {"text": "Buy LLM Credits"}],
                [{"text": "Last Purchase"}, {"text": "Help"}],
            ],
        )

    def test_bitrefill_command_quotes_and_buys_with_trusted_identity(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin._background_runner = lambda callback: callback()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/bitrefill test-gift-card-link 1 US",
                "1045618308",
                username="AlpskyKnedlik",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [
                ("telegram-chat", "Bitrefill purchase started. Approve it in iMessage; I'll post the result here."),
                ("telegram-chat", "Bitrefill delivered."),
            ],
        )
        self.assertEqual(client.create_wallet_calls, ["1045618308"])
        self.assertEqual(
            client.bitrefill_calls,
            [
                (
                    "1045618308",
                    "AlpskyKnedlik",
                    "test-gift-card-link",
                    "1",
                    "US",
                    {},
                    "user-access-token",
                )
            ],
        )

    def test_bitrefill_button_opens_country_aware_search_flow(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin._background_runner = lambda callback: callback()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        def dispatch(text):
            return context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    text,
                    "1045618308",
                    username="AlpskyKnedlik",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(dispatch("Buy Bitrefill"), plugin._SKIP_RESULT)
        self.assertIn("Country: CZ", gateway.adapters["telegram"].sent[-1][1])
        self.assertIn("Search Products", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("Change Country"), plugin._SKIP_RESULT)
        self.assertIn("Send a two-letter country code", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("DE"), plugin._SKIP_RESULT)
        self.assertIn("Country: DE", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("Search Products"), plugin._SKIP_RESULT)
        self.assertIn("What do you want to buy in DE?", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("amazon"), plugin._SKIP_RESULT)
        self.assertEqual(client.bitrefill_search_calls, [("amazon", "DE", False)])
        self.assertIn("1. Amazon Czech Republic", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertEqual(client.bitrefill_product_calls, [("amazon-cz", "DE")])
        self.assertIn("Choose amount for Amazon Czech Republic", gateway.adapters["telegram"].sent[-1][1])
        self.assertIn("1. 10", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertEqual(
            gateway.adapters["telegram"].sent[-2:],
            [
                ("telegram-chat", "Bitrefill purchase started. Approve it in iMessage; I'll post the result here."),
                ("telegram-chat", "Bitrefill delivered."),
            ],
        )
        self.assertEqual(
            client.bitrefill_calls,
            [
                (
                    "1045618308",
                    "AlpskyKnedlik",
                    "amazon-cz",
                    "amazon-cz-10",
                    "DE",
                    {},
                    "user-access-token",
                )
            ],
        )

    def test_bitrefill_wizard_collects_required_recipient(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.bitrefill_product_result = {
            "ok": True,
            "productId": "mobile-topup",
            "name": "Mobile Topup",
            "country": "CZ",
            "requiredRecipientFields": ["phone"],
            "packages": [
                {"packageId": "mobile-5", "value": "5", "priceUsd": "5.00"},
            ],
        }
        client.bitrefill_search_result = {
            "ok": True,
            "products": [
                {"productId": "mobile-topup", "name": "Mobile Topup", "country": "CZ"}
            ],
        }
        plugin._client_factory = lambda: client
        plugin._background_runner = lambda callback: callback()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        def dispatch(text):
            return context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    text,
                    "1045618308",
                    username="AlpskyKnedlik",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        dispatch("Buy Bitrefill")
        dispatch("Search Products")
        dispatch("mobile")
        dispatch("1")
        dispatch("1")

        self.assertIn("Send phone", gateway.adapters["telegram"].sent[-1][1])

        dispatch("+420777111222")

        self.assertEqual(
            client.bitrefill_calls[-1],
            (
                "1045618308",
                "AlpskyKnedlik",
                "mobile-topup",
                "mobile-5",
                "CZ",
                {"phone": "+420777111222"},
                "user-access-token",
            ),
        )

    def test_bitrefill_amount_list_uses_product_currency_for_non_usd_products(self):
        plugin = load_plugin()

        text = plugin._format_bitrefill_packages(
            {
                "name": "Wolt Czech Republic",
                "currency": "CZK",
            },
            [
                {"packageId": "wolt-cz<&>1200", "value": "1200", "priceUsd": "89796.00"},
                {"packageId": "wolt-cz<&>500", "value": "500", "priceUsd": "37415.00"},
            ],
        )

        self.assertIn("1. 1200 CZK", text)
        self.assertIn("2. 500 CZK", text)
        self.assertNotIn("$89796.00", text)
        self.assertNotIn("$37415.00", text)

    def test_connect_imessage_is_answered_in_pre_dispatch(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["connect-imessage"] = {
            "telegramText": "Send ABCDEFGH to iMessage"
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/connect_imessage",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            client.imessage_calls,
            [("connect-imessage", {"telegramUserId": "1045618308"})],
        )
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", "Send ABCDEFGH to iMessage")],
        )

    def test_limits_shows_current_limits_from_trusted_identity(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.limits_result = "Current spending limits."
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/limits telegramUserId=999",
                "1045618308",
                username="AlpskyKnedlik",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            client.limits_calls,
            [("1045618308", "AlpskyKnedlik", None, None)],
        )
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", "Current spending limits.")],
        )

    def test_set_limits_updates_limits_from_trusted_identity(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.limits_result = "Spending limits updated."
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/set_limits 0.005 0.05 telegramUserId=999",
                "1045618308",
                username="AlpskyKnedlik",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            client.limits_calls,
            [("1045618308", "AlpskyKnedlik", "0.005", "0.05")],
        )
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", "Spending limits updated.")],
        )

    def test_limits_with_two_numbers_updates_limits(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.limits_result = "Spending limits updated."
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/limits 0.004 0.04",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(client.limits_calls, [("1045618308", None, "0.004", "0.04")])

    def test_set_limits_requires_two_numbers(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/set_limits 0.005",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(client.limits_calls, [])
        self.assertIn("Usage", gateway.adapters["telegram"].sent[0][1])

    def test_parse_llm_buy_args(self):
        plugin = load_plugin()

        self.assertEqual(
            plugin._parse_llm_buy_args("10 user@example.com"),
            ("10", "user@example.com", ""),
        )
        self.assertIsNone(plugin._parse_llm_buy_args("10"))
        self.assertIsNone(plugin._parse_llm_buy_args("0.5 user@example.com"))
        self.assertIsNone(plugin._parse_llm_buy_args("10 not-an-email"))
        self.assertIsNone(plugin._parse_llm_buy_args("10 user@example.com bad-token!"))

    def test_llm_buy_accepts_optional_token_symbol(self):
        plugin = load_plugin()

        payload = plugin._llm_operation_payload(
            "start", "1 user@example.com USDC"
        )

        self.assertEqual(
            payload,
            {
                "amountUsd": "1",
                "email": "user@example.com",
                "paymentToken": "USDC",
            },
        )

    def test_llm_buy_accepts_token_address(self):
        plugin = load_plugin()
        token = "0x" + "a" * 40

        payload = plugin._llm_operation_payload(
            "start", f"1 user@example.com {token}"
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["paymentToken"], token)

    def test_llm_buy_without_token_omits_payment_token(self):
        plugin = load_plugin()

        payload = plugin._llm_operation_payload("start", "1 user@example.com")

        self.assertEqual(payload, {"amountUsd": "1", "email": "user@example.com"})

    def test_llm_buy_rejects_four_args(self):
        plugin = load_plugin()

        self.assertIsNone(
            plugin._llm_operation_payload("start", "1 user@example.com USDC extra")
        )

    def test_llm_buy_and_terms_use_trusted_identity(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.llm_results["start"] = {
            "ok": True,
            "telegramText": "Review Bankr terms.",
        }
        client.llm_results["accept-terms"] = {
            "ok": True,
            "telegramText": "Verification code sent.",
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        buy_result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/llm_buy 10 user@example.com",
                "1045618308",
                username="AlpskyKnedlik",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )
        terms_result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/llm_terms accept",
                "1045618308",
                username="AlpskyKnedlik",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(buy_result, plugin._SKIP_RESULT)
        self.assertEqual(terms_result, plugin._SKIP_RESULT)
        self.assertEqual(
            client.llm_calls,
            [
                {
                    "operation": "start",
                    "user_id": "1045618308",
                    "username": "AlpskyKnedlik",
                    "payload": {
                        "amountUsd": "10",
                        "email": "user@example.com",
                    },
                    "user_access_token": "user-access-token",
                },
                {
                    "operation": "accept-terms",
                    "user_id": "1045618308",
                    "username": "AlpskyKnedlik",
                    "payload": {},
                    "user_access_token": "user-access-token",
                },
            ],
        )

    def test_llm_code_runs_in_background_without_logging_otp(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        api_key = "bk_" + "secret_result"
        client.llm_results["verify"] = {
            "ok": True,
            "state": "COMPLETE",
            "apiKey": api_key,
            "telegramText": "Bankr LLM purchase complete for $10.",
        }
        callbacks = []
        plugin._client_factory = lambda: client
        plugin._background_runner = callbacks.append
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")
        log_output = io.StringIO()
        log_handler = logging.StreamHandler(log_output)
        plugin.logger.addHandler(log_handler)
        plugin.logger.setLevel(logging.WARNING)
        try:
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "/llm_code 123456",
                    "1045618308",
                    username="AlpskyKnedlik",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )
            self.assertEqual(result, plugin._SKIP_RESULT)
            self.assertEqual(client.llm_calls, [])
            self.assertEqual(
                gateway.adapters["telegram"].sent,
                [("telegram-chat", plugin._TELEGRAM_LLM_STARTED_MESSAGE)],
            )

            callbacks[-1]()
        finally:
            plugin.logger.removeHandler(log_handler)

        self.assertEqual(
            client.llm_calls,
            [
                {
                    "operation": "verify",
                    "user_id": "1045618308",
                    "username": "AlpskyKnedlik",
                    "payload": {"code": "123456"},
                    "user_access_token": "user-access-token",
                }
            ],
        )
        self.assertNotIn("123456", log_output.getvalue())
        final_text = gateway.adapters["telegram"].sent[-1][1]
        self.assertIn("Bankr LLM purchase complete", final_text)
        self.assertIn(api_key, final_text)

    def test_llm_credits_returns_balance_without_revealing_key(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.llm_results["credits"] = {
            "ok": True,
            "telegramText": "Bankr LLM credits: $9.00. Key fingerprint: abc123.",
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/llm_credits",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(client.llm_calls[0]["operation"], "credits")
        self.assertEqual(
            gateway.adapters["telegram"].sent[-1],
            (
                "telegram-chat",
                "Bankr LLM credits: $9.00. Key fingerprint: abc123.",
            ),
        )

    def test_telegram_buy_crypto_news_text_is_consumed_before_llm(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin._background_runner = lambda callback: callback()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "Can u buy a cryptonews with Firefly?",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(client.paid_tool_calls, [("news", "1045618308", None)])
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [
                ("telegram-chat", plugin._TELEGRAM_PAID_TOOL_STARTED_MESSAGE),
                ("telegram-chat", "Crypto News unlocked."),
            ],
        )

    def test_telegram_buy_crypto_news_starts_purchase_in_background(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        callbacks = []
        plugin._client_factory = lambda: client
        plugin._background_runner = callbacks.append
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "buy crypto news",
                "1045618308",
                platform="telegram",
                chat_id="telegram-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(client.paid_tool_calls, [])
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", plugin._TELEGRAM_PAID_TOOL_STARTED_MESSAGE)],
        )

        callbacks[-1]()

        self.assertEqual(client.paid_tool_calls, [("news", "1045618308", None)])
        # The purchase authenticates as the user via their per-user token.
        self.assertEqual(client.paid_tool_tokens, ["user-access-token"])
        self.assertEqual(
            gateway.adapters["telegram"].sent[-1],
            ("telegram-chat", "Crypto News unlocked."),
        )

    def test_telegram_buy_crypto_news_uses_direct_bot_api_when_token_is_available(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin._background_runner = lambda callback: callback()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")
        requests = []

        def fake_opener(request, timeout):
            requests.append((request, timeout))
            return FakeTelegramResponse()

        plugin._telegram_api_opener = fake_opener

        with patch.dict(plugin.os.environ, {"TELEGRAM_BOT_TOKEN": "telegram-token"}):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "buy crypto news",
                    "1045618308",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(gateway.adapters["telegram"].sent, [])
        self.assertEqual(len(requests), 2)
        request, timeout = requests[0]
        self.assertEqual(timeout, plugin._TELEGRAM_SEND_TIMEOUT_SECONDS)
        self.assertEqual(
            request.full_url,
            "https://api.telegram.org/bottelegram-token/sendMessage",
        )
        payload = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["chat_id"], ["telegram-chat"])
        self.assertEqual(payload["text"], [plugin._TELEGRAM_PAID_TOOL_STARTED_MESSAGE])
        self.assertEqual(payload["disable_web_page_preview"], ["true"])
        final_payload = parse_qs(requests[1][0].data.decode("utf-8"))
        self.assertEqual(final_payload["text"], ["Crypto News unlocked."])

    def test_photon_pairing_code_is_consumed_before_llm(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["link"] = {
            "ok": True,
            "imessageText": "iMessage linked.",
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway()

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "ABCDEFGH",
                "+15551234567",
                username="Photon User",
                platform="photon",
                chat_id="photon-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            client.imessage_calls,
            [
                (
                    "link",
                    {"code": "ABCDEFGH", "photonUserId": "+15551234567"},
                )
            ],
        )
        self.assertEqual(gateway.adapters["photon"].sent, [("photon-chat", "iMessage linked.")])
        self.assertEqual(
            gateway.pairing_store.generated,
            [("photon", "+15551234567", "Photon User")],
        )
        self.assertEqual(gateway.pairing_store.approved, [("photon", "HERMES1")])

    def test_imessage_platform_pairing_code_is_consumed_before_llm(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["link"] = {
            "ok": True,
            "imessageText": "iMessage linked.",
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        platform = FakePlatform("imessage")
        gateway = FakeGateway(adapter_key=platform)

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "4JTLV6XQ",
                "+15551234567",
                username="Photon User",
                platform="imessage",
                chat_id="photon-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            client.imessage_calls,
            [
                (
                    "link",
                    {"code": "4JTLV6XQ", "photonUserId": "+15551234567"},
                )
            ],
        )
        self.assertEqual(gateway.adapters[platform].sent, [("photon-chat", "iMessage linked.")])

    def test_photon_yes_without_pending_passes_through_to_normal_chat(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["pending"] = {"ok": True, "pending": False}
        plugin._client_factory = lambda: client
        plugin.register(context)

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("yes", "+15551234567", platform="photon"),
            gateway=FakeGateway(),
        )

        self.assertIsNone(result)
        self.assertEqual(
            client.imessage_calls,
            [("pending", {"photonUserId": "+15551234567"})],
        )

    def test_photon_yes_with_pending_is_decided_and_consumed(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["pending"] = {"ok": True, "pending": True}
        client.imessage_results["decision"] = {
            "ok": True,
            "imessageText": "Sign402 test approval approved.",
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway()

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                " yes ",
                "+15551234567",
                platform="photon",
                chat_id="photon-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(
            result,
            {"action": "skip", "reason": "sign402-imessage-handled"},
        )
        self.assertEqual(
            client.imessage_calls,
            [
                ("pending", {"photonUserId": "+15551234567"}),
                (
                    "decision",
                    {"photonUserId": "+15551234567", "decision": "YES"},
                ),
            ],
        )
        self.assertEqual(
            gateway.adapters["photon"].sent,
            [("photon-chat", "Sign402 test approval approved.")],
        )


if __name__ == "__main__":
    unittest.main()
