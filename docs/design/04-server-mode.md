# nodus-mcp Phase 1 — Design Doc 4: Server Mode

**Doc:** 04-server-mode.md  
**Phase:** 1 (design)  
**Status:** Complete — 2026-05-28  
**Decisions grounded:** 1, 2, 3, 8, 10, 16  
**Covers:** Question clusters A–E from the Phase 1 design pass.

---

## Purpose

Server mode inverts the adapter layer: instead of Nodus calling external MCP
tools, external MCP clients call Nodus tools. This doc defines enumeration,
inbound invocation, server-issued elicitation, server lifecycle, and the
simultaneous client+server design surface.

Inherited constraints in force throughout:
- Doc 1 B1: raw registry names outbound, no alias prefix.
- Doc 1 C1: capabilities read per-request from inbound `_meta`; sent in
  outbound `_meta`; no cache.
- Doc 1 D-table: same error mapping, we are now the producer.
- Decision 3: `std:tool` registry is source of truth.

---

## A — Enumeration: registry → `tools/list`

### A1. Same registry, read direction inverted

`NodusRuntime.tool_registry.list_tools()` is the source for `tools/list`
(Decision 3). `list_tools()` (`embedding.py:157–172`) merges:
- `_python_registered_tools` — tools registered via the embedding API,
  persisting across `run_source()` calls.
- `vm.tool_registry` — tools registered inside a live Nodus script during
  an active `run_source()`, merged with lock.

This is the same registry that doc 1 A3 invokes tools against for the client
role. Server enumeration is that same registry read in the other direction:
enumerate all entries and emit each as a JSON-RPC tool definition.

The server walks `list_tools()` on every inbound `tools/list` request.
There is no cache of the tool list — the list is dynamic; tools may be
registered or unregistered between calls. A stale snapshot would violate
the source-of-truth guarantee.

### A2. Deprecated tools: `annotations.deprecated: true` on the wire

Every registry entry carries `deprecated: bool` (`embedding.py:76`,
`tool_module.py:229`). On `tools/list`, the server reads `entry["deprecated"]`
and conditionally includes:

```json
{
  "name": "old_tool",
  "description": "...",
  "inputSchema": { "type": "object", ... },
  "annotations": {
    "deprecated": true
  }
}
```

Non-deprecated tools omit the `annotations` field entirely (not
`"annotations": {}`). This is the first time deprecated tools surface on
the wire — doc 1 B3 defined the rule; this is where it executes.

### A3. Schema → `inputSchema` outbound: verbatim with empty-schema fallback

Same rule as doc 1 B2, applied in the server direction. The registry stores
validated JSON Schema in `entry["schema"]`. The server emits it verbatim as
`inputSchema`. The one transform: if `entry["schema"]` is an empty dict
`{}` (tool registered with no schema), the server injects
`{"type": "object"}` as the minimum valid MCP `inputSchema`.

This is symmetric with doc 1 B2's inbound rule: both directions validate
`type: "object"` and pass the schema body through unchanged.

---

## B — Inbound invocation: the mirror of doc 1

### B1. Inbound `tools/call` invocation path

The server adapter receives an inbound `tools/call`, validates it, and
dispatches via `runtime.tool_registry.invoke(name, args)` — the same
`ToolRegistry.invoke()` defined in `embedding.py:98–142`. No change to the
invocation path; the client path and server path share it.

```
inbound tools/call { name, arguments, requestState, _meta }
  1. parse name, arguments from params
  2. schema validation: _validate_args(arguments, entry["schema"])
  3. if valid: runtime.tool_registry.invoke(name, args_as_python_dict)
     → embedding.py:98 → handler(args) or vm.run_closure(handler, [args])
  4. wrap result as JSON-RPC tools/call response
```

No `_root_vm`/`_caller_vm` traversal (doc 1 A3). `ToolRegistry.invoke()`
uses `vm.tool_registry.get()` when a VM is active, falling back to
`_python_registered_tools` when no VM is running. Since the MCP server
handles inbound requests at any time (not only during `run_source()`), the
fallback path (`_python_registered_tools`) is the normal production path
for persistently-registered tools.

