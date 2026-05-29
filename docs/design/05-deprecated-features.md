# nodus-mcp Phase 1 — Design Doc 5: Deprecated Features (Roots + Sampling)

**Doc:** 05-deprecated-features.md  
**Phase:** 1 (design)  
**Status:** Complete — 2026-05-28  
**Decisions grounded:** 12  
**Covers:** Question clusters A–D from the Phase 1 design pass.

---

## Purpose

Roots and Sampling are included in v0.1 despite being marked deprecated in
the 2026-07-28 RC (Decision 12). This doc specifies both features, records
why they are in v0.1, and establishes the deprecation posture so future
maintainers do not remove them prematurely or carry them past the protocol's
removal date.

Logging is **not** implemented in v0.1. Host application logging (Python's
`logging` module, observability pipelines) covers all use cases. This is a
deliberate scope exclusion, not an omission.

---

## C — Deprecation posture (reason this doc exists)

This section is first because it frames everything else.

### C1. Why Roots and Sampling are in v0.1

**Do not remove Roots or Sampling without checking this section.**

They are in v0.1 because:

1. **Real interop need.** Existing MCP servers in the ecosystem use Roots and
   Sampling. Without them, nodus-mcp cannot talk to those servers. The RC
   keeps both functional for ≥12 months from the RC date (2026-07-28).
   That window extends to at least 2027-07-28 — well past v0.1's useful life.

2. **Bidirectional commitment.** Decision 10 requires nodus-mcp to implement
   both client and server roles. A server that does not support Roots or
   Sampling cannot interoperate with clients that use them.

3. **Not the same as Logging.** Logging was excluded because it is the least
   essential of the three deprecated capabilities and host-level logging
   already covers the need. Roots and Sampling have no non-deprecated
   replacements in the current RC. Excluding them would leave real gaps.

**The 12-month window runs from 2026-07-28. Roots and Sampling should not be
removed from nodus-mcp before 2027-07-28, and only then if the protocol has
actually removed them and nodus-mcp has shipped replacement support.**

### C2. Exit story and TECH_DEBT tracking

The 2026-07-28 RC marks Roots and Sampling deprecated but does not name
specific successors. The exit path is:

- When the MCP spec names a successor mechanism, nodus-mcp adds it.
- nodus-mcp's own deprecation notice for Roots and Sampling ships before
  the protocol removes them, giving library users time to migrate.
- The deprecated implementation is removed in the release cycle after the
  protocol removes the features (not before).

**TECH_DEBT entry:** `TECH_DEBT.md` (to be created in the nodus-mcp repo)
tracks:

```
TD-001: Roots — deprecated in RC (2026-07-28); functional ≥12 months.
  Remove when: protocol removes Roots AND nodus-mcp has shipped alternative.
  Target: no earlier than 2027-07-28.

TD-002: Sampling — deprecated in RC (2026-07-28); functional ≥12 months.
  Remove when: protocol removes Sampling AND nodus-mcp has shipped alternative.
  Target: no earlier than 2027-07-28.
```

### C3. Capability gating: no handler → not advertised → request → unsupported error

Both Roots and Sampling gate exactly like elicitation (doc 4 D2):

- Not advertised in `server/discover` unless a handler is configured.
- Inbound request with no handler → `tool_error / category: "unsupported"`,
  JSON-RPC error `-32601 MethodNotFound` (per doc 1 D-table: not-found maps
  to -32601; "method not supported" is the semantic equivalent when the method
  is known but unconfigured).

This means a default `McpServer` with no handlers configured advertises
only `tools: {}` in `server/discover`. Adding a Roots or Sampling handler
turns on the capability.

---

## A — Roots

### A1. Direction: both client and server in v0.1

Roots tells a server which filesystem locations a client considers its
working context. Both directions are implemented in v0.1:

**Client role (we tell upstream servers our roots):** When `mcp.connect()`
is called with a `roots` list, nodus-mcp advertises `roots: {}` in `_meta`
on every outbound request. If the upstream server calls `roots/list`, we
respond with the configured roots.

**Server role (we ask connecting clients for their roots):** When a
client calls us with `roots: {}` in its `_meta` capabilities, a
server-side tool handler can ask for the client's roots via a server-issued
`roots/list` request.

