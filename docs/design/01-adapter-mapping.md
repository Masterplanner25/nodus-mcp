# nodus-mcp Phase 1 — Design Doc 1: Adapter Mapping Core

**Doc:** 01-adapter-mapping.md  
**Phase:** 1 (design)  
**Status:** Complete — 2026-05-28  
**Decisions grounded:** 1, 2, 3, 4, 5, 8, 16  
**Covers:** Question clusters A, B, C1, D, E from the Phase 1 design pass.

---

## Purpose

This document pins the spine of the adapter layer: how a stateless MCP
request maps onto the Nodus tool registry, what a connection handle contains
and who mints it, which invocation path the adapter uses, how tool names
translate between the two systems, and the complete error contract.

Elicitation internals (MRTR loop, callback wiring, `_io_channels`) are in
`02-elicitation.md`. Transport framing is in `03-transports.md`. Server-mode
enumeration and per-request capability reading are in `04-server-mode.md`.

---

## A — Request lifecycle

### A1. Inbound request shape and `requestState` at the adapter boundary

A `tools/call` request arriving at the adapter has this wire shape (RC):

```json
{
  "jsonrpc": "2.0",
  "id": "req-123",
  "method": "tools/call",
  "params": {
    "name": "my_tool",
    "arguments": { "key": "value" },
    "requestState": "<base64-opaque>",
    "_meta": {
      "clientInfo": { "name": "...", "version": "..." },
      "capabilities": { "tools": {}, "elicitation": {} },
      "progressToken": "token-abc"
    }
  }
}
```

`requestState` is the server's own elicitation-continuation blob, round-tripped
by the client. It stays opaque to Nodus throughout. Specifically:

- **Fresh call** (no `requestState` or empty): adapter calls the tool handler
  directly with no continuation context.
- **Continuation call** (non-empty `requestState`): adapter base64-decodes and
  JSON-parses the blob inside the Python handler to extract elicitation
  correlation state, then resumes the MRTR loop. The parsed content is never
  exposed to Nodus code.

The `requestState` blob is Python-internal. The server creates it (Python dict
→ JSON → base64) when it needs the client to carry state across an elicitation
round-trip. On return, the handler re-encodes the updated state. Nodus scripts
never see `requestState`, `inputRequests`, or `InputRequiredResult` (Decision 5).

`_meta` carries per-request capabilities and client info (Decision 1). Because
there is no session, the adapter reads `_meta` fresh on every call. See C1.

### A2. Connection handle — contents and who mints it

`mcp.connect(url, alias, bearer_token=None)` returns a `McpConnection` handle.

**Contents of `McpConnection`:**

| Field | Type | Purpose |
|---|---|---|
| `alias` | `str` | Namespace prefix for tools registered from this server |
| `url` | `str` | Server URL (HTTP) or process spec (stdio) |
| `transport` | transport object | Active stdio pipe or HTTP session |
| `bearer_token` | `str \| None` | Auth token for HTTP; `None` for stdio |
| `server_info` | `dict` | `serverInfo` block from `server/discover` |
| `server_capabilities` | `dict` | `capabilities` block from `server/discover` |
| `registered_tools` | `list[str]` | Full namespaced names (`mcp.<alias>.<tool>`) registered into `_python_registered_tools`; used by `disconnect()` to unregister cleanly |

**Who mints it:** The Python adapter layer (`nodus_mcp.client.connect()`).
The `NodusRuntime` does not know about `McpConnection`. The runtime only sees
Python callables written into `_python_registered_tools`. The handle is
returned to the Nodus `.nd` layer as an opaque Nodus value; scripts hold it
and pass it to `mcp.disconnect()` but never inspect its fields.

### A3. Invocation path — adapter does not traverse `_root_vm/_caller_vm`

The adapter does **not** traverse the `_root_vm`/`_caller_vm` VM hierarchy
(the 3C.2 child-VM path). It uses the `NodusRuntime.tool_registry` interface
exclusively (Decision 8).

Path from Nodus script to MCP wire call:

```
tool.invoke("mcp.srv1.my_tool", args)
  → vm.invoke_function("mcp.srv1.my_tool")
    → vm.tool_registry.get("mcp.srv1.my_tool")          # populated from
      # _python_registered_tools at vm construction      # embedding.py:477-478
    → entry["handler"](args)                             # Python callable
      → adapter strips "mcp.srv1." prefix
      → transport.send_tools_call("my_tool", args)       # wire
```

