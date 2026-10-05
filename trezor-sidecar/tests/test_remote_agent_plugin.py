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
        parts = self.text[1:].split(maxsplit=1) if self.text.startswith("/") else []
        return parts[0] if parts else ""


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.commands = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description=""):
        self.commands[name] = {"handler": handler, "description": description}


class FakeAdapter:
    def __init__(self, sent):
        self.sent = sent

    async def send(self, chat_id, text):
        self.sent.append(text)


class FakeGateway:
    def __init__(self, sent):
        self.adapters = {"telegram": FakeAdapter(sent)}


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
        # The command path still works where no other plugin ends the hook
        # with a catch-all skip. Identity comes from the hook, never the text.
        plugin = load_plugin()
        controller = FakeController()
        plugin._controller_factory = lambda: controller
        context = FakeContext()
        plugin.register(context)
        # Capture directly: the hook itself now answers /trezor_* commands, so
        # going through it would consume the identity before the command runs.
        plugin.capture_identity(event=FakeEvent("/trezor_status"))
        self.assertEqual(asyncio.run(context.commands["trezor-status"]["handler"]("")), "enrolled")
        plugin.capture_identity(event=FakeEvent("/trezor_prepare product 1 us"))
        self.assertEqual(
            asyncio.run(context.commands["trezor-prepare"]["handler"]("product 1 us")),
            "summary",
        )
        self.assertEqual(
            controller.calls,
            [("status", "12345"), ("prepare", "12345", "product", "1", "US")],
        )

    def test_hook_handles_commands_before_another_plugin_can_skip_dispatch(self):
        # Break caught: the Sign402 catch-all ends dispatch first, so a
        # command-only plugin would never run and Telegram would go silent.
        plugin, controller, context, sent = self._wired_plugin()
        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("/trezor_prepare product my package id us"),
            gateway=FakeGateway(sent),
        )
        self.assertEqual(result.get("action"), "skip")
        self.assertEqual(
            controller.calls,
            [("prepare", "12345", "product", "my package id", "US")],
        )
        self.assertIn("summary", sent)

    def test_hook_ignores_text_that_is_not_a_trezor_command(self):
        # Break caught: the plugin swallows ordinary messages meant for others.
        plugin, controller, context, sent = self._wired_plugin()
        for text in ("hello", "/bitrefill", "/wallet", "", "/trezorish"):
            with self.subTest(text=text):
                result = context.hooks["pre_gateway_dispatch"](
                    event=FakeEvent(text), gateway=FakeGateway(sent)
                )
                self.assertIsNone(result)
        self.assertEqual(controller.calls, [])
        self.assertEqual(sent, [])

    def test_hook_reports_usage_without_calling_the_device(self):
        # Break caught: a malformed command reaches the remote agent anyway.
        plugin, controller, context, sent = self._wired_plugin()
        result = context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("/trezor_prepare only-two args"),
            gateway=FakeGateway(sent),
        )
        self.assertEqual(result.get("action"), "skip")
        self.assertEqual(controller.calls, [])
        self.assertEqual(sent, [plugin._USAGE_PREPARE])

    def test_hook_acknowledges_before_waiting_on_the_device(self):
        # Break caught: the operator sees nothing while the Trezor waits.
        plugin, controller, context, sent = self._wired_plugin()
        context.hooks["pre_gateway_dispatch"](
            event=FakeEvent("/trezor_confirm ABCD1234"),
            gateway=FakeGateway(sent),
        )
        self.assertEqual(sent, [plugin._ACKNOWLEDGED, "complete"])

    def _wired_plugin(self):
        plugin = load_plugin()
        controller = FakeController()
        plugin._controller_factory = lambda: controller
        plugin._spawn_worker = lambda worker: worker()
        context = FakeContext()
        plugin.register(context)
        return plugin, controller, context, []

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
