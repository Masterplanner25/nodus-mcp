# nodus-mcp Phase 1 — Design Doc 3: Transports

**Doc:** 03-transports.md  
**Phase:** 1 (design)  
**Status:** Complete — 2026-05-28  
**Decisions grounded:** 1, 11, 15  
**Covers:** Question clusters A–E from the Phase 1 design pass.

---

## Purpose

This document defines the shared-core transport design, the reader thread
contracts that doc 2 depends on, HTTP statelessness mechanics including MRTR,
connection lifecycle, and the error taxonomy feeding doc 1's `-32603` mapping.

Decision 11 collapsed HTTP and Streamable HTTP into one transport. This doc
also settles how thin the "shared core" actually is — stdio and HTTP share
a framing layer and a callable interface; they do NOT share a reader-thread
model, and the doc says so.

---

## A — Shared core boundary

### A1. What the shared core is, concretely

The shared core is a two-part contract:

**Part 1 — JSON-RPC framing layer (`McpCodec`):**
- `encode(method, params, id) → bytes` — serialize a JSON-RPC 2.0 request
- `decode(raw) → dict` — deserialize a JSON-RPC 2.0 response
- `encode_notification(method, params) → bytes` — for server-side outbound
- `make_error(code, message, id) → dict` — construct a JSON-RPC error response
- `make_result(result, id) → dict` — construct a JSON-RPC success response

Both stdio and HTTP use `McpCodec`. Neither transport implements its own
JSON-RPC serialization.

**Part 2 — Transport interface (`McpTransport`):**

```python
class McpTransport:
    def send_request(self, method: str, params: dict) -> dict:
        """Send one JSON-RPC request and return the response dict."""
        ...

    def send_notification(self, method: str, params: dict) -> None:
        """Send a notification (no response expected)."""
        ...

    def close(self) -> None:
        """Shut down the transport cleanly."""
        ...
```

`send_request` is the only method the adapter calls for `tools/call`,
`server/discover`, `roots/list`, and `sampling/createMessage`. The transport
hides all byte-level I/O — the adapter above never distinguishes stdio from HTTP.

`McpConnection` (doc 1 A2) holds a `McpTransport` instance. The rest of
the adapter is transport-agnostic.

### A2. The seam: what is above vs below the shared layer

```
┌─────────────────────────────────────────────────────────┐
│  Adapter layer (doc 1, doc 2)                           │
│  tool registry, MRTR loop, elicitation, capabilities    │
├─────────────────────────────────────────────────────────┤
│  McpCodec — JSON-RPC framing (shared)                   │
│  encode / decode / make_error / make_result             │
├──────────────────────┬──────────────────────────────────┤
│  StdioTransport      │  HttpTransport                   │
│  (below the seam)    │  (below the seam)                │
│  • persistent pipe   │  • stateless POST per request    │
│  • reader thread     │  • no persistent reader thread   │
│  • newline-delimited │  • HTTP Content-Type: app/json   │
└──────────────────────┴──────────────────────────────────┘
```

Above the seam: JSON-RPC message structure, request/response correlation
by `id`, all adapter logic.

Below the seam: how bytes arrive and depart — stdin/stdout pipes versus
HTTP POST/response.

### A3. `McpConnection.transport` is a `McpTransport` instance

Yes. `McpConnection.transport` holds an opaque `McpTransport`. The adapter
layer calls `transport.send_request(...)` without knowing whether it is
talking to a child process or an HTTP server. This is the invariant that
makes tool handlers transport-agnostic.

---

## B — The reader thread

### B1. Stdio: one persistent reader thread per connection

For stdio transport, a persistent reader thread is started when the connection
is established (process spawned, pipes opened). It runs until the process dies
or `close()` is called.

```
Reader thread (stdio):
  while True:
      line = stdin.readline()          # blocks until newline
      if not line:
          break                        # process closed its stdout
      msg = McpCodec.decode(line)
      id  = msg.get("id")
      if id is not None:
          waiter = pending.pop(id, None)
          if waiter is not None:
              waiter.result_box[0] = msg
              waiter.wake_event.set()  # wakes send_request()
      else:
          notification_handler(msg)    # e.g. progress, server notifications
```

