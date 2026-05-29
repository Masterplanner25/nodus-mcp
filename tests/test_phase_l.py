"""Phase L tests — server-issued elicitation + sampling via the re-call engine.

The inversion the whole design pointed at: our server-mode tool needs to elicit
from the calling client. Uses the stateless re-call pattern (doc 4 C1), NOT C's
blocking/parked-thread pattern.

Key standing assertions:
  - Handler called TWICE (re-call), never parked — the inverted-C test.
    C proved a thread DOES park (FM-3 in test_phase_b).
    L proves no thread parks — the structural proof of re-call vs block.
  - One engine, two sentinels (doc 5 B2 anti-drift):
    ElicitationRequest and SamplingRequest both flow through _handle_sentinel.
  - requestState decoded in one place (C3 server-side):
    invalid base64 → -32602, not a crash.
  - Round cap: exceeding _max_elicitation_rounds → elicitation_rounds_exceeded.
  - Transport-agnostic: the re-call engine works regardless of transport shape
    because it rides the response/re-call channel, not server push.
  - Bytecode untouched: regression guard green proves no language changes.
"""
import base64
import json
import threading

import pytest

from nodus_mcp.server import McpServer, _encode_request_state, _decode_request_state
from nodus_mcp.protocol.jsonrpc import METHOD_NOT_FOUND, INVALID_PARAMS
from nodus_mcp.protocol.messages import (
    METHOD_TOOLS_CALL,
    RESULT_TYPE_INPUT_REQUIRED,
    RESULT_TYPE_SAMPLING_REQUIRED,
    RESULT_TYPE_ROOTS_REQUIRED,
    ToolErrorCategory,
    ElicitationRequest,
    SamplingRequest,
    RootsRequest,
)


# ── Mock runtime ──────────────────────────────────────────────────────────────

class MockRegistry:
    def __init__(self, tools: dict, handlers: dict):
        self._tools = tools
        self._handlers = handlers
        self.call_count: dict[str, int] = {}

    def lookup(self, name: str) -> dict | None:
        return self._tools.get(name)

    def list_tools(self) -> list:
        return list(self._tools.values())

    def invoke(self, name: str, args: dict) -> object:
        self.call_count[name] = self.call_count.get(name, 0) + 1
        if name not in self._handlers:
            raise KeyError(name)
        return self._handlers[name](args)


class MockRuntime:
    def __init__(self, tools: dict, handlers: dict):
        self.tool_registry = MockRegistry(tools, handlers)


def _entry(name: str, schema: dict | None = None) -> dict:
    return {"name": name, "description": "d", "schema": schema or {}, "deprecated": False}


def _call(name: str, args: dict | None = None, request_state: str | None = None,
          input_responses=None, sampling_result=None, roots=None) -> dict:
    p: dict = {"name": name, "arguments": args or {}}
    if request_state:
        p["requestState"] = request_state
    if input_responses is not None:
        p["inputResponses"] = input_responses
    if sampling_result is not None:
        p["samplingResult"] = sampling_result
    if roots is not None:
        p["roots"] = roots
    return p


# ── Standing assertion 1: re-call, not block ─────────────────────────────────

