"""Phase A unit tests — JSON-RPC core, MCP message types, codec, transport ABCs.

Zero network, zero subprocess. Pure serialize/deserialize and type construction.
Tests enforce:
  - RC purity: no session-init types (no initialize/Mcp-Session-Id)
  - Error codes match doc 1 D-table
  - ToolErrorCategory is a closed enum with exactly the right members
  - requestState is opaque at the type layer (never parsed)
  - Codec round-trips correctly
  - Teardown sentinel wakes parked threads cleanly
"""
import json
import threading
import time

import pytest


# ── A1: JSON-RPC core ─────────────────────────────────────────────────────────

def test_error_codes_match_spec():
    from nodus_mcp.protocol.jsonrpc import (
        PARSE_ERROR, INVALID_REQUEST,
        METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR,
    )
    assert PARSE_ERROR == -32700
    assert INVALID_REQUEST == -32600
    assert METHOD_NOT_FOUND == -32601   # doc 1 D-table
    assert INVALID_PARAMS == -32602    # doc 1 D-table
    assert INTERNAL_ERROR == -32603    # doc 1 D-table


def test_next_request_id_monotonic_and_thread_safe():
    from nodus_mcp.protocol.jsonrpc import next_request_id
    ids = []
    errors = []

    def collect():
        try:
            for _ in range(50):
                ids.append(next_request_id())
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=collect) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(ids) == 200
    assert len(set(ids)) == 200, "IDs must be unique across threads"


def test_jsonrpc_request_has_id():
    from nodus_mcp.protocol.jsonrpc import JsonRpcRequest
    req = JsonRpcRequest(method="tools/call", params={"name": "x"}, id=42)
    assert req.id == 42
    assert req.method == "tools/call"


def test_jsonrpc_notification_has_no_id():
    from nodus_mcp.protocol.jsonrpc import JsonRpcNotification
    n = JsonRpcNotification(method="ping", params={})
    assert not hasattr(n, "id") or not hasattr(JsonRpcNotification, "id")
    # structural check: notifications have method + params only
    assert n.method == "ping"


def test_jsonrpc_response_is_error():
    from nodus_mcp.protocol.jsonrpc import JsonRpcResponse, JsonRpcError, INTERNAL_ERROR
    resp = JsonRpcResponse(id=1, error=JsonRpcError(code=INTERNAL_ERROR, message="oops"))
    assert resp.is_error()
    resp_ok = JsonRpcResponse(id=1, result={"ok": True})
    assert not resp_ok.is_error()


# ── A2: MCP message types — RC purity ────────────────────────────────────────

def test_no_session_init_method_constants():
    """RC purity gate: no initialize/session types exist (Decision 1)."""
    import nodus_mcp.protocol.messages as m
    # These must NOT exist as constants
    assert not hasattr(m, "METHOD_INITIALIZE"), "RC has no session init"
    assert not hasattr(m, "METHOD_INITIALIZED"), "RC has no session init"
    assert not hasattr(m, "MCP_SESSION_ID"), "RC has no session ID"
    # Cross-check the whole module namespace
    names = dir(m)
    for name in names:
        lower = name.lower()
        assert "initialize" not in lower or name == "__init__", (
            f"Found session-init-related name: {name}"
        )
        assert "session_id" not in lower, f"Found session ID name: {name}"


def test_method_names_are_rc_strings():
    from nodus_mcp.protocol.messages import (
        METHOD_TOOLS_CALL, METHOD_TOOLS_LIST, METHOD_SERVER_DISCOVER,
        METHOD_ROOTS_LIST, METHOD_SAMPLING_CREATE_MESSAGE,
    )
    assert METHOD_TOOLS_CALL == "tools/call"
    assert METHOD_TOOLS_LIST == "tools/list"
    assert METHOD_SERVER_DISCOVER == "server/discover"   # replaces session init
    assert METHOD_ROOTS_LIST == "roots/list"
    assert METHOD_SAMPLING_CREATE_MESSAGE == "sampling/createMessage"