`pending` is a `dict[id, Waiter]` protected by a `threading.Lock`. Each
`send_request()` call inserts a `Waiter` (result_box + wake_event) before
writing the request to stdout, then calls `wake_event.wait(timeout)`. The
reader thread pops the waiter on receipt and fires the event.

This is the `waiter.wake_event.set()` call that doc 2 A2 depends on.

### B2. HTTP: no persistent reader thread

**The genuine divergence from stdio:** Stateless HTTP is request/response.
`send_request()` on `HttpTransport` does:

```python
def send_request(self, method: str, params: dict) -> dict:
    body = McpCodec.encode(method, params, id=self._next_id())
    headers = {"Content-Type": "application/json"}
    if self._bearer_token:
        headers["Authorization"] = f"Bearer {self._bearer_token}"
    resp = self._http_client.post(self._url, content=body, headers=headers)
    if resp.status_code != 200:
        raise TransportError(resp.status_code, resp.text)
    return McpCodec.decode(resp.content)
```

There is no persistent reader. The HTTP client's `.post()` blocks until the
server sends a response. The response IS the reply to the request, correlated
by the single-request HTTP round-trip, not by JSON-RPC `id` matching.

The JSON-RPC `id` field is still set and echoed back (protocol correctness),
but the correlation mechanism is HTTP-level (one response per POST), not a
reader-thread dispatch table.

