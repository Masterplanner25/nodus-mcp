"""Phase M tests — concrete server transports (StdioServerTransport, HttpServerTransport).

M wires H–L's transport-agnostic dispatch to real I/O.
Tests confirm: H–L logic runs over real transports, purity gates hold.

Standing assertions:
  M1 (stdio): serve() dispatches real requests from stdin → stdout
  M1: stdin-EOF → clean shutdown, nothing leaks (inverted-B lifecycle)
  M1: no session, no per-connection state
  M2 (http): POST dispatched → JSON-RPC response
  M2: bearer auth — missing/wrong → 401; correct → dispatched
  M2: statelessness — no session between POSTs (transport mirror of H's gate)
  M2: no SSE/push path in HttpServerTransport
  INTEGRATION: L's re-call over real HTTP (two discrete POSTs, requestState,
    tool called twice, final result correct) — the keystone integration proof

All HTTP tests start the server in a background thread; use httpx for requests.
Stdio unit tests use BytesIO + threading (no subprocess needed for unit coverage;
subprocess integration test proves real stdin/stdout works).
"""
import io
import json
import sys
import threading
import time

import httpx
import pytest

from nodus_mcp.server import McpServer
from nodus_mcp.server_transport import StdioServerTransport, HttpServerTransport
from nodus_mcp.codec import McpCodec
from nodus_mcp.protocol.messages import (
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    METHOD_SERVER_DISCOVER,
    RESULT_TYPE_INPUT_REQUIRED,
    ElicitationRequest,
    ToolErrorCategory,
)
from nodus_mcp.protocol.jsonrpc import next_request_id, METHOD_NOT_FOUND


# ── Mock runtime + helpers ────────────────────────────────────────────────────

class MockRegistry:
    def __init__(self, tools: dict, handlers: dict):
        self._tools = tools
        self._handlers = handlers
        self.call_count: dict[str, int] = {}

    def lookup(self, name):
        return self._tools.get(name)

    def list_tools(self):
        return list(self._tools.values())

    def invoke(self, name, args):
        self.call_count[name] = self.call_count.get(name, 0) + 1
        if name not in self._handlers:
            raise KeyError(name)
        return self._handlers[name](args)


class MockRuntime:
    def __init__(self, tools=None, handlers=None):
        self.tool_registry = MockRegistry(tools or {}, handlers or {})


def _entry(name, schema=None):
    return {"name": name, "description": "d", "schema": schema or {}, "deprecated": False}


codec = McpCodec()


def _rpc_frame(method: str, params: dict) -> bytes:
    """Build a newline-terminated JSON-RPC request frame."""
    req_id = next_request_id()
    return codec.encode_request(method, params, id=req_id)


def _parse_response(data: bytes) -> dict:
    return json.loads(data.decode().strip())


# ── M1: StdioServerTransport — unit tests using BytesIO pipes ────────────────

class _PipeBuffer:
    """Thread-safe pipe: one thread writes, another reads via blocking readline()."""

    def __init__(self):
        r, w = __import__("os").pipe()
        self.reader = __import__("os").fdopen(r, "rb", buffering=0)
        self.writer = __import__("os").fdopen(w, "wb", buffering=0)

    def close_write(self):
        try:
            self.writer.close()
        except OSError:
            pass


def _start_stdio_server(server: McpServer, *, stdin, stdout) -> StdioServerTransport:
    """Start StdioServerTransport in a daemon thread; return transport."""
    t = StdioServerTransport(stdin=stdin, stdout=stdout)
    th = threading.Thread(target=t.serve, args=(server.dispatch,), daemon=True)
    th.start()
    return t


def test_m1_stdio_dispatches_request():
    """M1: real stdin/stdout I/O — tools/list request produces a JSON-RPC response."""
    server = McpServer(runtime=MockRuntime(
        tools={"a.tool": _entry("a.tool")},
    ))

    stdin_pipe = _PipeBuffer()
    stdout_pipe = _PipeBuffer()
    t = _start_stdio_server(server, stdin=stdin_pipe.reader, stdout=stdout_pipe.writer)

    # Write a tools/list request
    stdin_pipe.writer.write(_rpc_frame(METHOD_TOOLS_LIST, {}))
    stdin_pipe.writer.flush()

    # Read the response
    response_line = stdout_pipe.reader.readline()
    resp = _parse_response(response_line)

    assert "result" in resp
    assert resp["result"]["tools"][0]["name"] == "a.tool"

    stdin_pipe.close_write()


