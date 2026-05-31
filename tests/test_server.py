"""NodusServer tests using in-process anyio memory streams."""
from __future__ import annotations

import json

import anyio
import pytest

from nodus_mcp_aindy import NodusServer, ToolDefinition, ToolRegistry
from mcp import ClientSession


def _make_registry(*tools) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _make_tool(name: str, handler=None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Tool: {name}",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        handler=handler or (lambda args: {"name": name, "args": args}),
    )


async def _run_server_test(server: NodusServer, test_fn):
    """Run *test_fn(session)* with an in-process MCP client."""
    send1, recv1 = anyio.create_memory_object_stream(100)
    send2, recv2 = anyio.create_memory_object_stream(100)

    async with anyio.create_task_group() as tg:
        tg.start_soon(server.run_async, recv1, send2)
        async with ClientSession(recv2, send1) as session:
            await session.initialize()
            await test_fn(session)
        tg.cancel_scope.cancel()


# ── Tool count ────────────────────────────────────────────────────────────────

def test_get_tool_count_empty():
    server = NodusServer(ToolRegistry())
    assert server.get_tool_count() == 0


def test_get_tool_count_with_tools():
    reg = _make_registry(_make_tool("a"), _make_tool("b"), _make_tool("c"))
    server = NodusServer(reg)
    assert server.get_tool_count() == 3


def test_deprecated_tools_excluded_from_count():
    reg = ToolRegistry()
    reg.register(_make_tool("active"))
    reg.register(ToolDefinition(
        name="old",
        description="Deprecated",
        input_schema={"type": "object", "properties": {}},
        handler=lambda _: {},
        deprecated=True,
    ))
    server = NodusServer(reg)
    assert server.get_tool_count() == 1


# ── In-process list_tools ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_tools_returns_all():
    reg = _make_registry(_make_tool("nodus_memory_read"), _make_tool("nodus_flow_run"))
    server = NodusServer(reg)

    async def check(session):
        result = await session.list_tools()
        names = {t.name for t in result.tools}
        assert "nodus_memory_read" in names
        assert "nodus_flow_run" in names
        assert len(result.tools) == 2

    await _run_server_test(server, check)


@pytest.mark.asyncio
async def test_list_tools_schema_preserved():
    tool = ToolDefinition(
        name="my_tool",
        description="Test",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=lambda _: {},
    )
    server = NodusServer(_make_registry(tool))

    async def check(session):
        result = await session.list_tools()
        assert len(result.tools) == 1
        t = result.tools[0]
        assert t.inputSchema["properties"]["query"]["type"] == "string"
        assert "query" in t.inputSchema.get("required", [])

    await _run_server_test(server, check)


# ── In-process call_tool ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_tool_returns_result():
    tool = _make_tool("nodus_memory_read", handler=lambda args: {"nodes": [], "query": args.get("q")})
    server = NodusServer(_make_registry(tool))

    async def check(session):
        result = await session.call_tool("nodus_memory_read", {"q": "hello"})
        assert result.content
        data = json.loads(result.content[0].text)
        assert data["query"] == "hello"

    await _run_server_test(server, check)


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error():
    server = NodusServer(ToolRegistry())

    async def check(session):
        # MCP returns an error result, not a Python exception
        result = await session.call_tool("nonexistent", {})
        assert result.isError is True

    await _run_server_test(server, check)


# ── auth_hook ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_hook_called():
    calls = []

    def hook(name, args, meta):
        calls.append((name, args))

    tool = _make_tool("secure_tool")
    server = NodusServer(_make_registry(tool), auth_hook=hook)

    async def check(session):
        await session.call_tool("secure_tool", {"x": "1"})
        assert len(calls) == 1
        assert calls[0][0] == "secure_tool"
        assert calls[0][1]["x"] == "1"

    await _run_server_test(server, check)


@pytest.mark.asyncio
async def test_auth_hook_exception_returns_error():
    def deny_hook(name, args, meta):
        raise PermissionError("Access denied")

    tool = _make_tool("restricted")
    server = NodusServer(_make_registry(tool), auth_hook=deny_hook)

    async def check(session):
        # auth hook exception becomes an MCP error result
        result = await session.call_tool("restricted", {})
        assert result.isError is True

    await _run_server_test(server, check)
