import asyncio
import importlib.util
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
        self.paid_tool_calls = []
        self.paid_tool_result = "Crypto News unlocked."

    def execute(self, operation, identity):
        self.calls.append((operation, identity))
        if self.error:
            raise self.error
        return self.result

    def execute_imessage(self, operation, payload):
        self.imessage_calls.append((operation, payload))
        if self.error:
            raise self.error
        return self.imessage_results.get(operation, {"ok": True})

    def execute_paid_tool(self, tool, identity):
        self.paid_tool_calls.append((tool, identity.user_id, identity.username))
        if self.error:
            raise self.error
        return self.paid_tool_result


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
                "wallet",
                "balance",
                "connect-imessage",
            },
        )
        for command in context.commands.values():
            self.assertTrue(command["description"])

    def test_command_uses_bound_identity_and_ignores_raw_arguments(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        context.hooks["pre_gateway_dispatch"](
            event=FakeEvent(
                "/wallet telegramUserId=999",
                user_id="1045618308",
                username="AlpskyKnedlik",
            )
        )

        result = asyncio.run(
            context.commands["wallet"]["handler"](
                "telegramUserId=999 telegramUsername=Attacker"
            )
        )

        self.assertEqual(result, "gateway telegram text")
        self.assertEqual(len(client.calls), 1)
        operation, identity = client.calls[0]
        self.assertEqual(operation, "create-wallet")
        self.assertEqual(identity.user_id, "1045618308")
        self.assertEqual(identity.username, "AlpskyKnedlik")

    def test_identity_is_consumed_after_one_command(self):
        plugin = load_plugin()
        context = FakeContext()
        client = FakeClient()
        plugin._client_factory = lambda: client
        plugin.register(context)
        context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("/wallet", user_id="1045618308")
        )
        handler = context.commands["wallet"]["handler"]

        async def run_twice():
            return await handler(""), await handler("")

        first, second = asyncio.run(run_twice())

        self.assertEqual(first, "gateway telegram text")
        self.assertIn("Telegram", second)
        self.assertEqual(len(client.calls), 1)

    def test_gateway_error_returns_only_safe_user_message(self):
        plugin = load_plugin()
        context = FakeContext()
        safe_message = "Wallet service is temporarily unavailable."
        client = FakeClient(error=plugin.GatewayClientError(safe_message))
        plugin._client_factory = lambda: client
        plugin.register(context)
        context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("/balance", user_id="1045618308")
        )

        result = asyncio.run(context.commands["balance"]["handler"](""))

        self.assertEqual(result, safe_message)

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
        context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("/connect_imessage telegramUserId=999", "1045618308")
        )

        result = asyncio.run(context.commands["connect-imessage"]["handler"](""))

        self.assertEqual(result, "Send ABCDEFGH to iMessage")
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
        context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("/start telegramUserId=999", "1045618308")
        )

        result = asyncio.run(context.commands["start"]["handler"](""))

        self.assertIn("Welcome to Sign402.", result)
        self.assertIn("Your Base agent wallet:\n0xabc", result)
        self.assertIn("/balance", result)
        self.assertIn("/connect_imessage", result)
        self.assertEqual(len(client.calls), 1)
        operation, identity = client.calls[0]
        self.assertEqual(operation, "create-wallet")
        self.assertEqual(identity.user_id, "1045618308")

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
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(
            gateway.adapters["telegram"].sent,
            [("telegram-chat", plugin._TELEGRAM_PAID_TOOL_STARTED_MESSAGE)],
        )

        callbacks[0]()

        self.assertEqual(client.paid_tool_calls, [("news", "1045618308", None)])
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
