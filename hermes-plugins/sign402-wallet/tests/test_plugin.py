import asyncio
import importlib.util
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path


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


@dataclass
class FakePlatform:
    value: str = "telegram"


@dataclass
class FakeSource:
    platform: FakePlatform
    user_id: str
    user_name: str | None = None
    chat_id: str = "chat-1"


class FakeEvent:
    def __init__(self, command: str, user_id: str, username: str | None = None):
        self.text = command
        self.source = FakeSource(FakePlatform(), user_id, username)

    def get_command(self):
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

    def execute(self, operation, identity):
        self.calls.append((operation, identity))
        if self.error:
            raise self.error
        return self.result


class PluginRegistrationTests(unittest.TestCase):
    def test_registers_dispatch_hook_and_wallet_commands(self):
        plugin = load_plugin()
        context = FakeContext()

        plugin.register(context)

        self.assertEqual(set(context.hooks), {"pre_gateway_dispatch"})
        self.assertEqual(
            set(context.commands),
            {"wallet", "create-wallet", "balance"},
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
                "/create_wallet telegramUserId=999",
                user_id="1045618308",
                username="AlpskyKnedlik",
            )
        )

        result = asyncio.run(
            context.commands["create-wallet"]["handler"](
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


if __name__ == "__main__":
    unittest.main()