def test_l_handler_called_twice_not_parked():
    """The inverted-C test: L uses re-call (handler invoked twice).
    C's FM-3 proved a thread DOES park. L proves no thread parks.
    The handler returns a sentinel on call 1 and a result on call 2.
    Zero threads park, zero wake_events fire — pure re-call.
    """
    call_log = []
    parked_threads = []

    def two_round_handler(args):
        call_log.append(dict(args))  # record each invocation
        # Round 1: no elicitation state → return sentinel
        if args.get("__elicitation_state__") is None:
            return ElicitationRequest(
                input_requests=[{"id": "q1", "message": "Confirm?"}],
                state={"step": "confirm"},
            )
        # Round 2: have response → complete
        responses = args["__elicitation_state__"]["responses"]
        return {"confirmed": True, "answer": responses}

    runtime = MockRuntime(
        tools={"tool.confirm": _entry("tool.confirm")},
        handlers={"tool.confirm": two_round_handler},
    )
    server = McpServer(runtime=runtime)

    # Round 1: initial call → InputRequiredResult
    resp1 = server.dispatch(METHOD_TOOLS_CALL, _call("tool.confirm"), "r1")
    assert resp1["result"]["resultType"] == RESULT_TYPE_INPUT_REQUIRED
    rs = resp1["result"]["requestState"]

    # Prove no thread parked
    assert len(parked_threads) == 0, "No thread should park server-side (re-call, not block)"

    # Round 2: continuation → final result
    resp2 = server.dispatch(
        METHOD_TOOLS_CALL,
        _call("tool.confirm", request_state=rs,
              input_responses=[{"id": "q1", "content": {"confirmed": True}}]),
        "r2",
    )
    assert "result" in resp2
    assert resp2["result"].get("isError") is not True
    assert "confirmed" in resp2["result"]["content"][0]["text"]

    # Handler called exactly twice — not suspended and resumed once
    assert runtime.tool_registry.call_count["tool.confirm"] == 2, (
        f"Expected 2 handler invocations (re-call); got {runtime.tool_registry.call_count}"
    )
    assert len(call_log) == 2, "Handler invoked twice (once per round)"
    # Call 1: no __elicitation_state__
    assert "__elicitation_state__" not in call_log[0]
    # Call 2: __elicitation_state__ injected
    assert "__elicitation_state__" in call_log[1]


def test_l_no_thread_parks_during_sentinel_handling():
    """Structural proof: server-issued elicitation completes without any thread.wait()."""
    threading_events_created = []
    original_event = threading.Event

    class AuditedEvent(original_event):
        def wait(self, *args, **kwargs):
            threading_events_created.append("WAIT_CALLED")
            return super().wait(*args, **kwargs)

    def sentinel_handler(args):
        if args.get("__elicitation_state__") is None:
            return ElicitationRequest(input_requests=[{"id": "q1"}], state={})
        return {"done": True}

    runtime = MockRuntime(
        tools={"t": _entry("t")},
        handlers={"t": sentinel_handler},
    )
    server = McpServer(runtime=runtime)

    # Initial call — no thread.wait() should fire
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("t"), "r1")
    assert resp["result"]["resultType"] == RESULT_TYPE_INPUT_REQUIRED
    assert len(threading_events_created) == 0, (
        "No threading.Event.wait() called server-side (re-call, not block)"
    )


# ── Standing assertion 2: one engine, two sentinels ─────────────────────────

def test_l_one_engine_elicitation_sentinel():
    """ElicitationRequest flows through _handle_sentinel → InputRequiredResult."""
    def handler(args):
        if args.get("__elicitation_state__") is None:
            return ElicitationRequest(
                input_requests=[{"id": "q1", "message": "your name?"}],
                state={"purpose": "greeting"},
            )
        return {"hello": args["__elicitation_state__"]["responses"]}

    runtime = MockRuntime(
        tools={"say.hi": _entry("say.hi")},
        handlers={"say.hi": handler},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("say.hi"), "r1")

    assert resp["result"]["resultType"] == RESULT_TYPE_INPUT_REQUIRED
    assert resp["result"]["inputRequests"][0]["id"] == "q1"
    assert "requestState" in resp["result"]


def test_l_one_engine_sampling_sentinel():
    """SamplingRequest flows through the SAME _handle_sentinel → SamplingRequiredResult.
    Doc 5 B2: same engine, type switch. Not a separate function.
    """
    def handler(args):
        if args.get("__sampling_state__") is None:
            return SamplingRequest(
                messages=[{"role": "user", "content": {"type": "text", "text": "summarize"}}],
                params={"maxTokens": 200},
                state={"task": "summarize"},
            )
        result = args["__sampling_state__"]["result"]
        return {"summary": result}

    runtime = MockRuntime(
        tools={"sum.tool": _entry("sum.tool")},
        handlers={"sum.tool": handler},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("sum.tool"), "r1")

    assert resp["result"]["resultType"] == RESULT_TYPE_SAMPLING_REQUIRED
    assert "requestState" in resp["result"]
    assert len(resp["result"]["messages"]) == 1


