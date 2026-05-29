"""Phase F tests — Roots, Sampling, Elicitation/create routing, re-entrancy.

Standing assertions for Phase F:
  - Routing third-case: inbound roots/list gets a response, not dropped/waiter-poisoned
  - F1: roots/list returns configured roots; unconfigured → empty list
  - F2: sampling handler invoked; absent handler → {"action":"decline"}
  - F3: elicitation/create → elicitation_handler invoked; absent → decline
  - Capability gating: roots/sampling absent from _meta when unconfigured
  - Re-entrancy: inbound request handled while a tools/call is in-flight; both complete

All concurrency tests use real StdioTransport + real subprocess scripts.
F1/F2/F3 logic tests use mock inbound-handler directly (no subprocess needed
for the handler-dispatch logic itself).
"""
import json
import sys
import threading
import time

import pytest

from nodus_mcp.stdio import StdioTransport
from nodus_mcp.client import McpClient
from nodus_mcp.connection import McpConnection, ActiveElicitationRegistry
from nodus_mcp.protocol.messages import (
    METHOD_ROOTS_LIST,
    METHOD_SAMPLING_CREATE_MESSAGE,
    METHOD_ELICITATION_CREATE,
)
from nodus_mcp.transport import McpTransport, TransportError


# ── Helper: build an inbound handler via a real McpClient ─────────────────────

def _make_client_with_conn(roots=None, sampling_handler=None, elicitation_handler=None):
    """Return (client, conn) with a minimal stub transport."""
    class StubTransport(McpTransport):
        def send_request(self, m, p): return {}
        def send_notification(self, m, p): pass
        def close(self): pass

    client = McpClient()
    if sampling_handler:
        client.set_sampling_handler(sampling_handler)
    if elicitation_handler:
        client.set_elicitation_handler(elicitation_handler)

    conn = McpConnection(
        alias="srv", url="stub://", transport=StubTransport(),
        bearer_token=None, server_info={}, server_capabilities={},
        registered_tools=[], roots=list(roots) if roots else [],
    )
    client._connections["srv"] = conn
    inbound_fn = client._build_inbound_handler(conn)
    return client, conn, inbound_fn


# ── Routing third-case: method+id dispatch in StdioTransport ─────────────────

# Server that emits an inbound roots/list BEFORE responding to the client's request.
# After receiving the client's roots response, it responds to the original request.
# This tests that the client handles the inbound request correctly and that
# the server gets the roots response — all in one request round-trip.
_SERVER_EMIT_INBOUND_ROOTS = """\
import sys, json

line = sys.stdin.readline().strip()
req = json.loads(line)
req_id = req.get("id")

# Emit inbound roots/list before responding to the client's request
inbound = {"jsonrpc":"2.0","id":"srv-r1","method":"roots/list","params":{}}
sys.stdout.write(json.dumps(inbound)+"\\n")
sys.stdout.flush()

# Read the client's roots/list response (sent by our inbound handler daemon thread)
try:
    roots_line = sys.stdin.readline().strip()
    roots_resp = json.loads(roots_line)
    roots_seen = roots_resp.get("result", {}).get("roots", [])
except Exception:
    roots_seen = []

# Now respond to the ORIGINAL client request (same id)
resp = {"jsonrpc":"2.0","id":req_id,"result":{"roots_we_saw": roots_seen}}
sys.stdout.write(json.dumps(resp)+"\\n")
sys.stdout.flush()

try:
    sys.stdin.read()
except:
    pass
"""

def _argv(script):
    return [sys.executable, "-c", script]


def test_routing_third_case_inbound_request_not_dropped():
    """Inbound roots/list (method+id) must get a response, not be silently dropped
    or routed to a pending waiter (the pre-F dispatch bug). The server emits an
    inbound roots/list immediately after receiving our first request; our client
    must respond before the server will answer our second request.
    """
    roots = [{"uri": "file:///project", "name": "Project"}]
    transport = StdioTransport(
        _argv(_SERVER_EMIT_INBOUND_ROOTS),
    )
    # Wire inbound handler before any request
    client = McpClient()
    conn = McpConnection(
        alias="srv", url="stdio://test", transport=transport,
        bearer_token=None, server_info={}, server_capabilities={},
        registered_tools=[], roots=roots,
    )
    client._connections["srv"] = conn
    transport._inbound_request_handler = client._build_inbound_handler(conn)

    try:
        # Single request: server emits inbound roots/list, receives our response
        # in the roots/list response, then replies to this request.
        # If the routing is broken (inbound treated as pending response), the
        # server never receives our roots response and this call hangs.
        resp = transport.send_request("tools/call", {"name": "test"})
        result = resp.get("result", {})
        roots_seen = result.get("roots_we_saw", [])
        assert len(roots_seen) == 1, (
            f"Server should have seen our roots response. Got: {roots_seen!r}"
        )
        assert roots_seen[0]["uri"] == "file:///project"
    finally:
        transport.close()


