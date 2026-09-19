"""The SSE transport, driven end to end through the SDK client (#11).

`test_server.py` speaks to `NodusServer` over in-memory streams, and
`test_run_sse_app_mounts_messages_endpoint` inspects the app's routes. Nothing
ran the SSE app and connected a client to it -- which is how a transport can
ship broken: nodus-mcp-server's `--http` mode answered every request with a 500
for three months while its process started and printed its URL.

This starts `run_sse_app()` under uvicorn on a free port and drives it with the
package's own client adapters -- `discover_tools` and `MCPClientAdapter`, the
two things aindy-runtime calls -- through a real `initialize`, `tools/list` and
`tools/call`. Under either SDK major: the server has one implementation with a
branch per major, and this is what holds both branches to the same contract.
"""

import asyncio
import json
import socket
import threading
import time
import urllib.request

import pytest

from nodus_mcp_aindy import MCPClientAdapter, NodusServer, ToolDefinition, ToolRegistry, discover_tools
from nodus_mcp_aindy.server import _MCP_AVAILABLE, _SDK_V2

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")
pytestmark = pytest.mark.skipif(not _MCP_AVAILABLE, reason="mcp SDK not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="echo",
        description="Echo the query back",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        handler=lambda args: {"echoed": args["q"]},
    ))
    reg.register(ToolDefinition(
        name="fail",
        description="Always raises",
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: (_ for _ in ()).throw(RuntimeError("boom")),
    ))
    return reg


class _Served:
    """`run_sse_app()` under uvicorn on a daemon thread, for one test."""

    def __init__(self, server: NodusServer):
        self.port = _free_port()
        config = uvicorn.Config(server.run_sse_app(), host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                # Any response means the socket is up; the SSE route streams,
                # so ask for something that returns promptly.
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/nothing", timeout=1)
            except urllib.error.HTTPError:
                return self  # 404: the app is answering
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("uvicorn did not come up")

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/sse"


def test_discover_and_call_over_a_real_sse_transport():
    seen = []

    def hook(name, args, meta):
        seen.append(meta)

    server = NodusServer(_registry(), name="e2e", auth_hook=hook)
    with _Served(server) as served:
        tools = asyncio.run(discover_tools(served.url))
        names = {t.name for t in tools}
        assert {"echo", "fail"} <= names, names
        echo = next(t for t in tools if t.name == "echo")
        # The schema must survive the trip: under mcp 2.x a `getattr` on the
        # 1.x attribute name returned {} for every tool, silently (#11).
        assert echo.input_schema.get("required") == ["q"], echo.input_schema

        async def call():
            async with MCPClientAdapter(served.url) as adapter:
                ok = await adapter.call_tool("echo", {"q": "hi"})
                bad = await adapter.call_tool("fail", {})
                return ok, bad

        ok, bad = asyncio.run(call())
    assert ok == {"echoed": "hi"}, ok
    # A handler exception reaches the client as text, not a dropped connection.
    assert "boom" in json.dumps(bad), bad
    # And the auth hook saw real per-call context over the wire (#8), on both
    # majors -- from the SDK contextvar on 1.x, from `ctx` on 2.x.
    assert seen and seen[0].get("session") is not None and "request_id" in seen[0], seen[:1]
    assert any("headers" in m for m in seen), "no transport headers reached the auth hook over SSE"


def test_which_branch_ran_is_visible():
    """Not an assertion about behaviour -- a line in the log naming the SDK
    major this run exercised, so a green CI on one major is not mistaken for
    coverage of both."""
    from importlib.metadata import version

    branch = "2.x add_request_handler" if _SDK_V2 else "1.x decorators"
    print(f"\n[nodus-mcp] mcp {version('mcp')} -- server branch: {branch}")
