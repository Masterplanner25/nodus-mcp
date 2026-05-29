# nodus-mcp Phase 1 — Design Doc 2: Elicitation

**Doc:** 02-elicitation.md  
**Phase:** 1 (design)  
**Status:** Complete — 2026-05-28  
**Decisions grounded:** 2, 5, 6, 7, 13, 14  
**Covers:** Question clusters A–F from the Phase 1 design pass.

---

## Purpose

This document pins the elicitation design: the MRTR loop, the `_io_channels`
production substrate and its real constraints, the callback contract, timeout
mechanics, and the test substrate. It resolves the M5 correction from Phase 0
— Decision 7 states `_io_channels` as the production path, and this doc
explains precisely what that means and where it applies.

---

## A — The `_io_channels` substrate

### A1. How a Channel registers on `_io_channels` and blocks a Nodus coroutine

The `_io_channels` mechanism (`scheduler.py:44`, `scheduler.py:139–163`) is the
scheduler's hook for thread-backed async I/O. It works through five steps:

1. A `Channel()` is created and appended to `scheduler._io_channels`.
2. A Nodus coroutine calls `recv(ch)` (builtin in `coroutine.py:137–171`).
   `recv` sets `coroutine.state = "suspended"`, saves coroutine state,
   appends the coroutine to `ch.waiting_receivers`, and returns a
   `ChannelRecvRequest(ch)`.
3. `call_builtin` (`vm.py:1590–1591`) detects the `ChannelRecvRequest` return
   value and returns `("yield", {CHANNEL_WAIT_KEY: True})` to the scheduler.
4. The scheduler's `run_loop` sees `channel_wait` and does `continue` — the
   coroutine is parked, and the scheduler runs other ready tasks.
5. A background thread appends a value to `ch.queue` (CPython `deque.append`
   is GIL-atomic). `_drain_io_channels` is called on each scheduler tick;
   it matches pending queue values to `waiting_receivers`, sets
   `receiver.stack[-1] = value`, clears `receiver.blocked_on`, and appends
   the receiver to `ready_queue`. The coroutine resumes with the received
   value.

**Critical constraint — the setup must happen before the return:**
For step 3 to work correctly, the coroutine must already be in
`ch.waiting_receivers` before `_drain_io_channels` runs. The `recv` builtin
does this setup in step 2. A Python callable that returns a bare
`ChannelRecvRequest` without performing the step-2 setup would be silently
broken: the scheduler would see `channel_wait` but the coroutine would never
be in `waiting_receivers`, so `_drain_io_channels` would never wake it.

**The M5 correction: Python callable handlers block synchronously.** When a
tool is registered as a Python callable and invoked via `tool.invoke()`, the
dispatch path is:

```
call_builtin("tool_invoke", ...)
  → builtin_tool_invoke(name, args)          # tool_module.py:264
    → handler(host_args)                     # Python callable, blocks
    → _to_runtime_value(result)              # pushes result on stack
  → return None                              # no yield, no suspension
```

The handler runs synchronously on the VM thread. The scheduler is not
involved. Other Nodus coroutines cannot run while the handler is executing.

This is not a bug — it is the correct behavior for a blocking operation. For
elicitation, the handler blocks on `threading.Event.wait(timeout)` while
waiting for a human to provide input. Since elicitation is always a
human-in-the-loop operation, freezing other Nodus coroutines during the wait
is acceptable in v0.1.

**When `_io_channels` DOES apply to elicitation:**
If a tool handler is registered as a **Nodus closure** (not a Python callable),
it runs as a coroutine step inside the VM. A Nodus closure handler that calls
`recv(elicitation_response_ch)` goes through the proper five-step path above —
the coroutine suspends, the scheduler runs other tasks, and the background
transport thread wakes it when the response arrives. This is the true
production path described in Decision 7.

For v0.1, all MCP tool handlers are Python callables (they need to make HTTP
or stdio requests). The synchronous blocking model is used. Decision 7's
`_io_channels` description names the design target for a future non-blocking
implementation, not the v0.1 mechanics.

### A2. Background-thread I/O model

The transport owns a reader thread (designed in `03-transports.md`). For
elicitation in the Python callable handler path:

```
VM thread                           Transport reader thread (or same thread)
─────────────────────────────       ─────────────────────────────────────────
handler() called synchronously
  send_tools_call(name, args) ─────→ wire: JSON-RPC tools/call
  event = threading.Event()
  event.wait(timeout=T) ←──────────── transport reads response
                                      response is InputRequiredResult?
                                        push inputRequests to callback queue
                                        event.set()
  event returns True
  invoke elicitation_callback(...)
  (callback blocks on human input)
  continuation = callback result
  send_tools_call(name, args,
    inputResponses=continuation,
    requestState=state)
  ... repeat until final result
```