def test_routing_third_case_dispatch_discriminator():
    """Confirm _dispatch uses method presence as discriminator.
    A message with both 'id' and 'method' must NOT route to the pending map.
    """
    from nodus_mcp.stdio import _Waiter

    # Directly test _dispatch by constructing a StdioTransport with a silent server
    # and injecting messages
    _SILENT = "import sys; sys.stdin.read()"
    transport = StdioTransport(_argv(_SILENT))

    routed_as_inbound = []
    routed_as_response = []

    # Patch _handle_inbound_request to record calls
    orig_handle = transport._handle_inbound_request
    def track_inbound(req_id, method, params):
        routed_as_inbound.append((req_id, method))
        orig_handle(req_id, method, params)
    transport._handle_inbound_request = track_inbound

    try:
        # Inject an inbound request (has both method and id)
        inbound_msg = {"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list", "params": {}}
        transport._dispatch(inbound_msg)
        time.sleep(0.02)

        # Inject a response (has id, no method)
        waiter = _Waiter(result_box=[None])
        with transport._pending_lock:
            transport._pending[42] = waiter
        response_msg = {"jsonrpc": "2.0", "id": 42, "result": {"ok": True}}
        transport._dispatch(response_msg)
        waiter.wake_event.wait(timeout=0.5)

        assert len(routed_as_inbound) == 1, "Inbound request should have been dispatched"
        assert routed_as_inbound[0][1] == "roots/list"
        assert waiter.result_box[0] == response_msg, "Response should have woken the waiter"
        # Waiter should NOT have received the inbound request
        assert waiter.result_box[0] is not inbound_msg
    finally:
        transport.close()


# ── F1: Roots auto-responder ──────────────────────────────────────────────────

def test_f1_roots_list_returns_configured_roots():
    """F1: roots/list inbound request returns the configured roots list."""
    roots = [
        {"uri": "file:///home/user/project", "name": "My Project"},
        {"uri": "file:///data", "name": "Data"},
    ]
    _, _, handler = _make_client_with_conn(roots=roots)
    result = handler(METHOD_ROOTS_LIST, {})
    assert result == {"roots": roots}


def test_f1_roots_list_empty_when_unconfigured():
    """F1: no roots configured → return empty list (not an error)."""
    _, _, handler = _make_client_with_conn(roots=None)
    result = handler(METHOD_ROOTS_LIST, {})
    assert result == {"roots": []}


def test_f1_roots_are_snapshot_at_connect_time():
    """F1: roots list captured at connect; later mutation of original doesn't affect response."""
    original_roots = [{"uri": "file:///a", "name": "A"}]
    _, _, handler = _make_client_with_conn(roots=original_roots)
    original_roots.append({"uri": "file:///b", "name": "B"})  # mutate after connect
    result = handler(METHOD_ROOTS_LIST, {})
    assert len(result["roots"]) == 1  # snapshot, not live reference


# ── F2: Sampling servicing ────────────────────────────────────────────────────

def test_f2_sampling_handler_invoked_with_params():
    """F2: sampling/createMessage → invoke handler with params."""
    received = []
    def sampling_fn(params):
        received.append(params)
        return {"role": "assistant", "content": {"type": "text", "text": "LLM says hi"}}

    _, _, handler = _make_client_with_conn(sampling_handler=sampling_fn)
    params = {"messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}],
              "maxTokens": 100}
    result = handler(METHOD_SAMPLING_CREATE_MESSAGE, params)

    assert result["content"]["text"] == "LLM says hi"
    assert received[0] == params


