import asyncio
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import TestCase


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes-remote-plugin"
PACKAGE_NAME = "sign402_trezor_remote_plugin_test"


def load_plugin():
    for name in tuple(sys.modules):
        if name == PACKAGE_NAME or name.startswith(PACKAGE_NAME + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
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


class FakeEvent:
    def __init__(self, text, user_id="12345", platform="telegram"):
        self.text = text
        self.source = FakeSource(FakePlatform(platform), user_id)

    def get_command(self):
        return self.text[1:].split(maxsplit=1)[0]


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
        self.calls.append(("status", user_id))
        return "enrolled"

    def prepare(self, *args):
        self.calls.append(("prepare", *args))
        return "summary"

    def intent_test(self, user_id):
        self.calls.append(("test", user_id))
        return "tested"

    def confirm(self, *args):
        self.calls.append(("confirm", *args))
        return "complete"

    def cancel(self, user_id):
        self.calls.append(("cancel", user_id))
        return "cancelled"


class RemoteAgentPluginTests(TestCase):
    def test_registers_only_new_trezor_commands(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)
        self.assertEqual(
            set(context.commands),
            {"trezor-status", "trezor-test", "trezor-prepare", "trezor-confirm", "trezor-cancel"},
        )
        self.assertNotIn("bitrefill", context.commands)
        self.assertNotIn("wallet", context.commands)

    def test_commands_use_trusted_telegram_identity(self):
        plugin = load_plugin()
        controller = FakeController()
        plugin._controller_factory = lambda: controller
        context = FakeContext()
        plugin.register(context)
        context.hooks["pre_gateway_dispatch"](event=FakeEvent("/trezor_status"))
        self.assertEqual(asyncio.run(context.commands["trezor-status"]["handler"]("")), "enrolled")
        context.hooks["pre_gateway_dispatch"](event=FakeEvent("/trezor_prepare product 1 us"))
        self.assertEqual(
            asyncio.run(context.commands["trezor-prepare"]["handler"]("product 1 us")),
            "summary",
        )
        self.assertEqual(
            controller.calls,
            [("status", "12345"), ("prepare", "12345", "product", "1", "US")],
        )

    def test_nontelegram_request_never_reaches_controller(self):
        plugin = load_plugin()
        controller = FakeController()
        plugin._controller_factory = lambda: controller
        context = FakeContext()
        plugin.register(context)
        context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("/trezor_status", platform="whatsapp")
        )
        result = asyncio.run(context.commands["trezor-status"]["handler"](""))
        self.assertIn("authenticated Telegram", result)
        self.assertEqual(controller.calls, [])