Server-issued `roots/list` uses the same stateless re-call pattern as
doc 4 C1 (see A2 below for the specific shape). No new mechanism.

### A2. Representation and wire shape

**Client-side roots configuration:**

```python
mcp.connect(url, alias="srv1", roots=[
    {"uri": "file:///home/user/project", "name": "My Project"},
    {"uri": "file:///data", "name": "Data Store"},
])
```

Stored on `McpConnection` as `roots: list[dict]`. The client responds to
`roots/list` with this list.

**`roots/list` wire shape (RC, deprecated-but-functional):**

```
Client → Server: { method: "roots/list", params: {}, _meta: {...} }
Server → Client: { result: { roots: [
    { uri: "file:///home/user/project", name: "My Project" },
    ...
] } }
```

**Server-side handler (we ask the calling client for its roots):**

```python
runtime.set_roots_handler(fn)

# fn signature:
def fn() -> list[dict]:
    # Returns [{uri: str, name: str}, ...] — the server's configured roots
    # (what the server exposes when asked)
    ...
```

Wait — there are two distinct usages to separate cleanly:

1. **Client configured roots** (answering `roots/list` from an upstream
   server): stored on `McpConnection`; the transport responds automatically.
   No handler needed.

2. **Server asking the calling MCP client for its roots**: nodus-mcp sends
   `roots/list` to the client. The client responds with its roots. The
   server-side tool handler receives them via the re-call pattern.

For case 2, a tool handler that needs the calling client's roots:

```python
from nodus_mcp import RootsRequest

def my_handler(args):
    if args.get("__roots__") is None:
        return RootsRequest(state=args)   # ask client for roots; re-call with them
    roots = args["__roots__"]            # injected by adapter on round 2
    # ... use roots ...
    return {"result": "..."}
```

`RootsRequest` follows the same re-call pattern as `ElicitationRequest`
(doc 4 C1). The sentinel triggers a `roots/list` request to the client;
`requestState` carries the handler's checkpoint; the handler is called again
with `__roots__` injected. If the client did not advertise `roots` capability
(not in inbound `_meta`), the adapter returns `tool_error / elicitation_unsupported`
(category: `"roots_unsupported"`) without issuing the request.

### A3. RC wire shape: live RC shape only; deprecation is label not wire change

The deprecation in the 2026-07-28 RC marks Roots as scheduled for removal;
it does not change the wire format. nodus-mcp implements the RC's `roots/list`
request/response shape verbatim. No pre-RC compatibility shim.

---

## B — Sampling

### B1. Direction: both client and server in v0.1

Sampling: the server asks the calling client's LLM to generate a completion.