For the aspirational Nodus-closure path (future v0.2+):

```
Nodus coroutine (scheduler)         Transport reader thread
────────────────────────────        ─────────────────────────────────────────
recv(elicitation_resp_ch)
  → suspend; add to waiting_receivers
  → scheduler runs other tasks
                                    transport reads InputRequiredResult
                                    push inputRequests result to resp_ch.queue
                                    _drain_io_channels wakes the coroutine
coroutine resumes with result
invokes callback
```

### A3. Relationship to `std:http` streaming/SSE: parallel pattern, not reuse

`std:http` streaming (`http_module.py:433–509`) and SSE (`http_module.py:512–590`)
use `_io_channels` in exactly this pattern: background worker thread writes
to `ch.queue`; Nodus script calls `recv(stream.chunks)` which suspends;
`_drain_io_channels` wakes it with each chunk.

The elicitation `_io_channels` path (for Nodus-closure handlers) uses the
same primitives — `Channel`, `scheduler._io_channels`, `recv()` — but it is
a **parallel pattern**, not a code reuse of the HTTP module. The HTTP module
produces a stream of many values; elicitation produces one `inputRequests`
dict and consumes one `inputResponses` dict per round-trip. The background
thread identity also differs: HTTP uses a `threading.Thread` started
explicitly; the transport reader for MCP (stdio or HTTP) is described in
`03-transports.md`.

For the v0.1 Python callable path, there is no code-path relationship to
`std:http` at all — both use Python's threading primitives, but independently.

---

## B — MRTR loop and RC mechanics

### B1. Round-trip count: cap at 10 per invocation

The MCP RC does not define a hard cap on elicitation rounds. The adapter
imposes a configurable cap of **10 rounds** per `tools/call` invocation.
If the cap is exceeded, the handler returns a `tool_error` with
`category: "elicitation_rounds_exceeded"`. The cap is configurable:
`McpClient(max_elicitation_rounds=N)` or per-call via call options.

Rationale for 10: a legitimate elicitation flow (fill a form, confirm a
step, choose an option, confirm again) is unlikely to exceed 4–5 rounds.
10 provides headroom for unusual but valid flows while preventing runaway
loops. Anything above 10 is almost certainly a protocol error or server bug.

### B2. Continuity across round-trips: `requestState` is the carrier

Yes — `requestState` (Decision 2) is exactly what carries continuity.
The adapter does not need a session or server-side memory because the client
round-trips the `requestState` blob back on each continuation.

**Wire shapes for a two-round elicitation:**

```
Round 1 — initial call:
→ tools/call { name, arguments }

← InputRequiredResult {
    resultType: "input_required",
    inputRequests: [{id: "r1", schema: {...}, message: "..."}],
    requestState: "<base64: {correlationId: 'c-abc', round: 1}>"
  }

Round 2 — continuation:
→ tools/call {
    name, arguments,
    inputResponses: [{id: "r1", content: {...}}],
    requestState: "<base64: {correlationId: 'c-abc', round: 1}>"
  }

← ToolResult { content: [...] }   (final)
```

The `requestState` blob is created by the server when it first sends
`InputRequiredResult`. In nodus-mcp's server role, the blob serializes
whatever context the server-side tool handler needs to resume: the
correlation ID for the pending elicitation wait, round number, and
any partial state the handler passed in when it triggered elicitation.

The blob is opaque to MCP clients. The client must echo it back unchanged.
The adapter validates that a continuation `tools/call` carries back the same
`requestState` (the correlation ID must match). On mismatch → `tool_error /
category: "invalid_request_state"`.

### B3. SEP-2322: cited as origin; designed against the RC wire format

SEP-2322 is the Semantic Enhancement Proposal that introduced the MRTR
(Multi-Round-Trip Request) pattern into the MCP RC. The doc cites it as
the origin. The wire format is the RC's `InputRequiredResult` shape: the
`resultType`, `inputRequests`, and `requestState` fields described above.

The nodus-mcp adapter is designed against the RC wire format directly, not
against a separate reading of SEP-2322. If there is a discrepancy between
SEP-2322 and the RC, the RC governs (Decision 1).

---

## C — Callback contract

### C1. Synchronous from Nodus, blocking from Python

