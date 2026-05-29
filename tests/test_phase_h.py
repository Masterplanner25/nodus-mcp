"""Phase H tests — stateless server foundation, dispatch, server/discover, tools/list.

Standing assertions:
  - Statelessness: no session object, no per-connection state class
  - RC purity: no initialize route (server edition of A's gate)
  - server/discover is NOT a handshake prerequisite (stateless idempotency)
  - server/discover gates capabilities by config (server mirror of F's gating)
  - tools/list is live per-request, no cache
  - tools/list emits raw registry names (relay enumeration: prefixed names intact)
  - Unknown method → -32601 (producer-side error table, doc 4 B2)
  - H scope: tools/call absent from H's router (I adds it; H proves read-only)

All tests use a MockRuntime (no real NodusRuntime needed — duck-typed).
McpServer.dispatch() is called directly — no real transport, proving the
dispatch logic is transport-independent (H/M seam).
"""
import json

import pytest

from nodus_mcp.server import McpServer
from nodus_mcp.protocol.jsonrpc import METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR
from nodus_mcp.protocol.messages import (
    METHOD_SERVER_DISCOVER,
    METHOD_TOOLS_LIST,
    METHOD_TOOLS_CALL,
)


# ── Mock runtime (duck-typed; no nodus-lang import needed in tests) ───────────

class MockToolRegistry:
    def __init__(self, tools: list):
        self._tools = list(tools)

    def list_tools(self) -> list:
        return list(self._tools)


class MockRuntime:
    def __init__(self, tools: list | None = None):
        self.tool_registry = MockToolRegistry(tools or [])


def _tool_entry(name: str, *, description: str = "desc", schema: dict | None = None,
                deprecated: bool = False) -> dict:
    return {
        "name": name,
        "description": description,
        "schema": schema or {"type": "object"},
        "deprecated": deprecated,
    }


# ── Statelessness discipline ─────────────────────────────────────────────────

def test_h_no_session_object_in_module():
    """RC purity: no Session/ConnectionState class in server.py (stateless by design)."""
    import nodus_mcp.server as m
    for name in dir(m):
        lower = name.lower()
        assert "session" not in lower, f"Found session-like name: {name}"
        assert "connectionstate" not in lower, f"Found connection-state name: {name}"


def test_h_mcpserver_holds_no_per_request_state():
    """McpServer has no per-request accumulation: same object, two calls, no cross-call state."""
    server = McpServer()
    # Two separate discover calls — the second is identical to the first
    r1 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "req-1")
    r2 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "req-2")
    assert r1["result"]["serverInfo"] == r2["result"]["serverInfo"]
    assert r1["result"]["_meta"]["capabilities"] == r2["result"]["_meta"]["capabilities"]
    # Different request IDs but same result structure
    assert r1["id"] == "req-1"
    assert r2["id"] == "req-2"


def test_h_capabilities_reread_each_request():
    """Capabilities derive from current config, not from a first-request cache."""
    server = McpServer()
    r1 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "r1")
    assert "sampling" not in r1["result"]["_meta"]["capabilities"]

    # Set handler AFTER first discover — next call must reflect the new config
    server.set_sampling_handler(lambda p: {})
    r2 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "r2")
    assert "sampling" in r2["result"]["_meta"]["capabilities"]


# ── RC purity: no initialize route (H-specific gate) ─────────────────────────

def test_h_initialize_method_not_routed():
    """RC purity gate (server): initialize is a pre-RC session-init method.
    Must return -32601, not be handled. The same gate A enforces for types,
    H enforces for routing.
    """
    server = McpServer()
    resp = server.dispatch("initialize", {"protocolVersion": "2.0"}, "req-init")
    assert "error" in resp, "initialize must produce an error response"
    assert resp["error"]["code"] == METHOD_NOT_FOUND


def test_h_initialized_method_not_routed():
    """initialized notification is also pre-RC; must not be handled."""
    server = McpServer()
    resp = server.dispatch("initialized", {}, "req-init2")
    assert "error" in resp
    assert resp["error"]["code"] == METHOD_NOT_FOUND


