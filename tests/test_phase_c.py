"""Phase C tests — MRTR state machine, five terminal conditions, alias strip.

All tests use a MockTransport (canned response sequences). No subprocess,
no NodusRuntime. The state machine is isolated from the transport layer
(B already proved the transport; C tests the logic running on top of it).

Standing assertions for Phase C (analogous to B's two race tests):
  - Each of the five terminal conditions is a distinct test with a distinct
    asserted return shape.
  - 3-round elicitation proves requestState continuity across rounds.
  - requestState is echoed opaquely — grep confirms one decode locus (C3).
"""
import base64
import json
import time
import threading

import pytest

from nodus_mcp.client import (
    McpClient,
    _run_tools_call,
    _DEFAULT_TIMEOUT_S,
    _DEFAULT_MAX_ROUNDS,
    _CLIENT_META,
)
from nodus_mcp.connection import ActiveElicitationRegistry, TEARDOWN_SENTINEL
from nodus_mcp.protocol.messages import (
    METHOD_TOOLS_CALL,
    METHOD_SERVER_DISCOVER,
    RESULT_TYPE_INPUT_REQUIRED,
    ToolErrorCategory,
)
from nodus_mcp.transport import McpTransport, TransportError


# ── Mock transport ────────────────────────────────────────────────────────────

class MockTransport(McpTransport):
    """Returns canned responses from a queue. Raises TransportError if queue empty."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self._requests: list[tuple[str, dict]] = []  # recorded calls

    def send_request(self, method: str, params: dict) -> dict:
        self._requests.append((method, params))
        if not self._responses:
            raise TransportError("MockTransport: no more canned responses")
        return self._responses.pop(0)

    def send_notification(self, method: str, params: dict) -> None:
        pass

    def close(self) -> None:
        pass

    def last_request(self) -> tuple[str, dict]:
        return self._requests[-1]

    def all_requests(self) -> list[tuple[str, dict]]:
        return list(self._requests)


def _success_response(content: dict | str = "done") -> dict:
    if isinstance(content, str):
        content = {"type": "text", "text": content}
    return {"result": {"content": [content]}}


def _input_required(input_requests: list, request_state: str) -> dict:
    return {"result": {
        "resultType": RESULT_TYPE_INPUT_REQUIRED,
        "inputRequests": input_requests,
        "requestState": request_state,
    }}


def _rpc_error(code: int, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _run(responses, handler=None, registry=None, timeout_s=None, max_rounds=None):
    """Convenience wrapper around _run_tools_call."""
    return _run_tools_call(
        raw_name="read_file",
        args={"path": "/tmp/x"},
        transport=MockTransport(responses),
        elicitation_handler=handler,
        elicitation_registry=registry,
        elicitation_timeout_s=timeout_s if timeout_s is not None else _DEFAULT_TIMEOUT_S,
        max_elicitation_rounds=max_rounds if max_rounds is not None else _DEFAULT_MAX_ROUNDS,
        get_meta=lambda: _CLIENT_META,
    )


# ── Terminal condition 1: success ────────────────────────────────────────────

def test_tc1_success_returns_result_unchanged():
    """TC-1: Success — server result passes through with no isError field."""
    result = _run([_success_response("hello")])
    assert "isError" not in result
    assert result["content"][0]["text"] == "hello"


def test_tc1_server_iserror_passes_through():
    """TC-1 variant: server-returned isError:true is passed through unchanged."""
    server_error = {"result": {"content": [{"type": "text", "text": "boom"}], "isError": True}}
    result = _run([server_error])
    assert result.get("isError") is True
    assert result["content"][0]["text"] == "boom"


def test_tc1_rpc_not_found_returns_named_category():
    from nodus_mcp.protocol.jsonrpc import METHOD_NOT_FOUND
    result = _run([_rpc_error(METHOD_NOT_FOUND, "Tool not found")])
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.NOT_FOUND.value
    assert result["isError"] is True


def test_tc1_rpc_invalid_params_returns_named_category():
    from nodus_mcp.protocol.jsonrpc import INVALID_PARAMS
    result = _run([_rpc_error(INVALID_PARAMS, "bad arg")])
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.INVALID_PARAMS.value


def test_tc1_transport_error_returns_transport_error_category():
    class FailTransport(McpTransport):
        def send_request(self, m, p): raise TransportError("pipe broken")
        def send_notification(self, m, p): pass
        def close(self): pass

    result = _run_tools_call(
        "read_file", {}, FailTransport(), None, None,
        _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ROUNDS, lambda: _CLIENT_META,
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.TRANSPORT_ERROR.value


# ── Terminal condition 2: decline ────────────────────────────────────────────

def test_tc2_decline_is_not_an_error():
    """TC-2: Decline — is_error=False so tool can distinguish from broken elicitation."""
    handler = lambda req: {"action": "decline"}
    result = _run(
        [_input_required([{"id": "q1", "message": "sure?"}], "state-xyz")],
        handler=handler,
    )
    # Must NOT have isError: True
    assert result.get("isError") is not True
    # Must carry the decline signal
    payload = json.loads(result["content"][0]["text"])
    assert payload["action"] == "decline"


def test_tc2_decline_vs_error_are_distinguishable():
    """TC-2: A tool can tell decline apart from timeout/unsupported by checking isError."""
    # decline: is_error=False
    decline_result = _run(
        [_input_required([{"id": "q1"}], "s1")],
        handler=lambda req: {"action": "decline"},
    )
    assert decline_result.get("isError") is not True

    # timeout: isError=True
    timeout_result = _run(
        [_input_required([{"id": "q1"}], "s2")],
        handler=lambda req: time.sleep(10),  # will be killed by short timeout
        timeout_s=0.01,
    )
    assert timeout_result.get("isError") is True


# ── Terminal condition 3: timeout ────────────────────────────────────────────

def test_tc3_timeout_returns_elicitation_timeout():
    """TC-3: Timeout — callback doesn't return within deadline."""
    def slow_handler(req):
        time.sleep(10)  # longer than our 10ms timeout
        return {"action": "accept", "content": {}}

    result = _run(
        [_input_required([{"id": "q1"}], "state-abc")],
        handler=slow_handler,
        timeout_s=0.01,
    )
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.ELICITATION_TIMEOUT.value