From the Nodus script's perspective, `tool.invoke("mcp.srv.search", args)`
is a synchronous call that returns a result (Decision 5). The MRTR loop
runs entirely inside the Python tool handler — Nodus code never sees
`InputRequiredResult`, `inputRequests`, or `requestState`.

From the Python adapter's perspective, the handler IS blocking. It holds
the VM thread for the duration of the elicitation flow, which may include
one or more blocking waits for human input. This is acceptable in v0.1
because elicitation is a human-in-the-loop operation; total latency is
bounded by the 5-minute timeout (Decision 14).

### C2. No-handler check: at first elicitation attempt, not at discovery

The "no handler registered" check occurs when the handler first receives
an `InputRequiredResult` response, not at `mcp.connect()` / discovery time.

Rationale:
- Many tools never need elicitation; checking at discovery would penalize them.
- The handler could be registered after discovery.
- The check at invocation time matches the error semantics: the Nodus script
  called `tool.invoke`, got an error back; it can inspect `err.category`.

Check location in the handler:

```python
if response.get("resultType") == "input_required":
    callback = runtime._elicitation_handler  # set via set_elicitation_handler()
    if callback is None:
        return make_tool_error("elicitation_unsupported",
                               "no elicitation handler registered")
    # proceed with callback
```

On the **server side**: if a Nodus tool triggers elicitation but no
callback is registered on the runtime, the server returns a tool result
with `isError: true` and `category: "elicitation_unsupported"` to the MCP
client, matching the client-side error contract.

### C3. Handler thread: VM thread (synchronous, not event-loop)