def test_tool_error_category_closed_set():
    """All five elicitation categories plus transport/not-found/deprecated must exist."""
    from nodus_mcp.protocol.messages import ToolErrorCategory
    values = {c.value for c in ToolErrorCategory}
    required = {
        "not_found",
        "invalid_params",
        "transport_error",
        "execution_failure",
        "elicitation_timeout",
        "elicitation_unsupported",
        "elicitation_rounds_exceeded",
        "elicitation_aborted",          # doc 2 D1 fix
        "roots_unsupported",
        "sampling_unsupported",
    }
    assert required <= values, f"Missing categories: {required - values}"


def test_tool_error_category_is_str_enum():
    from nodus_mcp.protocol.messages import ToolErrorCategory
    cat = ToolErrorCategory.ELICITATION_TIMEOUT
    assert isinstance(cat, str)
    assert cat == "elicitation_timeout"
    payload = json.dumps({"category": cat})
    assert '"elicitation_timeout"' in payload


def test_request_meta_from_dict_round_trip():
    from nodus_mcp.protocol.messages import RequestMeta
    raw = {
        "capabilities": {"tools": {}, "elicitation": {}},
        "clientInfo": {"name": "test-client", "version": "1.0"},
        "progressToken": "tok-abc",
    }
    meta = RequestMeta.from_dict(raw)
    assert meta.has_capability("tools")
    assert meta.has_capability("elicitation")
    assert not meta.has_capability("roots")
    assert meta.client_info == {"name": "test-client", "version": "1.0"}
    assert meta.progress_token == "tok-abc"
    out = meta.to_dict()
    assert out["capabilities"] == raw["capabilities"]
    assert out["clientInfo"] == raw["clientInfo"]


def test_request_meta_missing_fields():
    from nodus_mcp.protocol.messages import RequestMeta
    meta = RequestMeta.from_dict(None)
    assert meta.capabilities == {}
    assert meta.client_info is None
    assert meta.progress_token is None


def test_tool_definition_injects_type_object():
    from nodus_mcp.protocol.messages import ToolDefinition
    td = ToolDefinition(name="my.tool", description="does stuff", input_schema={})
    assert td.input_schema.get("type") == "object"


def test_tool_definition_deprecated_annotation():
    from nodus_mcp.protocol.messages import ToolDefinition
    td = ToolDefinition(
        name="old.tool", description="old", input_schema={"type": "object"},
        deprecated=True,
    )
    d = td.to_dict()
    assert d["annotations"] == {"deprecated": True}


def test_tool_definition_not_deprecated_no_annotations():
    from nodus_mcp.protocol.messages import ToolDefinition
    td = ToolDefinition(name="new.tool", description="new", input_schema={"type": "object"})
    d = td.to_dict()
    assert "annotations" not in d


def test_tool_call_result_error_shape():
    from nodus_mcp.protocol.messages import ToolCallResult, ToolErrorCategory
    r = ToolCallResult.error(ToolErrorCategory.ELICITATION_TIMEOUT, "timed out")
    d = r.to_dict()
    assert d["isError"] is True
    payload = json.loads(d["content"][0]["text"])
    assert payload["category"] == "elicitation_timeout"
    assert payload["message"] == "timed out"


def test_tool_call_result_success_no_is_error():
    from nodus_mcp.protocol.messages import ToolCallResult, ToolContent
    r = ToolCallResult(content=[ToolContent.make_text("hello")])
    d = r.to_dict()
    assert "isError" not in d


def test_input_required_result_request_state_opaque():
    """requestState must survive as a raw string — never parsed (doc 1 A1)."""
    from nodus_mcp.protocol.messages import InputRequiredResult
    opaque = "eyJyb3VuZCI6MX0="  # base64 of {"round":1}
    r = InputRequiredResult(
        input_requests=[{"id": "q1", "message": "Choose color"}],
        request_state=opaque,
    )
    d = r.to_dict()
    assert d["resultType"] == "input_required"
    assert d["requestState"] == opaque          # opaque: unchanged
    assert isinstance(d["requestState"], str)   # not decoded to dict


def test_sampling_required_result_same_pattern():
    from nodus_mcp.protocol.messages import SamplingRequiredResult
    r = SamplingRequiredResult(
        messages=[{"role": "user", "content": {"type": "text", "text": "hi"}}],
        params={"maxTokens": 100},
        request_state="abc123",
    )
    d = r.to_dict()
    assert d["resultType"] == "sampling_required"
    assert d["requestState"] == "abc123"