def test_tc3_timeout_category_distinguishable_from_unsupported():
    timeout_result = _run(
        [_input_required([{"id": "q1"}], "s1")],
        handler=lambda req: time.sleep(10),
        timeout_s=0.01,
    )
    unsupported_result = _run(
        [_input_required([{"id": "q1"}], "s2")],
        handler=None,
    )
    t_cat = json.loads(timeout_result["content"][0]["text"])["category"]
    u_cat = json.loads(unsupported_result["content"][0]["text"])["category"]
    assert t_cat == ToolErrorCategory.ELICITATION_TIMEOUT.value
    assert u_cat == ToolErrorCategory.ELICITATION_UNSUPPORTED.value
    assert t_cat != u_cat


# ── Terminal condition 4: unsupported ────────────────────────────────────────

def test_tc4_unsupported_no_handler_at_first_input_required():
    """TC-4: Unsupported — no handler at first InputRequiredResult (doc 2 C2)."""
    result = _run(
        [_input_required([{"id": "q1"}], "state")],
        handler=None,   # no handler registered
    )
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.ELICITATION_UNSUPPORTED.value


def test_tc4_unsupported_check_is_at_first_input_required_not_at_discovery():
    """TC-4: Tools that never elicit work fine with no handler (doc 2 C2 contract)."""
    # Tool returns success directly — handler=None should not cause any error
    result = _run([_success_response("ok")], handler=None)
    assert result.get("isError") is not True
    assert result["content"][0]["text"] == "ok"


# ── Terminal condition 5: rounds_exceeded ────────────────────────────────────

def test_tc5_rounds_exceeded_stops_loop():
    """TC-5: rounds_exceeded — loop hits cap before success."""
    handler = lambda req: {"action": "accept", "content": {"q1": "yes"}}
    # Server keeps returning InputRequiredResult — the cap protects against runaway
    canned = [
        _input_required([{"id": "q1"}], f"state-{i}") for i in range(15)
    ] + [_success_response("never reached")]

    result = _run(canned, handler=handler, max_rounds=3)
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.ELICITATION_ROUNDS_EXCEEDED.value


def test_tc5_rounds_exceeded_cap_is_configurable():
    """TC-5: cap is configurable; a cap of 1 stops after one round."""
    handler = lambda req: {"action": "accept", "content": {"q1": "yes"}}
    canned = [
        _input_required([{"id": "q1"}], "s1"),  # round 1
        _input_required([{"id": "q1"}], "s2"),  # would be round 2
        _success_response("unreachable"),
    ]
    result = _run(canned, handler=handler, max_rounds=1)
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.ELICITATION_ROUNDS_EXCEEDED.value


