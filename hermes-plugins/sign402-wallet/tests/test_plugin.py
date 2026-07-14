import asyncio
import importlib.util
import io
import json
import logging
import os
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
        self.approval_results = {}
        self.approval_calls = []
        self.execute_tokens = []
        self.paid_tool_calls = []
        self.paid_tool_tokens = []
        self.paid_tool_result = "Crypto News unlocked."
        self.bitrefill_calls = []
        self.bitrefill_result = "Bitrefill delivered."
        self.bitrefill_search_calls = []
        self.bitrefill_list_calls = []
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
        self.bitrefill_list_result = {
            "ok": True,
            "products": [
                {
                    "productId": "wolt-cz",
                    "name": "Wolt Czech Republic",
                    "country": "CZ",
                    "category": "food",
                }
            ],
            "start": 0,
            "limit": 8,
            "hasPrevious": False,
            "hasNext": False,
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
        self.withdraw_tokens_calls = []
        self.withdraw_calls = []
        self.withdraw_tokens_result = {
            "ok": True,
            "tokens": [
                {
                    "symbol": "SINGIT",
                    "contractAddress": "0xc2c1e0b7C401e6217193732272444D928646eba3",
                    "balance": "250",
                    "decimals": 18,
                    "verified": True,
                },
                {
                    "symbol": "OTHER",
                    "contractAddress": "0x2222222222222222222222222222222222222222",
                    "balance": "3",
                    "decimals": 8,
                    "verified": False,
                },
            ],
        }
        self.withdraw_result = "Withdrawal sent."

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

    def execute_approval(self, operation, payload):
        self.approval_calls.append((operation, payload))
        if self.error:
            raise self.error
        return self.approval_results.get(operation, {"ok": True})

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
        payment_token=None,
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
                payment_token,
                user_access_token,
            )
        )
        if self.error:
            raise self.error
        return self.bitrefill_result

    def search_bitrefill_products(
        self,
        *,
        query,
        country,
        search_all_countries=True,
        include_test_products=False,
    ):
        self.bitrefill_search_calls.append(
            (query, country, search_all_countries, include_test_products)
        )
        if self.error:
            raise self.error
        return self.bitrefill_search_result

    def list_bitrefill_products(
        self,
        *,
        country,
        category,
        start,
        limit,
        include_international=True,
        include_test_products=False,
    ):
        self.bitrefill_list_calls.append(
            (
                country,
                category,
                start,
                limit,
                include_international,
                include_test_products,
            )
        )
        if self.error:
            raise self.error
        return self.bitrefill_list_result

    def get_bitrefill_product(self, *, product_id, country):
        self.bitrefill_product_calls.append((product_id, country))
        if self.error:
            raise self.error
        return self.bitrefill_product_result

    def execute_spending_limits(
        self,
        identity,
        *,
        max_per_tx_usdc=None,
        daily_cap_usdc=None,
        user_access_token=None,
    ):
        self.limits_calls.append(
            (
                identity.user_id,
                identity.username,
                max_per_tx_usdc,
                daily_cap_usdc,
                user_access_token,
            )
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

    def withdraw_tokens(self, identity, *, user_access_token):
        self.withdraw_tokens_calls.append((identity.user_id, user_access_token))
        if self.error:
            raise self.error
        return self.withdraw_tokens_result

    def execute_withdrawal(
        self,
        identity,
        *,
        token_address,
        amount,
        to_address,
        user_access_token,
    ):
        self.withdraw_calls.append(
            (
                identity.user_id,
                token_address,
                amount,
                to_address,
                user_access_token,
            )
        )
        if self.error:
            raise self.error
        return self.withdraw_result


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


class FakePhotonResponse:
    def __init__(self):
        self.closed = False

    def read(self, size=-1):
        return (
            b'{"succeed":true,"data":{"id":"user-1","type":"shared",'
            b'"phoneNumber":"+420773173967","assignedPhoneNumber":"+16282647754"}}'
        )

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
    def test_photon_registration_rejects_oversized_response(self):
        plugin = load_plugin()

        class OversizedResponse:
            def __init__(self):
                self.closed = False
                self.read_size = None

            def read(self, size=-1):
                self.read_size = size
                return b"x" * 262_145

            def close(self):
                self.closed = True

        response = OversizedResponse()
        with self.assertRaisesRegex(
            plugin.GatewayClientError,
            "registration service",
        ):
            plugin._read_photon_json_response(response)

        self.assertEqual(response.read_size, 262_145)
        self.assertTrue(response.closed)

    def setUp(self):
        # A deployed wallet plugin must always have an explicit Telegram
        # policy. Tests that exercise normal wallet handling opt into the
        # public Sign402 policy, while policy-specific tests override it.
        self._sign402_policy = patch.dict(
            os.environ,
            {"SIGN402_TELEGRAM_ALLOWED_USERS": "*"},
        )
        self._sign402_policy.start()
        self.addCleanup(self._sign402_policy.stop)

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
                "connect-whatsapp",
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
                "connect_whatsapp",
                "limits",
                "withdraw",
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
        self.assertEqual(client.calls, [])
        self.assertEqual(client.create_wallet_calls, ["1045618308"])
        self.assertEqual(plugin._USER_ACCESS_TOKENS["1045618308"], "user-access-token")

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

    def test_expired_user_access_token_is_refreshed_before_request(self):
        plugin = load_plugin()
        client = FakeClient()
        identity = plugin.TelegramIdentity(user_id="1045618308")

        with patch.object(plugin.time, "time", return_value=1_000.0):
            first = plugin._user_access_token(client, identity)
        client.access_token = "fresh-user-access-token"
        with patch.object(
            plugin.time,
            "time",
            return_value=(
                1_000.0
                + plugin._USER_ACCESS_TOKEN_TTL_SECONDS
                - plugin._USER_ACCESS_TOKEN_REFRESH_SKEW_SECONDS
            ),
        ):
            refreshed = plugin._user_access_token(client, identity)

        self.assertEqual(first, "user-access-token")
        self.assertEqual(refreshed, "fresh-user-access-token")
        self.assertEqual(client.create_wallet_calls, ["1045618308", "1045618308"])

    def test_user_access_token_cache_is_bounded(self):
        plugin = load_plugin()

        with patch.object(plugin, "_USER_ACCESS_TOKEN_CACHE_MAX_USERS", 2), patch.object(
            plugin.time,
            "time",
            side_effect=(1_000.0, 1_001.0, 1_002.0),
        ):
            for user_id in ("1", "2", "3"):
                plugin._remember_user_access_token(
                    plugin.TelegramIdentity(user_id=user_id),
                    {"accessToken": f"token-{user_id}"},
                )

        self.assertEqual(plugin._USER_ACCESS_TOKENS, {"2": "token-2", "3": "token-3"})

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
        self.assertIn("connect-whatsapp", context.commands)
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

    def test_connect_whatsapp_uses_trusted_telegram_identity(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.approval_results["connect-imessage"] = {
            "ok": True,
            "telegramText": "Send ABCDEFGH to WhatsApp",
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {"SIGN402_WHATSAPP_PUBLIC_LINE": "+15551431969"},
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "/connect_whatsapp telegramUserId=999",
                    "1045618308",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertIn(
            "+15551431969",
            gateway.adapters["telegram"].sent[0][1],
        )
        self.assertEqual(
            client.approval_calls,
            [
                (
                    "connect-imessage",
                    {"telegramUserId": "1045618308", "channel": "whatsapp"},
                )
            ],
        )

    def test_connect_imessage_includes_public_imessage_line_when_configured(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["connect-imessage"] = {
            "telegramText": "Send ABCDEFGH to iMessage"
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(plugin.os.environ, {"SIGN402_IMESSAGE_PUBLIC_LINE": "+420123456789"}):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "/connect_imessage",
                    "1045618308",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        text = gateway.adapters["telegram"].sent[0][1]
        self.assertIn("+420123456789", text)
        self.assertIn("ABCDEFGH", text)
        self.assertIn("send", text.lower())

    def test_connect_imessage_auto_register_prompts_for_phone(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {
                "SIGN402_PHOTON_AUTO_REGISTER_USERS": "1",
                "SIGN402_IMESSAGE_PUBLIC_LINE": "+420111222333",
            },
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "Connect iMessage",
                    "1045618308",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(client.imessage_calls, [])
        text = gateway.adapters["telegram"].sent[0][1]
        self.assertIn("phone number", text.lower())
        self.assertIn("+420", text)
        self.assertIn("iMessage", text)
        self.assertIn("private pairing line", text)
        self.assertNotIn("+420111222333", text)

    def test_connect_imessage_auto_registers_phone_before_pairing(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["connect-imessage"] = {
            "telegramText": "Send ABCDEFGH to iMessage"
        }
        plugin._client_factory = lambda: client
        photon_requests = []

        def fake_photon_opener(request, timeout):
            photon_requests.append((request, timeout))
            return FakePhotonResponse()

        plugin._photon_api_opener = fake_photon_opener
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {
                "SIGN402_PHOTON_AUTO_REGISTER_USERS": "1",
                "PHOTON_PROJECT_ID": "project-id",
                "PHOTON_PROJECT_SECRET": "project-secret",
                "PHOTON_API_BASE_URL": "https://spectrum.test",
                "SIGN402_IMESSAGE_PUBLIC_LINE": "+420111222333",
            },
        ):
            context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "Connect iMessage",
                    "1045618308",
                    username="AlpskyKnedlik",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "+420773173967",
                    "1045618308",
                    username="AlpskyKnedlik",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(len(photon_requests), 1)
        request, timeout = photon_requests[0]
        self.assertEqual(
            request.full_url,
            "https://spectrum.test/projects/project-id/users/",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, plugin._PHOTON_API_TIMEOUT_SECONDS)
        self.assertIn("Basic ", request.headers["Authorization"])
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertEqual(request.headers["User-agent"], "Sign402-Hermes/0.1")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "type": "shared",
                "phoneNumber": "+420773173967",
            },
        )
        self.assertEqual(
            client.imessage_calls,
            [("connect-imessage", {"telegramUserId": "1045618308"})],
        )
        text = gateway.adapters["telegram"].sent[-1][1]
        self.assertIn("+16282647754", text)
        self.assertNotIn("+420111222333", text)
        self.assertIn("ABCDEFGH", text)

    def test_imessage_phone_validation_matches_gateway_minimum_length(self):
        plugin = load_plugin()

        self.assertFalse(plugin._is_e164_phone_number("+1234567"))
        self.assertTrue(plugin._is_e164_phone_number("+12345678"))

    def test_imessage_auto_registration_is_rate_limited_per_telegram_user(self):
        plugin = load_plugin()
        client = FakeClient()
        client.imessage_results["connect-imessage"] = {
            "telegramText": "Send ABCDEFGH to iMessage"
        }
        plugin._client_factory = lambda: client
        photon_requests = []

        def fake_photon_opener(request, timeout):
            photon_requests.append((request, timeout))
            return FakePhotonResponse()

        plugin._photon_api_opener = fake_photon_opener
        identity = plugin.TelegramIdentity(user_id="1045618308")
        with patch.dict(
            plugin.os.environ,
            {
                "PHOTON_PROJECT_ID": "project-id",
                "PHOTON_PROJECT_SECRET": "project-secret",
                "PHOTON_API_BASE_URL": "https://spectrum.test",
            },
        ):
            for number in ("+420773173960", "+420773173961", "+420773173962"):
                plugin._connect_imessage_after_phone_registration(
                    identity=identity,
                    phone_number=number,
                )
            with self.assertRaises(plugin.GatewayClientError) as raised:
                plugin._connect_imessage_after_phone_registration(
                    identity=identity,
                    phone_number="+420773173963",
                )

        self.assertEqual(
            raised.exception.user_message,
            "Too many iMessage registration attempts. Please try again in an hour.",
        )
        self.assertEqual(len(photon_requests), 3)

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
        self.assertIn("Support ID: 1045618308", text)
        self.assertIn("1. Wallet", text)
        self.assertIn("2. Balance", text)
        self.assertIn("3. Connect iMessage", text)
        self.assertIn("phone number", text)
        self.assertIn("4. Limits", text)
        self.assertIn("5. Buy", text)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.create_wallet_calls, ["1045618308"])

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
        self.assertEqual(client.create_wallet_calls, ["1045618308"])
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
                [{"text": "Connect iMessage"}, {"text": "Connect WhatsApp"}],
                [{"text": "Limits"}],
                [{"text": "Withdraw"}],
                [{"text": "Buy Bitrefill"}, {"text": "Buy LLM Credits"}],
                [{"text": "Last Purchase"}, {"text": "Help"}],
            ],
        )

    def test_sign402_only_mode_catches_unknown_telegram_text(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {
                "SIGN402_TELEGRAM_SIGN402_ONLY": "1",
                "SIGN402_TELEGRAM_ALLOWED_USERS": "*",
            },
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "hello, what can you do?",
                    "1045618308",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(len(gateway.adapters["telegram"].sent), 1)
        text = gateway.adapters["telegram"].sent[0][1]
        self.assertIn("Use the Sign402 menu", text)
        self.assertIn("Wallet", text)

    def test_public_mode_requires_explicit_sign402_access_policy(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {
                "SIGN402_TELEGRAM_SIGN402_ONLY": "1",
                "SIGN402_TELEGRAM_ALLOWED_USERS": "",
            },
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "/wallet",
                    "8538252718",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(client.create_wallet_calls, [])
        self.assertEqual(gateway.adapters["telegram"].sent, [])

    def test_public_mode_allows_sign402_without_opening_hermes(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {
                "SIGN402_TELEGRAM_SIGN402_ONLY": "1",
                "SIGN402_TELEGRAM_ALLOWED_USERS": "*",
            },
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "/wallet",
                    "8538252718",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(client.create_wallet_calls, ["8538252718"])

    def test_telegram_pre_dispatch_exception_fails_closed(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {
                "SIGN402_TELEGRAM_SIGN402_ONLY": "1",
                "SIGN402_TELEGRAM_ALLOWED_USERS": "*",
            },
        ), patch.object(
            plugin,
            "_telegram_public_command",
            side_effect=RuntimeError("unexpected parser failure"),
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "/model",
                    "8538252718",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", plugin._UNEXPECTED_ERROR_MESSAGE)],
        )

    def test_photon_pre_dispatch_exception_fails_closed(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="photon")

        with patch.object(
            plugin,
            "_handle_pre_gateway_dispatch",
            side_effect=RuntimeError("unexpected parser failure"),
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "YES",
                    "+420736255120",
                    platform="photon",
                    chat_id="photon-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(gateway.adapters["photon"].sent, [])

    def test_unknown_telegram_text_falls_through_when_sign402_only_is_disabled(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {"SIGN402_TELEGRAM_ALLOWED_USERS": "1045618308"},
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "hello, what can you do?",
                    "1045618308",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertIsNone(result)
        self.assertEqual(gateway.adapters["telegram"].sent, [])

    def test_public_sign402_policy_forces_sign402_only_mode(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {
                "SIGN402_TELEGRAM_ALLOWED_USERS": "*",
                "SIGN402_TELEGRAM_SIGN402_ONLY": "",
            },
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "hello, what can you do?",
                    "8538252718",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertIn("Use the Sign402 menu", gateway.adapters["telegram"].sent[0][1])

    def test_missing_telegram_access_policy_blocks_by_default(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(plugin.os.environ, {}, clear=True):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "/wallet",
                    "8538252718",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(client.create_wallet_calls, [])
        self.assertEqual(gateway.adapters["telegram"].sent, [])

    def test_private_hermes_allowlist_remains_supported(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="telegram")

        with patch.dict(
            plugin.os.environ,
            {"TELEGRAM_ALLOWED_USERS": "8538252718"},
            clear=True,
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "/wallet",
                    "8538252718",
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(client.create_wallet_calls, ["8538252718"])

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
                "/bitrefill test-gift-card-link 1 US SINGIT",
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
                ("telegram-chat", plugin._TELEGRAM_BITREFILL_STARTED_MESSAGE),
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
                    {
                        **client.withdraw_tokens_result["tokens"][0],
                        "native": False,
                    },
                    "user-access-token",
                )
            ],
        )

    def test_bitrefill_direct_command_requires_token_argument(self):
        plugin = load_plugin()

        self.assertIsNone(plugin._parse_bitrefill_args("gift 1 US"))
        self.assertEqual(
            plugin._parse_bitrefill_args("gift 1 US USDC"),
            ("gift", "1", "US", "USDC"),
        )

    def test_bitrefill_wizard_requires_token_button_before_purchase(self):
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

        dispatch("Buy Bitrefill")
        dispatch("Search Products")
        dispatch("amazon")
        dispatch("1")
        dispatch("1")

        self.assertEqual(client.bitrefill_calls, [])
        self.assertIn("Choose a token to pay with", gateway.adapters["telegram"].sent[-1][1])
        self.assertEqual(client.withdraw_tokens_calls, [("1045618308", "user-access-token")])

        dispatch("2")

        self.assertEqual(client.bitrefill_calls[-1][6]["symbol"], "OTHER")

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
        self.assertEqual(client.bitrefill_search_calls, [("amazon", "DE", True, False)])
        self.assertIn("1. Amazon Czech Republic", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertEqual(client.bitrefill_product_calls, [("amazon-cz", "CZ")])
        self.assertIn("Choose amount for Amazon Czech Republic", gateway.adapters["telegram"].sent[-1][1])
        self.assertIn("1. 10", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertIn("Choose a token to pay with", gateway.adapters["telegram"].sent[-1][1])
        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertEqual(
            gateway.adapters["telegram"].sent[-2:],
            [
                ("telegram-chat", plugin._TELEGRAM_BITREFILL_STARTED_MESSAGE),
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
                    "CZ",
                    {},
                    {
                        **client.withdraw_tokens_result["tokens"][0],
                        "native": False,
                    },
                    "user-access-token",
                )
            ],
        )

    def test_bitrefill_global_search_uses_selected_products_real_country(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.bitrefill_search_result = {
            "ok": True,
            "products": [
                {
                    "productId": "bitrefill-giftcard-usd",
                    "name": "Bitrefill Gift Card (USD)",
                    "country": "US",
                    "category": "gift_card",
                }
            ],
        }
        client.bitrefill_product_result = {
            "ok": True,
            "productId": "bitrefill-giftcard-usd",
            "name": "Bitrefill Gift Card (USD)",
            "country": "US",
            "requiredRecipientFields": [],
            "packages": [
                {"packageId": "bitrefill-giftcard-usd<&>1", "value": "1", "priceUsd": "1.00"}
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
                    platform="telegram",
                    chat_id="telegram-chat",
                ),
                gateway=gateway,
            )

        dispatch("Buy Bitrefill")
        dispatch("Search Products")
        dispatch("Bitrefill Gift Card")
        self.assertIn("Bitrefill Gift Card (USD) (US)", gateway.adapters["telegram"].sent[-1][1])

        dispatch("1")

        self.assertEqual(
            client.bitrefill_product_calls,
            [("bitrefill-giftcard-usd", "US")],
        )

    def test_bitrefill_back_after_failed_purchase_returns_to_main_menu(self):
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

        dispatch("Buy Bitrefill")
        dispatch("Search Products")
        dispatch("amazon")
        dispatch("1")
        client.error = plugin.GatewayClientError(
            "This Bitrefill amount is above the current live purchase limit ($5.00). Choose a smaller amount or another product."
        )

        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertIn("current live purchase limit", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("Back"), plugin._SKIP_RESULT)
        self.assertIn("Back to Sign402 main menu.", gateway.adapters["telegram"].sent[-1][1])

    def test_withdraw_button_collects_token_amount_and_destination(self):
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

        self.assertEqual(dispatch("Withdraw"), plugin._SKIP_RESULT)
        self.assertEqual(client.withdraw_tokens_calls, [("1045618308", "user-access-token")])
        self.assertIn("Choose an asset to withdraw", gateway.adapters["telegram"].sent[-1][1])
        self.assertIn("1. SINGIT: 250", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertIn("How much SINGIT", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("100"), plugin._SKIP_RESULT)
        self.assertIn("Send the Base address", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(
            dispatch("0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd"),
            plugin._SKIP_RESULT,
        )
        self.assertEqual(
            gateway.adapters["telegram"].sent[-2:],
            [
                ("telegram-chat", plugin._TELEGRAM_WITHDRAW_STARTED_MESSAGE),
                ("telegram-chat", "Withdrawal sent."),
            ],
        )
        self.assertEqual(
            client.withdraw_calls,
            [
                (
                    "1045618308",
                    "0xc2c1e0b7C401e6217193732272444D928646eba3",
                    "100",
                    "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
                    "user-access-token",
                )
            ],
        )

    def test_withdraw_normalizer_accepts_native_eth_only_with_native_marker(self):
        plugin = load_plugin()

        tokens = plugin._normalize_withdraw_tokens(
            [
                {
                    "symbol": "ETH",
                    "contractAddress": "native",
                    "balance": "0.01",
                    "decimals": 18,
                    "verified": True,
                    "native": True,
                },
                {
                    "symbol": "ETH",
                    "contractAddress": "native",
                    "balance": "0.01",
                    "decimals": 18,
                    "verified": True,
                },
            ]
        )

        self.assertEqual(len(tokens), 1)
        self.assertTrue(tokens[0]["native"])
        self.assertIn("leave ETH for gas", plugin._format_withdraw_tokens(tokens))

    def test_bitrefill_catalog_browses_categories_pages_and_buys(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        first_page = [
            {
                "productId": f"food-{index}",
                "name": f"Food Product {index}",
                "country": "CZ" if index < 8 else "XI",
                "category": "food",
            }
            for index in range(1, 9)
        ]
        second_page = [
            {
                "productId": "wolt-cz",
                "name": "Wolt Czech Republic",
                "country": "CZ",
                "category": "food",
            }
        ]

        def list_products(**kwargs):
            client.bitrefill_list_calls.append(
                (
                    kwargs["country"],
                    kwargs["category"],
                    kwargs["start"],
                    kwargs["limit"],
                    kwargs["include_international"],
                    kwargs["include_test_products"],
                )
            )
            if kwargs["start"] == 0:
                return {
                    "ok": True,
                    "products": first_page,
                    "start": 0,
                    "limit": 8,
                    "hasPrevious": False,
                    "hasNext": True,
                }
            return {
                "ok": True,
                "products": second_page,
                "start": 8,
                "limit": 8,
                "hasPrevious": True,
                "hasNext": False,
            }

        client.list_bitrefill_products = list_products
        client.bitrefill_product_result = {
            "ok": True,
            "productId": "wolt-cz",
            "name": "Wolt Czech Republic",
            "country": "CZ",
            "currency": "CZK",
            "requiredRecipientFields": [],
            "packages": [
                {"packageId": "wolt-cz<&>500", "value": "500", "priceUsd": "20.88"}
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

        self.assertEqual(dispatch("Buy Bitrefill"), plugin._SKIP_RESULT)
        self.assertEqual(dispatch("Browse Catalog"), plugin._SKIP_RESULT)
        self.assertIn("Choose a Bitrefill category", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("Food"), plugin._SKIP_RESULT)
        self.assertEqual(
            client.bitrefill_list_calls[-1],
            ("CZ", "food", 0, 8, True, False),
        )
        self.assertIn("Food Product 1", gateway.adapters["telegram"].sent[-1][1])
        self.assertIn(
            [{"text": "Next"}],
            plugin._bitrefill_catalog_reply_keyboard(
                8,
                has_previous=False,
                has_next=True,
            )["keyboard"],
        )

        self.assertEqual(dispatch("Next"), plugin._SKIP_RESULT)
        self.assertEqual(
            client.bitrefill_list_calls[-1],
            ("CZ", "food", 8, 8, True, False),
        )
        self.assertIn("Wolt Czech Republic", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertEqual(client.bitrefill_product_calls[-1], ("wolt-cz", "CZ"))
        self.assertIn("Choose amount for Wolt Czech Republic", gateway.adapters["telegram"].sent[-1][1])
        self.assertIn("500 CZK", gateway.adapters["telegram"].sent[-1][1])

        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertIn("Choose a token to pay with", gateway.adapters["telegram"].sent[-1][1])
        self.assertEqual(dispatch("1"), plugin._SKIP_RESULT)
        self.assertEqual(
            client.bitrefill_calls[-1],
            (
                "1045618308",
                "AlpskyKnedlik",
                "wolt-cz",
                "wolt-cz<&>500",
                "CZ",
                {},
                {
                    **client.withdraw_tokens_result["tokens"][0],
                    "native": False,
                },
                "user-access-token",
            ),
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
        self.assertIn("Choose a token to pay with", gateway.adapters["telegram"].sent[-1][1])
        dispatch("1")

        self.assertEqual(
            client.bitrefill_calls[-1],
            (
                "1045618308",
                "AlpskyKnedlik",
                "mobile-topup",
                "mobile-5",
                "CZ",
                {"phone": "+420777111222"},
                {
                    **client.withdraw_tokens_result["tokens"][0],
                    "native": False,
                },
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

    def test_bitrefill_catalog_displays_international_country_as_global(self):
        plugin = load_plugin()

        text = plugin._format_bitrefill_catalog_page(
            "CZ",
            "all",
            0,
            [
                {"name": "Viber", "country": "XI"},
                {"name": "NordVPN International", "country": "XI"},
                {"name": "Zalando Czech Republic", "country": "CZ"},
            ],
        )

        self.assertIn("1. Viber (Global)", text)
        self.assertIn("2. NordVPN International", text)
        self.assertNotIn("NordVPN International (Global)", text)
        self.assertIn("3. Zalando Czech Republic", text)
        self.assertNotIn("(XI)", text)

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
            [("1045618308", "AlpskyKnedlik", None, None, "user-access-token")],
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
            [
                (
                    "1045618308",
                    "AlpskyKnedlik",
                    "0.005",
                    "0.05",
                    "user-access-token",
                )
            ],
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
        self.assertEqual(
            client.limits_calls,
            [("1045618308", None, "0.004", "0.04", "user-access-token")],
        )

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

    def test_photon_pairing_code_resolves_shared_user_id_before_link(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["link"] = {
            "ok": True,
            "imessageText": "iMessage linked.",
        }
        plugin._client_factory = lambda: client
        photon_requests = []

        def fake_photon_opener(request, timeout):
            photon_requests.append((request, timeout))
            return FakePhotonResponse()

        plugin._photon_api_opener = fake_photon_opener
        plugin.register(context)
        gateway = FakeGateway()

        with patch.dict(
            plugin.os.environ,
            {
                "PHOTON_PROJECT_ID": "project-id",
                "PHOTON_PROJECT_SECRET": "project-secret",
                "PHOTON_API_BASE_URL": "https://spectrum.test",
            },
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "ABCDEFGH",
                    "c96ff937-53b5-4c86-8438-3ea65d8b5c44",
                    username="Photon User",
                    platform="photon",
                    chat_id="photon-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(len(photon_requests), 1)
        request, timeout = photon_requests[0]
        self.assertEqual(
            request.full_url,
            "https://spectrum.test/projects/project-id/users/c96ff937-53b5-4c86-8438-3ea65d8b5c44/",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, plugin._PHOTON_API_TIMEOUT_SECONDS)
        self.assertEqual(
            client.imessage_calls,
            [
                (
                    "link",
                    {"code": "ABCDEFGH", "photonUserId": "+420773173967"},
                )
            ],
        )
        self.assertEqual(gateway.adapters["photon"].sent, [("photon-chat", "iMessage linked.")])

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

    def test_whatsapp_cloud_pairing_code_is_linked_and_consumed(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.approval_results["link"] = {
            "ok": True,
            "imessageText": "WhatsApp linked.",
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="whatsapp_cloud")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "4JTLV6XQ",
                "420777111222",
                username="WhatsApp User",
                platform="whatsapp_cloud",
                chat_id="whatsapp-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(
            client.approval_calls,
            [
                (
                    "link",
                    {
                        "code": "4JTLV6XQ",
                        "approvalUserId": "420777111222",
                        "channel": "whatsapp",
                    },
                )
            ],
        )
        self.assertEqual(
            gateway.adapters["whatsapp_cloud"].sent,
            [("whatsapp-chat", "WhatsApp linked.")],
        )

    def test_whatsapp_cloud_button_decides_exact_approval_and_is_consumed(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.approval_results["decision"] = {
            "ok": True,
            "imessageText": "Approved.",
        }
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="whatsapp_cloud")
        event = FakeEvent(
            "Approve",
            "420777111222",
            platform="whatsapp_cloud",
            chat_id="whatsapp-chat",
        )
        event.raw_message = {
            "type": "button",
            "button": {"payload": "sign402:approve:approval-123"},
        }

        result = context.hooks["pre_gateway_dispatch"](event=event, gateway=gateway)

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(
            client.approval_calls,
            [
                (
                    "decision",
                    {
                        "approvalUserId": "420777111222",
                        "channel": "whatsapp",
                        "decision": "YES",
                        "approvalId": "approval-123",
                    },
                )
            ],
        )

    def test_whatsapp_cloud_plain_text_is_dropped_before_general_chat(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway(adapter_key="whatsapp_cloud")

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "hello agent",
                "420777111222",
                platform="whatsapp_cloud",
                chat_id="whatsapp-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(client.approval_calls, [])
        self.assertEqual(gateway.adapters["whatsapp_cloud"].sent, [])

    def test_photon_yes_without_pending_is_dropped_before_general_chat(self):
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

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(
            client.imessage_calls,
            [("pending", {"photonUserId": "+15551234567"})],
        )

    def test_photon_general_message_is_dropped_before_general_chat(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        gateway = FakeGateway()

        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "What can you do?",
                "+15551234567",
                platform="photon",
                chat_id="photon-chat",
            ),
            gateway=gateway,
        )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(client.imessage_calls, [])
        self.assertEqual(gateway.adapters["photon"].sent, [])

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

    def test_photon_yes_resolves_shared_user_id_before_decision(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        client.imessage_results["pending"] = {"ok": True, "pending": True}
        client.imessage_results["decision"] = {
            "ok": True,
            "imessageText": "Sign402 test approval approved.",
        }
        plugin._client_factory = lambda: client

        def fake_photon_opener(request, timeout):
            return FakePhotonResponse()

        plugin._photon_api_opener = fake_photon_opener
        plugin.register(context)
        gateway = FakeGateway()

        with patch.dict(
            plugin.os.environ,
            {
                "PHOTON_PROJECT_ID": "project-id",
                "PHOTON_PROJECT_SECRET": "project-secret",
                "PHOTON_API_BASE_URL": "https://spectrum.test",
            },
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event=FakeEvent(
                    "yes",
                    "c96ff937-53b5-4c86-8438-3ea65d8b5c44",
                    platform="photon",
                    chat_id="photon-chat",
                ),
                gateway=gateway,
            )

        self.assertEqual(result, plugin._SKIP_RESULT)
        self.assertEqual(
            client.imessage_calls,
            [
                ("pending", {"photonUserId": "+420773173967"}),
                (
                    "decision",
                    {"photonUserId": "+420773173967", "decision": "YES"},
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