def test_m1_stdio_discover_request():
    """M1: server/discover over stdio returns capabilities."""
    server = McpServer()
    stdin_pipe = _PipeBuffer()
    stdout_pipe = _PipeBuffer()
    _start_stdio_server(server, stdin=stdin_pipe.reader, stdout=stdout_pipe.writer)

    stdin_pipe.writer.write(_rpc_frame(METHOD_SERVER_DISCOVER, {}))
    stdin_pipe.writer.flush()

    resp = _parse_response(stdout_pipe.reader.readline())
    assert "result" in resp
    assert "tools" in resp["result"]["_meta"]["capabilities"]

    stdin_pipe.close_write()


def test_m1_stdin_eof_clean_shutdown():
    """M1: parent-stdin-EOF → serve() exits cleanly. Nothing leaks.

    Inverted-B lifecycle: B tested child-stdout-EOF → fail waiters.
    M1 tests parent-stdin-EOF → serve() exits (no waiters to drain — L parks nothing).
    """
    server = McpServer()
    stdin_pipe = _PipeBuffer()
    stdout_pipe = _PipeBuffer()

    t = StdioServerTransport(stdin=stdin_pipe.reader, stdout=stdout_pipe.writer)
    done = threading.Event()

    def run():
        t.serve(server.dispatch)
        done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()

    # Close stdin (simulate parent process exiting)
    stdin_pipe.close_write()

    # serve() must exit cleanly within a bounded time
    assert done.wait(timeout=2.0), "StdioServerTransport.serve() did not exit on stdin EOF"
    th.join(timeout=1.0)


def test_m1_malformed_frame_skipped():
    """M1: a malformed frame is skipped; server continues processing subsequent requests."""
    server = McpServer()
    stdin_pipe = _PipeBuffer()
    stdout_pipe = _PipeBuffer()
    _start_stdio_server(server, stdin=stdin_pipe.reader, stdout=stdout_pipe.writer)

    # Write garbage first, then a valid request
    stdin_pipe.writer.write(b"not json at all\n")
    stdin_pipe.writer.write(_rpc_frame(METHOD_SERVER_DISCOVER, {}))
    stdin_pipe.writer.flush()

    # Should get a valid response (the malformed frame was skipped)
    resp = _parse_response(stdout_pipe.reader.readline())
    assert "result" in resp

    stdin_pipe.close_write()


def test_m1_no_session_no_per_connection_state():
    """M1: StdioServerTransport holds no per-request state — config only."""
    t = StdioServerTransport()
    attrs = vars(t)
    for key in attrs:
        assert "session" not in key.lower(), f"Session-like attribute: {key}"
        assert "request_history" not in key.lower()
        assert "client_state" not in key.lower()


# ── M2: HttpServerTransport — HTTP server tests ───────────────────────────────

def _start_http_server(server: McpServer, *, bearer_token=None) -> tuple[HttpServerTransport, int]:
    """Start HttpServerTransport in a daemon thread; return (transport, port)."""
    t = HttpServerTransport("localhost", 0, bearer_token=bearer_token)
    ready = threading.Event()

    def run():
        # Signal readiness after the port is bound (serve() starts blocking after bind)
        # We use a slightly different approach: patch serve to signal after HTTPServer init
        import http.server as hs_mod
        original_init = hs_mod.HTTPServer.__init__

        def patched_init(self_httpd, *args, **kwargs):
            original_init(self_httpd, *args, **kwargs)
            ready.set()

        hs_mod.HTTPServer.__init__ = patched_init
        try:
            t.serve(server.dispatch)
        finally:
            hs_mod.HTTPServer.__init__ = original_init

    th = threading.Thread(target=run, daemon=True)
    th.start()
    ready.wait(timeout=3.0)
    return t, t.port


