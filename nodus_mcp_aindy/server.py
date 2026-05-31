"""NodusServer — expose a ToolRegistry as an MCP server.

Uses the low-level ``mcp.server.Server`` API for full JSON Schema control.
Each ToolDefinition's ``input_schema`` is passed verbatim as the MCP tool's
``inputSchema``, preserving structured parameter definitions.

Usage::

    from nodus_mcp import ToolDefinition, ToolRegistry, NodusServer

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="nodus_memory_read",
        description="Recall memory nodes",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=lambda args: {"nodes": [], "count": 0},
    ))

    server = NodusServer(registry)
    server.run(transport="stdio")   # blocks

In-process testing::

    import anyio, asyncio
    from mcp import ClientSession

    async def test():
        send1, recv1 = anyio.create_memory_object_stream(100)
        send2, recv2 = anyio.create_memory_object_stream(100)
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.run_async, recv1, send2)
            async with ClientSession(recv2, send1) as session:
                await session.initialize()
                tools = await session.list_tools()
                tg.cancel_scope.cancel()
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from .tool import ToolDefinition, ToolRegistry

logger = logging.getLogger(__name__)

try:
    import mcp.types as _types
    from mcp.server import Server as _Server

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    _Server = None
    _types = None


class NodusServer:
    """Expose a ``ToolRegistry`` as an MCP server.

    Args:
        registry:   ``ToolRegistry`` to expose.
        name:       MCP server name shown to clients.
        version:    Server version string.
        auth_hook:  Optional callable invoked before each tool call.
                    Signature: ``(tool_name: str, args: dict, meta: dict) → None``.
                    Raise any exception to deny the call.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        name: str = "nodus",
        version: str = "0.1.0",
        auth_hook: Optional[Callable[[str, dict, dict], None]] = None,
    ) -> None:
        if not _MCP_AVAILABLE:
            raise ImportError(
                "mcp package is required for NodusServer. "
                "Install with: pip install nodus-mcp  (mcp is a required dependency)"
            )
        self._registry = registry
        self._name = name
        self._version = version
        self._auth_hook = auth_hook
        self._server: "_Server" = _Server(name)
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        server = self._server
        registry = self._registry
        auth_hook = self._auth_hook

        @server.list_tools()
        async def _list_tools() -> list:
            return [
                _types.Tool(
                    name=t.name,
                    description=t.description,
                    inputSchema=t.input_schema,
                )
                for t in registry.list()
            ]

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict | None) -> list:
            tool = registry.get(name)
            if tool is None:
                raise ValueError(f"Unknown tool: {name!r}")

            args = dict(arguments or {})

            if auth_hook is not None:
                auth_hook(name, args, {})

            try:
                result = tool.handler(args)
            except Exception as exc:
                logger.warning("[NodusServer] Tool %r raised: %s", name, exc)
                raise

            # MCP returns text content; serialise non-string results
            if isinstance(result, str):
                text = result
            elif isinstance(result, dict):
                text = json.dumps(result)
            else:
                text = str(result)

            return [_types.TextContent(type="text", text=text)]

    def get_tool_count(self) -> int:
        """Return the number of (non-deprecated) tools in the registry."""
        return len(self._registry.list())

    async def run_async(self, read_stream: Any, write_stream: Any) -> None:
        """Run the server on pre-created anyio memory streams.

        Useful for in-process testing::

            send1, recv1 = anyio.create_memory_object_stream(100)
            send2, recv2 = anyio.create_memory_object_stream(100)
            await server.run_async(recv1, send2)
        """
        opts = self._server.create_initialization_options()
        await self._server.run(read_stream, write_stream, opts)

    def run(self, *, transport: str = "stdio") -> None:
        """Run the MCP server (blocking).

        Args:
            transport: ``"stdio"`` (default) or ``"sse"``.

        For production SSE deployment, use ``run_sse_app()`` with uvicorn.
        """
        import anyio

        if transport == "stdio":
            from mcp.server.stdio import stdio_server

            async def _run_stdio() -> None:
                async with stdio_server() as (read, write):
                    opts = self._server.create_initialization_options()
                    await self._server.run(read, write, opts)

            anyio.run(_run_stdio)

        elif transport == "sse":
            raise NotImplementedError(
                "SSE transport: use NodusServer.run_sse_app() with uvicorn/starlette. "
                "Example: uvicorn mymodule:sse_app --host 0.0.0.0 --port 8080"
            )
        else:
            raise ValueError(f"Unknown transport: {transport!r}. Use 'stdio' or 'sse'.")

    def run_sse_app(self) -> Any:
        """Return a Starlette application for SSE transport.

        Usage::

            import uvicorn
            server = NodusServer(registry)
            uvicorn.run(server.run_sse_app(), host="0.0.0.0", port=8080)
        """
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.routing import Route

        sse = SseServerTransport("/messages/")

        async def _handle_sse(request: Any) -> Any:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as (read, write):
                opts = self._server.create_initialization_options()
                await self._server.run(read, write, opts)

        return Starlette(routes=[Route("/sse", endpoint=_handle_sse)])
