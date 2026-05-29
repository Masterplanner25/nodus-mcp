"""McpServer — transport-agnostic stateless MCP server (Phases H + I + ...).

Statelessness discipline (the core invariant this file enforces):
  - No per-connection state. No session object. McpServer holds only
    configuration (runtime, handlers, roots) — nothing per-caller.
  - Capabilities are derived from configuration on every request. They are
    NOT cached per-connection or read once at "initialization." doc 1 C1 /
    Decision 2: _meta read fresh each call.
  - server/discover is idempotent, not a handshake. A tools/call (Phase I)
    with no prior discover must work. There is no required call order.
  - If a "Session" class or per-caller state accumulation appears here,
    the RC stateless model has been violated.

Phase H: dispatch core + server/discover + tools/list (read-only, no invoke)
Phase I: tools/call → registry.invoke() + producer-side error table (doc 4 B1/B2)
Phase J/K: server resources + prompts
Phase L: server-issued elicitation/sampling (doc 4 C1 re-call pattern)
Phase M: concrete transports (StdioServerTransport, HttpServerTransport)

Validation taxonomy (confirmed against nodus-lang embedding.py):
  ToolRegistry.invoke() does NOT validate args — it calls the handler directly
  and raises KeyError (not found) or handler exceptions. Phase I validates
  separately BEFORE calling invoke(), so validation failure (→ -32602) and
  execution failure (→ isError:true) are distinguishable without nesting.

Relay enumeration (doc 4 D3): tools/list enumerates ALL of
runtime.tool_registry — including client-discovered mcp.<alias>.<name> tools.
The alias prefix is the raw registry name; collision-safe by construction.
"""
from __future__ import annotations

from typing import Any

from .codec import McpCodec
from .protocol.jsonrpc import METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR
from .protocol.messages import (
    METHOD_SERVER_DISCOVER,
    METHOD_TOOLS_LIST,
    METHOD_TOOLS_CALL,
    ToolDefinition,
    ToolCallResult,
    ToolErrorCategory,
    ElicitationRequest,
    SamplingRequest,
    RootsRequest,
)


# ── Internal error sentinels (never cross the module boundary) ────────────────

class _MethodNotFoundError(Exception):
    pass


class _InvalidParamsError(Exception):
    pass


# ── Server version (re-exported from package root) ───────────────────────────

_SERVER_INFO = {"name": "nodus-mcp", "version": "0.1.0"}


# ── McpServer ─────────────────────────────────────────────────────────────────

