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
Phase J: server resources (list + read, handler-configured, no language-side registry)
Phase K: server prompts (list + get, handler-configured, required-arg validation)
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
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    METHOD_PROMPTS_LIST,
    METHOD_PROMPTS_GET,
    ToolDefinition,
    ToolCallResult,
    ToolErrorCategory,
    ElicitationRequest,
    SamplingRequest,
    RootsRequest,
    InputRequiredResult,
    SamplingRequiredResult,
    RootsRequiredResult,
    ResourceDescriptor,
    ResourceContent,
    PromptDescriptor,
    PromptMessage,
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
        self._runtime = runtime          # duck-typed runtime; only .tool_registry.* used
        self._elicitation_handler = None  # Phase L: set to advertise elicitation capability
        self._sampling_handler = None     # Phase L
        self._roots: list = []            # Phase L: server's own roots (for roots/list responses)

        # Phase L: re-call engine config (round cap, same as C's MRTR cap, doc 2 B1)
        self._max_elicitation_rounds: int = 10

        # Phase J: resource handlers (handler-configured; no language-side registry)
        self._resource_list_handler = None  # fn() → list[dict]   — enumerate resources
        self._resource_read_handler = None  # fn(uri) → list[dict] — read resource contents

        # Phase K: prompt handlers (handler-configured; no language-side registry)
        self._prompt_list_handler = None    # fn() → list[dict]             — enumerate prompts
        self._prompt_get_handler = None     # fn(name, args) → dict         — get rendered prompt

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

    def set_resource_list_handler(self, fn) -> None:
        """Register the resources/list handler (Phase J).
        fn() → list[dict]  — each dict is a resource descriptor:
            {uri, name, mimeType?, description?}
        Gates 'resources' in server capabilities. If absent → resources/list returns -32601.
        """
        self._resource_list_handler = fn

    def set_resource_read_handler(self, fn) -> None:
        """Register the resources/read handler (Phase J).
        fn(uri: str) → list[dict]  — each dict is resource content:
            {uri, text?} or {uri, blob?}  (exactly one of text or blob per item)
        Raise KeyError for unknown uri (→ -32601). Raise for other errors (→ -32603).
        """
        self._resource_read_handler = fn

    def set_prompt_list_handler(self, fn) -> None:
        """Register the prompts/list handler (Phase K).
        fn() → list[dict]  — each dict is a prompt descriptor:
            {name, description?, arguments?: [{name, description?, required?}]}
        Gates 'prompts' in server capabilities. If absent → prompts/list returns -32601.
        Used to validate required arguments in prompts/get.
        """
        self._prompt_list_handler = fn

    def set_prompt_get_handler(self, fn) -> None:
        """Register the prompts/get handler (Phase K).
        fn(name: str, arguments: dict) → dict  — rendered prompt:
            {description?, messages: [{role, content: {type, ...}}]}
        Raise KeyError for unknown prompt name (→ -32601).
        """
        self._prompt_get_handler = fn

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
        if method == METHOD_RESOURCES_LIST:      # Phase J
            return self._handle_resources_list(params)
        if method == METHOD_RESOURCES_READ:      # Phase J
            return self._handle_resources_read(params)
        if method == METHOD_PROMPTS_LIST:        # Phase K
            return self._handle_prompts_list(params)
        if method == METHOD_PROMPTS_GET:         # Phase K
            return self._handle_prompts_get(params)
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
        if self._resource_list_handler is not None:
            caps["resources"] = {}
        if self._prompt_list_handler is not None:
            caps["prompts"] = {}

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
        """Handle tools/call — initial call path (Phases I + L).

        L adds: requestState check at the top (continuation path) and
        sentinel detection replacing I's stub. The re-call engine
        (_handle_sentinel, _handle_continuation) is separate from I's
        invoke path so the two concerns don't blur.
        """
        # L: continuation path — has requestState from a prior sentinel return
        request_state_blob = params.get("requestState")
        if request_state_blob:
            return self._handle_tools_call_continuation(params, request_state_blob)

        # I: initial path (no requestState)
        if self._runtime is None:
            raise _MethodNotFoundError("no runtime configured")

        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _InvalidParamsError("tools/call requires a non-empty string 'name'")

        entry = self._runtime.tool_registry.lookup(name)
        if entry is None:
            raise _MethodNotFoundError(f"Tool '{name}' is not registered")

        args = params.get("arguments") or {}
        schema = entry.get("schema") or {}
        val_err = _validate_args(args, schema)
        if val_err:
            raise _InvalidParamsError(f"Tool '{name}': {val_err}")

        try:
            result = self._runtime.tool_registry.invoke(name, args)
        except KeyError:
            raise _MethodNotFoundError(f"Tool '{name}' is not registered")
        except Exception as exc:
            return ToolCallResult.error(ToolErrorCategory.EXECUTION_FAILURE, str(exc)).to_dict()

        # L: sentinel return → re-call engine (replaces I's Phase L stub)
        if isinstance(result, (ElicitationRequest, SamplingRequest, RootsRequest)):
            return self._handle_sentinel(result, name, args, round_count=1)

        if isinstance(result, dict) and result.get("__nodus_err__"):
            msg = result.get("message") or repr(result)
            return ToolCallResult.error(ToolErrorCategory.EXECUTION_FAILURE, msg).to_dict()

        return ToolCallResult.from_python_value(result).to_dict()

    # ── Phase L: re-call engine ────────────────────────────────────────────────────

    def _handle_sentinel(
        self,
        sentinel: ElicitationRequest | SamplingRequest | RootsRequest,
        tool_name: str,
        orig_args: dict,
        round_count: int,
    ) -> dict:
        """One re-call engine for all sentinel types (doc 5 B2 anti-drift).

        Dispatches by sentinel type — ElicitationRequest, SamplingRequest, and
        RootsRequest all flow through this function with a type switch. If L
        had _handle_elicitation_recall and _handle_sampling_recall as parallel
        functions, doc 5 B2's anti-drift settlement would be broken.

        Re-call, not block (the inversion of C): no thread parks here.
        The sentinel is returned, requestState is encoded, the elicitation
        goes out as the response to the inbound tools/call. The continuation
        arrives as a new request — transport-agnostic, because it rides the
        response/re-call channel, not an out-of-band push (doc 4 C1).
        """
        if round_count > self._max_elicitation_rounds:
            return ToolCallResult.error(
                ToolErrorCategory.ELICITATION_ROUNDS_EXCEEDED,
                f"elicitation exceeded {self._max_elicitation_rounds} rounds",
            ).to_dict()

        if isinstance(sentinel, ElicitationRequest):
            rs = _encode_request_state(tool_name, orig_args, round_count, sentinel.state, "elicit")
            return InputRequiredResult(
                input_requests=sentinel.input_requests,
                request_state=rs,
            ).to_dict()
        if isinstance(sentinel, SamplingRequest):
            rs = _encode_request_state(tool_name, orig_args, round_count, sentinel.state, "sample")
            return SamplingRequiredResult(
                messages=sentinel.messages,
                params=sentinel.params,
                request_state=rs,
            ).to_dict()
        if isinstance(sentinel, RootsRequest):
            rs = _encode_request_state(tool_name, orig_args, round_count, sentinel.state, "roots")
            return RootsRequiredResult(request_state=rs).to_dict()
        # unreachable — isinstance guard above covers all sentinel types
        raise _InvalidParamsError(f"Unknown sentinel: {type(sentinel).__name__}")

    def _handle_tools_call_continuation(self, params: dict, rs_blob: str) -> dict:
        """Handle a tools/call continuation (the re-called path).

        Decodes requestState (in exactly one place — C3 server-side),
        injects the response into orig_args, re-invokes the tool.
        The tool sees its checkpoint state restored as if it was mid-execution,
        but there is no parked thread — this is a fresh invocation.
        """
        if self._runtime is None:
            raise _MethodNotFoundError("no runtime configured")

        state = _decode_request_state(rs_blob)
        if state is None:
            raise _InvalidParamsError("Invalid requestState: cannot decode")

        tool_name = state.get("t")
        orig_args = state.get("a") or {}
        round_count = state.get("r", 1)
        handler_state = state.get("s") or {}
        sentinel_type = state.get("st")

        if not tool_name or not sentinel_type:
            raise _InvalidParamsError("Invalid requestState: missing fields")

        # Cap check (same bound as initial-call path, doc 2 B1)
        if round_count > self._max_elicitation_rounds:
            return ToolCallResult.error(
                ToolErrorCategory.ELICITATION_ROUNDS_EXCEEDED,
                f"elicitation exceeded {self._max_elicitation_rounds} rounds",
            ).to_dict()

        # Inject the client's response into augmented_args for the re-call
        response_data = _extract_continuation_response(params, sentinel_type)
        augmented_args = _inject_continuation(orig_args, handler_state, sentinel_type, response_data)

        try:
            result = self._runtime.tool_registry.invoke(tool_name, augmented_args)
        except KeyError:
            raise _MethodNotFoundError(f"Tool '{tool_name}' is not registered")
        except Exception as exc:
            return ToolCallResult.error(ToolErrorCategory.EXECUTION_FAILURE, str(exc)).to_dict()

        # Another sentinel → loop through the same engine (bounded by cap above)
        if isinstance(result, (ElicitationRequest, SamplingRequest, RootsRequest)):
            return self._handle_sentinel(result, tool_name, orig_args, round_count + 1)

        if isinstance(result, dict) and result.get("__nodus_err__"):
            msg = result.get("message") or repr(result)
            return ToolCallResult.error(ToolErrorCategory.EXECUTION_FAILURE, msg).to_dict()

        return ToolCallResult.from_python_value(result).to_dict()


    # ── Phase J: server resources ─────────────────────────────────────────────────

    def _handle_resources_list(self, params: dict) -> dict:
        """Handle resources/list (Phase J).

        Calls the configured list handler; emits RC-shaped ResourceDescriptor dicts.
        No handler → -32601 (unsupported, not configured).
        Reuses ResourceDescriptor from Phase D — same RC shape, server emits/client parses.
        No resources/subscribe (TD-006: server-push deferred to v0.2).
        """
        fn = self._resource_list_handler
        if fn is None:
            raise _MethodNotFoundError("resources/list is not configured")
        try:
            raw = fn() or []
        except Exception as exc:
            raise Exception(f"Resource list handler failed: {exc}")
        return {"resources": [ResourceDescriptor.from_dict(r).to_dict() for r in raw]}

    def _handle_resources_read(self, params: dict) -> dict:
        """Handle resources/read (Phase J).

        Validates uri param → calls read handler → emits ResourceContent dicts.
        Handler raises KeyError for unknown uri → -32601.
        text/blob invariant (exactly one per content item) enforced by ResourceContent.
        """
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise _InvalidParamsError("resources/read requires a non-empty string 'uri'")
        fn = self._resource_read_handler
        if fn is None:
            raise _MethodNotFoundError("resources/read is not configured")
        try:
            raw = fn(uri) or []
        except KeyError:
            raise _MethodNotFoundError(f"Resource '{uri}' not found")
        except Exception as exc:
            raise Exception(f"Resource read handler failed: {exc}")
        return {"contents": [ResourceContent.from_dict(c).to_dict() for c in raw]}

    # ── Phase K: server prompts ───────────────────────────────────────────────────

    def _handle_prompts_list(self, params: dict) -> dict:
        """Handle prompts/list (Phase K).

        Calls the configured list handler; emits RC-shaped PromptDescriptor dicts.
        Reuses PromptDescriptor/PromptArgument from Phase E — same RC shape.
        """
        fn = self._prompt_list_handler
        if fn is None:
            raise _MethodNotFoundError("prompts/list is not configured")
        try:
            raw = fn() or []
        except Exception as exc:
            raise Exception(f"Prompt list handler failed: {exc}")
        return {"prompts": [PromptDescriptor.from_dict(p).to_dict() for p in raw]}

    def _handle_prompts_get(self, params: dict) -> dict:
        """Handle prompts/get (Phase K).

        Validates name param → checks required arguments (using list handler if available,
        mirror of E's argument schema) → calls get handler → emits messages.
        Missing required argument → -32602 (K-specific validation, lighter than I's full schema).
        Handler raises KeyError for unknown prompt name → -32601.
        """
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _InvalidParamsError("prompts/get requires a non-empty string 'name'")

        fn = self._prompt_get_handler
        if fn is None:
            raise _MethodNotFoundError("prompts/get is not configured")

        arguments: dict = params.get("arguments") or {}

        # K-specific validation: check required arguments against prompt definition.
        # Uses the list handler as the authoritative source of required-arg metadata.
        if self._prompt_list_handler is not None:
            try:
                prompts_raw = self._prompt_list_handler() or []
            except Exception:
                prompts_raw = []
            for p in prompts_raw:
                if p.get("name") == name:
                    pd = PromptDescriptor.from_dict(p)
                    for arg in pd.arguments:
                        if arg.required and arg.name not in arguments:
                            raise _InvalidParamsError(
                                f"Prompt '{name}': missing required argument '{arg.name}'"
                            )
                    break

        try:
            raw = fn(name, arguments)
        except KeyError:
            raise _MethodNotFoundError(f"Prompt '{name}' not found")
        except Exception as exc:
            raise Exception(f"Prompt get handler failed: {exc}")

        messages = []
        for msg in (raw.get("messages") or []):
            pm = PromptMessage.from_dict(msg)
            messages.append({"role": pm.role, "content": pm.content.to_dict()})

        result: dict = {"messages": messages}
        if raw.get("description"):
            result["description"] = raw["description"]
        return result