The elicitation callback is invoked synchronously from within the Python
tool handler, which runs on the VM thread (the thread executing the
scheduler's `run_loop`). The callback blocks the VM thread until it returns.

The callback signature (Decision 13):

```python
def my_elicitation_fn(request: dict) -> dict:
    # request: {"inputRequests": [...], "requestState": "...", "round": N}
    # Return:
    return {"action": "accept", "content": {"field": "value"}}
    # or:
    return {"action": "decline"}
```

The callback is NOT dispatched to a worker thread or an asyncio event loop.
If the host application needs async I/O to collect user input (e.g. a web
server waiting for a form submission), the callback is responsible for
blocking until the response is available (e.g. `asyncio.run(...)` or
`concurrent.futures.Future.result()`).

This design matches Decision 13's explicit statement: "The `fn` is called
synchronously in the handler thread; it blocks until the host returns the
response." Worker-thread dispatch is a v0.2 option if real async host
applications surface a genuine need for it.

---

## D — Timeout mechanics

### D1. Per-invocation timer: `threading.Event.wait`, not the scheduler timer heap

The existing `task_timeout_ms` in the Nodus scheduler (`scheduler.py:189`)
is a per-coroutine timer (set at `run_source()` / task spawn time). It is
not per-tool-invocation and is not consulted during a blocking Python call.

For elicitation, the per-invocation timer is implemented at the Python level:

```python
event = threading.Event()
# ... start background work ...
signalled = event.wait(timeout=self._elicitation_timeout_s)  # default 300.0
if not signalled:
    return make_tool_error("elicitation_timeout",
                           f"elicitation timed out after {T}s")
```

The timeout value is resolved in priority order:
1. Per-call option: `tool.invoke("mcp.srv.tool", args, {elicitation_timeout_s: 60})`
2. Per-client construction: `McpClient(elicitation_timeout_s=120)`
3. Default: `300` (5 minutes, Decision 14)

**GAP-ELICIT-TIMEOUT-STATIC (mitigated):** If `elicitation_timeout_s ≥
run_source_timeout_ms / 1000` a single elicitation wait can outlive the
outer `run_source()` budget. The adapter validates this at construction
and logs a warning. Mitigated by the static check.

**GAP-ELICIT-TIMEOUT-CUMULATIVE (open, v0.1 known-gap):** Even with
`elicitation_timeout_s < run_source_timeout_ms / 1000`, multiple tool calls
in one `run_source()` burn the outer budget cumulatively. A late elicitation
starts its full wait with little remaining budget. The outer `run_source()`
timer fires via the scheduler's timer heap — but the VM thread is parked
inside `threading.Event.wait()` and cannot be preempted by the scheduler.
This relationship between elapsed outer time and remaining elicitation budget
is a runtime condition, undetectable at construction.

**v0.1 resolution: teardown sentinel.** Rather than leaving a parked thread
to drain on its own (undefined write-back state), `run_source()` teardown
signals every active elicitation `threading.Event` with a `TEARDOWN_SENTINEL`
value. The parked handler wakes immediately, reads the sentinel, and returns
`tool_error / category: "elicitation_aborted"` — a clean error, not a leak:

```python
# Handler (simplified):
result_box = [None]
wake_event = threading.Event()
token = runtime._register_active_elicitation(result_box, wake_event)
try:
    fired = wake_event.wait(timeout=T)
    if not fired:
        return tool_error("elicitation_timeout", ...)
    if result_box[0] is TEARDOWN_SENTINEL:
        return tool_error("elicitation_aborted",
                          "run_source teardown interrupted elicitation")
    # real response is in result_box[0]
finally:
    runtime._unregister_active_elicitation(token)

# run_source teardown:
for box, event in runtime._active_elicitations:
    box[0] = TEARDOWN_SENTINEL
    event.set()
```

This is the same primitive as D2's timeout sentinel — `event.set()` + a
typed value in a result box — triggered by outer teardown instead of by the
inner timer. The cumulative gap shrinks from "orphaned thread, undefined
state" to "elicitation cut short by outer teardown, clean `elicitation_aborted`
returned." The Nodus script still receives a `tool_error`; which elicitation
gets cut short is non-deterministic wall-clock. That residual is documented
and not fixed in v0.1. `03-transports.md` D3 references this mechanism for
the transport-side shutdown path.

For the aspirational Nodus-closure path: the timeout would be a timer entry
in the scheduler's `timers` heap. On fire, a sentinel value would be appended
to the elicitation channel. The coroutine would receive it, detect the
sentinel, and return `tool_error / elicitation_timeout`. `advance_clock` in
tests would drive this path (see E2).

### D2. Channel cleanup on timeout

For v0.1 (synchronous handler): no channel was registered; no cleanup is
required. The `threading.Event` expiry is self-contained — the event is
garbage-collected when the handler exits.

For the `_io_channels` path (future):

```python
# On timeout fire:
scheduler._io_channels.remove(elicitation_ch)
elicitation_ch.closed = True
# _drain_io_channels sees ch.closed:
#   wakes all waiting_receivers with None
#   then removes ch from _io_channels
```

Alternatively, push a typed sentinel `{"__elicitation_timeout": True}` into
the channel queue rather than closing it. The coroutine receives the sentinel,
checks for it, and returns `tool_error / elicitation_timeout`. This avoids
conflating channel-close semantics with timeout semantics.

A late-arriving real response after a timeout is handled by checking whether
the channel is still in `_io_channels` before appending:

```python
if elicitation_ch in scheduler._io_channels:
    elicitation_ch.queue.append(response)
```

If the channel was already removed on timeout, the background thread discards
the late response. No dead-coroutine wakeup, no channel leak.

---

## E — Test substrate

### E1. Deterministic round-trip testing: mock transport + stub callback

Since v0.1 uses synchronous blocking, `flush_async` / `advance_clock` are
**not needed** to drive the happy-path elicitation test. Tests use:

1. **Mock transport** — returns canned `InputRequiredResult` and final result
   immediately (no real `threading.Event` wait, no real wire I/O):

   ```python
   class MockTransport:
       def send_tools_call(self, name, args, **kw):
           if kw.get("input_responses") is None:
               return {
                   "resultType": "input_required",
                   "inputRequests": [{"id": "r1", "message": "What color?"}],
                   "requestState": base64.b64encode(b'{"round":1}').decode(),
               }
           return {"resultType": "success", "content": [{"type": "text", "text": "blue"}]}
   ```

2. **Stub elicitation callback** — returns a canned response immediately:

   ```python
   runtime.set_elicitation_handler(
       lambda req: {"action": "accept", "content": {"r1": "blue"}}
   )
   ```

The Nodus test file calls `tool.invoke` synchronously; the mock transport
resolves the MRTR loop without blocking. The test asserts the final result.
No `flush_async` needed.

For the aspirational Nodus-closure path, `flush_async` IS the mechanism:
the test injects the `inputRequests` result into the channel queue directly
(bypassing the transport), then calls `test.flush_async()` to let the
scheduler drain `_io_channels` and wake the blocked coroutine.

```nodus
// Hypothetical future test (Nodus-closure handler path)
spawn(coroutine(fn() {
    let result = tool.invoke("mcp.srv.tool", {q: "foo"})
    // ...
}))
test.flush_async()              // coroutine reaches recv(elicitation_ch)
// inject into channel from test:
mcp._test_inject_elicitation_response({action: "accept", content: {r1: "blue"}})
test.flush_async()              // coroutine wakes with response, completes
```

This is a v0.2+ design target.

### E2. Virtual-clock and timeout testing

**Synchronous handler path (v0.1):** `advance_clock` has no effect on a
blocking `threading.Event.wait()` — the scheduler clock is not consulted
during a Python-level block. Timeout tests use a very short explicit timeout:

```python
runtime = NodusRuntime()
client = McpClient(runtime, elicitation_timeout_s=0.01)  # 10ms
runtime.set_elicitation_handler(lambda req: time.sleep(1))  # longer than timeout
result = runtime.tool_registry.invoke("mcp.srv.tool", {})
assert result["category"] == "elicitation_timeout"
```

**`_io_channels` path (future):** `advance_clock(5 * 60 * 1000 + 1)` fires
the timeout timer entry in the scheduler's `timers` heap. The timer pushes
a timeout sentinel into the elicitation channel. `_drain_io_channels` wakes
the blocked coroutine with the sentinel. The coroutine returns
`tool_error / elicitation_timeout`. This is the clean `advance_clock` test:

```nodus
// Future design test
spawn(coroutine(fn() {
    let result = tool.invoke("mcp.srv.tool", {q: "foo"})
    test.assert_equal(result.category, "elicitation_timeout")
}))
test.flush_async()              // reaches recv(elicitation_ch)
test.advance_clock("5m 1ms")    // fires timeout timer
test.flush_async()              // coroutine wakes with timeout sentinel
```

---

## F — Bytecode impact: none, with justification

The MCP adapter adds no new opcodes and does not modify the VM. This
conclusion is non-trivial because elicitation touches scheduler internals
more deeply than the adapter mapping of doc 1. The justification:

| Component used | Layer | New bytecode? |
|---|---|---|
| `Channel` | Runtime object (`runtime/channel.py`) | No — existing class |
| `scheduler._io_channels` | Scheduler list (`runtime/scheduler.py:44`) | No — existing field |
| `ChannelRecvRequest` | Runtime object (`runtime/channel.py:16`) | No — existing class |
| `threading.Event.wait` | Python stdlib | No — not a Nodus construct |
| `_drain_io_channels` | Scheduler method | No — already called every tick |
| `call_builtin` detection | `vm.py:1590–1591` | No — existing check |

Every component the adapter uses exists in the current codebase. The
adapter is a Python layer that registers callables into `_python_registered_tools`
and uses the scheduler's existing I/O channel infrastructure from outside the
VM (not from a new opcode). No opcode is added because no new Nodus language
construct is introduced — elicitation is invisible to Nodus scripts (Decision 5).

`BYTECODE_VERSION` remains 4. This is not reflexive: the scheduler's timer
heap, `_io_channels`, and `Channel` are all runtime-layer constructs, not
bytecode-layer constructs. Bytecode governs what instructions the VM can
execute; the scheduler governs how coroutines are scheduled. These are separate
layers. A change that adds a new scheduler behavior without adding a new
instruction does not touch bytecode.

---

## Summary of settled contracts

| Question | Answer |
|---|---|
| v0.1 blocking mechanism | `threading.Event.wait(timeout)` in Python handler |
| `_io_channels` applies when | Tool handler is a Nodus closure calling `recv(ch)` |
| Background thread identity | Transport-owned (see `03-transports.md`) |
| Parallel vs reuse of http path | Parallel pattern (same primitives, different code) |
| MRTR round-trip cap | 10 (configurable); exceeded → `tool_error/elicitation_rounds_exceeded` |
| `requestState` role | Carries server-side elicitation context across rounds |
| SEP-2322 | Cited as origin; RC wire format governs |
| Callback call site | VM thread, synchronous, blocking |
| No-handler check | At first `InputRequiredResult`, not at discovery |
| Timeout attachment | `threading.Event.wait(T)` in handler; T = per-call → per-client → 300s |
| GAP-ELICIT-TIMEOUT-STATIC | Mitigated: construction-time warning if `elicitation_timeout_s ≥ run_source_timeout` |
| GAP-ELICIT-TIMEOUT-CUMULATIVE | Open v0.1 known-gap: cumulative burn undetectable at construction; teardown sentinel provides clean abort |
| Teardown sentinel mechanism | `runtime._active_elicitations` registry; teardown sets `TEARDOWN_SENTINEL` + `event.set()` |
| Channel cleanup on timeout | N/A for v0.1 (no channel); sentinel push for `_io_channels` path |
| Test happy path | Mock transport + stub callback; no `flush_async` needed |
| Test timeout path | Short `elicitation_timeout_s` + blocking stub; `advance_clock` for future `_io_channels` path |
| Bytecode changes | None; `BYTECODE_VERSION` stays 4 |
