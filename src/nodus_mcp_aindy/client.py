"""MCPClientAdapter — discover MCP server tools as ToolDefinitions.

Usage::

    from nodus_mcp import MCPClientAdapter, ToolRegistry

    async with MCPClientAdapter("http://my-mcp-server/sse") as adapter:
        tools = await adapter.list_tools()
        for t in tools:
            print(t.name, t.description)
        result = await adapter.call_tool("nodus_memory_read", {"query": "auth"})

Or, for one-shot discovery::

    from nodus_mcp import discover_tools
    tools = await discover_tools("http://my-mcp-server/sse")
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .schema import json_schema_to_lightweight
from .tool import ToolDefinition

logger = logging.getLogger(__name__)

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    ClientSession = None
    sse_client = None


def _mcp_tool_to_definition(
    tool: Any,
    *,
    server_url: str,
    adapter: "MCPClientAdapter",
) -> ToolDefinition:
    """Convert an ``mcp.types.Tool`` to a ``ToolDefinition`` that calls back to *adapter*."""
    name = tool.name
    description = str(getattr(tool, "description", "") or "")
    raw_schema = getattr(tool, "inputSchema", None) or {}

    async def _remote_handler(args: dict[str, Any]) -> dict[str, Any]:
        return await adapter.call_tool(name, args)

    # Provide a sync-compatible wrapper for callers that prefer sync handlers.
    # (Async callers should use adapter.call_tool directly.)
    def _sync_handler(args: dict[str, Any]) -> dict[str, Any]:
        import asyncio
        return asyncio.get_event_loop().run_until_complete(_remote_handler(args))

    # Prefer async handler; callers that need sync can use _sync_handler.
    # Store both on the ToolDefinition via a thin wrapper that auto-detects context.
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=raw_schema if isinstance(raw_schema, dict) else {},
        handler=_sync_handler,
        source=f"mcp://{server_url}",
        stable=True,
        deprecated=False,
    )


class MCPClientAdapter:
    """Persistent MCP client that maintains a connection to one server.

    The adapter is an async context manager::

        async with MCPClientAdapter("http://server/sse") as adapter:
            tools = await adapter.list_tools()
            result = await adapter.call_tool("tool_name", {})

    Args:
        server_url:  SSE endpoint URL (e.g. ``"http://localhost:8080/sse"``).
        name:        Client identity name (default: ``"nodus-client"``).
        timeout:     SSE connection timeout in seconds (default: 10).
    """

    def __init__(
        self,
        server_url: str,
        *,
        name: str = "nodus-client",
        timeout: float = 10.0,
    ) -> None:
        if not _MCP_AVAILABLE:
            raise ImportError(
                "mcp package is required for MCPClientAdapter. "
                "Install with: pip install nodus-mcp"
            )
        self._server_url = server_url
        self._name = name
        self._timeout = timeout
        self._session: Optional["ClientSession"] = None
        self._context_stack: Any = None

    async def connect(self) -> None:
        """Open the SSE connection and initialise the MCP session."""
        self._ctx = sse_client(self._server_url, timeout=self._timeout)
        read, write = await self._ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        logger.info("[MCPClientAdapter] Connected to %s", self._server_url)

    async def disconnect(self) -> None:
        """Close the MCP session and SSE connection."""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._ctx = None
        logger.info("[MCPClientAdapter] Disconnected from %s", self._server_url)

    async def list_tools(self) -> list[ToolDefinition]:
        """Return all tools exposed by the remote MCP server as ToolDefinitions."""
        if self._session is None:
            raise RuntimeError("Not connected. Call connect() first or use as async context manager.")
        result = await self._session.list_tools()
        return [
            _mcp_tool_to_definition(t, server_url=self._server_url, adapter=self)
            for t in result.tools
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call a remote tool by name.

        Args:
            name: MCP tool name.
            args: Tool arguments dict.

        Returns:
            Parsed dict result (attempts JSON decode of text content).
        """
        if self._session is None:
            raise RuntimeError("Not connected. Call connect() first.")
        result = await self._session.call_tool(name, args)
        if not result.content:
            return {}
        text = result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"result": text}

    async def __aenter__(self) -> "MCPClientAdapter":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()


async def discover_tools(
    server_url: str,
    *,
    timeout: float = 10.0,
) -> list[ToolDefinition]:
    """Connect to an MCP server, discover its tools, and disconnect.

    Each discovered tool's handler calls back to the remote server.
    Note: the handler creates a fresh connection per call.

    Args:
        server_url: SSE endpoint URL.
        timeout:    Connection timeout in seconds.

    Returns:
        List of ``ToolDefinition`` objects.
    """
    async with MCPClientAdapter(server_url, timeout=timeout) as adapter:
        return await adapter.list_tools()