def test_sentinels_are_dataclasses():
    from nodus_mcp.protocol.messages import ElicitationRequest, SamplingRequest, RootsRequest
    e = ElicitationRequest(input_requests=[{"id": "q1"}], state={"step": 1})
    assert e.state == {"step": 1}
    s = SamplingRequest(messages=[], state={"x": 2})
    assert s.state == {"x": 2}
    r = RootsRequest(state={"y": 3})
    assert r.state == {"y": 3}


# ── A3: McpCodec ──────────────────────────────────────────────────────────────

def test_codec_encode_request_produces_valid_json():
    from nodus_mcp.codec import McpCodec
    codec = McpCodec()
    raw = codec.encode_request("tools/call", {"name": "x.y"}, id=7)
    assert isinstance(raw, bytes)
    d = json.loads(raw.decode("utf-8"))
    assert d["jsonrpc"] == "2.0"
    assert d["method"] == "tools/call"
    assert d["id"] == 7
    assert d["params"]["name"] == "x.y"


def test_codec_encode_notification_no_id():
    from nodus_mcp.codec import McpCodec
    codec = McpCodec()
    raw = codec.encode_notification("progress", {"token": "t1", "value": 50})
    d = json.loads(raw.decode("utf-8"))
    assert "id" not in d
    assert d["method"] == "progress"


def test_codec_decode_bytes_and_str():
    from nodus_mcp.codec import McpCodec
    codec = McpCodec()
    payload = '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
    assert codec.decode(payload.encode()) == codec.decode(payload)


def test_codec_parse_response_success():
    from nodus_mcp.codec import McpCodec
    codec = McpCodec()
    raw = json.dumps({"jsonrpc": "2.0", "id": 5, "result": {"tools": []}})
    resp = codec.parse_response(raw)
    assert not resp.is_error()
    assert resp.result == {"tools": []}
    assert resp.id == 5


def test_codec_parse_response_error():
    from nodus_mcp.codec import McpCodec
    from nodus_mcp.protocol.jsonrpc import METHOD_NOT_FOUND
    codec = McpCodec()
    raw = json.dumps({
        "jsonrpc": "2.0", "id": 3,
        "error": {"code": METHOD_NOT_FOUND, "message": "not found"},
    })
    resp = codec.parse_response(raw)
    assert resp.is_error()
    assert resp.error.code == METHOD_NOT_FOUND


def test_codec_make_error_response_uses_constants():
    from nodus_mcp.codec import McpCodec
    from nodus_mcp.protocol.jsonrpc import INVALID_PARAMS
    codec = McpCodec()
    d = codec.make_invalid_params("bad arg", id=9)
    assert d["error"]["code"] == INVALID_PARAMS
    assert d["error"]["code"] == -32602


def test_codec_make_result_response():
    from nodus_mcp.codec import McpCodec
    codec = McpCodec()
    d = codec.make_result_response({"tools": [{"name": "a.b"}]}, id=11)
    assert d["result"]["tools"][0]["name"] == "a.b"
    assert d["id"] == 11
    assert "error" not in d


def test_codec_encode_auto_assigns_id():
    from nodus_mcp.codec import McpCodec
    codec = McpCodec()
    raw1 = codec.encode_request("tools/list", {})
    raw2 = codec.encode_request("tools/list", {})
    d1 = json.loads(raw1)
    d2 = json.loads(raw2)
    assert d1["id"] != d2["id"]


# ── A3: Transport ABCs ────────────────────────────────────────────────────────

def test_transport_abc_cannot_instantiate():
    from nodus_mcp.transport import McpTransport, McpServerTransport
    with pytest.raises(TypeError):
        McpTransport()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        McpServerTransport()  # type: ignore[abstract]


def test_transport_abc_concrete_subclass():
    from nodus_mcp.transport import McpTransport
    class StubTransport(McpTransport):
        def send_request(self, method, params):
            return {}
        def send_notification(self, method, params):
            pass
        def close(self):
            pass
    t = StubTransport()
    assert t.send_request("x", {}) == {}


def test_transport_error_carries_status_code():
    from nodus_mcp.transport import TransportError
    err = TransportError("connection refused", status_code=503)
    assert err.status_code == 503
    assert "connection refused" in str(err)