**Server role (we ask the calling client's model):** Our server-side tool
handler issues a `sampling/createMessage` request to the calling MCP client.
The client runs its LLM and returns the completion. Uses the doc 4 C1
re-call pattern (see B2).

**Client role (an upstream server asks us to sample):** When an upstream
MCP server issues `sampling/createMessage` to us, we invoke
`runtime._sampling_handler(request)` and return the result. Handler absent →
decline with `{"action": "decline"}`.

### B2. Server-issued sampling: doc 4 C1 re-call pattern, `SamplingRequest` sentinel

**Server-issued sampling IS doc 4 C1's stateless re-call pattern with
`SamplingRequest` instead of `ElicitationRequest`. Only the sentinel type
and payload differ.**

The structural identity:

| | Elicitation (doc 4 C1) | Sampling (B2) |
|---|---|---|
| Handler returns | `ElicitationRequest(input_requests, state)` | `SamplingRequest(messages, params, state)` |
| Adapter sends to client | `InputRequiredResult { inputRequests, requestState }` | `SamplingRequiredResult { samplingParams, requestState }` |
| Client continuation | `tools/call { inputResponses, requestState }` | `tools/call { samplingResult, requestState }` |
| Adapter injects on round 2 | `args["__elicitation_state__"]["responses"]` | `args["__sampling_state__"]["result"]` |

The adapter code that handles `ElicitationRequest` and `SamplingRequest` is
the same dispatch loop with a type switch. They are not separate mechanisms.

**Handler pattern:**

```python
from nodus_mcp import SamplingRequest

def my_handler(args):
    if args.get("__sampling_state__") is None:
        return SamplingRequest(
            messages=[{"role": "user", "content": {"type": "text",
                        "text": f"Summarize: {args['text']}"}}],
            params={"maxTokens": 500},
            state={"query": args["text"]},
        )
    result = args["__sampling_state__"]["result"]  # the LLM's response
    summary = result["content"]["text"]
    return {"summary": summary}
```

If the calling client did not advertise `sampling` capability in inbound
`_meta`, the adapter returns `tool_error / category: "sampling_unsupported"`
without issuing the request.

### B3. Client-side servicing: `set_sampling_handler`, symmetric with elicitation

```python
runtime.set_sampling_handler(fn)

# fn signature:
def fn(request: dict) -> dict:
    # request: { method: "sampling/createMessage",
    #             params: { messages: [...], maxTokens: N, ... } }
    # Return: { role: "assistant", content: { type: "text", text: "..." } }
    # or:
    return {"action": "decline"}
```

Symmetric with `set_elicitation_handler` (doc 2 C1): registered on the
`NodusRuntime` instance, called synchronously from the handler thread, blocks
until the host returns the response.

When no handler is registered and an upstream server issues
`sampling/createMessage`, the adapter returns `{"action": "decline"}` to the
server without invoking the Nodus script. This is the correct response — the
client is not obligated to sample.

The `sampling: {}` capability is advertised in outbound `_meta` only if
`runtime._sampling_handler` is set (doc 4 D2 capability-gating rule). This
ensures upstream servers do not issue sampling requests to clients that
cannot service them.

---

## D — Bytecode impact: trivially none

Roots and Sampling are Python-handler-level features throughout:

- `RootsRequest` and `SamplingRequest` sentinels are adapter-internal Python
  objects, never seen by the VM.
- Client-side roots are a list on `McpConnection`, not a Nodus value.
- `set_roots_handler()` and `set_sampling_handler()` store Python callables
  on the `NodusRuntime` instance — same pattern as `set_elicitation_handler()`.
- The re-call dispatch in the adapter is Python code that precedes any
  `ToolRegistry.invoke()` call.

No new opcodes. No new Nodus language constructs. `BYTECODE_VERSION` stays 4.
The justification is the same class as doc 4 E: sentinels are detected below
the VM boundary, by the adapter, before the VM is involved.

---

## Summary of settled contracts

| Question | Answer |
|---|---|
| Roots in v0.1 | Both directions — client (answer `roots/list`) and server (issue `roots/list`) |
| Client roots config | `roots=[...]` at `mcp.connect()` time; transport answers automatically |
| Server-issued roots | `RootsRequest` sentinel; doc 4 C1 re-call pattern |
| No-roots-cap → abort | `tool_error / category: "roots_unsupported"` without issuing request |
| Roots wire shape | RC shape verbatim; deprecation is label only, no wire change |
| Sampling in v0.1 | Both directions — server (issue `sampling/createMessage`) and client (service it) |
| Server-issued sampling | `SamplingRequest` sentinel; doc 4 C1 re-call pattern — same mechanism, different payload |
| Client-side servicing | `runtime.set_sampling_handler(fn)` symmetric with `set_elicitation_handler` |
| No handler → sampling request | Return `{"action": "decline"}` to upstream server |
| No sampling cap → abort | `tool_error / category: "sampling_unsupported"` without issuing request |
| Capability gating | Both advertised only if handler configured; absent → not in `server/discover` |
| No handler + incoming request | `-32601 MethodNotFound` (method known but unconfigured) |
| Why in v0.1 | Real interop need; RC functional window ≥ 2027-07-28; no named replacement |
| Do-not-remove gate | 2027-07-28 or later; only after protocol removes AND nodus-mcp ships replacement |
| TECH_DEBT entries | TD-001 (Roots), TD-002 (Sampling) — to be created in `TECH_DEBT.md` |
| Logging | Excluded; host-level logging covers use cases |
| Bytecode changes | None; `BYTECODE_VERSION` stays 4 |