### B2. Error mapping: we are the producer

Same table as doc 1 D — we now write the response:

| Condition | Response |
|---|---|
| Tool name not found in `list_tools()` | JSON-RPC error `-32601 MethodNotFound` |
| Arguments fail `_validate_args()` schema check | JSON-RPC error `-32602 InvalidParams` |
| `ToolRegistry.invoke()` raises `KeyError` (post-dispatch not-found) | JSON-RPC error `-32601` |
| Handler raises Python exception | Tool result `isError: true`, content: exception message |
| Handler returns `tool_error` err record | Tool result `isError: true`, content: err record fields |
| Server-side elicitation timeout (see C) | Tool result `isError: true`, `category: "elicitation_timeout"` |
| Handler returns `ElicitationRequest` (see C) | `InputRequiredResult` response (not an error) |

The adapter detects `tool_error` err records by checking for the
`__nodus_err__` key produced by `_to_host_value()` when translating a
Nodus `Record` with `kind="error"`. Normal result records without that
key are serialized as the tool result content.

### B3. Server read loop: `McpServerTransport`, not a reuse of `McpTransport`

The server needs to *accept* requests and *send* responses — the inverse of
the client's *send* requests and *receive* responses. `McpTransport`
(`send_request`, `send_notification`, `close`) is the wrong interface.

The server uses a parallel `McpServerTransport` with a `serve` interface:

```python
class McpServerTransport:
    def serve(self, handler: callable) -> None:
        """Accept and dispatch requests until close() is called.
        handler(method, params, request_id) → dict | None
        Called for each inbound request in the read loop.
        """
        ...

    def send_response(self, response: dict, request_id: Any) -> None:
        """Write a JSON-RPC response (used for multi-step flows)."""
        ...

    def close(self) -> None:
        ...
```

`McpServerTransport` implementations:

**`StdioServerTransport`:** Reads newline-delimited JSON from its own
`sys.stdin`, writes responses to `sys.stdout`. This is the "spawned child
process" case — the MCP client spawned us. The read loop is the server's
main thread (or a dedicated thread). Uses `McpCodec` for framing (shared
with the client transport per doc 3 A1).

**`HttpServerTransport`:** Listens on a TCP port, accepts HTTP POSTs,
calls `handler`, returns the result as a JSON HTTP response. Uses Python's
`http.server` or a lightweight WSGI wrapper for v0.1. Each POST is handled
synchronously in the server thread or a thread pool.

The `McpCodec` framing layer (doc 3 A1) is shared between client and server
transports. The read-loop architecture is not shared — client has a persistent
reader thread keyed to a pending map (doc 3 B1/B2); server has a request
dispatcher that calls a handler per request.

---

## C — Server-issued elicitation

### C1. Inversion: server → client, roles swapped

Client-mode elicitation (doc 2): remote server sends `InputRequiredResult` →
nodus-mcp adapter receives it → calls `elicitation_handler` callback → human
responds → sends continuation.

Server-mode elicitation: nodus-mcp server receives `tools/call` → our tool
handler, mid-execution, needs input from the MCP client → server returns
`InputRequiredResult` → client collects user input → client sends continuation
`tools/call` with `requestState`.

The MRTR loop structure is reused with roles swapped, but the implementation
is asymmetric. Client-mode uses a **blocking handler** (doc 2 A1): the
handler parks on `threading.Event.wait()` while the transport sends the
initial request and the callback waits for the human response. All of this
happens in one blocking call.

Server-mode cannot park on a blocking wait because:
1. Returning `InputRequiredResult` requires sending an HTTP response and
   closing the request. The handler cannot block the same request thread
   while waiting for a new HTTP request to arrive.
2. The continuation arrives as a new, separate inbound request. The server
   must be free to accept it.

Server-mode elicitation uses a **stateless re-call pattern** instead. The
handler is called twice (or N times), not suspended once:

```
Round 1:
  inbound: tools/call { name, arguments, no requestState }
  handler(args) → ElicitationRequest(input_requests, state)
  adapter: encode state → requestState blob
  outbound: InputRequiredResult { inputRequests, requestState }

Round 2:
  inbound: tools/call { name, arguments, inputResponses, requestState }
  adapter: decode requestState → state dict
  handler(args, inputResponses=..., elicitation_state=state) → final result
  outbound: ToolResult { content: [...] }
```

The `requestState` blob encodes whatever the handler placed in `state` on
round 1. The handler uses this to resume at the right logical point in its
execution on round 2. This reuses `requestState` exactly as designed in
doc 1 A1 and doc 2 B2 — the adapter serializes and deserializes it; the
handler writes and reads its own checkpoint.

### C2. API for a handler to request input: `ElicitationRequest` sentinel

There is no new Nodus language construct in v0.1 for server-side elicitation.
The mechanism works entirely at the Python adapter layer.

**Python callable handlers:** Return an `ElicitationRequest` object from
the handler. The server dispatcher detects it (before the result touches
the VM or `_to_runtime_value`) and returns `InputRequiredResult`:

```python
from nodus_mcp import ElicitationRequest

def my_handler(args):
    # Round 1: no elicitation_state present → need input
    if args.get("__elicitation_state__") is None:
        return ElicitationRequest(
            input_requests=[{
                "id": "q1",
                "message": "Confirm deletion of file?",
                "schema": {"type": "object",
                           "properties": {"confirmed": {"type": "boolean"}},
                           "required": ["confirmed"]},
            }],
            state={"action": "delete", "target": args["path"]},  # checkpoint
        )
    # Round 2: have responses
    responses = args["__elicitation_state__"]["responses"]  # injected by adapter
    if responses["q1"]["confirmed"]:
        _do_delete(args["path"])
        return {"status": "deleted"}
    return {"status": "cancelled"}
```

`ElicitationRequest` is a plain Python class. No VM involvement. The
dispatcher detects it by `isinstance(result, ElicitationRequest)` before
wrapping the result.

**Nodus closure handlers in v0.1:** Server-side elicitation is **not
supported** for Nodus closure handlers in v0.1. A Nodus closure handler
returning from `run_closure()` cannot carry an `ElicitationRequest` sentinel
back through `_to_host_value()` in a meaningful way without either a new
Nodus language construct or a new builtin.

A future `tool.elicit(requests)` std:tool builtin — described below — is
the v0.2 path for Nodus closures. It would be a new builtin function (no
new opcode) registered in the existing `CALL_BUILTIN` path:

```nodus
// Future v0.2 — NOT implemented in v0.1
let responses = tool.elicit([{
    id: "q1",
    message: "Confirm?",
    schema: {type: "object", properties: {confirmed: {type: "boolean"}}}
}])
```

The `tool.elicit()` builtin would return the elicitation response synchronously
from the closure's perspective (the closure is a coroutine; internally the
`_io_channels` mechanism would park it and let the server handle the
continuation). This is a v0.2 design target. No bytecode change is required —
it uses the existing `CALL_BUILTIN` opcode.

**Why `ElicitationRequest` is not a new opcode:**
The dispatcher checks the return value of `ToolRegistry.invoke()` in Python
before any opcode executes. The sentinel is an adapter-layer concept, invisible
to the VM. The handler returning `ElicitationRequest` from `handler(args)` is
equivalent to returning any other Python object — the VM never sees it.

### C3. Teardown asymmetry: server-side elicitation holds no blocking state

Client-mode elicitation (doc 2 D1): the Python handler parks on
`threading.Event.wait()`. Active handlers are registered in
`runtime._active_elicitations`. `_teardown_active_elicitations()` signals
them.

Server-mode elicitation: the handler does NOT block. It returns
`ElicitationRequest` and exits. The server sends `InputRequiredResult` as
the HTTP response and closes the request. The continuation will arrive as a
new inbound request — the server is free to accept it immediately.