**What "shared core" actually means:** The shared core is the framing layer
(`McpCodec`) and the `send_request(method, params) → dict` interface. It is
NOT a shared reader-thread model. The doc 2 A2 dependency ("a transport-owned
reader thread calls `event.set()` to wake the blocking elicitation handler")
applies only to stdio. For HTTP, `send_request()` itself IS the blocking
operation — there is no separate reader thread to wake anything; the HTTP
client's synchronous POST implicitly provides the same wake semantics.

Decision 11's "shared core" claim is correct for the framing and interface
layers. Reader-thread architecture is below the shared seam and genuinely
differs between the two transports.

### B3. Request routing: pending map (stdio) vs implicit HTTP correlation

**Stdio:** A `pending: dict[id, Waiter]` maps request IDs to waiters.
The reader thread pops the right waiter on receipt. Concurrent requests
from multiple threads (if the adapter ever issues them) are safe because
`pending` is lock-protected and `id` values are unique per connection.

**HTTP:** No routing needed. `send_request()` is a single synchronous
call; the HTTP response is always the reply to the request just sent.
If multiple threads call `send_request()` concurrently (not expected in
v0.1), each thread has its own blocking `.post()` call and the HTTP
client handles connection pooling. No shared dispatch table.

For MRTR elicitation (doc 2 B2), each round-trip over HTTP is a separate
`.post()` call. The `requestState` in the params body is what correlates
rounds, not the transport-level `id`. Over stdio, each round-trip is a new
JSON-RPC request written to stdin; the reader thread's pending map routes
each response back to the correct round.

---

## C — HTTP statelessness mechanics

### C1. MRTR over stateless HTTP: discrete POSTs carrying `requestState`

With no session and no held stream, each elicitation round-trip over HTTP
is a separate POST to the same endpoint. The `requestState` blob (doc 2 B2)
travels in the request body:

```
Round 1:
POST /mcp  { method: "tools/call", params: { name, arguments } }
← 200      { result: { resultType: "input_required",
                        inputRequests: [...],
                        requestState: "<base64>" } }

Round 2:
POST /mcp  { method: "tools/call", params: { name, arguments,
                        inputResponses: [...],
                        requestState: "<base64>" } }
← 200      { result: { content: [...] } }  (final)
```

The client issues N separate HTTP POST requests to complete an N-round
elicitation. Each POST is independent from the HTTP transport's perspective.
The MCP server reconstructs elicitation context from `requestState` on
each POST; it holds no session state between requests.

This works cleanly. There is no awkwardness from the RC's design: MRTR
was designed for stateless HTTP. The `requestState` blob is exactly the
mechanism that makes server-side elicitation state portable across discrete
requests.

Over stdio, the same round-trips are JSON-RPC messages on the same pipe.
The adapter layer (MRTR loop in doc 2) is identical for both transports —
only `send_request()` differs in implementation.

### C2. Authentication: bearer on HTTP; stdio is trusted/local, auth-free

**HTTP transport:** Every POST carries `Authorization: Bearer <token>` when
a token is configured (Decision 15). The token is set at connection time
(`McpClient(url, bearer_token="...")`) and attached by `HttpTransport` to
every request header. No token rotation. The connection holds the token for
its lifetime.

**Stdio transport:** No authentication. Stdio connects to a child process
that the adapter spawned on the local machine. The trust boundary is the
OS process model — the spawning process owns the child. Bearer tokens are
not sent over stdio pipes. If a specific stdio server requires authentication,
it must negotiate via the MCP message protocol itself (outside v0.1 scope).

This is stated explicitly: stdio is assumed trusted/local, auth-free in v0.1.

### C3. SSE: not needed in v0.1

The RC's `InputRequiredResult` + MRTR-over-discrete-requests replaces
held-SSE for elicitation. SSE was a pre-RC pattern for "server streams
partial results while waiting for human input." The RC's design eliminates
this: the server returns a full `InputRequiredResult` in the HTTP response
body; the client processes it and POSTs back. No stream held open.

Progress notifications (informing the client of ongoing work within a
single tool call) flow via the `_meta.progressToken` mechanism — the server
includes progress in the response body, not via SSE. No SSE channel is
maintained.

**SSE is not implemented in v0.1.** If a future server requires SSE for
some non-elicitation purpose, that is a v0.2 extension.

---

## D — Connection lifecycle and errors

### D1. Connect handshake: what `McpConnection` construction does per transport

Per Decision 1: no session initialization handshake. Capabilities travel
in `_meta` on every request. "Connect" means different things per transport:

**Stdio:** `McpConnection` construction:
1. Spawn the child process (`subprocess.Popen` with stdin/stdout pipes)
2. Start the reader thread
3. Send one `server/discover` request to learn server capabilities
4. Register discovered tools into `_python_registered_tools` (Decision 3)
5. Return the `McpConnection` handle

If step 3 fails (process doesn't respond, exits immediately), construction
raises `TransportError` and the handle is not returned.

**HTTP:** `McpConnection` construction:
1. Create an `httpx.Client` (connection pool, no actual connection yet)
2. Send one `server/discover` POST to learn server capabilities
3. Register discovered tools
4. Return the `McpConnection` handle

No persistent connection is established before the first POST. If the
`server/discover` POST fails (connection refused, 5xx, timeout), construction
raises `TransportError`.

Both transports perform `server/discover` at construction time. This is the
first and only "handshake" — it populates `McpConnection.server_capabilities`
and `McpConnection.registered_tools`.

### D2. Transport error taxonomy: what maps to JSON-RPC `-32603`

From doc 1 D-table: transport errors → JSON-RPC `-32603 InternalError`.
The boundary:

| Error | Layer | Wire shape |
|---|---|---|
| Stdio: process exited / pipe broken (during request) | Transport | `-32603` |
| Stdio: process never started (construction) | Transport | `TransportError` raised, no JSON-RPC response |
| HTTP: connection refused / DNS failure | Transport | `-32603` |
| HTTP: timeout before response | Transport | `-32603` |
| HTTP: 5xx response | Transport | `-32603` (with HTTP status in message) |
| HTTP: 4xx response (client error) | Transport | `-32603` (4xx is unexpected at transport level) |
| HTTP: malformed JSON in response body | Framing (`McpCodec`) | `-32603` |
| JSON-RPC `error` object in 200 response | Protocol | Propagated as-is (not wrapped in `-32603`) |
| Tool not found | Protocol | `-32601` (from doc 1 D-table) |
| Schema validation fail | Protocol | `-32602` (from doc 1 D-table) |

The distinction between "transport error" and "protocol error in a well-formed
response": a JSON-RPC error object inside a 200 HTTP response is a
protocol-level error, not a transport error. Only errors that prevent the
adapter from receiving a valid JSON-RPC response at all map to `-32603`.

### D3. Clean shutdown: stdio termination; HTTP has nothing to tear down

**HTTP:** `HttpTransport.close()` closes the `httpx.Client` connection pool.
No reader thread to join. Any in-flight `.post()` call will get a connection
error (which surfaces as `-32603`). If an elicitation is in progress (parked
`threading.Event.wait`), the teardown sentinel mechanism from doc 2 D1 fires
first, waking the parked handler with `tool_error / elicitation_aborted`
before the HTTP client is closed. Sequence:

```python
def close(self):
    runtime._teardown_active_elicitations()   # doc 2 D1 sentinel mechanism
    self._http_client.close()
```

**Stdio:** `StdioTransport.close()`:
1. Call `runtime._teardown_active_elicitations()` — same sentinel mechanism
2. Terminate the child process (`proc.terminate()`)
3. Close the stdin pipe (signals EOF to the process)
4. Join the reader thread (it exits when stdin closes or process dies)
5. `proc.wait()` to reap the exit code

The reader thread exits cleanly when `stdin.readline()` returns empty bytes
(pipe closed). If the thread does not join within a short timeout
(e.g. 2 seconds), the process is force-killed (`proc.kill()`).

**This is where the doc 2 D1 teardown sentinel lives.** Both transports call
`runtime._teardown_active_elicitations()` as the first step of `close()`.
This ensures no parked elicitation thread outlives the connection teardown,
regardless of whether the transport is stdio or HTTP.

---

## E — Bytecode impact: none

The transport layer is pure Python. No Nodus language surface is added.
`McpCodec`, `StdioTransport`, `HttpTransport`, and `McpTransport` are
Python classes. The adapter calls them from Python callable tool handlers,
which are themselves called via the existing `call_builtin` path (vm.py).

No new opcodes. No new scheduler primitives. `BYTECODE_VERSION` stays 4.

The `httpx.Client` used by `HttpTransport` is a Python dependency
(already in nodus-lang's stack via `std:http`). `subprocess.Popen` is
Python stdlib. Neither introduces Nodus bytecode.

---

## Summary of settled contracts

| Question | Answer |
|---|---|
| Shared core components | `McpCodec` (framing) + `McpTransport` interface (`send_request`, `send_notification`, `close`) |
| Shared by both transports | JSON-RPC encode/decode; `send_request(method, params) → dict` |
| NOT shared | Reader-thread model: stdio has one; HTTP has none |
| `McpConnection.transport` | Opaque `McpTransport`; adapter is transport-agnostic |
| Stdio reader thread lifecycle | One thread per connection; started at connect; joined at close |
| HTTP reader thread | None — `send_request()` is a synchronous `.post()` |
| Request routing (stdio) | `pending: dict[id, Waiter]` + lock; reader thread pops and fires event |
| Request routing (HTTP) | Implicit HTTP round-trip; no dispatch table |
| MRTR over HTTP | Discrete POSTs, `requestState` in body; works cleanly |
| MRTR over stdio | Same MRTR loop; JSON-RPC messages on the pipe |
| Bearer auth | HTTP only: `Authorization: Bearer` header per request |
| Stdio auth | None; assumed trusted/local |
| SSE in v0.1 | Not implemented; not needed (MRTR replaces held-SSE) |
| Stdio connect sequence | Spawn process → start reader → `server/discover` → register tools |
| HTTP connect sequence | Create client → `server/discover` POST → register tools |
| Transport error → wire | `-32603` for all errors preventing valid JSON-RPC response |
| Protocol error in 200 response | Propagated as-is (not wrapped in `-32603`) |
| Teardown order (both) | `_teardown_active_elicitations()` first, then transport close |
| Bytecode changes | None; `BYTECODE_VERSION` stays 4 |
