import asyncio
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import TestCase


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes-local-plugin"
PACKAGE_NAME = "sign402_trezor_local_plugin_test"


def load_plugin():
    for name in tuple(sys.modules):
        if name == PACKAGE_NAME or name.startswith(PACKAGE_NAME + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load local plugin")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class FakePlatform:
    value: str


@dataclass
class FakeSource:
    platform: FakePlatform
    user_id: str
    user_name: str | None = None
    chat_id: str = "chat-1"


class FakeEvent:
    def __init__(self, text, user_id="12345", platform="telegram"):
        self.text = text
        self.source = FakeSource(FakePlatform(platform), user_id)

    def get_command(self):
        return self.text[1:].split(maxsplit=1)[0] if self.text.startswith("/") else None


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.commands = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description=""):
        self.commands[name] = {"handler": handler, "description": description}


class FakeController:
    def __init__(self):
        self.calls = []

    def pair(self, user_id):
        self.calls.append(("pair", user_id))
        return "paired"

    def prepare(self, user_id, product_id, package_id, country):
        self.calls.append(("prepare", user_id, product_id, package_id, country))
        return "summary"

    def confirm(self, user_id, code):
        self.calls.append(("confirm", user_id, code))
        return "complete"

    def cancel(self, user_id):
        self.calls.append(("cancel", user_id))
        return "cancelled"


class LocalAgentPluginTests(TestCase):
    def test_registers_only_separate_local_commands_and_identity_hook(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)

        self.assertEqual(
            set(context.commands),
            {"trezor-pair", "trezor-prepare", "trezor-confirm", "trezor-cancel"},
        )
        self.assertEqual(set(context.hooks), {"pre_gateway_dispatch"})
        self.assertNotIn("bitrefill", context.commands)
        self.assertNotIn("wallet", context.commands)

    def test_commands_use_only_trusted_telegram_identity_and_exact_arguments(self):
        plugin = load_plugin()
        controller = FakeController()
        plugin._controller_factory = lambda: controller
        context = FakeContext()
        plugin.register(context)

        cases = [
            ("/trezor_pair", "trezor-pair", "", "paired"),
            ("/trezor_prepare test-gift 1 us", "trezor-prepare", "test-gift 1 us", "summary"),
            ("/trezor_confirm a1b2c3d4", "trezor-confirm", "a1b2c3d4", "complete"),
            ("/trezor_cancel", "trezor-cancel", "", "cancelled"),
        ]
        for text, command, raw_args, expected in cases:
            with self.subTest(command=command):
                context.hooks["pre_gateway_dispatch"](event=FakeEvent(text))
                result = asyncio.run(context.commands[command]["handler"](raw_args))
                self.assertEqual(result, expected)

        self.assertEqual(
            controller.calls,
            [
                ("pair", "12345"),
                ("prepare", "12345", "test-gift", "1", "US"),
                ("confirm", "12345", "A1B2C3D4"),
                ("cancel", "12345"),
            ],
        )

    def test_nontelegram_and_malformed_commands_fail_before_controller(self):
        plugin = load_plugin()
        controller = FakeController()
        plugin._controller_factory = lambda: controller
        context = FakeContext()
        plugin.register(context)

        context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("/trezor_pair", platform="whatsapp")
        )
        result = asyncio.run(context.commands["trezor-pair"]["handler"](""))
        self.assertIn("authenticated Telegram", result)

        context.hooks["pre_gateway_dispatch"](event=FakeEvent("/trezor_prepare bad"))
        usage = asyncio.run(context.commands["trezor-prepare"]["handler"]("bad"))
        self.assertIn("Usage", usage)
        self.assertEqual(controller.calls, [])

    def test_disabled_real_controller_returns_fixed_message_without_secrets(self):
        plugin = load_plugin()
        plugin._controller_factory = lambda: plugin.build_local_agent_controller({})
        context = FakeContext()
        plugin.register(context)
        context.hooks["pre_gateway_dispatch"](event=FakeEvent("/trezor_pair"))

        result = asyncio.run(context.commands["trezor-pair"]["handler"](""))

        self.assertEqual(result, "Local Trezor agent mode is disabled.")