def test_f2_sampling_absent_handler_declines():
    """F2: no sampling handler → decline (not an error) — doc 5 B3."""
    _, _, handler = _make_client_with_conn(sampling_handler=None)
    result = handler(METHOD_SAMPLING_CREATE_MESSAGE, {"messages": []})
    assert result == {"action": "decline"}


def test_f2_sampling_decline_is_not_error():
    """F2: decline is a result dict, not a raised exception."""
    _, _, handler = _make_client_with_conn(sampling_handler=None)
    # Should return dict, not raise
    result = handler(METHOD_SAMPLING_CREATE_MESSAGE, {})
    assert isinstance(result, dict)
    assert result.get("action") == "decline"


def test_f2_sampling_handler_set_after_connect_reflected():
    """F2: sampling handler may be set after connect (same pattern as elicitation)."""
    client, conn, _ = _make_client_with_conn(sampling_handler=None)

    # No handler yet → decline
    h1 = client._build_inbound_handler(conn)
    result1 = h1(METHOD_SAMPLING_CREATE_MESSAGE, {})
    assert result1 == {"action": "decline"}

    # Set handler after connect → reflected in new handler (late binding via self)
    client.set_sampling_handler(lambda p: {"role": "assistant", "content": {}})
    h2 = client._build_inbound_handler(conn)
    result2 = h2(METHOD_SAMPLING_CREATE_MESSAGE, {})
    assert result2.get("role") == "assistant"


# ── F3: Elicitation/create routing ───────────────────────────────────────────

def test_f3_elicitation_create_invokes_handler():
    """F3: elicitation/create inbound request → invoke elicitation_handler."""
    received = []
    def elicit_fn(params):
        received.append(params)
        return {"action": "accept", "content": {"name": "Alice"}}

    _, _, handler = _make_client_with_conn(elicitation_handler=elicit_fn)
    params = {"message": "Enter your name", "requestedSchema": {"type": "object"}}
    result = handler(METHOD_ELICITATION_CREATE, params)

    assert result["action"] == "accept"
    assert result["content"]["name"] == "Alice"
    assert received[0] == params


def test_f3_elicitation_create_absent_handler_declines():
    """F3: no elicitation handler → decline (same as F2's sampling absent-handler)."""
    _, _, handler = _make_client_with_conn(elicitation_handler=None)
    result = handler(METHOD_ELICITATION_CREATE, {"message": "confirm?"})
    assert result == {"action": "decline"}


def test_f3_elicitation_handler_symmetric_with_sampling():
    """F3: elicitation/create and sampling/createMessage both decline when no handler.
    The absent-handler contract is symmetric for both features (doc 5 C3).
    """
    _, _, handler = _make_client_with_conn()  # no handlers
    e_result = handler(METHOD_ELICITATION_CREATE, {})
    s_result = handler(METHOD_SAMPLING_CREATE_MESSAGE, {})
    assert e_result == {"action": "decline"}
    assert s_result == {"action": "decline"}


# ── Capability gating ─────────────────────────────────────────────────────────

def test_capability_gating_roots_absent_when_not_configured():
    """Roots capability NOT in _meta when no roots configured (doc 5 C3)."""
    client = McpClient()
    meta = client._build_meta()
    assert "roots" not in meta.capabilities


def test_capability_gating_roots_present_when_configured():
    """Roots capability in _meta when a connection has roots."""
    client = McpClient()
    # Simulate a connected server with roots
    class StubT(McpTransport):
        def send_request(self, m, p): return {}
        def send_notification(self, m, p): pass
        def close(self): pass

    conn = McpConnection("s", "u", StubT(), None, {}, {}, [],
                         roots=[{"uri": "file:///x", "name": "X"}])
    client._connections["s"] = conn
    meta = client._build_meta()
    assert "roots" in meta.capabilities


def test_capability_gating_sampling_absent_when_no_handler():
    """Sampling NOT in _meta without a handler."""
    client = McpClient()
    assert "sampling" not in client._build_meta().capabilities


def test_capability_gating_sampling_present_when_handler_set():
    """Sampling in _meta after set_sampling_handler."""
    client = McpClient()
    client.set_sampling_handler(lambda p: {})
    assert "sampling" in client._build_meta().capabilities


def test_capability_gating_tools_and_elicitation_always_present():
    """tools and elicitation always advertised regardless of configuration."""
    client = McpClient()
    meta = client._build_meta()
    assert "tools" in meta.capabilities
    assert "elicitation" in meta.capabilities