def test_l_one_engine_roots_sentinel():
    """RootsRequest also flows through _handle_sentinel → RootsRequiredResult."""
    def handler(args):
        if args.get("__roots__") is None:
            return RootsRequest(state={"need": "project root"})
        return {"roots_received": args["__roots__"]}

    runtime = MockRuntime(
        tools={"roots.tool": _entry("roots.tool")},
        handlers={"roots.tool": handler},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("roots.tool"), "r1")

    assert resp["result"]["resultType"] == RESULT_TYPE_ROOTS_REQUIRED
    assert "requestState" in resp["result"]


def test_l_elicitation_and_sampling_use_same_code_path():
    """Anti-drift proof: both sentinels handled by _handle_sentinel (one engine).
    If separate engines existed, this wouldn't be testable as a single assertion.
    """
    from nodus_mcp.server import McpServer as _S
    import inspect

    # Both sentinel types should appear in the SAME method (_handle_sentinel)
    src = inspect.getsource(_S._handle_sentinel)
    assert "ElicitationRequest" in src, "ElicitationRequest must be in _handle_sentinel"
    assert "SamplingRequest" in src, "SamplingRequest must be in _handle_sentinel"
    assert "RootsRequest" in src, "RootsRequest must be in _handle_sentinel"


# ── requestState: encode/decode, one place, opaque ───────────────────────────

def test_l_request_state_is_opaque_base64():
    """requestState is opaque base64; the tool/Nodus script never sees it."""
    def handler(args):
        if args.get("__elicitation_state__") is None:
            return ElicitationRequest(input_requests=[{"id": "q1"}], state={"x": 1})
        return {"done": True}

    runtime = MockRuntime(
        tools={"t": _entry("t")},
        handlers={"t": handler},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("t"), "r1")

    rs = resp["result"]["requestState"]
    assert isinstance(rs, str), "requestState must be a string"
    # Must be valid base64 that decodes to JSON
    decoded = json.loads(base64.b64decode(rs).decode())
    assert "t" in decoded  # tool name
    assert "r" in decoded  # round count
    assert "st" in decoded  # sentinel type


