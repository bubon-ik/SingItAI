import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .errors import SafeError


_MCP_URL = "http://127.0.0.1:21340/mcp"
_UNAVAILABLE_CODE = "trezor_unavailable"
_UNAVAILABLE_MESSAGE = "Trezor Suite is unavailable."

ALLOWED_TOOLS = frozenset({
    "trezor_get_address",
    "trezor_sign_typed_data",
    "trezor_send_transaction",
    "trezor_push_transaction",
})


def _unavailable() -> SafeError:
    return SafeError(_UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE, 503)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _bounded_bytes(value: str, max_bytes: int) -> None:
    if len(value.encode("utf-8")) > max_bytes:
        raise _unavailable()


def decode_tool_result(result: Any, max_bytes: int = 65536) -> dict[str, Any]:
    """Decode only a bounded mapping result from an MCP tool response."""
    try:
        if max_bytes < 0 or _field(result, "isError", False):
            raise _unavailable()

        structured = _field(result, "structuredContent")
        if isinstance(structured, Mapping):
            encoded = json.dumps(structured, separators=(",", ":"))
            _bounded_bytes(encoded, max_bytes)
            return dict(structured)

        content = _field(result, "content", ())
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            raise _unavailable()
        for item in content:
            if _field(item, "type") != "text":
                continue
            text = _field(item, "text")
            if not isinstance(text, str):
                raise _unavailable()
            _bounded_bytes(text, max_bytes)
            decoded = json.loads(text)
            if isinstance(decoded, Mapping):
                return dict(decoded)
            raise _unavailable()
    except SafeError:
        raise
    except Exception:
        raise _unavailable() from None
    raise _unavailable()


class McpToolCaller:
    def __init__(self, token: str, timeout_seconds: float = 120.0):
        self._token = token
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"McpToolCaller(timeout_seconds={self.timeout_seconds})"

    def __call__(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in ALLOWED_TOOLS:
            raise _unavailable()
        try:
            return asyncio.run(self._call_async(name, arguments))
        except SafeError:
            raise
        except Exception:
            raise _unavailable() from None

    async def _call_async(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": f"Bearer {self._token}"}
        async with httpx.AsyncClient(
            headers=headers, timeout=self.timeout_seconds
        ) as http_client:
            async with streamable_http_client(
                _MCP_URL, http_client=http_client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    await self._confirm_tool(session, name)
                    result = await session.call_tool(name, arguments)
        return decode_tool_result(result)

    @staticmethod
    async def _confirm_tool(session: Any, name: str) -> None:
        cursor = None
        seen_cursors = set()
        while True:
            tools = await session.list_tools(cursor=cursor)
            if McpToolCaller._contains_tool(_field(tools, "tools", ()), name):
                return
            next_cursor = _field(tools, "nextCursor")
            if next_cursor is None:
                raise _unavailable()
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                raise _unavailable()
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    @staticmethod
    def _contains_tool(tools: Any, name: str) -> bool:
        if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
            return False
        available = {_field(tool, "name") for tool in tools}
        return name in available


class TrezorMcpClient:
    def __init__(self, call_tool: Callable[[str, dict[str, Any]], dict[str, Any]]):
        self._call_tool = call_tool

    def _invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in ALLOWED_TOOLS:
            raise _unavailable()
        return self._call_tool(name, arguments)

    def get_base_address(self, path: str) -> dict[str, Any]:
        return self._invoke("trezor_get_address", {
            "coin": "base",
            "path": path,
            "showOnTrezor": True,
        })

    def sign_typed_data(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._invoke("trezor_sign_typed_data", {
            "coin": "base",
            "path": path,
            "data": data,
        })

    def sign_base_transaction(
        self, path: str, to: str, data: str
    ) -> dict[str, Any]:
        return self._invoke("trezor_send_transaction", {
            "coin": "base",
            "path": path,
            "to": to,
            "data": data,
            "chainId": 8453,
            "value": "0",
            "broadcast": False,
        })

    def push_base_transaction(self, tx: str) -> dict[str, Any]:
        return self._invoke("trezor_push_transaction", {"coin": "base", "tx": tx})