# ── Re-entrancy: inbound request while tools/call in-flight ──────────────────

# A server that:
#   1. Receives a tools/call
#   2. Immediately sends an inbound roots/list
#   3. Waits 50ms (simulates slow response time)
#   4. Then sends the tools/call response
_REENTRANT_SERVER = """\
import sys, json, time

line = sys.stdin.readline().strip()
req = json.loads(line)
req_id = req.get("id")

# Send inbound roots/list immediately
inbound = {"jsonrpc":"2.0","id":"inbound-1","method":"roots/list","params":{}}
sys.stdout.write(json.dumps(inbound)+"\\n")
sys.stdout.flush()

# Read the roots/list response
try:
    roots_line = sys.stdin.readline().strip()
    roots_resp = json.loads(roots_line)
except:
    roots_resp = {}

# Short delay then send tools/call response
time.sleep(0.05)
resp = {"jsonrpc":"2.0","id":req_id,"result":{"ok":True,"roots_received": bool(roots_resp)}}
sys.stdout.write(json.dumps(resp)+"\\n")
sys.stdout.flush()

try:
    sys.stdin.read()
except:
    pass
"""


def test_reentrant_inbound_during_tools_call_no_deadlock():
    """Re-entrancy: an in-flight tools/call and an inbound roots/list both complete.

    The inbound handler (daemon thread) writes its response via _send_raw_response
    while the tools/call waiter is blocked in wake_event.wait(). The _write_lock
    is held briefly by the inbound thread; the reader thread holds no lock during
    handler execution. No deadlock.
    """
    roots = [{"uri": "file:///x", "name": "X"}]
    transport = StdioTransport(_argv(_REENTRANT_SERVER))

    client = McpClient()
    conn = McpConnection(
        alias="s", url="stdio://", transport=transport,
        bearer_token=None, server_info={}, server_capabilities={},
        registered_tools=[], roots=roots,
    )
    client._connections["s"] = conn
    transport._inbound_request_handler = client._build_inbound_handler(conn)

    try:
        # This call will have an inbound roots/list handled concurrently
        resp = transport.send_request("tools/call", {"name": "test"})

        assert resp.get("result", {}).get("ok") is True
        # Confirm the server received our roots response
        assert resp["result"].get("roots_received") is True
    finally:
        transport.close()


def test_reentrant_does_not_deadlock_write_lock():
    """Re-entrancy doesn't deadlock: inbound handler uses _write_lock briefly;
    reader never holds _write_lock; caller's send_request released _write_lock
    before blocking on wake_event.wait().
    """
    # This test is intentionally a timing test — if it hangs, that's a deadlock.
    # The 2-second timeout on the transport.close() join backstop catches it.
    completed = threading.Event()

    def run():
        transport = StdioTransport(_argv(_REENTRANT_SERVER))
        roots = [{"uri": "file:///r", "name": "R"}]
        client = McpClient()
        conn = McpConnection("s", "u", transport, None, {}, {}, [], roots=roots)
        client._connections["s"] = conn
        transport._inbound_request_handler = client._build_inbound_handler(conn)
        try:
            resp = transport.send_request("tools/call", {})
            assert resp.get("result", {}).get("ok") is True
        finally:
            transport.close()
        completed.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert completed.wait(timeout=5.0), "Deadlock detected: re-entrant request did not complete"
    t.join(timeout=1.0)


# ── SEP-2322 purity: elicitation/create format ───────────────────────────────

def test_f3_elicitation_create_method_constant_value():
    """SEP-2322 method name: elicitation/create (RC wire name)."""
    assert METHOD_ELICITATION_CREATE == "elicitation/create"


def test_f3_elicitation_create_params_passed_verbatim():
    """F3: params from elicitation/create are passed verbatim to the handler."""
    received = []
    _, _, handler = _make_client_with_conn(
        elicitation_handler=lambda p: (received.append(p), {"action": "accept", "content": {}})[1]
    )
    params = {"message": "Choose", "requestedSchema": {"type": "object",
               "properties": {"choice": {"type": "string"}}}}
    handler(METHOD_ELICITATION_CREATE, params)
    assert received[0]["message"] == "Choose"
    assert "requestedSchema" in received[0]