class McpServer:
    """Transport-agnostic stateless MCP server foundation.

    Usage:
        server = McpServer(runtime=runtime)  # NodusRuntime or duck-typed equivalent
        server.set_elicitation_handler(fn)   # Phase L; gated in capabilities
        # Feed requests from any source:
        response = server.dispatch("tools/list", params, request_id)

    Integration with McpServerTransport (Phase M):
        transport.serve(server.dispatch)  # dispatch is the handler callable
    """

    def __init__(self, runtime: Any = None) -> None:
        # Configuration — set once, not mutated per-request.
        self._runtime = runtime          # duck-typed runtime; only .tool_registry.list_tools() used in H
        self._elicitation_handler = None  # Phase L: set to advertise elicitation capability
        self._sampling_handler = None     # Phase L
        self._roots: list = []            # Phase L: server's own roots (for roots/list responses)

        self._codec = McpCodec()

    # ── Handler registration (configuration, not per-request state) ───────────

    def set_elicitation_handler(self, fn) -> None:
        """Register an elicitation handler — gates elicitation capability advertisement."""
        self._elicitation_handler = fn

    def set_sampling_handler(self, fn) -> None:
        """Register a sampling handler — gates sampling capability advertisement."""
        self._sampling_handler = fn

    def set_roots(self, roots: list) -> None:
        """Configure the server's roots list — gates roots capability advertisement."""
        self._roots = list(roots)

    # ── Primary dispatch method ────────────────────────────────────────────────

    def dispatch(self, method: str, params: dict, request_id: Any) -> dict:
        """Process one inbound request; return a complete JSON-RPC response dict.

        Always returns a dict — never raises. Error cases produce JSON-RPC
        error responses with the appropriate code (doc 4 B2, producer side).

        Stateless: no state accumulated between calls. Each request is fully
        self-contained. Inbound _meta is read from params (not cached).

        This is the callable passed to McpServerTransport.serve() in Phase M:
            transport.serve(server.dispatch)
        """
        try:
            result = self._route(method, params)
            return self._codec.make_result_response(result, request_id)
        except _MethodNotFoundError:
            return self._codec.make_method_not_found(request_id)
        except _InvalidParamsError as exc:
            return self._codec.make_invalid_params(str(exc), request_id)
        except Exception as exc:
            return self._codec.make_internal_error(str(exc), request_id)

    # ── Internal routing ────────────────────────────────────────────────────────

    def _route(self, method: str, params: dict) -> dict:
        """Route by method string; raise _MethodNotFoundError for unknown methods."""
        if method == METHOD_SERVER_DISCOVER:
            return self._handle_discover(params)
        if method == METHOD_TOOLS_LIST:
            return self._handle_tools_list(params)
        if method == METHOD_TOOLS_CALL:          # Phase I
            return self._handle_tools_call(params)
        # resources/*, prompts/* are added by J, K respectively.
        raise _MethodNotFoundError(method)

    # ── H2: server/discover ─────────────────────────────────────────────────────

    def _handle_discover(self, params: dict) -> dict:
        """Handle server/discover (doc 4 D2).

        Capability advertisement:
          tools: {} always.
          elicitation: {} only if elicitation_handler is set.
          roots: {} only if roots are configured.
          sampling: {} only if sampling_handler is set.

        This is the server mirror of F's client capability gating — same rule,
        opposite role. No capability is advertised that the server cannot service.

        Not a handshake: no session gate. server/discover may arrive at any
        time and responds identically regardless of request history.
        """
        caps: dict = {"tools": {}}
        if self._elicitation_handler is not None:
            caps["elicitation"] = {}
        if self._roots:
            caps["roots"] = {}
        if self._sampling_handler is not None:
            caps["sampling"] = {}

        return {
            "serverInfo": _SERVER_INFO,
            "_meta": {"capabilities": caps},
            "tools": self._enumerate_tools(),
        }

    # ── H3: tools/list ──────────────────────────────────────────────────────────

    def _handle_tools_list(self, params: dict) -> dict:
        """Handle tools/list (doc 4 A1–A3).

        Live per-request enumeration — no cache. The list is dynamic; tools
        may be registered or unregistered between requests. A stale snapshot
        would violate the source-of-truth guarantee (doc 4 A1).

        Relay enumeration (doc 4 D3): client-discovered tools registered as
        mcp.<alias>.<name> enumerate with their full prefixed registry names.
        This is intentional — prefixed names are distinct, collision-safe.
        """
        return {"tools": self._enumerate_tools()}

    # ── Shared: registry enumeration ─────────────────────────────────────────────

    def _enumerate_tools(self) -> list:
        """Walk runtime.tool_registry.list_tools(); emit each as a ToolDefinition dict.

        Raw registry names, verbatim schema (type:object injected if empty),
        deprecated annotation (annotations.deprecated:true if deprecated).
        doc 4 A1/A2/A3 + doc 1 B1/B2/B3.
        """
        if self._runtime is None:
            return []
        result = []
        for entry in self._runtime.tool_registry.list_tools():
            td = ToolDefinition(
                name=entry["name"],
                description=entry.get("description") or entry["name"],
                input_schema=entry.get("schema") or {},
                deprecated=bool(entry.get("deprecated", False)),
            )
            result.append(td.to_dict())
        return result

    # ── Phase I: inbound tools/call invocation ────────────────────────────────────

    def _handle_tools_call(self, params: dict) -> dict:
        """Handle tools/call (doc 4 B1, B2).

        Pipeline: look up tool → validate args → invoke → wrap result.

        Producer-side error table (doc 4 B2 / doc 1 D-table):
          tool not found  → raises _MethodNotFoundError  → -32601 (never ran)
          schema mismatch → raises _InvalidParamsError   → -32602 (never ran)
          tool raises     → ToolCallResult isError:true, execution_failure
          tool_error rec  → ToolCallResult isError:true, execution_failure
          success         → ToolCallResult (no isError)

        Ordering invariant (doc 1 D2 producer side): invoke() is NEVER called
        with args that failed schema validation. Validated separately before
        invoke() because ToolRegistry.invoke() does not validate (confirmed
        against embedding.py:98–142 — it calls the handler directly).

        No run_source() context required (doc 4 B1): the server handles
        requests outside any enclosing script execution. _python_registered_tools
        is the normal production path when last_vm is None.
        """
        if self._runtime is None:
            raise _MethodNotFoundError("no runtime configured")

        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _InvalidParamsError("tools/call requires a non-empty string 'name'")

        # I2.a: Look up tool entry (not-found → -32601)
        entry = self._runtime.tool_registry.lookup(name)
        if entry is None:
            raise _MethodNotFoundError(f"Tool '{name}' is not registered")

        # I3: Validate args BEFORE invoke() — ordering invariant
        args = params.get("arguments") or {}
        schema = entry.get("schema") or {}
        val_err = _validate_args(args, schema)
        if val_err:
            raise _InvalidParamsError(f"Tool '{name}': {val_err}")

        # I2.b: Invoke — KeyError = post-lookup not-found; any other exc = execution fail
        try:
            result = self._runtime.tool_registry.invoke(name, args)
        except KeyError:
            raise _MethodNotFoundError(f"Tool '{name}' is not registered")
        except Exception as exc:
            return ToolCallResult.error(
                ToolErrorCategory.EXECUTION_FAILURE, str(exc)
            ).to_dict()

        # I2.c: Sentinel return types → server-issued elicitation/sampling (Phase L)
        if isinstance(result, (ElicitationRequest, SamplingRequest, RootsRequest)):
            return ToolCallResult.error(
                ToolErrorCategory.EXECUTION_FAILURE,
                f"Tool returned {type(result).__name__}; "
                "server-issued elicitation/sampling requires Phase L",
            ).to_dict()

        # I2.d: Nodus tool_error Record translated by _to_host_value → __nodus_err__
        if isinstance(result, dict) and result.get("__nodus_err__"):
            msg = result.get("message") or repr(result)
            return ToolCallResult.error(ToolErrorCategory.EXECUTION_FAILURE, msg).to_dict()

        # I2.e: Success
        return ToolCallResult.from_python_value(result).to_dict()