The Python callable registered for `mcp.srv1.my_tool` captures the transport
and strips the namespace prefix before sending the JSON-RPC request. No VM
traversal is needed from the adapter's side — the registry lookup is handled
by the standard `vm.tool_registry` path, exactly as for any other Python-registered
tool.

`_python_registered_tools` persists across `run_source()` calls (Decision 8):
tools discovered from a server survive individual script executions for the
lifetime of the `NodusRuntime` instance.

---

## B — Tool identity and namespace

### B1. `mcp.<alias>.<tool_name>` is client-side only

The `mcp.<alias>.<tool_name>` namespace lives in the Nodus registry.
It does **not** appear in the MCP wire protocol.

**Client side:** A tool `read_file` from server `srv1` is registered as
`mcp.srv1.read_file` in `_python_registered_tools`. The Nodus script calls
`tool.invoke("mcp.srv1.read_file", ...)`. The adapter handler strips the
`mcp.srv1.` prefix before sending `tools/call` with `name: "read_file"`.

**Server side:** `NodusRuntime.tool_registry.list_tools()` is the source for
`tools/list`. Tools are enumerated with their registry names as-is. If a tool
was registered as `search`, it is exposed to MCP clients as `search`. If it
was registered as `my.search.v2`, it is exposed as `my.search.v2`. No
`mcp.alias.` prefix is added. The alias is a local scoping mechanism for the
client role only — it prevents name collisions in the local registry when
multiple servers expose identically-named tools.

### B2. Registry JSON Schema → MCP `inputSchema`: passthrough with validation

The registry stores schemas validated by `_normalize_schema()`. The adapter
passes the schema through to `inputSchema` in the tool definition with one
check: MCP requires `inputSchema` to be a JSON Schema object with
`type: "object"` at the top level. The adapter:

1. Reads `entry["schema"]` from the registry.
2. Confirms it has `"type": "object"`. If missing, injects `"type": "object"`;
   if present but not `"object"`, logs a warning and wraps as
   `{"type": "object", "properties": {}}`.
3. Strips keys prefixed with `_` (internal Nodus extension keys, if any).
4. Passes the result verbatim as `inputSchema`.

No structural transformation is performed on the schema body. Properties,
`required`, `$defs`, and all other JSON Schema constructs are preserved as-is.

### B3. Deprecated-tool warnings in enumeration

Registry entries carry a `deprecated: bool` field (see `embedding.py:76`).
When `deprecated` is `True`, the adapter includes it in the tool's `annotations`
object in the `tools/list` response:

```json
{
  "name": "old_tool",
  "description": "...",
  "inputSchema": { "type": "object" },
  "annotations": {
    "deprecated": true
  }
}
```

`_meta` is per-request context — not the right place for per-tool metadata.
`annotations` is the correct location per the RC's tool definition shape.
Deprecation warnings are surfaced on enumeration (`tools/list`), not on
invocation, so MCP clients can inspect before calling.

---

## C — Capabilities and `_meta`

### C1. Outbound capabilities; reading inbound `_meta` without a session

**Outbound (nodus-mcp as client):**

nodus-mcp includes its capabilities in `_meta` on every request sent to a
server. v0.1 capability set:

```json
{
  "_meta": {
    "clientInfo": { "name": "nodus-mcp", "version": "0.1.0" },
    "capabilities": {
      "tools": {},
      "elicitation": {},
      "roots": {},
      "sampling": {}
    }
  }
}
```

These are appended to every outgoing JSON-RPC request by the transport layer.

**Inbound (nodus-mcp as server):**

Since there is no session (Decision 1), there is no capability cache. The
adapter reads capabilities fresh from each request's `_meta`:

```python
caps = request.get("params", {}).get("_meta", {}).get("capabilities", {})
```

What the adapter does with inbound caps:

- `capabilities.elicitation` present → may trigger elicitation on this call
  if the tool handler requests it (see `02-elicitation.md`).
- `capabilities.roots` present → may send `roots/list` during this call.
- `capabilities.sampling` present → may send `sampling/createMessage` during this call.
- `_meta` absent or `capabilities` absent → assume minimal capabilities
  (tools only; no elicitation, no roots, no sampling).

