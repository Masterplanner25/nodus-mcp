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

#: Which SDK generation is installed (#11). mcp 2.0 removed the decorator
#: registration (`@server.list_tools()` / `@server.call_tool()`) and the
#: `request_context` contextvar; a handler is registered with
#: `add_request_handler(method, params_type, handler)`, takes `(ctx, params)`,
#: and returns a result model itself -- including the `isError` result the 1.x
#: decorator used to build from a raised exception. One question, answered
#: once, here; both branches below are driven by the same tests, which speak
#: to the server through a real `ClientSession`.
_SDK_V2 = bool(_MCP_AVAILABLE and hasattr(_Server, "add_request_handler"))


def _request_meta(server: Any, ctx: Any = None) -> dict:
    """Best-effort per-call context for ``auth_hook``.

    On mcp 2.x the runner hands each handler a ``ServerRequestContext``
    (``ctx``); on 1.x the same fields live on the SDK's per-request
    ``RequestContext`` contextvar the low-level ``Server`` sets while handling
    a call. Either way, surface the pieces a host needs to map a caller to an
    identity:

    - ``request_id`` — opaque per-call id
    - ``session``    — the MCP ``ServerSession`` (stable per connection)
    - ``meta``       — client-supplied ``_meta``, if any
    - ``request``    — the transport request over SSE/HTTP (Starlette ``Request``)
    - ``headers``    — that request's headers as a plain dict (e.g. bearer token)

    Returns ``{}`` when no context is available — e.g. accessed outside a
    request, or an SDK build without ``request_context``. Never raises.
    """
    meta: dict = {}
    rc = ctx
    if rc is None:
        try:
            rc = server.request_context
        except (LookupError, AttributeError):
            return meta
    meta["request_id"] = getattr(rc, "request_id", None)
    meta["session"] = getattr(rc, "session", None)
    client_meta = getattr(rc, "meta", None)
    if client_meta is not None:
        meta["meta"] = client_meta
    request = getattr(rc, "request", None)
    if request is not None:
        meta["request"] = request
        headers = getattr(request, "headers", None)
        if headers is not None:
            try:
                meta["headers"] = dict(headers)
            except Exception:
                pass
    return meta


class NodusServer:
    """Expose a ``ToolRegistry`` as an MCP server.

    Args:
        registry:   ``ToolRegistry`` to expose.
        name:       MCP server name shown to clients.
        version:    Server version string.
        auth_hook:  Optional callable invoked before each tool call.
                    Signature: ``(tool_name: str, args: dict, context: dict) → None``.
                    *context* is a best-effort per-call dict — see
                    :func:`_request_meta`. Over SSE/HTTP it carries
                    ``session``, ``request_id``, ``request`` and ``headers``
                    (e.g. the bearer token), enabling per-session identity
                    mapping; over stdio it carries whatever the SDK exposes
                    (``session``/``request_id``) and is ``{}`` when no request
                    context is active. Raise any exception to deny the call.
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

    def _tools(self) -> list:
        return [
            _types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.input_schema,
            )
            for t in self._registry.list()
        ]

    def _invoke(self, name: str, arguments: Optional[dict], meta: dict) -> list:
        """Run one tool call: auth hook, handler, text content. Raises on
        an unknown tool, a denied call, or a handler error -- each SDK branch
        decides how a raise reaches the client, so this stays transport-free."""
        tool = self._registry.get(name)
        if tool is None:
            raise ValueError(f"Unknown tool: {name!r}")

        args = dict(arguments or {})

        if self._auth_hook is not None:
            self._auth_hook(name, args, meta)

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

    def _setup_handlers(self) -> None:
        server = self._server

        if _SDK_V2:
            # mcp >= 2: (ctx, params) -> result model. A raise inside a
            # handler is a protocol error to the runner, so the `isError`
            # result the 1.x decorator built for us is built here instead --
            # an unknown tool, a denied call and a handler exception all
            # reach the client as `CallToolResult(isError=True)`, as before.
            async def _list_tools_v2(ctx: Any, params: Any) -> Any:
                return _types.ListToolsResult(tools=self._tools())

            async def _call_tool_v2(ctx: Any, params: Any) -> Any:
                try:
                    content = self._invoke(params.name, params.arguments, _request_meta(server, ctx))
                except Exception as exc:
                    return _types.CallToolResult(
                        content=[_types.TextContent(type="text", text=str(exc))],
                        isError=True,
                    )
                return _types.CallToolResult(content=content)

            server.add_request_handler("tools/list", _types.PaginatedRequestParams, _list_tools_v2)
            server.add_request_handler("tools/call", _types.CallToolRequestParams, _call_tool_v2)
            return

        # mcp 1.x: the decorator API. It wraps a raise into an error result
        # and exposes the request context through `server.request_context`.
        @server.list_tools()
        async def _list_tools() -> list:
            return self._tools()

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict | None) -> list:
            return self._invoke(name, arguments, _request_meta(server))

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
        from starlette.responses import Response
        from starlette.routing import Mount, Route

        sse = SseServerTransport("/messages/")

        async def _handle_sse(request: Any) -> Any:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as (read, write):
                opts = self._server.create_initialization_options()
                await self._server.run(read, write, opts)
            # The stream has already been written through `request._send`;
            # Starlette still expects a callable response from a `Route`
            # endpoint, and `None` here is a `TypeError` after every session
            # closes (#11). The SDK's own SSE example returns exactly this.
            return Response()

        # The SSE transport is two-endpoint: clients open the GET event stream
        # at /sse and POST messages back to /messages/. Without the /messages/
        # mount the post-back 404s and the session never initialises (#7).
        return Starlette(
            routes=[
                Route("/sse", endpoint=_handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ]
        )
