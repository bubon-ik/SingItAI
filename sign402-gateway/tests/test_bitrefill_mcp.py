import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sign402_gateway.bitrefill_mcp import McpToolCaller, decode_mcp_tool_result


class FakeText:
    def __init__(self, text):
        self.text = text


class FakeToolResult:
    def __init__(self, *, structured=None, text="", is_error=False):
        self.structuredContent = structured
        self.content = [FakeText(text)] if text else []
        self.isError = is_error


class BitrefillMcpDecodeTests(unittest.TestCase):
    def test_decoder_prefers_structured_content(self):
        result = decode_mcp_tool_result(
            FakeToolResult(structured={"products": [{"id": "steam-usa"}]})
        )

        self.assertEqual(result["products"][0]["id"], "steam-usa")

    def test_decoder_accepts_json_text(self):
        result = decode_mcp_tool_result(
            FakeToolResult(text='{"invoice_id":"invoice_1"}')
        )

        self.assertEqual(result, {"invoice_id": "invoice_1"})

    def test_decoder_accepts_toon_text(self):
        result = decode_mcp_tool_result(
            FakeToolResult(text="products[1]{id,name}:\n  steam-usa,Steam")
        )

        self.assertEqual(result["products"][0]["name"], "Steam")

    def test_decoder_hides_tool_error_text(self):
        with self.assertRaisesRegex(ValueError, "Bitrefill MCP tool failed") as raised:
            decode_mcp_tool_result(FakeToolResult(text="key_123", is_error=True))

        self.assertNotIn("key_123", str(raised.exception))

    def test_decoder_rejects_oversized_response(self):
        with self.assertRaisesRegex(ValueError, "response is too large"):
            decode_mcp_tool_result(
                FakeToolResult(text='{"value":"oversized"}'),
                max_bytes=8,
            )

    def test_decoder_rejects_scalar_response(self):
        with self.assertRaisesRegex(ValueError, "non-object response"):
            decode_mcp_tool_result(FakeToolResult(text='"scalar"'))


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class BitrefillMcpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_caller_initializes_lists_and_calls_named_tool(self):
        session = SimpleNamespace(
            initialize=AsyncMock(),
            list_tools=AsyncMock(
                return_value=SimpleNamespace(
                    tools=[SimpleNamespace(name="search-products")]
                )
            ),
            call_tool=AsyncMock(
                return_value=FakeToolResult(structured={"products": []})
            ),
        )
        session_context = AsyncContext(session)
        transport_context = AsyncContext((Mock(name="read"), Mock(name="write"), Mock()))
        http_context = AsyncContext(Mock(name="http_client"))
        caller = McpToolCaller("https://api.bitrefill.com/mcp/key_123")

        with (
            patch(
                "sign402_gateway.bitrefill_mcp.httpx.AsyncClient",
                return_value=http_context,
            ),
            patch(
                "sign402_gateway.bitrefill_mcp.streamable_http_client",
                return_value=transport_context,
            ) as streamable,
            patch(
                "sign402_gateway.bitrefill_mcp.ClientSession",
                return_value=session_context,
            ),
        ):
            result = await caller._call("search-products", {"query": "Steam"})

        self.assertEqual(result, {"products": []})
        session.initialize.assert_awaited_once_with()
        session.list_tools.assert_awaited_once_with()
        session.call_tool.assert_awaited_once_with(
            "search-products",
            arguments={"query": "Steam"},
        )
        streamable.assert_called_once()

    async def test_tool_caller_rejects_missing_required_tool(self):
        session = SimpleNamespace(
            initialize=AsyncMock(),
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
            call_tool=AsyncMock(),
        )
        caller = McpToolCaller("https://api.bitrefill.com/mcp/key_123")

        with (
            patch(
                "sign402_gateway.bitrefill_mcp.httpx.AsyncClient",
                return_value=AsyncContext(Mock()),
            ),
            patch(
                "sign402_gateway.bitrefill_mcp.streamable_http_client",
                return_value=AsyncContext((Mock(), Mock(), Mock())),
            ),
            patch(
                "sign402_gateway.bitrefill_mcp.ClientSession",
                return_value=AsyncContext(session),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "required Bitrefill MCP tool"):
                await caller._call("buy-products", {})

        session.call_tool.assert_not_awaited()

    def test_tool_caller_repr_redacts_server_url(self):
        caller = McpToolCaller("https://api.bitrefill.com/mcp/key_123")

        self.assertNotIn("key_123", repr(caller))
        self.assertNotIn("api.bitrefill.com", repr(caller))


if __name__ == "__main__":
    unittest.main()