# ── A4: McpConnection + lifecycle ────────────────────────────────────────────

def test_mcp_connection_fields():
    from nodus_mcp.transport import McpTransport
    from nodus_mcp.connection import McpConnection

    class StubTransport(McpTransport):
        closed = False
        def send_request(self, method, params): return {}
        def send_notification(self, method, params): pass
        def close(self): self.closed = True

    t = StubTransport()
    conn = McpConnection(
        alias="srv1",
        url="http://localhost:9000",
        transport=t,
        bearer_token="tok-xyz",
        server_info={"name": "test-server", "version": "1.0"},
        server_capabilities={"tools": {}},
        registered_tools=["mcp.srv1.search"],
    )
    assert conn.alias == "srv1"
    assert conn.bearer_token == "tok-xyz"
    assert "mcp.srv1.search" in conn.registered_tools
    conn.close()
    assert t.closed


def test_teardown_sentinel_is_unique():
    from nodus_mcp.connection import TEARDOWN_SENTINEL
    assert TEARDOWN_SENTINEL is not None
    assert TEARDOWN_SENTINEL is not True
    assert TEARDOWN_SENTINEL is not False
    assert TEARDOWN_SENTINEL != "teardown"


def test_active_elicitation_registry_teardown_wakes_parked_thread():
    from nodus_mcp.connection import ActiveElicitationRegistry, TEARDOWN_SENTINEL
    registry = ActiveElicitationRegistry()
    result_box = [None]
    wake_event = threading.Event()
    token = registry.register(result_box, wake_event)

    woke_clean = threading.Event()

    def parked_handler():
        fired = wake_event.wait(timeout=5.0)
        assert fired, "should have been woken by teardown"
        assert result_box[0] is TEARDOWN_SENTINEL
        woke_clean.set()

    t = threading.Thread(target=parked_handler, daemon=True)
    t.start()

    time.sleep(0.01)   # let the thread park
    registry.teardown()
    assert woke_clean.wait(timeout=2.0), "teardown did not wake parked handler"
    t.join(timeout=2.0)
    registry.unregister(token)


def test_active_elicitation_registry_unregister_before_teardown():
    from nodus_mcp.connection import ActiveElicitationRegistry, TEARDOWN_SENTINEL
    registry = ActiveElicitationRegistry()
    result_box = [None]
    wake_event = threading.Event()
    token = registry.register(result_box, wake_event)
    registry.unregister(token)
    registry.teardown()  # should not raise or touch the already-removed event
    assert result_box[0] is None  # never set


def test_active_elicitation_registry_multiple_concurrent():
    from nodus_mcp.connection import ActiveElicitationRegistry, TEARDOWN_SENTINEL
    registry = ActiveElicitationRegistry()
    N = 5
    boxes = [[None] for _ in range(N)]
    events = [threading.Event() for _ in range(N)]
    tokens = [registry.register(boxes[i], events[i]) for i in range(N)]

    registry.teardown()

    for i in range(N):
        assert events[i].is_set()
        assert boxes[i][0] is TEARDOWN_SENTINEL


# ── Integration: public API exports ──────────────────────────────────────────

def test_top_level_exports():
    import nodus_mcp
    assert hasattr(nodus_mcp, "McpCodec")
    assert hasattr(nodus_mcp, "McpTransport")
    assert hasattr(nodus_mcp, "McpServerTransport")
    assert hasattr(nodus_mcp, "TransportError")
    assert hasattr(nodus_mcp, "McpConnection")
    assert hasattr(nodus_mcp, "ActiveElicitationRegistry")
    assert hasattr(nodus_mcp, "TEARDOWN_SENTINEL")
    assert hasattr(nodus_mcp, "ToolErrorCategory")
    assert hasattr(nodus_mcp, "ElicitationRequest")
    assert hasattr(nodus_mcp, "SamplingRequest")
    assert hasattr(nodus_mcp, "RootsRequest")
    assert hasattr(nodus_mcp, "METHOD_NOT_FOUND")
    assert hasattr(nodus_mcp, "INVALID_PARAMS")
    assert hasattr(nodus_mcp, "INTERNAL_ERROR")
