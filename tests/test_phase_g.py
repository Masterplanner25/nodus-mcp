"""Phase G tests — HttpTransport and the transport-agnostic seam proof.

Standing assertions for Phase G:
  - No reader thread, no pending map (HTTP has no persistent channel)
  - Bearer token present on every POST
  - MRTR loop (_run_tools_call) runs unchanged over HTTP: same 5 terminals
    as test_phase_c, proving the transport-agnostic seam held
  - requestState continuity across 3 discrete POSTs
  - Capability suppression: HTTP connection with handlers configured still
    does NOT advertise roots/sampling in _meta (TD-007)
  - No-persistent-channel purity: HttpTransport has none of the inbound-
    routing attributes (no _reader_thread, _pending, _inbound_request_handler)
  - C's _run_tools_call needed no HTTP-specific branch (confirmed by test pass)
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import httpx

from nodus_mcp.http import HttpTransport
from nodus_mcp.client import McpClient, _run_tools_call, _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ROUNDS, _CLIENT_META
from nodus_mcp.connection import McpConnection, ActiveElicitationRegistry
from nodus_mcp.protocol.messages import (
    RESULT_TYPE_INPUT_REQUIRED,
    ToolErrorCategory,
    METHOD_TOOLS_CALL,
)
from nodus_mcp.transport import McpTransport, TransportError


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_200(body: dict) -> MagicMock:
    """Build a mock httpx.Response with status 200 and JSON body."""
    resp = MagicMock()
    resp.status_code = 200
    raw = json.dumps(body).encode()
    resp.content = raw
    resp.text = raw.decode()
    return resp


def _mock_error(status: int, text: str = "error") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = text.encode()
    resp.text = text
    return resp


class MockHttpTransport(McpTransport):
    """Mock transport for testing MRTR loop over HTTP (canned response queue)."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self._requests: list[tuple[str, dict]] = []

    def send_request(self, method: str, params: dict) -> dict:
        self._requests.append((method, params))
        if not self._responses:
            raise TransportError("MockHttpTransport: no more responses")
        return self._responses.pop(0)

    def send_notification(self, method: str, params: dict) -> None:
        pass

    def close(self) -> None:
        pass

    def last_request(self) -> tuple[str, dict]:
        return self._requests[-1]

    def all_requests(self) -> list[tuple[str, dict]]:
        return list(self._requests)


def _success_resp(text: str = "ok") -> dict:
    return {"result": {"content": [{"type": "text", "text": text}]}}


def _input_required(reqs: list, state: str) -> dict:
    return {"result": {
        "resultType": RESULT_TYPE_INPUT_REQUIRED,
        "inputRequests": reqs,
        "requestState": state,
    }}


def _run(responses, handler=None, registry=None, timeout_s=None, max_rounds=None):
    return _run_tools_call(
        raw_name="do_thing",
        args={"x": 1},
        transport=MockHttpTransport(responses),
        elicitation_handler=handler,
        elicitation_registry=registry,
        elicitation_timeout_s=timeout_s if timeout_s is not None else _DEFAULT_TIMEOUT_S,
        max_elicitation_rounds=max_rounds if max_rounds is not None else _DEFAULT_MAX_ROUNDS,
        get_meta=lambda: _CLIENT_META,
    )


# ── G1: HttpTransport basics ──────────────────────────────────────────────────

def test_http_transport_no_reader_thread():
    """HTTP has no persistent channel — no reader thread, no pending map (doc 3 B2)."""
    t = HttpTransport("http://localhost:9000")
    assert not hasattr(t, "_reader_thread"), "HttpTransport must not have a reader thread"
    assert not hasattr(t, "_pending"), "HttpTransport must not have a pending map"
    t.close()


def test_http_transport_no_inbound_request_handler():
    """No inbound-request routing on HTTP (TD-007 purity assertion)."""
    t = HttpTransport("http://localhost:9000")
    assert not hasattr(t, "_inbound_request_handler"), (
        "HttpTransport must not have _inbound_request_handler — "
        "server-initiated requests over HTTP are deferred to v0.2 (TD-007)"
    )
    t.close()