No state is held between `InputRequiredResult` being sent and the continuation
arriving, aside from what the client carries in `requestState`. If the server
shuts down before the continuation arrives, the client will receive a connection
error on its next POST. This is correct behavior — the conversation is
stateless on the server side.

`_teardown_active_elicitations()` covers client-side blocking handlers only.
Server-side elicitation requires no teardown because no thread is parked.
The two inventories are separate and do not interact.

**Edge case — simultaneous client+server on one runtime:** If the runtime is
acting as both a client (with parked `threading.Event` handlers) and a server
(with stateless `ElicitationRequest` round-trips), `_teardown_active_elicitations()`
safely signals only the client-side parked events. Server-side elicitations
are not in `runtime._active_elicitations` and are unaffected.

---

## D — Server lifecycle and transport

### D1. Connect vs accept: per-transport server construction

The server role requires accept/listen, not connect. Per transport:

**`StdioServerTransport` construction:**
1. No subprocess — we ARE the subprocess.
2. Attach to `sys.stdin` / `sys.stdout` directly.
3. Start the read loop (either in the calling thread via `serve()` blocking,
   or in a background thread).
4. No `server/discover` at start — we wait for the client to call us.

**`HttpServerTransport` construction:**
1. Bind to `host:port`.
2. Start the HTTP server (blocking `serve_forever()` or threaded).
3. Register the adapter's request handler function.
4. No proactive outbound call — we wait for inbound POSTs.

Neither transport makes an outbound connection at construction. The server
is passive until the first request arrives.

### D2. `server/discover`: the server-mode home for capability reporting

`server/discover` is the inbound request type that doc 1 C2 deferred to this
doc. When a client calls `server/discover`, the server responds with its
capabilities and tool list:

```json
{
  "jsonrpc": "2.0",
  "id": "discover-1",
  "result": {
    "serverInfo": { "name": "nodus-mcp", "version": "0.1.0" },
    "_meta": {
      "capabilities": {
        "tools": {},
        "elicitation": {}
      }
    },
    "tools": [
      { "name": "...", "description": "...", "inputSchema": {...} }
    ]
  }
}
```

Capability set reported:
- `tools: {}` — always present (we can handle `tools/call`, `tools/list`)
- `elicitation: {}` — present if `runtime._elicitation_handler` is set
  (Decision 13: we can issue elicitation to clients)
- `roots: {}` — present if a roots handler is configured (see doc 5)
- `sampling: {}` — present if a sampling handler is configured (see doc 5)

Per Decision 1: no session initialization. `server/discover` is not a
handshake — it is a regular stateless request that can arrive at any time.
The capabilities we report reflect the runtime's current configuration.
If `set_elicitation_handler()` is called after the client has already called
`server/discover`, the client won't know. This is acceptable in v0.1 — the
client is expected to call `server/discover` once at startup.

### D3. Simultaneous client + server on one `NodusRuntime`

A single `NodusRuntime` can host both an `McpClient` (connecting to remote
servers) and an `McpServer` (accepting inbound calls) simultaneously.
Decision 10 (bidirectional in v0.1) permits this; Decision 3 (one registry)
is why it works without isolation.

**Registry interaction — documented, not an accident:**

- `McpClient.connect(url, alias="srv1")` registers tools as `mcp.srv1.tool_name`
  in `_python_registered_tools`.
- `McpServer.tools/list` enumerates `runtime.tool_registry.list_tools()`,
  which includes all `_python_registered_tools` entries — including
  `mcp.srv1.tool_name`.

**This means a runtime acting as a server will re-expose tools it discovered
as a client.** An `McpClient` connecting to `srv1` and an `McpServer`
accepting connections creates an effective MCP relay: clients calling the
server can invoke `mcp.srv1.tool_name`, which transparently proxies to srv1.