def test_tc5_at_cap_not_over():
    """TC-5: exactly max_rounds rounds should succeed; max_rounds+1 exceeds."""
    # With max_rounds=2: 2 rounds → success on 3rd response
    accepted = {"action": "accept", "content": {"q1": "yes"}}
    handler = lambda req: accepted
    canned = [
        _input_required([{"id": "q1"}], "s1"),  # round 1 → ok (1 <= 2)
        _input_required([{"id": "q1"}], "s2"),  # round 2 → ok (2 <= 2)
        _success_response("done"),               # round 3 → success
    ]
    result = _run(canned, handler=handler, max_rounds=2)
    assert result.get("isError") is not True


# ── 3-round continuity test ───────────────────────────────────────────────────

def test_3_round_continuity_carries_request_state():
    """Prove requestState continuity across 3 rounds (doc 2 B2).

    The client must echo requestState back unchanged on every continuation.
    Each round uses a different opaque blob; we verify the wire sends match.
    """
    state_r1 = base64.b64encode(b'{"round":1}').decode()
    state_r2 = base64.b64encode(b'{"round":2}').decode()

    accepted = {"action": "accept", "content": {"q1": "yes"}}
    handler = lambda req: accepted

    transport = MockTransport([
        _input_required([{"id": "q1", "message": "first?"}], state_r1),
        _input_required([{"id": "q1", "message": "second?"}], state_r2),
        _success_response("three rounds done"),
    ])

    result = _run_tools_call(
        "my_tool", {"x": 1}, transport,
        handler, None,
        _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ROUNDS, lambda: _CLIENT_META,
    )

    assert result.get("isError") is not True
    assert result["content"][0]["text"] == "three rounds done"

    # Verify requestState echoed correctly on each continuation
    reqs = transport.all_requests()
    assert len(reqs) == 3  # initial + 2 continuations

    # Round 1 continuation must carry state_r1
    assert reqs[1][1].get("requestState") == state_r1
    # Round 2 continuation must carry state_r2
    assert reqs[2][1].get("requestState") == state_r2


def test_3_round_request_state_is_never_decoded():
    """C3: requestState is passed through as opaque str — never decoded in client."""
    # Use a deliberately invalid base64 string — the client should just echo it
    # without attempting to decode it. If it decodes, it would raise an error.
    invalid_b64 = "NOT_VALID_BASE64!@#$"
    handler = lambda req: {"action": "accept", "content": {"q1": "y"}}
    transport = MockTransport([
        _input_required([{"id": "q1"}], invalid_b64),
        _success_response("ok"),
    ])
    # Should not raise — client echoes without decoding
    result = _run_tools_call(
        "tool", {}, transport, handler, None,
        _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ROUNDS, lambda: _CLIENT_META,
    )
    assert result.get("isError") is not True
    # Verify the invalid b64 was echoed back
    cont_params = transport.all_requests()[1][1]
    assert cont_params["requestState"] == invalid_b64


# ── Alias strip (doc 1 B1) ────────────────────────────────────────────────────

def test_alias_stripped_on_wire():
    """Nodus calls mcp.srv1.read_file; wire sends name: read_file (alias stripped)."""
    transport = MockTransport([_success_response("ok")])
    # raw_name is already stripped when passed to _run_tools_call (done by the
    # handler factory). Verify the wire params use the raw name.
    _run_tools_call(
        "read_file",               # raw_name: already stripped
        {"path": "/tmp/f"},
        transport, None, None,
        _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ROUNDS, lambda: _CLIENT_META,
    )
    method, params = transport.last_request()
    assert method == METHOD_TOOLS_CALL
    assert params["name"] == "read_file"   # no mcp.alias. prefix on the wire
    assert "mcp." not in params["name"]


def test_handler_factory_strips_alias():
    """_make_tool_handler captures the raw name; alias prefix absent on wire."""
    from nodus_mcp.client import _make_tool_handler

    transport = MockTransport([_success_response("ok")])
    handler = _make_tool_handler(
        raw_name="search",          # stripped from mcp.srv1.search
        transport=transport,
        get_elicitation_handler=lambda: None,
        elicitation_registry=None,
        elicitation_timeout_s=_DEFAULT_TIMEOUT_S,
        max_elicitation_rounds=_DEFAULT_MAX_ROUNDS,
        get_meta=lambda: _CLIENT_META,
    )
    handler({"q": "hello"})
    _, params = transport.last_request()
    assert params["name"] == "search"
    assert "mcp." not in params["name"]


# ── McpClient: connect + server/discover ─────────────────────────────────────

