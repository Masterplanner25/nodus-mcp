"""McpServer — transport-agnostic stateless MCP server foundation (Phase H).

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

Phase H scope (dispatch + read-only methods):
  H2 — server/discover (doc 4 D2)
  H3 — tools/list (doc 4 A1–A3)
  H*  — unknown method → -32601 MethodNotFound (doc 4 B2, producer side)

NOT in H:
  tools/call → registry invoke (Phase I)
  resources/list+read, prompts/list+get (Phases J, K)
  server-issued elicitation / sampling (Phase L)
  Concrete transports: StdioServerTransport, HttpServerTransport (Phase M)

Relay enumeration (doc 4 D3): tools/list enumerates ALL of
runtime.tool_registry — including client-discovered tools registered as
mcp.<alias>.<name>. The alias prefix is the raw registry name, so these
enumerate with distinct names (collision-safe). This is intentional; the
server acting as an implicit relay falls out of the shared-registry design.
"""
from __future__ import annotations

from typing import Any

from .codec import McpCodec
from .protocol.jsonrpc import METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR
from .protocol.messages import (
    METHOD_SERVER_DISCOVER,
    METHOD_TOOLS_LIST,
    ToolDefinition,
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
        """Route by method string; raise _MethodNotFoundError for unknown methods.

        H handles only read-only methods. tools/call is Phase I's route.
        """
        if method == METHOD_SERVER_DISCOVER:
            return self._handle_discover(params)
        if method == METHOD_TOOLS_LIST:
            return self._handle_tools_list(params)
        # tools/call, resources/*, prompts/* are added by I, J, K respectively.
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
