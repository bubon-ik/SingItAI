import json
from types import SimpleNamespace
from unittest import TestCase

from trezor_sidecar.errors import SafeError
from trezor_sidecar.mcp_client import McpToolCaller, TrezorMcpClient, decode_tool_result


class RecordingCaller:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        return self.response


class TrezorMcpClientTests(TestCase):
    def test_pairing_forces_base_path_and_device_display(self):
        caller = RecordingCaller({"address": "0x1111111111111111111111111111111111111111"})
        client = TrezorMcpClient(caller)

        client.get_base_address("m/44'/60'/0'/0/0")

        self.assertEqual(caller.calls, [("trezor_get_address", {
            "coin": "base",
            "path": "m/44'/60'/0'/0/0",
            "showOnTrezor": True,
        })])

    def test_signing_forces_base_chain_and_disables_broadcast(self):
        caller = RecordingCaller({"payload": {"serializedTx": "0x02aa"}})
        client = TrezorMcpClient(caller)

        client.sign_base_transaction(
            path="m/44'/60'/0'/0/0",
            to="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            data="0xa9059cbb" + "00" * 64,
        )

        name, arguments = caller.calls[0]
        self.assertEqual(name, "trezor_send_transaction")
        self.assertEqual(arguments["coin"], "base")
        self.assertEqual(arguments["chainId"], 8453)
        self.assertEqual(arguments["value"], "0")
        self.assertIs(arguments["broadcast"], False)

    def test_typed_data_and_push_use_only_allowed_tool_shapes(self):
        caller = RecordingCaller({"ok": True})
        client = TrezorMcpClient(caller)

        client.sign_typed_data("m/44'/60'/0'/0/0", {"domain": {"chainId": 8453}})
        client.push_base_transaction("0x02aa")

        self.assertEqual(caller.calls, [
            ("trezor_sign_typed_data", {
                "coin": "base",
                "path": "m/44'/60'/0'/0/0",
                "data": {"domain": {"chainId": 8453}},
            }),
            ("trezor_push_transaction", {"coin": "base", "tx": "0x02aa"}),
        ])
        self.assertFalse(hasattr(client, "call"))

    def test_decoder_returns_mapping_structured_content(self):
        result = SimpleNamespace(isError=False, structuredContent={"address": "0x1"})

        self.assertEqual(decode_tool_result(result), {"address": "0x1"})

    def test_decoder_accepts_json_text_content(self):
        result = SimpleNamespace(
            isError=False,
            structuredContent=None,
            content=[SimpleNamespace(type="text", text=json.dumps({"signed": "0x02aa"}))],
        )

        self.assertEqual(decode_tool_result(result), {"signed": "0x02aa"})

    def test_decoder_converts_tool_errors_to_safe_error(self):
        with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable.") as raised:
            decode_tool_result(SimpleNamespace(isError=True, content=[]))

        self.assertEqual(raised.exception.code, "trezor_unavailable")
        self.assertEqual(raised.exception.status, 503)

    def test_decoder_rejects_malformed_json_without_disclosing_content(self):
        result = SimpleNamespace(
            isError=False,
            structuredContent=None,
            content=[SimpleNamespace(type="text", text="{secret: canary}")],
        )

        with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable."):
            decode_tool_result(result)

    def test_decoder_rejects_oversized_response(self):
        result = SimpleNamespace(
            isError=False,
            structuredContent=None,
            content=[SimpleNamespace(type="text", text="x" * 65537)],
        )

        with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable."):
            decode_tool_result(result)

    def test_caller_repr_redacts_token_and_fixed_url(self):
        representation = repr(McpToolCaller("canary-secret"))

        self.assertEqual(representation, "McpToolCaller(timeout_seconds=120.0)")
        self.assertNotIn("canary-secret", representation)
        self.assertNotIn("127.0.0.1:21340", representation)

    def test_caller_rejects_unavailable_required_tool_before_calling_it(self):
        caller = McpToolCaller("canary-secret")

        with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable."):
            caller._require_tool([], "trezor_get_address")