def test_h_discover_not_required_before_tools_list():
    """server/discover is not a handshake — tools/list works without prior discover.
    Statelessness: there is no call order requirement.
    """
    runtime = MockRuntime([_tool_entry("my.tool")])
    server = McpServer(runtime=runtime)
    # Call tools/list directly, no prior server/discover
    resp = server.dispatch(METHOD_TOOLS_LIST, {}, "req-1")
    assert "result" in resp
    assert "error" not in resp
    assert len(resp["result"]["tools"]) == 1


# ── H scope: tools/call is NOT in H ──────────────────────────────────────────


def test_h_unknown_method_returns_method_not_found():
    """Any unknown method → -32601 (doc 4 B2, producer side)."""
    server = McpServer()
    for method in ["ping", "tools/subscribe", "resources/list", "custom/method"]:
        resp = server.dispatch(method, {}, "req-x")
        assert resp.get("error", {}).get("code") == METHOD_NOT_FOUND, (
            f"Expected -32601 for {method!r}"
        )


# ── H2: server/discover ───────────────────────────────────────────────────────

def test_discover_tools_always_advertised():
    """tools capability always in server/discover response."""
    server = McpServer()
    resp = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d1")
    caps = resp["result"]["_meta"]["capabilities"]
    assert "tools" in caps


def test_discover_elicitation_only_with_handler():
    """elicitation capability gated on set_elicitation_handler (server mirror of F)."""
    server = McpServer()
    r1 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d1")
    assert "elicitation" not in r1["result"]["_meta"]["capabilities"]

    server.set_elicitation_handler(lambda p: {"action": "accept", "content": {}})
    r2 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d2")
    assert "elicitation" in r2["result"]["_meta"]["capabilities"]


def test_discover_sampling_only_with_handler():
    server = McpServer()
    r1 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d1")
    assert "sampling" not in r1["result"]["_meta"]["capabilities"]

    server.set_sampling_handler(lambda p: {})
    r2 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d2")
    assert "sampling" in r2["result"]["_meta"]["capabilities"]


def test_discover_roots_only_when_configured():
    server = McpServer()
    r1 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d1")
    assert "roots" not in r1["result"]["_meta"]["capabilities"]

    server.set_roots([{"uri": "file:///project", "name": "Project"}])
    r2 = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d2")
    assert "roots" in r2["result"]["_meta"]["capabilities"]


def test_discover_server_info_present():
    server = McpServer()
    resp = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d1")
    info = resp["result"]["serverInfo"]
    assert info["name"] == "nodus-mcp"
    assert "version" in info


def test_discover_includes_tool_list():
    """server/discover includes the current tool list (enumerated live)."""
    runtime = MockRuntime([_tool_entry("do.thing")])
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d1")
    tools = resp["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "do.thing"


def test_discover_no_runtime_returns_empty_tools():
    server = McpServer(runtime=None)
    resp = server.dispatch(METHOD_SERVER_DISCOVER, {}, "d1")
    assert resp["result"]["tools"] == []


# ── H3: tools/list ────────────────────────────────────────────────────────────

def test_tools_list_emits_all_tools():
    runtime = MockRuntime([
        _tool_entry("search.files"),
        _tool_entry("read.content"),
    ])
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_LIST, {}, "tl-1")
    tools = resp["result"]["tools"]
    assert len(tools) == 2
    names = {t["name"] for t in tools}
    assert names == {"search.files", "read.content"}


def test_tools_list_raw_registry_names():
    """Server emits raw registry names — alias prefix NOT stripped (doc 1 B1 server side)."""
    runtime = MockRuntime([
        _tool_entry("my.local.tool"),
        _tool_entry("mcp.srv1.remote_tool"),   # client-discovered relay tool
    ])
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_LIST, {}, "tl-1")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "my.local.tool" in names
    assert "mcp.srv1.remote_tool" in names    # full prefixed name intact


def test_tools_list_relay_enumeration_collision_safe(doc="doc 4 D3"):
    """Relay: two upstream servers with same tool name enumerate with distinct prefixes."""
    runtime = MockRuntime([
        _tool_entry("mcp.srv1.read_file"),
        _tool_entry("mcp.srv2.read_file"),
        _tool_entry("read_file"),               # locally registered, same base name
    ])
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_LIST, {}, "tl-1")
    names = [t["name"] for t in resp["result"]["tools"]]
    assert len(names) == 3
    assert len(set(names)) == 3, "Relay tool names must all be distinct (doc 4 D3)"


