import json
import sys
from types import SimpleNamespace
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

from trezor_sidecar.errors import SafeError
from trezor_sidecar.mcp_client import (
    ALLOWED_TOOLS,
    McpToolCaller,
    TrezorMcpClient,
    decode_tool_result,
)


class RecordingCaller:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        return self.response


class FakeMcpTransport:
    def __init__(self, pages, result=None, failure=None):
        self.pages = pages
        self.result = result
        self.failure = failure
        self.http_clients = []
        self.stream_calls = []
        self.list_cursors = []
        self.tool_calls = []

    def modules(self):
        transport = self

        class AsyncClient:
            def __init__(self, **kwargs):
                transport.http_clients.append(kwargs)

            async def __aenter__(self):
                if transport.failure:
                    raise transport.failure
                return self

            async def __aexit__(self, *_):
                return None

        class Stream:
            async def __aenter__(self):
                return ("read", "write", lambda: None)

            async def __aexit__(self, *_):
                return None

        def streamable_http_client(url, *, http_client):
            transport.stream_calls.append((url, http_client))
            return Stream()

        class ClientSession:
            def __init__(self, read_stream, write_stream):
                self.read_stream = read_stream
                self.write_stream = write_stream

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def initialize(self):
                return None

            async def list_tools(self, cursor=None):
                transport.list_cursors.append(cursor)
                return transport.pages[len(transport.list_cursors) - 1]

            async def call_tool(self, name, arguments):
                transport.tool_calls.append((name, arguments))
                return transport.result

        httpx = ModuleType("httpx")
        httpx.AsyncClient = AsyncClient
        mcp = ModuleType("mcp")
        mcp.ClientSession = ClientSession
        mcp_client = ModuleType("mcp.client")
        streamable_http = ModuleType("mcp.client.streamable_http")
        streamable_http.streamable_http_client = streamable_http_client
        return {
            "httpx": httpx,
            "mcp": mcp,
            "mcp.client": mcp_client,
            "mcp.client.streamable_http": streamable_http,
        }


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

    def test_decoder_preserves_only_allowlisted_structured_device_cancellation(self):
        for code in ("device_rejected", "device_cancelled", "action_cancelled"):
            result = SimpleNamespace(
                isError=True,
                structuredContent={"code": code, "message": "device detail canary"},
                content=[SimpleNamespace(type="text", text="text canary")],
            )
            with self.subTest(code=code), self.assertRaises(SafeError) as raised:
                decode_tool_result(result)
            self.assertEqual(raised.exception.code, "device_rejected")
            self.assertEqual(
                raised.exception.message,
                "Trezor operation was cancelled.",
            )
            self.assertEqual(raised.exception.status, 400)
            self.assertNotIn("canary", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_decoder_rejects_unknown_malformed_and_text_only_error_codes(self):
        cases = (
            SimpleNamespace(
                isError=True,
                structuredContent={"code": "unknown_error", "message": "secret canary"},
                content=[],
            ),
            SimpleNamespace(
                isError=True,
                structuredContent={
                    "code": "device_rejected",
                    "message": "secret canary",
                    "detail": "not closed",
                },
                content=[],
            ),
            SimpleNamespace(
                isError=True,
                structuredContent=None,
                content=[
                    SimpleNamespace(
                        type="text",
                        text='{"code":"device_rejected","message":"secret canary"}',
                    )
                ],
            ),
        )
        for result in cases:
            with self.subTest(result=result), self.assertRaises(SafeError) as raised:
                decode_tool_result(result)
            self.assertEqual(raised.exception.code, "trezor_unavailable")
            self.assertNotIn("canary", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_decoder_rejects_malformed_json_without_disclosing_content(self):
        result = SimpleNamespace(
            isError=False,
            structuredContent=None,
            content=[SimpleNamespace(type="text", text="{secret: canary}")],
        )

        with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable.") as raised:
            decode_tool_result(result)

        self.assertIsNone(raised.exception.__cause__)

    def test_decoder_rejects_oversized_response(self):
        result = SimpleNamespace(
            isError=False,
            structuredContent=None,
            content=[SimpleNamespace(type="text", text="x" * 65537)],
        )

        with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable."):
            decode_tool_result(result)

    def test_decoder_enforces_utf8_byte_limit(self):
        result = SimpleNamespace(
            isError=False,
            structuredContent=None,
            content=[SimpleNamespace(type="text", text="€€")],
        )

        with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable."):
            decode_tool_result(result, max_bytes=3)

    def test_caller_default_allow_list_is_unchanged_by_the_override(self):
        # Break caught: an operator preview widens what the sidecar may call.
        caller = McpToolCaller("canary-secret")

        self.assertEqual(caller._allowed_tools, ALLOWED_TOOLS)
        self.assertNotIn("trezor_sign_message", caller._allowed_tools)
        with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable."):
            caller("trezor_sign_message", {"coin": "base"})

    def test_caller_override_admits_only_its_own_tools(self):
        # Break caught: a narrow override silently keeps the default set too.
        caller = McpToolCaller(
            "canary-secret", allowed_tools=frozenset({"trezor_sign_message"})
        )

        for name in ("trezor_send_transaction", "trezor_sign_typed_data"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable."):
                    caller(name, {"coin": "base"})

    def test_caller_repr_redacts_token_and_fixed_url(self):
        representation = repr(McpToolCaller("canary-secret"))

        self.assertEqual(representation, "McpToolCaller(timeout_seconds=120.0)")
        self.assertNotIn("canary-secret", representation)
        self.assertNotIn("127.0.0.1:21340", representation)

    def test_caller_discovers_required_tool_on_later_page(self):
        transport = FakeMcpTransport(
            pages=[
                SimpleNamespace(tools=[SimpleNamespace(name="other")], nextCursor="page-2"),
                SimpleNamespace(
                    tools=[SimpleNamespace(name="trezor_get_address")], nextCursor=None
                ),
            ],
            result=SimpleNamespace(isError=False, structuredContent={"address": "0x1"}),
        )

        with patch.dict(sys.modules, transport.modules()):
            output = McpToolCaller("canary-secret")("trezor_get_address", {"coin": "base"})

        self.assertEqual(output, {"address": "0x1"})
        self.assertEqual(transport.list_cursors, [None, "page-2"])
        self.assertEqual(transport.tool_calls, [("trezor_get_address", {"coin": "base"})])
        self.assertEqual(transport.stream_calls[0][0], "http://127.0.0.1:21340/mcp")
        self.assertEqual(
            transport.http_clients[0]["headers"], {"Authorization": "Bearer canary-secret"}
        )
        self.assertIs(transport.http_clients[0]["trust_env"], False)

    def test_caller_skips_tool_call_when_no_discovery_page_has_required_tool(self):
        transport = FakeMcpTransport(
            pages=[
                SimpleNamespace(tools=[SimpleNamespace(name="other")], nextCursor="page-2"),
                SimpleNamespace(tools=[SimpleNamespace(name="still-other")], nextCursor=None),
            ]
        )

        with patch.dict(sys.modules, transport.modules()):
            with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable."):
                McpToolCaller("canary-secret")("trezor_get_address", {"coin": "base"})

        self.assertEqual(transport.list_cursors, [None, "page-2"])
        self.assertEqual(transport.tool_calls, [])

    def test_caller_suppresses_transport_failure_cause(self):
        transport = FakeMcpTransport([], failure=RuntimeError("canary transport detail"))

        with patch.dict(sys.modules, transport.modules()):
            with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable.") as raised:
                McpToolCaller("canary-secret")("trezor_get_address", {"coin": "base"})

        self.assertIsNone(raised.exception.__cause__)