# ── Phase I helpers: arg validation (inlined; no nodus-lang internal import) ───

def _validate_args(args: dict, schema: dict) -> str | None:
    """Validate args against the JSON Schema subset used by std:tool.

    Implements the same rules as tool_module._validate_args in nodus-lang,
    keeping server.py nodus-lang-import-free. Returns error message or None.

    Validation happens before ToolRegistry.invoke() (I3 ordering invariant).
    """
    if not schema or schema.get("type") != "object":
        return None
    required = schema.get("required") or []
    props = schema.get("properties") or {}
    for req in required:
        if req not in args:
            return f"missing required argument: '{req}'"
    for key, val in args.items():
        if key in props:
            expected = props[key].get("type")
            if expected:
                err = _check_arg_type(val, expected, key)
                if err:
                    return err
    return None


def _check_arg_type(val: object, expected: str, key: str) -> str | None:
    """Check one arg value against a JSON Schema type string."""
    if expected == "string":
        if not isinstance(val, str):
            return f"argument '{key}' must be a string"
    elif expected == "integer":
        if not isinstance(val, int) or isinstance(val, bool):
            return f"argument '{key}' must be an integer"
    elif expected == "number":
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return f"argument '{key}' must be a number"
    elif expected == "boolean":
        if not isinstance(val, bool):
            return f"argument '{key}' must be a boolean"
    elif expected == "object":
        if not isinstance(val, dict):
            return f"argument '{key}' must be an object"
    elif expected == "array":
        if not isinstance(val, list):
            return f"argument '{key}' must be an array"
    return None