The adapter never caches inbound caps between requests — there is no session
state to write to (Decision 2).

**C2** (server-mode enumeration and `server/discover` contract) is covered in
`04-server-mode.md`.

---

## D — Error contract

### D1. Full mapping: registry invocation failure → MCP wire shape

| Failure case | MCP wire shape | Code / content |
|---|---|---|
| Tool name not found | JSON-RPC error | `-32601` `MethodNotFound` |
| Input validation fails (JSON Schema) | JSON-RPC error | `-32602` `InvalidParams` — see D2 |
| Tool raises Python exception | Tool result `isError: true` | content: `[{"type":"text","text":"<exception message>"}]` |
| Tool returns `tool_error` err record | Tool result `isError: true` | content: err record serialized to text |
| Elicitation timeout (Decision 6) | Tool result `isError: true` | content: `{"category":"elicitation_timeout","message":"elicitation timed out after Ns"}` |
| Elicitation unsupported (Decision 13) | Tool result `isError: true` | content: `{"category":"elicitation_unsupported","message":"no elicitation handler registered"}` |
| Transport / network failure | JSON-RPC error | `-32603` `InternalError`, message: transport error string |

The distinction between JSON-RPC errors and tool results with `isError: true`:

- **JSON-RPC errors** (`-32601`, `-32602`, `-32603`) indicate the request
  itself was invalid or undeliverable — the tool never ran.
- **Tool result `isError: true`** indicates the tool ran (or was running) but
  produced a failure — the invocation completed with an error outcome.

Callers (Nodus scripts) receive `tool_error` err records for both cases, but
the `category` field distinguishes them: `"not_found"`, `"invalid_params"`,
`"transport_error"` for JSON-RPC errors; `"tool_error"` or a specific
sub-category for execution failures.

### D2. JSON Schema input validation failure: JSON-RPC error, not tool result

Input validation (schema mismatch) before tool execution produces a
**JSON-RPC error** with code `-32602 InvalidParams`. The tool never ran;
returning a JSON-RPC error is semantically correct — the request was malformed.

Validation occurs at two points:

1. **Server side** (inbound `tools/call`): the adapter validates
   `params.arguments` against the tool's `inputSchema` before calling the
   handler. Failure → `-32602`.
2. **Client side** (outbound `tools/call`, optional): the adapter MAY validate
   args against the discovered schema before sending the request.
   Failure → Nodus `tool_error` with `category: "invalid_params"` raised in
   the Python handler (does not produce a wire request).

The server-side path is required; the client-side validation is optional and
advisory (the server will validate anyway).

---

## E — Bytecode impact

**None.** The MCP adapter is a pure Python layer built on top of the existing
nodus-lang embedding API. It registers Python callables into
`_python_registered_tools` via `NodusRuntime.tool_registry.register()` — the
same path used by any Python host application embedding Nodus. No new opcodes
are added. No VM behavior changes. `BYTECODE_VERSION` remains 4.

This is consistent with commit `ea16b10` (third-party `.nd` resolution), which
established that library adapters are Python-level additions that do not reach
into the bytecode layer.

---

## Summary of settled contracts

| Question | Answer |
|---|---|
| `requestState` visibility | Python-only; deserialized inside MRTR handler; never Nodus-visible |
| Handle minted by | Python adapter (`nodus_mcp.client.connect()`); runtime does not know about it |
| Invocation path | `vm.tool_registry` → `_python_registered_tools` entry callable; no VM traversal |
| `mcp.<alias>.<tool>` scope | Client-side Nodus registry only; stripped before wire send; not used server-side |
| Schema passthrough | Verbatim; adapter injects/validates `type: object` only |
| Deprecated tool surface | `annotations.deprecated: true` in `tools/list` tool definition |
| Inbound `_meta` caps | Read fresh per-request; no cache; absent → assume tools-only |
| Not-found error | JSON-RPC `-32601` |
| Schema validation failure | JSON-RPC `-32602` (server-side); `tool_error/invalid_params` (client-side) |
| Tool execution failure | Tool result `isError: true` |
| Bytecode changes | None; `BYTECODE_VERSION` stays 4 |