This is consistent with "protocols are adapters" (Decision 3). Whether it is
desirable is the host application's concern. If the host does not want
client-discovered tools exposed outbound, it registers server tools in a
separate namespace and filters `list_tools()` before emitting — or uses two
separate `NodusRuntime` instances.

**No structural interference:** Client connections write to
`_python_registered_tools` using the `mcp.alias.*` namespace. Server
enumeration reads from the same store. Both operations are protected by
`_tool_registry_lock`. No race condition, no double-dispatch.

The two sides do not share a transport, a read loop, or an elicitation
inventory. The only shared resource is the tool registry, which is the
intended design.

---

## E — Bytecode impact: none, with C2 justification

C2 is the one place a bytecode impact could hide — if a "server-side tool
requests elicitation" primitive required a new Nodus language construct,
that would reach the VM.

It does not, for three reasons:

1. **Python callable handlers:** `ElicitationRequest` is returned from the
   handler in Python and detected by the server dispatcher before `_to_runtime_value()`
   runs. The VM never sees `ElicitationRequest`. No opcode involved.

2. **Future `tool.elicit()` builtin (v0.2):** This would be a new builtin
   function added to std:tool's registry (like `tool_register`, `tool_invoke`).
   Builtin functions are called via the existing `CALL_BUILTIN` opcode
   (`vm.py:1563`). Adding a new builtin name does not require a new opcode —
   it is a new entry in `vm.builtins: dict[str, BuiltinInfo]`. The bytecode
   produced by `tool.elicit(requests)` is `CALL_BUILTIN "tool_elicit" 1` —
   the same opcode, a new registered name. `BYTECODE_VERSION` would not change
   for this addition.

3. **`ElicitationRequest` sentinel is adapter-internal:** The sentinel is
   created and consumed entirely within the nodus-mcp Python layer. It is
   never stored in Nodus data, never emitted by a compiler, never referenced
   by a Nodus opcode. It is not a Nodus type.

`BYTECODE_VERSION` stays 4. This is the strongest "no new opcode" conclusion
of the five Phase 1 docs: the server-side elicitation mechanism was the one
place a language surface addition could have forced a bytecode increment, and
the `ElicitationRequest` sentinel design explicitly avoids it by keeping the
mechanism below the VM boundary.

---

## Summary of settled contracts

| Question | Answer |
|---|---|
| Enumeration source | `runtime.tool_registry.list_tools()` — same registry as client invocation |
| `tools/list` on every request? | Yes — live enumeration, no cache |
| Deprecated tools | `annotations.deprecated: true` in tool definition; absent if not deprecated |
| Empty schema outbound | Inject `{"type": "object"}` — symmetric with doc 1 B2 |
| Inbound invocation path | `runtime.tool_registry.invoke(name, args)` — shared with client path |
| Not-found (inbound) | `-32601` — we are the producer |
| Schema validation (inbound) | `-32602` — we validate before invoke |
| Tool execution failure | `isError: true` — we are the producer |
| Server read loop | `McpServerTransport.serve(handler)` — parallel to client transport, not reuse |
| Server-side elicitation v0.1 | Stateless re-call: handler returns `ElicitationRequest`; adapter encodes `requestState` |
| Server-side elicitation future | `tool.elicit()` builtin for closures (no new opcode; v0.2) |
| No-handler check (server elicitation) | Elicitation capability not advertised in `server/discover` if no handler set |
| Server-side teardown | No blocking state; `_teardown_active_elicitations()` is client-side only |
| Server transport: stdio | `StdioServerTransport` reads own stdin; no subprocess spawned |
| Server transport: HTTP | `HttpServerTransport` binds port; accepts POSTs |
| `server/discover` | Inbound request; response includes capabilities + tool list |
| Capabilities reported | `tools` always; `elicitation`/`roots`/`sampling` if handler configured |
| Simultaneous client+server | Supported; shared registry; client-discovered tools re-exposed by server |
| Registry isolation | Not provided in v0.1; host separates by namespace or uses two runtimes |
| Bytecode changes | None; `BYTECODE_VERSION` stays 4 |
