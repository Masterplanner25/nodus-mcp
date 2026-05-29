"""McpClient — client-role tool calls, resources, prompts, and MRTR loop.

Implements:
  - tools/call + MRTR elicitation (Phase C)
  - resources/list + resources/read (Phase D)
  - prompts/list + prompts/get (Phase E)
  - server/discover for tool registration (doc 3 D1, doc 4 D2)
  - McpClient class for per-client config + connection management

requestState is opaque to the client throughout this file.
The client receives it as a str and echoes it back unchanged (doc 2 B2).
It is NEVER decoded here. Decode lives in Phase H (server role).

D and E reuse _simple_call — the same transport.send_request path as Phase C,
no parallel call mechanism (doc 3 A1 thin-shared-core discipline).
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Callable

from .codec import McpCodec
from .connection import ActiveElicitationRegistry, McpConnection, TEARDOWN_SENTINEL
from .protocol.jsonrpc import METHOD_NOT_FOUND, INVALID_PARAMS
from .protocol.messages import (
    METHOD_TOOLS_CALL,
    METHOD_SERVER_DISCOVER,
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    METHOD_PROMPTS_LIST,
    METHOD_PROMPTS_GET,
    METHOD_ROOTS_LIST,
    METHOD_SAMPLING_CREATE_MESSAGE,
    METHOD_ELICITATION_CREATE,
    RESULT_TYPE_INPUT_REQUIRED,
    RequestMeta,
    ToolCallResult,
    ToolContent,
    ToolDefinition,
    ToolErrorCategory,
)
from .transport import McpTransport, TransportError

_DEFAULT_TIMEOUT_S: float = 300.0   # 5 minutes (Decision 14)
_DEFAULT_MAX_ROUNDS: int = 10        # doc 2 B1

# Internal sentinel: elicitation callback timed out.
# Distinct from TEARDOWN_SENTINEL (which is transport-teardown-triggered).
_TIMEOUT_SENTINEL: object = object()

# Base capabilities always advertised (tools + elicitation).
# roots and sampling are added dynamically by _build_meta() when configured.
_CLIENT_INFO = {"name": "nodus-mcp", "version": "0.1.0"}

# Static full-capability meta used in tests and as a fallback constant.
# Production code uses McpClient._build_meta() for capability gating.
_CLIENT_META = RequestMeta(
    capabilities={"tools": {}, "elicitation": {}, "roots": {}, "sampling": {}},
    client_info=_CLIENT_INFO,
)


# ── Shared call path (D + E reuse this; C's MRTR loop inlines the same logic) ─

def _simple_call(transport: McpTransport, method: str, params: dict) -> dict:
    """Send one request and return the result dict (or an error dict).

    This is the shared call path for resources and prompts (Phases D and E).
    D and E do not introduce a second send mechanism — they call transport
    .send_request through here, same codec, same error mapping (doc 1 D-table).

    Returns a plain dict: either the server's result body, or a
    ToolCallResult.error(...).to_dict() on failure.
    """
    try:
        response = transport.send_request(method, params)
    except TransportError as exc:
        return ToolCallResult.error(ToolErrorCategory.TRANSPORT_ERROR, str(exc)).to_dict()

    if "error" in response:
        err = response["error"]
        code = err.get("code")
        msg = err.get("message", "RPC error")
        if code == METHOD_NOT_FOUND:
            cat = ToolErrorCategory.NOT_FOUND
        elif code == INVALID_PARAMS:
            cat = ToolErrorCategory.INVALID_PARAMS
        else:
            cat = ToolErrorCategory.TRANSPORT_ERROR
        return ToolCallResult.error(cat, msg).to_dict()

    return response.get("result") or {}


# ── Elicitation callback invocation ──────────────────────────────────────────

def _invoke_elicitation_callback(
    callback: Callable,
    request: dict,
    registry: ActiveElicitationRegistry | None,
    timeout_s: float,
) -> object:
    """Run the elicitation callback in a daemon thread, bounded by timeout_s.

    Returns:
      - A dict {"action": "accept"/"decline", ...} on normal completion.
      - TEARDOWN_SENTINEL if the registry fired a teardown signal.
      - _TIMEOUT_SENTINEL if the callback did not return within timeout_s.
      - An Exception if the callback raised.

    The callback runs in a background thread so the timeout can fire without
    killing the callback. The result_box/wake_event pattern (doc 2 D1) allows
    the teardown registry to interrupt the wait by setting TEARDOWN_SENTINEL
    before firing the event.
    """
    result_box: list = [None]
    wake_event = threading.Event()

    token: int | None = None
    if registry is not None:
        token = registry.register(result_box, wake_event)

    def _run() -> None:
        try:
            result_box[0] = callback(request)
        except Exception as exc:
            result_box[0] = exc
        wake_event.set()

    threading.Thread(target=_run, daemon=True).start()

    fired = wake_event.wait(timeout=timeout_s)

    if registry is not None and token is not None:
        registry.unregister(token)

    if not fired:
        return _TIMEOUT_SENTINEL

    val = result_box[0]
    # TEARDOWN_SENTINEL: registry.teardown() fired while we were waiting.
    # Note: there is a benign race where the callback completes and writes to
    # result_box just as teardown fires.  In that case we may return a normal
    # result instead of TEARDOWN_SENTINEL — acceptable for v0.1 (not a hang).
    if val is TEARDOWN_SENTINEL:
        return TEARDOWN_SENTINEL

    return val


# ── MRTR state machine ────────────────────────────────────────────────────────

def _run_tools_call(
    raw_name: str,
    args: dict,
    transport: McpTransport,
    elicitation_handler: Callable | None,
    elicitation_registry: ActiveElicitationRegistry | None,
    elicitation_timeout_s: float,
    max_elicitation_rounds: int,
    get_meta: Callable[[], RequestMeta],
) -> dict:
    """Execute one tools/call, driving the full MRTR loop if elicitation occurs.

    Five terminal conditions (each returns a distinct dict shape):
      success             — normal ToolCallResult from server (isError absent/False)
      isError tool result — server-returned error (isError: True, named category)
      decline             — callback returned {"action": "decline"}; is_error=False
      timeout             — callback did not return within elicitation_timeout_s
      unsupported         — no elicitation_handler registered at first InputRequired
      rounds_exceeded     — round_count exceeded max_elicitation_rounds

    requestState: received from server as opaque str; echoed back unchanged.
    Never base64-decoded here (doc 2 B2; client-side is always echo-only).
    """
    params: dict = {
        "name": raw_name,
        "arguments": args or {},
        "_meta": get_meta().to_dict(),
    }
    round_count = 0

    while True:
        # ── Send and receive ──────────────────────────────────────────────────
        try:
            response = transport.send_request(METHOD_TOOLS_CALL, params)
        except TransportError as exc:
            return ToolCallResult.error(
                ToolErrorCategory.TRANSPORT_ERROR, str(exc)
            ).to_dict()

        # JSON-RPC error object in response (not a tool result — transport/protocol error)
        if "error" in response:
            err = response["error"]
            code = err.get("code")
            msg = err.get("message", "RPC error")
            if code == METHOD_NOT_FOUND:
                cat = ToolErrorCategory.NOT_FOUND
            elif code == INVALID_PARAMS:
                cat = ToolErrorCategory.INVALID_PARAMS
            else:
                cat = ToolErrorCategory.TRANSPORT_ERROR
            return ToolCallResult.error(cat, msg).to_dict()

        result = response.get("result") or {}

        # ── Branch: final result (success or isError tool result) ─────────────
        if result.get("resultType") != RESULT_TYPE_INPUT_REQUIRED:
            # Pass server result through unchanged (may have isError: true)
            return result

        # ── Branch: InputRequiredResult — drive one MRTR round ────────────────
        round_count += 1

        # Terminal: no handler (doc 2 C2: checked at first InputRequiredResult)
        if elicitation_handler is None:
            return ToolCallResult.error(
                ToolErrorCategory.ELICITATION_UNSUPPORTED,
                "no elicitation handler registered",
            ).to_dict()

        # Terminal: rounds exceeded (doc 2 B1)
        if round_count > max_elicitation_rounds:
            return ToolCallResult.error(
                ToolErrorCategory.ELICITATION_ROUNDS_EXCEEDED,
                f"elicitation exceeded {max_elicitation_rounds} rounds",
            ).to_dict()

        # requestState is opaque — pass to callback for context, echo back unchanged
        request_state: str = result.get("requestState", "")
        input_requests: list = result.get("inputRequests", [])

        callback_request = {
            "inputRequests": input_requests,
            "requestState": request_state,
            "round": round_count,
        }

        # ── Invoke callback (with timeout + teardown support) ─────────────────
        callback_result = _invoke_elicitation_callback(
            elicitation_handler,
            callback_request,
            elicitation_registry,
            elicitation_timeout_s,
        )

        # Terminal: teardown sentinel (doc 2 D1 cumulative-timeout fix)
        if callback_result is TEARDOWN_SENTINEL:
            return ToolCallResult.error(
                ToolErrorCategory.ELICITATION_ABORTED,
                "run_source teardown interrupted elicitation",
            ).to_dict()

        # Terminal: timeout
        if callback_result is _TIMEOUT_SENTINEL:
            return ToolCallResult.error(
                ToolErrorCategory.ELICITATION_TIMEOUT,
                f"elicitation timed out after {elicitation_timeout_s}s",
            ).to_dict()

        # Callback exception
        if isinstance(callback_result, Exception):
            return ToolCallResult.error(
                ToolErrorCategory.EXECUTION_FAILURE,
                f"elicitation callback raised: {callback_result}",
            ).to_dict()

        if not isinstance(callback_result, dict):
            return ToolCallResult.error(
                ToolErrorCategory.EXECUTION_FAILURE,
                "elicitation callback returned invalid response",
            ).to_dict()

        # Terminal: decline — not an error (human said no is a valid outcome)
        if callback_result.get("action") == "decline":
            return ToolCallResult(
                content=[ToolContent.make_text(json.dumps({"action": "decline"}))],
                is_error=False,
            ).to_dict()

        # Continue: accept — build continuation params
        # requestState echoed back unchanged (doc 2 B2; opaque to client)
        input_responses = _build_input_responses(input_requests, callback_result)
        params = {
            "name": raw_name,
            "arguments": args or {},
            "inputResponses": input_responses,
            "requestState": request_state,   # opaque echo
            "_meta": get_meta().to_dict(),
        }


def _build_input_responses(input_requests: list, accepted: dict) -> list:
    """Build inputResponses from accepted callback result.

    The callback returned {"action": "accept", "content": {...}}.
    Pair each inputRequest id with the corresponding content value.
    """
    content = accepted.get("content") or {}
    responses = []
    for req in input_requests:
        req_id = req.get("id")
        if req_id is not None and req_id in content:
            responses.append({"id": req_id, "content": content[req_id]})
        elif req_id is not None:
            responses.append({"id": req_id, "content": None})
    return responses


# ── Handler factory ───────────────────────────────────────────────────────────

def _make_tool_handler(
    raw_name: str,
    transport: McpTransport,
    get_elicitation_handler: Callable[[], Callable | None],
    elicitation_registry: ActiveElicitationRegistry | None,
    elicitation_timeout_s: float,
    max_elicitation_rounds: int,
    get_meta: Callable[[], RequestMeta],
) -> Callable:
    """Return a Python callable to register in _python_registered_tools.

    raw_name is the wire name (alias already stripped — doc 1 B1).
    get_elicitation_handler and get_meta are zero-arg callables so handler/meta
    changes after connect() are reflected in subsequent tool calls (Phase F
    capability gating — roots/sampling only in _meta when configured).
    """
    def handler(args: dict) -> dict:
        return _run_tools_call(
            raw_name,
            args,
            transport,
            get_elicitation_handler(),
            elicitation_registry,
            elicitation_timeout_s,
            max_elicitation_rounds,
            get_meta,
        )
    handler.__name__ = f"mcp_handler_{raw_name}"
    return handler


# ── McpClient ─────────────────────────────────────────────────────────────────

class McpClient:
    """Manages MCP client connections and elicitation config.

    Usage:
        client = McpClient()
        client.set_elicitation_handler(lambda req: {"action": "accept", ...})
        conn = client.connect(transport, alias="srv1")
        # tools registered as mcp.srv1.<name> in runtime (if runtime provided)
        conn.close()
    """

    def __init__(
        self,
        *,
        elicitation_timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_elicitation_rounds: int = _DEFAULT_MAX_ROUNDS,
        elicitation_registry: ActiveElicitationRegistry | None = None,
    ) -> None:
        self._elicitation_handler: Callable | None = None
        self._sampling_handler: Callable | None = None   # Phase F2 (doc 5 B3)
        self._elicitation_timeout_s = elicitation_timeout_s
        self._max_elicitation_rounds = max_elicitation_rounds
        self._registry = elicitation_registry or ActiveElicitationRegistry()
        self._connections: dict[str, McpConnection] = {}

    # ── Capability gating (doc 5 C3, doc 4 D2 client mirror) ─────────────────

    def _build_meta(self, suppress_server_initiated: bool = False) -> RequestMeta:
        """Build the current outbound _meta reflecting configured capabilities.

        roots and sampling are only advertised when (doc 5 C3):
          - configured (handler set / roots provided), AND
          - the connection can actually service inbound requests.

        suppress_server_initiated=True for HTTP connections (TD-007): HTTP is
        stateless; the server cannot send roots/sampling requests to the client,
        so advertising those capabilities would be a lie. Even if a handler is
        configured, HTTP connections must not claim those capabilities.

        tools and elicitation are always advertised (elicitation here means the
        MRTR path in tools/call responses — which works over HTTP since it is
        response-folded, not server-initiated).
        """
        caps: dict = {"tools": {}, "elicitation": {}}
        if not suppress_server_initiated:
            if any(conn.roots for conn in self._connections.values()):
                caps["roots"] = {}
            if self._sampling_handler is not None:
                caps["sampling"] = {}
        return RequestMeta(capabilities=caps, client_info=_CLIENT_INFO)

    # ── Handler registration (Phase C + F) ───────────────────────────────────

    def set_elicitation_handler(self, fn: Callable | None) -> None:
        """Register the elicitation callback (doc 2 C1, Decision 13).

        fn(request: dict) -> dict  where request has inputRequests, requestState, round.
        Return {"action": "accept", "content": {...}} or {"action": "decline"}.
        Also invoked for direct elicitation/create inbound requests (Phase F3).
        May be set/changed after connect().
        """
        self._elicitation_handler = fn

    def set_sampling_handler(self, fn: Callable | None) -> None:
        """Register the sampling callback (doc 5 B3) — symmetric with set_elicitation_handler.

        fn(request: dict) -> dict  where request is the sampling/createMessage params.
        Return the completion result dict.
        Absent handler → {"action": "decline"} sent to server.
        Advertising sampling capability in _meta requires a handler to be set.
        """
        self._sampling_handler = fn

    # ── Connection management ────────────────────────────────────────────────

    def connect(
        self,
        transport: McpTransport,
        alias: str,
        url: str = "",
        bearer_token: str | None = None,
        runtime=None,
        roots: list | None = None,
    ) -> McpConnection:
        """Discover server tools and (optionally) register them in a NodusRuntime.

        Steps (doc 3 D1):
          1. Detect transport type (HTTP vs stdio) for capability suppression (TD-007).
          2. Send server/discover (with capability-gated _meta) to learn tools.
          3. For each tool: create a handler closure (alias stripped — doc 1 B1).
          4. If runtime provided: register each tool as mcp.<alias>.<name>.
          5. Wire the transport's inbound-request handler (stdio only — TD-007).
          6. Return McpConnection handle.

        roots: list of {uri, name} dicts; if provided, advertise roots capability
        and auto-respond to roots/list inbound requests (Phase F1, doc 5 A2).
        HTTP connections suppress roots/sampling in _meta regardless (TD-007).
        runtime: NodusRuntime instance. If None, tools are discovered but not
        registered (useful for unit tests).
        """
        from .http import HttpTransport as _HttpTransport
        is_http = isinstance(transport, _HttpTransport)

        # Build meta for discover — suppress server-initiated caps for HTTP (TD-007)
        if is_http:
            disc_meta = RequestMeta(
                capabilities={"tools": {}, "elicitation": {}},
                client_info=_CLIENT_INFO,
            )
        else:
            disc_caps: dict = {"tools": {}, "elicitation": {}}
            if roots:
                disc_caps["roots"] = {}
            if self._sampling_handler is not None:
                disc_caps["sampling"] = {}
            disc_meta = RequestMeta(capabilities=disc_caps, client_info=_CLIENT_INFO)

        discover_resp = transport.send_request(
            METHOD_SERVER_DISCOVER,
            {"_meta": disc_meta.to_dict()},
        )

        if "error" in discover_resp:
            raise TransportError(
                f"server/discover failed: {discover_resp['error'].get('message')}"
            )

        disc_result = discover_resp.get("result") or {}
        server_info = disc_result.get("serverInfo") or {}
        server_caps = (
            disc_result.get("_meta", {}).get("capabilities")
            or disc_result.get("capabilities")
            or {}
        )
        tools_raw: list = disc_result.get("tools") or []
        registered_tools: list[str] = []

        for tool_raw in tools_raw:
            raw_name: str = tool_raw.get("name", "")
            if not raw_name:
                continue

            description: str = tool_raw.get("description") or raw_name
            input_schema: dict = tool_raw.get("inputSchema") or {}
            deprecated: bool = bool(
                (tool_raw.get("annotations") or {}).get("deprecated", False)
            )

            # get_meta uses suppress_server_initiated for HTTP (TD-007)
            _suppress = is_http
            tool_handler = _make_tool_handler(
                raw_name=raw_name,
                transport=transport,
                get_elicitation_handler=lambda: self._elicitation_handler,
                elicitation_registry=self._registry,
                elicitation_timeout_s=self._elicitation_timeout_s,
                max_elicitation_rounds=self._max_elicitation_rounds,
                get_meta=lambda _s=_suppress: self._build_meta(_s),
            )

            namespaced_name = f"mcp.{alias}.{raw_name}"
            if runtime is not None:
                runtime.tool_registry.register({
                    "name": namespaced_name,
                    "handler": tool_handler,
                    "description": description,
                    "schema": input_schema,
                    "deprecated": deprecated,
                })
            registered_tools.append(namespaced_name)

        conn = McpConnection(
            alias=alias,
            url=url,
            transport=transport,
            bearer_token=bearer_token,
            server_info=server_info,
            server_capabilities=server_caps,
            registered_tools=registered_tools,
            roots=list(roots) if roots else [],
            stateless_http=is_http,
        )
        self._connections[alias] = conn

        # Wire Phase F inbound-request routing onto the transport (stdio only).
        # HTTP has no persistent channel for server-initiated requests (TD-007).
        if not is_http and hasattr(transport, "_inbound_request_handler"):
            transport._inbound_request_handler = self._build_inbound_handler(conn)

        return conn

    # ── Phase D: resources ───────────────────────────────────────────────────

    def resources_list(self, alias: str) -> dict:
        """Fetch the server's resource list (resources/list)."""
        conn = self._get_connection(alias)
        return _simple_call(
            conn.transport, METHOD_RESOURCES_LIST,
            {"_meta": self._build_meta(conn.stateless_http).to_dict()},
        )

    def resources_read(self, alias: str, uri: str) -> dict:
        """Read one resource by URI (resources/read)."""
        conn = self._get_connection(alias)
        return _simple_call(
            conn.transport, METHOD_RESOURCES_READ,
            {"uri": uri, "_meta": self._build_meta(conn.stateless_http).to_dict()},
        )

    # ── Phase E: prompts ─────────────────────────────────────────────────────

    def prompts_list(self, alias: str) -> dict:
        """Fetch the server's prompt list (prompts/list)."""
        conn = self._get_connection(alias)
        return _simple_call(
            conn.transport, METHOD_PROMPTS_LIST,
            {"_meta": self._build_meta(conn.stateless_http).to_dict()},
        )

    def prompts_get(self, alias: str, name: str, arguments: dict | None = None) -> dict:
        """Fetch a rendered prompt by name (prompts/get)."""
        conn = self._get_connection(alias)
        params: dict = {"name": name, "_meta": self._build_meta(conn.stateless_http).to_dict()}
        if arguments:
            params["arguments"] = arguments
        return _simple_call(conn.transport, METHOD_PROMPTS_GET, params)

    # ── Phase F: inbound-request routing (F1 Roots, F2 Sampling, F3 Elicitation) ─

    def _build_inbound_handler(self, conn: McpConnection) -> Callable:
        """Return the per-connection callable wired on StdioTransport._inbound_request_handler.

        Handles three server-initiated request methods (Phase F):
          roots/list           → F1: return this connection's configured roots
          sampling/createMessage → F2: invoke sampling_handler or decline
          elicitation/create   → F3: invoke elicitation_handler or decline
        """
        roots_snapshot = list(conn.roots)  # copy at connect time; immutable per connection

        def handle(method: str, params: dict) -> dict:
            if method == METHOD_ROOTS_LIST:
                # F1: auto-respond with configured roots (doc 5 A2)
                return {"roots": roots_snapshot}

            if method == METHOD_SAMPLING_CREATE_MESSAGE:
                # F2: invoke handler or decline (doc 5 B3)
                fn = self._sampling_handler
                if fn is None:
                    return {"action": "decline"}
                return fn(params)

            if method == METHOD_ELICITATION_CREATE:
                # F3: invoke elicitation handler or decline (SEP-2322 direct request)
                fn = self._elicitation_handler
                if fn is None:
                    return {"action": "decline"}
                return fn(params)

            # Unknown method — raise so _execute_inbound sends INTERNAL_ERROR
            raise ValueError(f"Unknown server-initiated method: {method!r}")

        return handle

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _get_connection(self, alias: str) -> McpConnection:
        conn = self._connections.get(alias)
        if conn is None:
            raise KeyError(f"no connection with alias '{alias}'")
        return conn

    def disconnect(self, alias: str, runtime=None) -> None:
        """Tear down a connection and unregister its tools from runtime."""
        conn = self._connections.pop(alias, None)
        if conn is None:
            return

        if runtime is not None:
            for tool_name in conn.registered_tools:
                try:
                    runtime.tool_registry.unregister(tool_name)
                except KeyError:
                    pass

        self._registry.teardown()
        conn.close()
