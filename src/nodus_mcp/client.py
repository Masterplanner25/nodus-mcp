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

# Client _meta sent with every outbound request (doc 1 C1).
_CLIENT_META = RequestMeta(
    capabilities={"tools": {}, "elicitation": {}, "roots": {}, "sampling": {}},
    client_info={"name": "nodus-mcp", "version": "0.1.0"},
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
    client_meta: RequestMeta,
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
        "_meta": client_meta.to_dict(),
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
            "_meta": client_meta.to_dict(),
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
    client_meta: RequestMeta,
) -> Callable:
    """Return a Python callable to register in _python_registered_tools.

    raw_name is the wire name (alias already stripped — doc 1 B1).
    get_elicitation_handler is a zero-arg callable returning the current
    handler (allows handler to be set after connect()).
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
            client_meta,
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
        self._elicitation_timeout_s = elicitation_timeout_s
        self._max_elicitation_rounds = max_elicitation_rounds
        self._registry = elicitation_registry or ActiveElicitationRegistry()
        self._connections: dict[str, McpConnection] = {}

    def set_elicitation_handler(self, fn: Callable | None) -> None:
        """Register the elicitation callback (doc 2 C1, Decision 13).

        fn(request: dict) -> dict  where request has inputRequests, requestState, round.
        Return {"action": "accept", "content": {...}} or {"action": "decline"}.
        May be set/changed after connect().
        """
        self._elicitation_handler = fn

    def connect(
        self,
        transport: McpTransport,
        alias: str,
        url: str = "",
        bearer_token: str | None = None,
        runtime=None,
    ) -> McpConnection:
        """Discover server tools and (optionally) register them in a NodusRuntime.

        Steps (doc 3 D1):
          1. Send server/discover to learn capabilities and tool list.
          2. For each tool: create a handler closure (alias stripped — doc 1 B1).
          3. If runtime provided: register each tool as mcp.<alias>.<name>.
          4. Return McpConnection handle.

        runtime: a NodusRuntime instance. If None, tools are discovered but not
        registered (useful for unit tests and server-mode usage).
        """
        discover_resp = transport.send_request(
            METHOD_SERVER_DISCOVER,
            {"_meta": _CLIENT_META.to_dict()},
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

            handler = _make_tool_handler(
                raw_name=raw_name,
                transport=transport,
                get_elicitation_handler=lambda: self._elicitation_handler,
                elicitation_registry=self._registry,
                elicitation_timeout_s=self._elicitation_timeout_s,
                max_elicitation_rounds=self._max_elicitation_rounds,
                client_meta=_CLIENT_META,
            )

            namespaced_name = f"mcp.{alias}.{raw_name}"

            if runtime is not None:
                runtime.tool_registry.register({
                    "name": namespaced_name,
                    "handler": handler,
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
        )
        self._connections[alias] = conn
        return conn

    # ── Phase D: resources ───────────────────────────────────────────────────

    def resources_list(self, alias: str) -> dict:
        """Fetch the server's resource list (resources/list).

        Returns the raw result dict: {resources: [{uri, name, mimeType?, description?}]}
        or a ToolCallResult.error dict on failure.
        Use ResourceDescriptor.from_dict(r) to parse individual entries.
        """
        conn = self._get_connection(alias)
        return _simple_call(
            conn.transport,
            METHOD_RESOURCES_LIST,
            {"_meta": _CLIENT_META.to_dict()},
        )

    def resources_read(self, alias: str, uri: str) -> dict:
        """Read one resource by URI (resources/read).

        Returns {contents: [{uri, text?}|{uri, blob?}]} or an error dict.
        blob values are base64 strings; decoding is the host's responsibility.
        Use ResourceContent.from_dict(c) to parse individual content items.
        """
        conn = self._get_connection(alias)
        return _simple_call(
            conn.transport,
            METHOD_RESOURCES_READ,
            {"uri": uri, "_meta": _CLIENT_META.to_dict()},
        )

    # ── Phase E: prompts ─────────────────────────────────────────────────────

    def prompts_list(self, alias: str) -> dict:
        """Fetch the server's prompt list (prompts/list).

        Returns {prompts: [{name, description?, arguments?}]} or an error dict.
        Use PromptDescriptor.from_dict(p) to parse individual entries.
        """
        conn = self._get_connection(alias)
        return _simple_call(
            conn.transport,
            METHOD_PROMPTS_LIST,
            {"_meta": _CLIENT_META.to_dict()},
        )

    def prompts_get(
        self,
        alias: str,
        name: str,
        arguments: dict | None = None,
    ) -> dict:
        """Fetch a rendered prompt by name (prompts/get).

        Returns {description?, messages: [{role, content}]} or an error dict.
        Use PromptMessage.from_dict(m) to parse individual messages.
        """
        conn = self._get_connection(alias)
        params: dict = {"name": name, "_meta": _CLIENT_META.to_dict()}
        if arguments:
            params["arguments"] = arguments
        return _simple_call(conn.transport, METHOD_PROMPTS_GET, params)

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