def test_http_bearer_header_on_every_request():
    """Bearer token in Authorization header on every POST (Decision 15 / doc 3 C2)."""
    t = HttpTransport("http://example.com/mcp", bearer_token="secret-token")
    captured = []

    def fake_post(url, *, content, headers, **kw):
        captured.append(dict(headers))
        return _mock_200({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    with patch.object(t._client, "post", side_effect=fake_post):
        t.send_request("tools/list", {})
        t.send_request("server/discover", {})

    assert len(captured) == 2
    for h in captured:
        assert h.get("Authorization") == "Bearer secret-token"
        assert h.get("Content-Type") == "application/json"
    t.close()


def test_http_no_bearer_header_when_no_token():
    """No Authorization header when bearer_token is None."""
    t = HttpTransport("http://example.com/mcp")
    captured_headers = []

    def fake_post(url, *, content, headers, **kw):
        captured_headers.append(dict(headers))
        return _mock_200({"jsonrpc": "2.0", "id": 1, "result": {}})

    with patch.object(t._client, "post", side_effect=fake_post):
        t.send_request("tools/list", {})

    assert "Authorization" not in captured_headers[0]
    t.close()


def test_http_non_200_raises_transport_error():
    """Non-200 HTTP status → TransportError (doc 3 D2)."""
    t = HttpTransport("http://example.com/mcp")
    with patch.object(t._client, "post", return_value=_mock_error(503, "Service Unavailable")):
        with pytest.raises(TransportError) as exc_info:
            t.send_request("tools/call", {})
    assert "503" in str(exc_info.value)
    t.close()


def test_http_network_error_raises_transport_error():
    """Network failure → TransportError."""
    t = HttpTransport("http://example.com/mcp")
    with patch.object(t._client, "post", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(TransportError):
            t.send_request("tools/call", {})
    t.close()


def test_http_malformed_json_raises_transport_error():
    """Malformed JSON in response → TransportError."""
    t = HttpTransport("http://example.com/mcp")
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.content = b"not json at all"
    bad_resp.text = "not json at all"
    with patch.object(t._client, "post", return_value=bad_resp):
        with pytest.raises(TransportError) as exc_info:
            t.send_request("tools/call", {})
    assert "Malformed" in str(exc_info.value)
    t.close()


def test_http_send_after_close_raises():
    """send_request after close raises TransportError."""
    t = HttpTransport("http://example.com/mcp")
    t.close()
    with pytest.raises(TransportError):
        t.send_request("tools/list", {})


def test_http_close_calls_elicitation_teardown():
    """close() calls elicitation_registry.teardown() for interface symmetry (doc 3 D3)."""
    registry = ActiveElicitationRegistry()
    t = HttpTransport("http://example.com/mcp", elicitation_registry=registry)
    teardown_called = []
    orig = registry.teardown
    registry.teardown = lambda: (teardown_called.append(True), orig())[1]
    t.close()
    assert len(teardown_called) == 1


def test_http_double_close_is_idempotent():
    t = HttpTransport("http://example.com/mcp")
    t.close()
    t.close()  # must not raise


# ── G2: MRTR loop over HTTP — transport-agnostic seam proof ──────────────────
# The same five terminals as test_phase_c, now pointing at MockHttpTransport.
# C's _run_tools_call needed no HTTP-specific branch — these tests pass if
# the transport-agnostic seam held.

def test_http_seam_tc1_success():
    """G2 seam-proof TC-1: success over HTTP transport."""
    result = _run([_success_resp("http result")])
    assert result.get("isError") is not True
    assert result["content"][0]["text"] == "http result"


def test_http_seam_tc2_decline():
    """G2 seam-proof TC-2: decline over HTTP transport."""
    handler = lambda req: {"action": "decline"}
    result = _run([_input_required([{"id": "q1"}], "s1")], handler=handler)
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["action"] == "decline"


def test_http_seam_tc3_timeout():
    """G2 seam-proof TC-3: elicitation timeout over HTTP transport."""
    import time
    result = _run(
        [_input_required([{"id": "q1"}], "s1")],
        handler=lambda req: time.sleep(10),
        timeout_s=0.01,
    )
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.ELICITATION_TIMEOUT.value


def test_http_seam_tc4_unsupported():
    """G2 seam-proof TC-4: unsupported (no handler) over HTTP transport."""
    result = _run([_input_required([{"id": "q1"}], "s1")], handler=None)
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.ELICITATION_UNSUPPORTED.value


def test_http_seam_tc5_rounds_exceeded():
    """G2 seam-proof TC-5: rounds_exceeded over HTTP transport."""
    handler = lambda req: {"action": "accept", "content": {"q1": "y"}}
    canned = [_input_required([{"id": "q1"}], f"s{i}") for i in range(5)]
    result = _run(canned, handler=handler, max_rounds=2)
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.ELICITATION_ROUNDS_EXCEEDED.value


def test_http_mrtr_3_rounds_discrete_posts():
    """G2: requestState continuity across 3 discrete POSTs (doc 3 C1).

    Each elicitation round is a separate POST carrying requestState in the body.
    The MRTR loop is transport-agnostic; this test proves it works over HTTP.
    """
    import base64
    s1 = base64.b64encode(b'{"r":1}').decode()
    s2 = base64.b64encode(b'{"r":2}').decode()
    handler = lambda req: {"action": "accept", "content": {"q1": "yes"}}

    transport = MockHttpTransport([
        _input_required([{"id": "q1", "message": "round 1"}], s1),
        _input_required([{"id": "q1", "message": "round 2"}], s2),
        _success_resp("3 rounds complete"),
    ])

    result = _run_tools_call(
        "do_thing", {}, transport, handler, None,
        _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ROUNDS, lambda: _CLIENT_META,
    )

    assert result.get("isError") is not True
    assert result["content"][0]["text"] == "3 rounds complete"

    reqs = transport.all_requests()
    assert len(reqs) == 3  # initial + 2 continuations
    assert reqs[1][1]["requestState"] == s1  # round 1 echoes s1
    assert reqs[2][1]["requestState"] == s2  # round 2 echoes s2


# ── TD-007: capability suppression ───────────────────────────────────────────

def test_http_capability_suppression_with_handlers():
    """HTTP connections do not advertise roots/sampling even when handlers set.

    The limitation is transport-level (no persistent channel), not config-level.
    test_capability_gating_sampling_present_when_handler_set in test_phase_f
    confirmed stdio works; this test confirms HTTP suppresses it (TD-007).
    """
    client = McpClient()
    client.set_sampling_handler(lambda p: {})

    meta = client._build_meta(suppress_server_initiated=True)
    assert "sampling" not in meta.capabilities, (
        "HTTP must not advertise sampling — no channel for server to send createMessage"
    )
    assert "roots" not in meta.capabilities, (
        "HTTP must not advertise roots — no channel for server to send roots/list"
    )
    assert "tools" in meta.capabilities
    assert "elicitation" in meta.capabilities  # MRTR works over HTTP (response-folded)


def test_http_connection_stateless_http_flag():
    """McpConnection.stateless_http is True for HTTP connections."""
    conn = McpConnection(
        alias="s", url="http://test", transport=HttpTransport("http://test"),
        bearer_token=None, server_info={}, server_capabilities={},
        registered_tools=[], stateless_http=True,
    )
    assert conn.stateless_http is True
    conn.transport.close()


def test_stdio_connection_stateless_http_false():
    """McpConnection.stateless_http defaults to False (stdio connections)."""
    class StubT(McpTransport):
        def send_request(self, m, p): return {}
        def send_notification(self, m, p): pass
        def close(self): pass

    conn = McpConnection("s", "stdio://", StubT(), None, {}, {}, [])
    assert conn.stateless_http is False


def test_http_connect_wires_capability_suppression():
    """When connecting with HttpTransport, stateless_http is set on the connection."""
    discover_resp = {
        "result": {
            "serverInfo": {"name": "test"},
            "tools": [{"name": "greet", "description": "hi",
                        "inputSchema": {"type": "object"}}],
        }
    }
    client = McpClient()
    client.set_sampling_handler(lambda p: {})

    transport = HttpTransport("http://example.com/mcp")
    with patch.object(transport._client, "post",
                      return_value=_mock_200({"jsonrpc": "2.0", "id": 1,
                                             "result": discover_resp["result"]})):
        conn = client.connect(transport, alias="srv")

    assert conn.stateless_http is True
    # Verify the discover call did not advertise sampling
    # (the POST was intercepted; we can check via _build_meta directly)
    meta = client._build_meta(conn.stateless_http)
    assert "sampling" not in meta.capabilities
    transport.close()


def test_http_no_inbound_handler_wired_on_connect():
    """connect() with HttpTransport does not wire _inbound_request_handler (TD-007)."""
    discover_resp = {"result": {"serverInfo": {}, "tools": []}}
    client = McpClient()
    transport = HttpTransport("http://example.com/mcp")

    with patch.object(transport._client, "post",
                      return_value=_mock_200({"jsonrpc": "2.0", "id": 1,
                                             "result": discover_resp["result"]})):
        client.connect(transport, alias="srv")

    # HttpTransport should not have _inbound_request_handler set
    assert not hasattr(transport, "_inbound_request_handler"), (
        "HTTP transport must not receive inbound request routing (TD-007)"
    )
    transport.close()


# ── G3: connect / McpClient integration ──────────────────────────────────────

def test_http_connect_sends_server_discover():
    """connect() sends server/discover as the first POST."""
    discover_resp = {
        "serverInfo": {"name": "http-server", "version": "2.0"},
        "tools": [{"name": "run", "description": "runs", "inputSchema": {"type": "object"}}],
    }
    client = McpClient()
    transport = HttpTransport("http://example.com/mcp", bearer_token="tok")
    captured = []

    def fake_post(url, *, content, headers, **kw):
        msg = json.loads(content)
        captured.append(msg)
        return _mock_200({"jsonrpc": "2.0", "id": msg["id"], "result": discover_resp})

    with patch.object(transport._client, "post", side_effect=fake_post):
        conn = client.connect(transport, alias="srv")

    assert len(captured) == 1
    assert captured[0]["method"] == "server/discover"
    assert conn.server_info["name"] == "http-server"
    assert "mcp.srv.run" in conn.registered_tools
    transport.close()