def test_m2_http_dispatches_request():
    """M2: POST → dispatched through H, response returned in POST reply."""
    server = McpServer(runtime=MockRuntime(
        tools={"echo": _entry("echo")},
        handlers={"echo": lambda args: {"echoed": args}},
    ))
    t, port = _start_http_server(server)
    try:
        resp = httpx.post(
            f"http://localhost:{port}",
            json={"jsonrpc": "2.0", "id": 1, "method": METHOD_TOOLS_CALL,
                  "params": {"name": "echo", "arguments": {"x": 42}}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert data["result"].get("isError") is not True
    finally:
        t.close()


def test_m2_http_tools_list():
    """M2: tools/list over HTTP returns registry."""
    server = McpServer(runtime=MockRuntime(
        tools={"my.tool": _entry("my.tool")},
    ))
    t, port = _start_http_server(server)
    try:
        resp = httpx.post(
            f"http://localhost:{port}",
            json={"jsonrpc": "2.0", "id": 2, "method": METHOD_TOOLS_LIST, "params": {}},
        )
        data = resp.json()
        assert data["result"]["tools"][0]["name"] == "my.tool"
    finally:
        t.close()


# ── M2: Bearer auth ───────────────────────────────────────────────────────────

def test_m2_bearer_missing_returns_401():
    """M2: no Authorization header → HTTP 401 (Decision 15 server side)."""
    server = McpServer()
    t, port = _start_http_server(server, bearer_token="secret")
    try:
        resp = httpx.post(
            f"http://localhost:{port}",
            json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
        )
        assert resp.status_code == 401
    finally:
        t.close()


def test_m2_bearer_wrong_returns_401():
    """M2: wrong bearer token → HTTP 401."""
    server = McpServer()
    t, port = _start_http_server(server, bearer_token="correct-token")
    try:
        resp = httpx.post(
            f"http://localhost:{port}",
            json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401
    finally:
        t.close()


def test_m2_bearer_correct_dispatches():
    """M2: correct bearer token → request dispatched, 200 response."""
    server = McpServer()
    t, port = _start_http_server(server, bearer_token="my-api-key")
    try:
        resp = httpx.post(
            f"http://localhost:{port}",
            json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
            headers={"Authorization": "Bearer my-api-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
    finally:
        t.close()


def test_m2_no_bearer_configured_accepts_all():
    """M2: bearer_token=None → no auth check, all requests accepted."""
    server = McpServer()
    t, port = _start_http_server(server, bearer_token=None)
    try:
        resp = httpx.post(
            f"http://localhost:{port}",
            json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
        )
        assert resp.status_code == 200
    finally:
        t.close()


# ── M2: Statelessness purity ──────────────────────────────────────────────────

def test_m2_no_session_between_posts():
    """M2 statelessness: two independent POSTs; no cross-request state retained.
    Transport-level mirror of H's test_h_no_session_object.
    """
    request_count = [0]

    def counting_handler(method, params, req_id):
        request_count[0] += 1
        server = McpServer()
        return server.dispatch(method, params, req_id)

    t = HttpServerTransport("localhost", 0)
    ready = threading.Event()

    import http.server as _hs
    _orig = _hs.HTTPServer.__init__
    def _patch(self, *a, **kw):
        _orig(self, *a, **kw); ready.set()
    _hs.HTTPServer.__init__ = _patch
    th = threading.Thread(target=t.serve, args=(counting_handler,), daemon=True)
    th.start()
    _hs.HTTPServer.__init__ = _orig
    ready.wait(timeout=3.0)
    port = t.port

    try:
        # Two independent POSTs; each should be self-contained
        for i in range(2):
            resp = httpx.post(
                f"http://localhost:{port}",
                json={"jsonrpc": "2.0", "id": i + 1,
                      "method": "server/discover", "params": {}},
            )
            assert resp.status_code == 200
        assert request_count[0] == 2
    finally:
        t.close()


def test_m2_no_sse_push_path():
    """M2 purity: HttpServerTransport has no SSE or server-push path.
    Mirror of TD-006/TD-007: v0.1 dropped SSE/long-poll.
    """
    import inspect
    import nodus_mcp.server_transport as st_mod
    src = inspect.getsource(st_mod)
    # These should not exist in the transport implementation
    for term in ["EventSource", "text/event-stream", "server-sent", "long.poll",
                 "WebSocket", "websocket"]:
        assert term.lower() not in src.lower(), (
            f"Found server-push term '{term}' in server_transport.py — "
            "SSE/long-poll is deferred to v0.2"
        )


# ── INTEGRATION: L's re-call over real HTTP (the keystone proof) ─────────────

def test_m_integration_l_recall_over_real_http():
    """Keystone integration test: L's server-issued re-call works over real HTTP POSTs.

    Two discrete POSTs, no held connection, no server push:
      POST 1: tools/call → server tool returns ElicitationRequest →
              InputRequiredResult with requestState returned as POST 1 response
      POST 2: tools/call with requestState + inputResponses →
              tool re-invoked with answer injected → final result as POST 2 response

    This validates the response-folded design on actual I/O (not just mocks).
    L proved it over MockHttpTransport in G's analog; M proves it over real HTTP.
    """
    call_log = []

    def two_round_tool(args):
        call_log.append(dict(args))
        if args.get("__elicitation_state__") is None:
            return ElicitationRequest(
                input_requests=[{"id": "q1", "message": "What color?"}],
                state={"phase": "asking"},
            )
        answer = args["__elicitation_state__"]["responses"]
        return {"color_chosen": answer, "phase": args["__elicitation_state__"]["state"]["phase"]}

    runtime = MockRuntime(
        tools={"pick.color": _entry("pick.color")},
        handlers={"pick.color": two_round_tool},
    )
    server = McpServer(runtime=runtime)
    t, port = _start_http_server(server)

    try:
        # POST 1: initial tools/call
        resp1 = httpx.post(
            f"http://localhost:{port}",
            json={"jsonrpc": "2.0", "id": 10,
                  "method": METHOD_TOOLS_CALL,
                  "params": {"name": "pick.color", "arguments": {"palette": "primary"}}},
        )
        assert resp1.status_code == 200
        r1 = resp1.json()["result"]
        assert r1["resultType"] == RESULT_TYPE_INPUT_REQUIRED, (
            f"POST 1 must return InputRequiredResult; got: {r1}"
        )
        request_state = r1["requestState"]
        assert request_state, "requestState must be present"

        # POST 2: continuation with requestState
        resp2 = httpx.post(
            f"http://localhost:{port}",
            json={"jsonrpc": "2.0", "id": 11,
                  "method": METHOD_TOOLS_CALL,
                  "params": {
                      "name": "pick.color",
                      "arguments": {"palette": "primary"},
                      "requestState": request_state,
                      "inputResponses": [{"id": "q1", "content": {"color": "blue"}}],
                  }},
        )
        assert resp2.status_code == 200
        r2 = resp2.json()["result"]
        assert r2.get("isError") is not True, f"Final result must not be an error: {r2}"

        # Tool was called exactly twice — re-call, not resume
        assert runtime.tool_registry.call_count.get("pick.color") == 2, (
            "Tool must be invoked twice across the two POSTs"
        )
        # Call 1: no injection
        assert "__elicitation_state__" not in call_log[0]
        # Call 2: injection present
        assert "__elicitation_state__" in call_log[1]
        assert call_log[1]["__elicitation_state__"]["state"]["phase"] == "asking"

    finally:
        t.close()


def test_m_integration_l_recall_over_real_stdio():
    """Integration: L's re-call works over real stdio pipes (subprocess roundtrip).

    Spawns a Python subprocess that runs StdioServerTransport with a sentinel
    tool. Exchanges two JSON-RPC frames; asserts the re-call works on real I/O.
    """
    _SCRIPT = """
import sys, os
sys.path.insert(0, os.environ.get("NODUS_SRC", ""))
from nodus_mcp.server import McpServer
from nodus_mcp.server_transport import StdioServerTransport
from nodus_mcp.protocol.messages import ElicitationRequest

call_count = 0

def handler(args):
    global call_count
    call_count += 1
    if args.get("__elicitation_state__") is None:
        return ElicitationRequest(input_requests=[{"id":"q1"}], state={"n": call_count})
    return {"done": True, "state_n": args["__elicitation_state__"]["state"]["n"]}

class FakeRegistry:
    def lookup(self, n): return {"name": n, "schema": {}, "description": "d", "deprecated": False}
    def list_tools(self): return []
    def invoke(self, n, a): return handler(a)

class FakeRuntime:
    tool_registry = FakeRegistry()

server = McpServer(runtime=FakeRuntime())
StdioServerTransport().serve(server.dispatch)
"""
    import subprocess
    nodus_src = str(__import__("pathlib").Path(__file__).parent.parent / "src")
    proc = subprocess.Popen(
        [sys.executable, "-c", _SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**__import__("os").environ, "NODUS_SRC": nodus_src},
    )

    try:
        c = McpCodec()
        # Round 1: initial call
        proc.stdin.write(c.encode_request("tools/call",
                         {"name": "t", "arguments": {}}, id=1))
        proc.stdin.flush()
        r1 = json.loads(proc.stdout.readline())
        assert r1["result"]["resultType"] == RESULT_TYPE_INPUT_REQUIRED
        rs = r1["result"]["requestState"]

        # Round 2: continuation
        proc.stdin.write(c.encode_request("tools/call", {
            "name": "t", "arguments": {},
            "requestState": rs,
            "inputResponses": [{"id": "q1", "content": True}],
        }, id=2))
        proc.stdin.flush()
        r2 = json.loads(proc.stdout.readline())
        assert r2["result"].get("isError") is not True
        result_text = r2["result"]["content"][0]["text"]
        result_data = json.loads(result_text)
        assert result_data["done"] is True

    finally:
        proc.stdin.close()
        proc.wait(timeout=3.0)