# ── Phase L helpers: re-call engine (requestState encode/decode, injection) ─────

def _encode_request_state(
    tool_name: str,
    orig_args: dict,
    round_count: int,
    handler_state: dict,
    sentinel_type: str,
) -> str:
    """Encode server-side requestState as an opaque base64 blob.

    The one encode location (C3 server-side). The blob carries what a
    re-called tool needs:
      t: tool name  a: original args  r: round count
      s: handler checkpoint (sentinel.state)  st: sentinel type

    sentinel_type ("elicit" | "sample" | "roots") is stored so the
    continuation path knows which injection key to use on re-call.
    """
    import base64 as _b64, json as _json
    blob = {
        "t": tool_name,
        "a": orig_args,
        "r": round_count,
        "s": handler_state or {},
        "st": sentinel_type,
    }
    return _b64.b64encode(_json.dumps(blob, separators=(",", ":")).encode()).decode()


def _decode_request_state(blob: str) -> dict | None:
    """Decode requestState blob. The one decode location (C3 server-side).

    Returns None on any decode error — caller converts to -32602.
    Invalid base64/JSON is handled gracefully; opaqueness boundary enforced.
    """
    import base64 as _b64, json as _json
    try:
        return _json.loads(_b64.b64decode(blob).decode())
    except Exception:
        return None


def _extract_continuation_response(params: dict, sentinel_type: str) -> object:
    """Extract the client's response from a continuation tools/call params."""
    if sentinel_type == "elicit":
        return params.get("inputResponses") or []
    if sentinel_type == "sample":
        return params.get("samplingResult")
    if sentinel_type == "roots":
        return params.get("roots") or []
    return None


def _inject_continuation(
    orig_args: dict,
    handler_state: dict,
    sentinel_type: str,
    response_data: object,
) -> dict:
    """Inject the continuation response into a copy of orig_args (doc 4 C2).

    The tool handler checks for the injection key to know it's on a re-call:
      __elicitation_state__: {"responses": ..., "state": <handler checkpoint>}
      __sampling_state__:    {"result": ..., "state": <handler checkpoint>}
      __roots__:             [list of roots dicts]
    """
    augmented = dict(orig_args)
    if sentinel_type == "elicit":
        augmented["__elicitation_state__"] = {"responses": response_data, "state": handler_state}
    elif sentinel_type == "sample":
        augmented["__sampling_state__"] = {"result": response_data, "state": handler_state}
    elif sentinel_type == "roots":
        augmented["__roots__"] = response_data
    return augmented


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
