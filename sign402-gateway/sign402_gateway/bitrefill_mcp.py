import asyncio
import json
from copy import deepcopy
from typing import Any

import httpx
import toons
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MAX_MCP_RESPONSE_BYTES = 1024 * 1024


def decode_mcp_tool_result(
    result: Any,
    *,
    max_bytes: int = MAX_MCP_RESPONSE_BYTES,
) -> dict[str, Any]:
    if bool(getattr(result, "isError", False)):
        raise ValueError("Bitrefill MCP tool failed")

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return deepcopy(structured)

    text = "\n".join(
        str(block.text)
        for block in getattr(result, "content", [])
        if hasattr(block, "text")
    ).strip()
    if len(text.encode("utf-8")) > int(max_bytes):
        raise ValueError("Bitrefill MCP response is too large")

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        try:
            decoded = toons.loads(text)
        except Exception as exc:
            raise ValueError("Bitrefill MCP returned malformed data") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Bitrefill MCP returned a non-object response")
    return decoded


class McpToolCaller:
    def __init__(
        self,
        server_url: str,
        *,
        timeout_seconds: float = 60.0,
        max_response_bytes: int = MAX_MCP_RESPONSE_BYTES,
    ):
        url = str(server_url).strip()
        if not url:
            raise ValueError("Bitrefill MCP server URL is required")
        self._server_url = url
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("Bitrefill MCP timeout must be positive")
        self.max_response_bytes = int(max_response_bytes)
        if self.max_response_bytes <= 0:
            raise ValueError("Bitrefill MCP response limit must be positive")

    def __repr__(self) -> str:
        return (
            "McpToolCaller(server_url='<redacted>', "
            f"timeout_seconds={self.timeout_seconds})"
        )

    def __call__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return asyncio.run(self._call(tool_name, arguments))
        except Exception as exc:
            raise ValueError("Bitrefill MCP request failed") from exc

    async def _call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
        ) as http_client:
            async with streamable_http_client(
                self._server_url,
                http_client=http_client,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if tool_name not in {tool.name for tool in tools.tools}:
                        raise ValueError(
                            f"required Bitrefill MCP tool is unavailable: {tool_name}"
                        )
                    result = await session.call_tool(
                        tool_name,
                        arguments=deepcopy(arguments),
                    )
                    return decode_mcp_tool_result(
                        result,
                        max_bytes=self.max_response_bytes,
                    )
