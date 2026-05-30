# 06 — Embedding Runtime Blockers and Design Decisions

**Phase:** 1 (Design)
**Status:** Decision recorded — input from v4.0.0 embedding-API stress-test
**Depends on:** `00-decisions.md`, `03-transports.md`, `04-server-mode.md`

---

## Purpose

The v4.0.0 embedding-API readiness run identified two runtime gaps that block naive
implementations of the stdio transport core loop and the server-mode elicitation
path. This document records both gaps and the design decision made for nodus-mcp
Phase 1 implementation.

---

## BLOCKER 1 — No public keep-alive for host-fed channels

### The gap

A coroutine blocked on `recv()` of an empty channel is orphaned: `run_loop` sees
no work in any tracked structure and exits, stranding it silently (**CHAN-001**).
The only thing that prevents this exit is `scheduler._io_channels` — an
underscore-private attribute. `NodusRuntime` exposes no public method to register
a host-managed channel as a keep-alive source.

Additionally, `_drain_io_channels` has a close-ordering race: setting `ch.closed`
removes the channel from `_io_channels` even with undelivered values still queued.
This means streaming via a host-managed channel can silently lose data.

The one reliable host→coroutine streaming pattern today is the **subprocess pipe**:
daemon threads pump `ch.queue` continuously, with no close-race, keeping
`_io_channels` alive. See EMBED-003 (#99) in nodus-lang.

### Decision

**Design around the subprocess-pipe pattern (Option B).** Do not depend on
`_io_channels` direct registration or host-managed plain channels for Phase 1.

Concrete implications:
- The stdio transport reads from the host process's stdin using a daemon pump
  thread that continuously writes to a channel queue — exactly the subprocess-pipe
  model. No `_io_channels` registration is needed.
- The channel is pre-populated with queued items before `run_loop` is called, OR
  the pump thread feeds it continuously, keeping the scheduler alive.
- The close-race does not apply because the pump thread manages the channel
  lifecycle explicitly.

### Nodus-lang prerequisite (backlog, not blocking Phase 1)

When nodus-lang exposes `runtime.register_io_channel(ch)` and fixes the
close-drain ordering, nodus-mcp can migrate to that cleaner path in v0.2.
Until then, the subprocess-pipe pattern is the supported approach. Filed as
nodus-lang issue to-be-created; relates to EMBED-003 (#99).

---

## BLOCKER 2 — No public on_error hook (EMBED-002 / nodus-lang #98)

### The gap

`Scheduler.run_loop` accepts `on_error` but `NodusRuntime` never exposes or passes
it. When a handler coroutine dies on an uncaught error, the scheduler prints to
stderr, marks it `state="finished"`, and continues. After `run_source` returns, an
errored coroutine and a normally-completed one are indistinguishable.

An MCP server must detect that handler coroutine N died, send an error response for
request N, and keep serving others. The current runtime gives no programmatic signal.

### Decision

**Use the in-Nodus error protocol (Option B).** Require every request-handler
coroutine to catch its own errors and write a structured error record to its output
channel rather than letting exceptions propagate to the scheduler.

Pattern:
```nd
fn handle_request(request, out_ch) {
    try {
        let result = process(request)
        send(out_ch, {ok: true, result: result})
    } catch err {
        send(out_ch, {ok: false, error: err.message, kind: err.kind})
    }
}
```

The host reads from `out_ch` and dispatches the error response to the MCP client.
No runtime hook needed; composable with existing channel primitives; error isolation
already verified (one bad handler does not kill others — confirmed in embedding
stress-test).

### Nodus-lang prerequisite (backlog, not blocking Phase 1)

When nodus-lang adds a public `on_error` hook to `NodusRuntime` (EMBED-002 / #98),
nodus-mcp can expose richer per-handler error metadata. Until then, the in-Nodus
catch pattern is the required approach.

---

## What is already viable (de-risked, no design changes needed)

| Surface | Status | Evidence |
|---------|--------|----------|
| Long-lived loops | Viable | No memory/timer/state leak over multi-second runs |
| Error isolation | Viable | One handler dying does not kill others |
| Genuine concurrency | Viable via subprocess_spawn + channel reads | 5 × 200ms I/O → ~260ms (not 1s serial) |
| EMBED-001 (timeout default) | Documented | Hosts must pass `timeout_ms=None`; see #97 |
| EMBED-004 (serial async) | Known limitation | Document; `subprocess_spawn` is the concurrency path |

---

## Phase 1 transport design constraints (from this document)

1. The stdio transport core loop must be expressed using the subprocess-pipe-inspired
   pattern for host→coroutine streaming (daemon thread → channel queue).
2. Every request handler must catch its own errors and write structured error output
   to a result channel. No reliance on scheduler on_error.
3. `NodusRuntime(timeout_ms=None, max_steps=None)` must be used for the server
   embedding — the default 200ms deadline kills any request over 200ms cumulative.
4. Serial async I/O is a known throughput ceiling for handler code that uses
   `http_get` or `subprocess_run`; `subprocess_spawn` is the genuine-concurrency
   path and is preferred for I/O-bound handlers.

These constraints feed directly into `03-transports.md` (stdio transport design)
and `04-server-mode.md` (handler coroutine lifecycle).
