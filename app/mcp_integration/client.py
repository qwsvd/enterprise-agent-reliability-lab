from __future__ import annotations

from copy import deepcopy
from typing import Any

import anyio
from mcp import Client
from mcp.types import TextContent, Tool


class MCPClientError(RuntimeError):
    """The MCP server could not be reached or used for discovery."""


class MCPToolAdapter:
    """Adapt MCP-discovered tools to the synchronous Phase 2 tool interface."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.protocol_version: str | None = None
        self._tools: dict[str, Tool] = {}

    async def _discover(self) -> tuple[list[Tool], str]:
        discovered: list[Tool] = []
        async with Client(self.server, raise_exceptions=False) as client:
            cursor: str | None = None
            while True:
                page = await client.list_tools(cursor=cursor)
                discovered.extend(page.tools)
                cursor = page.next_cursor
                if cursor is None:
                    break
            return discovered, str(client.protocol_version)

    def discover(self) -> list[dict[str, Any]]:
        try:
            tools, protocol_version = anyio.run(self._discover)
        except Exception as exc:
            raise MCPClientError(f"MCP tool discovery failed: {exc}") from exc
        self._tools = {tool.name: tool for tool in tools}
        self.protocol_version = protocol_version
        return self.schemas()

    def schemas(self) -> list[dict[str, Any]]:
        if not self._tools:
            raise MCPClientError("MCP tools have not been discovered")
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": deepcopy(tool.input_schema),
                },
            }
            for tool in self._tools.values()
        ]

    async def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout_seconds: float | None,
    ) -> Any:
        if timeout_seconds is None:
            async with Client(self.server, raise_exceptions=False) as client:
                return await client.call_tool(name, arguments=arguments)
        with anyio.fail_after(timeout_seconds):
            async with Client(self.server, raise_exceptions=False) as client:
                return await client.call_tool(name, arguments=arguments)

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if name not in self._tools:
            return self._failure("unknown_mcp_tool", f"Unknown MCP tool: {name}")
        if not isinstance(arguments, dict):
            return self._failure("invalid_arguments", "MCP tool arguments must be an object")
        try:
            result = anyio.run(self._call, name, arguments, timeout_seconds)
        except TimeoutError:
            return self._failure(
                "tool_timeout",
                f"MCP tool {name} exceeded its timeout",
                retryable=True,
                ambiguous=True,
            )
        except Exception as exc:
            return self._failure(
                "mcp_connection_or_protocol_error",
                str(exc),
                retryable=True,
                ambiguous=True,
            )
        if result.is_error:
            message = "\n".join(
                block.text for block in result.content if isinstance(block, TextContent)
            ) or f"MCP tool {name} failed"
            return self._failure("mcp_tool_error", message)
        structured = result.structured_content
        if not isinstance(structured, dict):
            return self._failure("malformed_mcp_result", "MCP result has no usable structured content")
        if structured.get("ok") is True and isinstance(structured.get("data"), dict):
            return structured
        if structured.get("ok") is False and isinstance(structured.get("error"), dict):
            return structured
        return self._failure("malformed_mcp_result", "MCP structured result has an invalid shape")

    @staticmethod
    def _failure(
        kind: str,
        message: str,
        *,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"type": kind, "message": message}
        if retryable:
            error["retryable"] = True
        if ambiguous:
            error["ambiguous"] = True
        return {"ok": False, "error": error}