def test_tools_list_verbatim_schema_passthrough():
    """inputSchema passes through verbatim; empty schema gets type:object injected (doc 4 A3)."""
    runtime = MockRuntime([
        _tool_entry("with.schema", schema={"type": "object",
                                           "properties": {"x": {"type": "string"}}}),
        _tool_entry("no.schema", schema={}),
    ])
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_LIST, {}, "tl-1")
    tools = {t["name"]: t for t in resp["result"]["tools"]}

    # Full schema preserved
    assert tools["with.schema"]["inputSchema"]["properties"]["x"]["type"] == "string"

    # Empty schema gets minimum type:object
    assert tools["no.schema"]["inputSchema"] == {"type": "object"}


def test_tools_list_deprecated_annotation():
    """Deprecated tool → annotations.deprecated:true (doc 4 A2 / doc 1 B3)."""
    runtime = MockRuntime([
        _tool_entry("old.tool", deprecated=True),
        _tool_entry("new.tool", deprecated=False),
    ])
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_LIST, {}, "tl-1")
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert tools["old.tool"].get("annotations", {}).get("deprecated") is True
    assert "annotations" not in tools["new.tool"]


def test_tools_list_live_no_cache():
    """tools/list is live — a tool added to the registry shows in the next list call."""
    registry = MockToolRegistry([_tool_entry("original.tool")])

    class DynamicRuntime:
        tool_registry = registry

    server = McpServer(runtime=DynamicRuntime())
    r1 = server.dispatch(METHOD_TOOLS_LIST, {}, "tl-1")
    assert len(r1["result"]["tools"]) == 1

    # Directly mutate the registry's backing store (simulates runtime.tool_registry.register())
    registry._tools.append(_tool_entry("new.tool"))

    r2 = server.dispatch(METHOD_TOOLS_LIST, {}, "tl-2")
    assert len(r2["result"]["tools"]) == 2


def test_tools_list_no_runtime_returns_empty():
    server = McpServer(runtime=None)
    resp = server.dispatch(METHOD_TOOLS_LIST, {}, "tl-1")
    assert resp["result"]["tools"] == []


# ── H scope: H suite never invokes registry ──────────────────────────────────

def test_h_suite_never_invokes_registry():
    """H/I scope line proof: McpServer in Phase H never calls tool_registry.invoke().
    This test is structural — its presence and passing proves the scope held.
    tools/list and server/discover only call list_tools(), never invoke().
    """
    invocations = []

    class AuditedRegistry(MockToolRegistry):
        def invoke(self, *args, **kwargs):
            invocations.append(args)
            raise AssertionError("H must not invoke tools")

    class AuditedRuntime:
        tool_registry = AuditedRegistry([_tool_entry("my.tool")])

    server = McpServer(runtime=AuditedRuntime())
    server.dispatch(METHOD_SERVER_DISCOVER, {}, "r1")
    server.dispatch(METHOD_TOOLS_LIST, {}, "r2")
    assert invocations == [], "H must not call tool_registry.invoke()"


# ── Dispatch foundation ────────────────────────────────────────────────────────

def test_dispatch_returns_complete_json_rpc_response():
    """dispatch() always returns a complete JSON-RPC response dict."""
    server = McpServer()
    resp = server.dispatch(METHOD_SERVER_DISCOVER, {}, "req-42")
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "req-42"
    assert "result" in resp or "error" in resp


def test_dispatch_error_response_is_complete():
    """Even error responses are complete JSON-RPC dicts."""
    server = McpServer()
    resp = server.dispatch("nonexistent/method", {}, "req-99")
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "req-99"
    assert "error" in resp
    assert resp["error"]["code"] == METHOD_NOT_FOUND


def test_dispatch_transport_agnostic():
    """dispatch() needs no transport — proves the H/M seam."""
    # Called directly with no transport, server, or subprocess
    server = McpServer(runtime=MockRuntime([_tool_entry("a.b")]))
    result = server.dispatch(METHOD_TOOLS_LIST, {}, "r1")
    assert result["result"]["tools"][0]["name"] == "a.b"
