"""Phase I tests — inbound tools/call, four-outcome producer-side error table.

Standing assertions (mirror of C's five terminals, now outbound-from-us):
  TC-1: success → normal ToolCallResult, no isError
  TC-2: tool not-found → -32601 (protocol-level error, tool never ran)
  TC-3: schema validation failure → -32602 (protocol-level, tool never ran)
  TC-4: execution failure (handler raises) → isError:true, execution_failure

Ordering invariant (I3 / doc 1 D2 producer side):
  invoke() is never called when args fail schema validation.
  Proven via instrumented registry that asserts invoke() was not called.

Validate-vs-execute distinction:
  -32602 (bad args, validate-only path) vs isError:true (good args, ran and failed)
  prove these are distinct wire shapes; a caller can tell them apart.

No-run_source() context (doc 4 B1):
  Server invokes tools outside any enclosing script execution.
  _python_registered_tools is the normal production path (no VM needed).

All tests use mock runtimes. No real nodus-lang NodusRuntime needed.
McpServer.dispatch() called directly — no transport.
"""
import json

import pytest

from nodus_mcp.server import McpServer, _validate_args
from nodus_mcp.protocol.jsonrpc import METHOD_NOT_FOUND, INVALID_PARAMS
from nodus_mcp.protocol.messages import (
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    ToolErrorCategory,
)


# ── Mock runtime with invoke() support ────────────────────────────────────────

class MockRegistry:
    def __init__(self, tools: list, handlers: dict):
        self._tools = {t["name"]: t for t in tools}
        self._handlers = handlers
        self.invoke_calls: list[tuple] = []   # recorded for ordering tests

    def lookup(self, name: str) -> dict | None:
        return self._tools.get(name)

    def list_tools(self) -> list:
        return list(self._tools.values())

    def invoke(self, name: str, args: dict) -> object:
        self.invoke_calls.append((name, args))
        if name not in self._handlers:
            raise KeyError(f"Tool '{name}' not registered")
        return self._handlers[name](args)


class MockRuntime:
    def __init__(self, tools: list | None = None, handlers: dict | None = None):
        self.tool_registry = MockRegistry(tools or [], handlers or {})


def _entry(name: str, schema: dict | None = None, deprecated: bool = False) -> dict:
    return {"name": name, "description": "desc", "schema": schema or {}, "deprecated": deprecated}


def _call(name: str, arguments: dict | None = None) -> dict:
    return {"name": name, "arguments": arguments or {}}


# ── TC-1: Success ─────────────────────────────────────────────────────────────

def test_tc1_success_normal_result():
    """TC-1: tool ran, returns result → ToolCallResult with no isError."""
    runtime = MockRuntime(
        tools=[_entry("my.tool")],
        handlers={"my.tool": lambda args: {"answer": 42}},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("my.tool"), "req-1")

    assert "result" in resp, "Successful dispatch must have 'result'"
    assert "error" not in resp
    result = resp["result"]
    assert result.get("isError") is not True
    assert len(result["content"]) > 0


def test_tc1_success_string_result():
    runtime = MockRuntime(
        tools=[_entry("greet")],
        handlers={"greet": lambda args: f"Hello, {args.get('name', 'world')}"},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("greet", {"name": "Alice"}), "r1")
    result = resp["result"]
    assert result.get("isError") is not True
    assert "Alice" in result["content"][0]["text"]


def test_tc1_no_run_source_context(doc="doc 4 B1"):
    """TC-1: invocation works without any enclosing run_source() (doc 4 B1).
    _python_registered_tools is the normal server-side production path.
    The mock runtime has no active VM — this is the server's normal state.
    """
    runtime = MockRuntime(
        tools=[_entry("ping")],
        handlers={"ping": lambda args: {"pong": True}},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("ping"), "r1")
    assert resp["result"].get("isError") is not True


# ── TC-2: Tool not-found → -32601 ────────────────────────────────────────────

def test_tc2_tool_not_found_returns_32601():
    """TC-2: unknown tool name → -32601 (protocol-level, tool never ran)."""
    runtime = MockRuntime(tools=[], handlers={})
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("nonexistent.tool"), "r1")

    assert "error" in resp
    assert resp["error"]["code"] == METHOD_NOT_FOUND