def test_mcp_client_connect_discovers_tools():
    """connect() parses server/discover response and returns McpConnection."""
    discover_resp = {
        "result": {
            "serverInfo": {"name": "test-server", "version": "1.0"},
            "_meta": {"capabilities": {"tools": {}}},
            "tools": [
                {"name": "greet", "description": "Say hi", "inputSchema": {"type": "object"}},
                {"name": "sum", "description": "Add numbers", "inputSchema": {"type": "object"}},
            ],
        }
    }
    transport = MockTransport([discover_resp])
    client = McpClient()
    conn = client.connect(transport, alias="srv1", url="stdio://test")

    assert conn.alias == "srv1"
    assert conn.server_info["name"] == "test-server"
    assert "mcp.srv1.greet" in conn.registered_tools
    assert "mcp.srv1.sum" in conn.registered_tools


def test_mcp_client_connect_registers_with_runtime():
    """connect() with a runtime calls tool_registry.register for each tool."""
    from nodus_mcp.client import _CLIENT_META

    registered = {}

    class MockRegistry:
        def register(self, meta):
            registered[meta["name"]] = meta

    class MockRuntime:
        tool_registry = MockRegistry()

    discover_resp = {
        "result": {
            "serverInfo": {},
            "tools": [
                {"name": "do_thing", "description": "Does thing",
                 "inputSchema": {"type": "object"}},
            ],
        }
    }
    transport = MockTransport([discover_resp])
    client = McpClient()
    client.connect(transport, alias="remote", runtime=MockRuntime())

    assert "mcp.remote.do_thing" in registered
    entry = registered["mcp.remote.do_thing"]
    assert callable(entry["handler"])


def test_mcp_client_set_elicitation_handler_after_connect():
    """Handler can be set after connect(); active for subsequent calls."""
    discover_resp = {"result": {"serverInfo": {}, "tools": [
        {"name": "ask", "description": "Asks", "inputSchema": {"type": "object"}},
    ]}}
    call_transport = MockTransport([
        _input_required([{"id": "q1"}], "s1"),
        _success_response("answered"),
    ])

    # Wrap transport to reuse it post-connect
    class SequencedTransport(McpTransport):
        def __init__(self):
            self._calls = 0
            self._responses = [
                {"result": {"serverInfo": {}, "tools": [
                    {"name": "ask", "description": "Asks",
                     "inputSchema": {"type": "object"}}
                ]}},
                _input_required([{"id": "q1"}], "s1"),
                _success_response("answered"),
            ]
        def send_request(self, m, p):
            r = self._responses.pop(0)
            return r
        def send_notification(self, m, p): pass
        def close(self): pass

    transport = SequencedTransport()
    client = McpClient()
    conn = client.connect(transport, alias="s")

    # Set handler AFTER connect
    client.set_elicitation_handler(
        lambda req: {"action": "accept", "content": {"q1": "yes"}}
    )

    # Call the handler directly (simulating tool.invoke)
    from nodus_mcp.client import _make_tool_handler
    handler = _make_tool_handler(
        "ask", transport,
        get_elicitation_handler=lambda: client._elicitation_handler,
        elicitation_registry=client._registry,
        elicitation_timeout_s=client._elicitation_timeout_s,
        max_elicitation_rounds=client._max_elicitation_rounds,
        get_meta=lambda: _CLIENT_META,
    )
    result = handler({})
    assert result.get("isError") is not True


# ── _meta attached to every request ──────────────────────────────────────────

def test_client_meta_in_every_request():
    """_meta with capabilities is attached to initial and continuation requests."""
    transport = MockTransport([
        _input_required([{"id": "q1"}], "s1"),
        _success_response("ok"),
    ])
    handler = lambda req: {"action": "accept", "content": {"q1": "y"}}
    _run_tools_call(
        "tool", {}, transport, handler, None,
        _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ROUNDS, lambda: _CLIENT_META,
    )
    for _, params in transport.all_requests():
        assert "_meta" in params
        assert "capabilities" in params["_meta"]


# ── Teardown sentinel integration ─────────────────────────────────────────────

def test_teardown_sentinel_aborts_elicitation():
    """Registry teardown during elicitation wait returns elicitation_aborted."""
    registry = ActiveElicitationRegistry()

    def blocking_handler(req):
        time.sleep(10)  # blocks until the test fires teardown
        return {"action": "accept", "content": {}}

    result_holder = [None]
    done = threading.Event()

    def run():
        result_holder[0] = _run(
            [_input_required([{"id": "q1"}], "s1")],
            handler=blocking_handler,
            registry=registry,
            timeout_s=5.0,
        )
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.05)
    registry.teardown()

    assert done.wait(timeout=2.0), "teardown did not abort elicitation"
    result = result_holder[0]
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.ELICITATION_ABORTED.value
    t.join(timeout=1.0)