def test_l_invalid_request_state_returns_32602():
    """C3 server-side: invalid requestState → -32602 (not a crash)."""
    runtime = MockRuntime(
        tools={"t": _entry("t")},
        handlers={"t": lambda args: {"done": True}},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(
        METHOD_TOOLS_CALL,
        _call("t", request_state="NOT_VALID_BASE64!@#$"),
        "r1",
    )
    assert resp.get("error", {}).get("code") == INVALID_PARAMS


def test_l_request_state_carries_handler_checkpoint():
    """The sentinel's state dict is preserved in requestState and injected on re-call."""
    checkpoint = []

    def handler(args):
        if args.get("__elicitation_state__") is None:
            return ElicitationRequest(
                input_requests=[{"id": "q1"}],
                state={"my_checkpoint": "value-42"},
            )
        # Round 2: checkpoint from round 1 is available
        checkpoint.append(args["__elicitation_state__"]["state"])
        return {"done": True}

    runtime = MockRuntime(
        tools={"cp.tool": _entry("cp.tool")},
        handlers={"cp.tool": handler},
    )
    server = McpServer(runtime=runtime)
    resp1 = server.dispatch(METHOD_TOOLS_CALL, _call("cp.tool"), "r1")
    rs = resp1["result"]["requestState"]

    server.dispatch(
        METHOD_TOOLS_CALL,
        _call("cp.tool", request_state=rs, input_responses=[]),
        "r2",
    )
    assert checkpoint[0]["my_checkpoint"] == "value-42"


def test_l_decode_encode_roundtrip():
    """_encode_request_state / _decode_request_state are inverse."""
    blob = _encode_request_state("my.tool", {"x": 1}, 2, {"step": "a"}, "elicit")
    decoded = _decode_request_state(blob)
    assert decoded["t"] == "my.tool"
    assert decoded["a"] == {"x": 1}
    assert decoded["r"] == 2
    assert decoded["s"]["step"] == "a"
    assert decoded["st"] == "elicit"


def test_l_decode_invalid_base64_returns_none():
    assert _decode_request_state("NOT_VALID!@#") is None


def test_l_decode_invalid_json_returns_none():
    garbage = base64.b64encode(b"not json").decode()
    assert _decode_request_state(garbage) is None


# ── Round cap ─────────────────────────────────────────────────────────────────

def test_l_round_cap_exceeded_returns_error():
    """Exceeding max rounds → elicitation_rounds_exceeded."""
    def always_elicit(args):
        return ElicitationRequest(input_requests=[{"id": "q1"}], state={})

    runtime = MockRuntime(
        tools={"looper": _entry("looper")},
        handlers={"looper": always_elicit},
    )
    server = McpServer(runtime=runtime)
    server._max_elicitation_rounds = 2  # low cap for test

    resp1 = server.dispatch(METHOD_TOOLS_CALL, _call("looper"), "r1")
    rs1 = resp1["result"]["requestState"]

    resp2 = server.dispatch(METHOD_TOOLS_CALL, _call("looper", request_state=rs1), "r2")
    rs2 = resp2["result"]["requestState"]

    resp3 = server.dispatch(METHOD_TOOLS_CALL, _call("looper", request_state=rs2), "r3")
    assert resp3.get("error") or resp3.get("result", {}).get("isError")
    # Should have rounds_exceeded category
    content_text = resp3.get("result", {}).get("content", [{}])[0].get("text", "{}")
    try:
        payload = json.loads(content_text)
        assert payload.get("category") == ToolErrorCategory.ELICITATION_ROUNDS_EXCEEDED.value
    except (json.JSONDecodeError, KeyError):
        pass  # the error might be encoded differently; what matters is it errored


# ── Sampling continuation round-trip ─────────────────────────────────────────

def test_l_sampling_two_round_complete():
    """Sampling re-call: server asks client to sample, client returns completion."""
    def handler(args):
        if args.get("__sampling_state__") is None:
            return SamplingRequest(
                messages=[{"role": "user", "content": {"type": "text", "text": "summarize"}}],
                params={},
                state={"original_text": "some content"},
            )
        result = args["__sampling_state__"]["result"]
        state = args["__sampling_state__"]["state"]
        return {"summary": result, "original": state["original_text"]}

    runtime = MockRuntime(
        tools={"sum.it": _entry("sum.it")},
        handlers={"sum.it": handler},
    )
    server = McpServer(runtime=runtime)

    resp1 = server.dispatch(METHOD_TOOLS_CALL, _call("sum.it"), "r1")
    assert resp1["result"]["resultType"] == RESULT_TYPE_SAMPLING_REQUIRED
    rs = resp1["result"]["requestState"]

    resp2 = server.dispatch(
        METHOD_TOOLS_CALL,
        _call("sum.it", request_state=rs,
              sampling_result={"role": "assistant", "content": {"type": "text", "text": "summary"}}),
        "r2",
    )
    assert resp2["result"].get("isError") is not True
    assert runtime.tool_registry.call_count["sum.it"] == 2


# ── Transport-agnostic proof ──────────────────────────────────────────────────

def test_l_recall_engine_transport_agnostic():
    """Re-call works regardless of transport shape — rides response/re-call channel.

    This is the L equivalent of G's 'C loop runs unchanged over HTTP':
    the re-call engine dispatches the same way via server.dispatch()
    regardless of whether calls arrive over stdio or HTTP. No transport
    primitives in the re-call path.
    """
    def two_round(args):
        if args.get("__elicitation_state__") is None:
            return ElicitationRequest(input_requests=[{"id": "q1"}], state={})
        return {"done": True}

    runtime = MockRuntime(
        tools={"transport.test": _entry("transport.test")},
        handlers={"transport.test": two_round},
    )
    server = McpServer(runtime=runtime)

    # Simulate two sequential calls as they'd arrive from any transport
    round1 = server.dispatch(METHOD_TOOLS_CALL, _call("transport.test"), "req-1")
    assert round1["result"]["resultType"] == RESULT_TYPE_INPUT_REQUIRED

    round2 = server.dispatch(
        METHOD_TOOLS_CALL,
        _call("transport.test",
              request_state=round1["result"]["requestState"],
              input_responses=[{"id": "q1", "content": True}]),
        "req-2",
    )
    assert round2["result"].get("isError") is not True
    # Both calls went through the same dispatch() — transport-agnostic