def test_tc2_not_found_tool_never_ran():
    """TC-2: -32601 path — invoke() must not have been called."""
    runtime = MockRuntime(tools=[], handlers={})
    server = McpServer(runtime=runtime)
    server.dispatch(METHOD_TOOLS_CALL, _call("no.such.tool"), "r1")
    assert runtime.tool_registry.invoke_calls == [], "invoke() called for missing tool"


def test_tc2_no_runtime_returns_32601():
    """TC-2: server with no runtime treats all tool calls as not-found."""
    server = McpServer(runtime=None)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("any.tool"), "r1")
    assert "error" in resp
    assert resp["error"]["code"] == METHOD_NOT_FOUND


def test_tc2_missing_name_param_returns_32602():
    """Missing 'name' parameter is an invalid-params error (-32602)."""
    server = McpServer(runtime=MockRuntime())
    resp = server.dispatch(METHOD_TOOLS_CALL, {}, "r1")  # no 'name'
    assert "error" in resp
    assert resp["error"]["code"] == INVALID_PARAMS


# ── TC-3: Schema validation failure → -32602 ─────────────────────────────────

def test_tc3_schema_validation_failure_returns_32602():
    """TC-3: args fail schema → -32602 (protocol-level, tool never ran)."""
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    runtime = MockRuntime(
        tools=[_entry("counter", schema=schema)],
        handlers={"counter": lambda args: args},
    )
    server = McpServer(runtime=runtime)
    # Send wrong type: string instead of integer
    resp = server.dispatch(
        METHOD_TOOLS_CALL,
        _call("counter", {"count": "not-an-int"}),
        "r1",
    )
    assert "error" in resp
    assert resp["error"]["code"] == INVALID_PARAMS


def test_tc3_missing_required_field_returns_32602():
    schema = {"type": "object", "properties": {"x": {}}, "required": ["x"]}
    runtime = MockRuntime(
        tools=[_entry("needs_x", schema=schema)],
        handlers={"needs_x": lambda args: args},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("needs_x", {}), "r1")
    assert "error" in resp
    assert resp["error"]["code"] == INVALID_PARAMS


# ── I3: Validate-before-invoke ordering ──────────────────────────────────────

def test_i3_validate_before_invoke_invoke_never_called():
    """I3 ordering invariant: invoke() is NOT called when args fail validation.

    This is the structural proof of doc 1 D2 producer side. An instrumented
    registry asserts the ordering — the same pattern H used to prove it
    never invokes.
    """
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    runtime = MockRuntime(
        tools=[_entry("typed.tool", schema=schema)],
        handlers={"typed.tool": lambda args: args},
    )
    server = McpServer(runtime=runtime)

    # Send schema-invalid args
    resp = server.dispatch(
        METHOD_TOOLS_CALL,
        _call("typed.tool", {"n": "not-an-int"}),
        "r1",
    )

    # Validation must have fired (-32602)
    assert resp.get("error", {}).get("code") == INVALID_PARAMS, (
        "Expected -32602 from schema validation"
    )
    # invoke() must NOT have been called
    assert runtime.tool_registry.invoke_calls == [], (
        "invoke() was called despite failed schema validation — ordering invariant violated"
    )


def test_i3_valid_args_do_reach_invoke():
    """I3 complement: with valid args, invoke() IS called."""
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    runtime = MockRuntime(
        tools=[_entry("typed.tool", schema=schema)],
        handlers={"typed.tool": lambda args: {"doubled": args["n"] * 2}},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("typed.tool", {"n": 7}), "r1")
    assert resp["result"].get("isError") is not True
    assert runtime.tool_registry.invoke_calls == [("typed.tool", {"n": 7})]


# ── TC-4: Execution failure → isError:true ────────────────────────────────────

def test_tc4_handler_raises_returns_is_error_true():
    """TC-4: handler raises an exception → isError:true, execution_failure category."""
    runtime = MockRuntime(
        tools=[_entry("boom")],
        handlers={"boom": lambda args: (_ for _ in ()).throw(ValueError("something broke"))},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("boom"), "r1")

    assert "result" in resp   # tool-result shape, not JSON-RPC error
    result = resp["result"]
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.EXECUTION_FAILURE.value


def test_tc4_execution_failure_is_result_not_json_rpc_error():
    """TC-4: execution failure is a tool result (isError:true), not a JSON-RPC error (-32603).
    The distinction: -32602 means the request was malformed; isError:true means
    the tool ran and failed. These require different caller handling.
    """
    runtime = MockRuntime(
        tools=[_entry("fails")],
        handlers={"fails": lambda args: (_ for _ in ()).throw(RuntimeError("oops"))},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("fails"), "r1")

    # Must be a result (not an error)
    assert "result" in resp, "Execution failure must be a tool result, not a JSON-RPC error"
    assert "error" not in resp
    assert resp["result"]["isError"] is True


def test_tc4_nodus_error_record_returns_is_error():
    """TC-4: handler returns __nodus_err__ dict (Nodus tool_error record) → isError:true."""
    runtime = MockRuntime(
        tools=[_entry("nodus.tool")],
        handlers={"nodus.tool": lambda args: {
            "__nodus_err__": True,
            "kind": "error",
            "message": "Tool failed",
        }},
    )
    server = McpServer(runtime=runtime)
    resp = server.dispatch(METHOD_TOOLS_CALL, _call("nodus.tool"), "r1")
    assert resp["result"].get("isError") is True


# ── Validate-vs-execute distinction ──────────────────────────────────────────

def test_validate_vs_execute_distinct_wire_shapes():
    """The two I failure modes produce different wire shapes.

    Bad args (validate path)   → JSON-RPC error with code=-32602 (no 'result' key)
    Good args, handler raises  → tool result with isError:true (no 'error' key)

    A caller must be able to distinguish: 'I sent bad arguments' from
    'the tool blew up on good arguments'. These need different caller responses.
    """
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    runtime = MockRuntime(
        tools=[_entry("explosive", schema=schema)],
        handlers={"explosive": lambda args: (_ for _ in ()).throw(ValueError("boom"))},
    )
    server = McpServer(runtime=runtime)

    # Path 1: bad args → -32602 (no result, has error with code=-32602)
    bad_resp = server.dispatch(
        METHOD_TOOLS_CALL, _call("explosive", {"x": "not-int"}), "r1"
    )
    assert "error" in bad_resp
    assert "result" not in bad_resp
    assert bad_resp["error"]["code"] == INVALID_PARAMS

    # Path 2: good args, handler raises → isError:true (has result, no error)
    good_resp = server.dispatch(
        METHOD_TOOLS_CALL, _call("explosive", {"x": 42}), "r2"
    )
    assert "result" in good_resp
    assert "error" not in good_resp
    assert good_resp["result"]["isError"] is True


# ── Validation helper unit tests ──────────────────────────────────────────────

def test_validate_args_no_schema_passes():
    assert _validate_args({}, {}) is None
    assert _validate_args({"x": 1}, {}) is None


def test_validate_args_required_missing():
    schema = {"type": "object", "required": ["x"]}
    err = _validate_args({}, schema)
    assert err is not None
    assert "x" in err


def test_validate_args_type_mismatch():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    err = _validate_args({"n": "not-int"}, schema)
    assert err is not None


def test_validate_args_passes_on_valid():
    schema = {"type": "object",
              "properties": {"n": {"type": "integer"}, "s": {"type": "string"}},
              "required": ["n"]}
    assert _validate_args({"n": 5, "s": "hello"}, schema) is None


# ── Integration: I and H routes coexist ──────────────────────────────────────

def test_tools_list_and_tools_call_both_work():
    """H's tools/list and I's tools/call coexist on the same McpServer."""
    runtime = MockRuntime(
        tools=[_entry("do.it")],
        handlers={"do.it": lambda args: {"done": True}},
    )
    server = McpServer(runtime=runtime)

    list_resp = server.dispatch(METHOD_TOOLS_LIST, {}, "r1")
    assert list_resp["result"]["tools"][0]["name"] == "do.it"

    call_resp = server.dispatch(METHOD_TOOLS_CALL, _call("do.it"), "r2")
    assert call_resp["result"].get("isError") is not True
