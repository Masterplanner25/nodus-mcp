# nodus-mcp Phase 0 Decisions

**Cycle:** nodus-mcp v0.1
**Phase 0 date:** 2026-05-28
**Status:** Fully locked. All 16 decisions settled as of 2026-05-28.
**Maintainer:** Shawn Knight (Masterplanner25)

---

## Purpose

This document captures the design decisions resolved during Phase 0 of
nodus-mcp v0.1. It follows the structure of nodus-lang's
`docs/design/v4/00-phase-0-decisions.md`. Each decision records: what
was decided, the source of the decision, and implementation implications.

Phase 0 is complete when all open decisions are resolved. All 16 decisions
are settled as of 2026-05-28. Phase 1 design docs can begin.

---

## Decision 1 — Target specification: 2026-07-28 RC

**Decision:** nodus-mcp v0.1 implements against the MCP **2026-07-28 RC**
specification, not the 2025-11-25 revision that Decision 16 in nodus-lang
originally pinned.

**Source:** Feasibility report (design conversation, 2026-05-28). The RC is a
breaking change from 2025-11-25: the session initialization handshake
(`initialize`/`initialized`) is eliminated; client info and capabilities
travel in `_meta` on every request; `Mcp-Session-Id` header is gone;
`server/discover` replaces capability exchange.

**Why:** Building against the 2025-11-25 spec and retrofitting to the RC
after the fact would require redesigning the entire lifecycle and transport
layer. The RC is cleaner (stateless-first eliminates an entire class of
server state management) and is already the canonical reference for new
implementations.

**Implementation implication:** The library is stateless-first from Phase A.
There is no session object in the API, no session state to manage. Per-request
context (client info, capabilities) is passed in `_meta`.

**Fallback reference:** The 2025-11-25 spec remains useful as historical
context for capability categories, message shapes, and the rationale behind
features. Where the RC is ambiguous, the 2025-11-25 spec clarifies intent.

---

## Decision 2 — Stateless-first architecture

**Decision:** Every server-side handler is stateless by default. Request state
for multi-round-trip flows (elicitation) is carried in the `requestState`
opaque blob on the wire, not in server-side memory. There is no session
persistence layer in v0.1.

**Source:** MCP 2026-07-28 RC; SEP-1442 (Stateless MCP); feasibility report
(2026-05-28 design conversation).

**Why:** The RC designed sessions out of the protocol because shared storage
across server instances is operationally expensive. Nodus workflows are
themselves stateless between invocations; building nodus-mcp on a stateless
foundation aligns the library with both the protocol direction and Nodus's
execution model.

**Implementation implication:** Server handlers receive the full context they
need on each call. `requestState` is an opaque base64 blob round-tripped by
the client; the server serializes/deserializes it. No Redis, no PostgreSQL, no
in-process session store for the common case.

---

## Decision 3 — Protocols-are-adapters: tool registry as source of truth

**Decision:** The Nodus `std:tool` registry is the single source of truth for
tools that are callable via MCP. nodus-mcp's server role exposes the
`NodusRuntime.tool_registry` to MCP clients; its client role registers
discovered MCP tools into the registry.

**Source:** nodus-lang `docs/governance/LIBRARY_ECOSYSTEM.md`
§"Architectural commitment: protocols are adapters" and §"Practical
implication for v4.0"; feasibility report M2 hypothesis (confirmed).

**Why:** If MCP owned the architecture, every future protocol would need a
different registry, a different discovery path, a different execution model.
Using the Nodus tool registry means MCP is one adapter among many — adding
nodus-a2a later means adding another adapter to the same registry, not a
parallel system.

**Implementation implication:**

- **Client role:** When connecting to an MCP server, nodus-mcp calls
  `server/discover`, iterates the tool list, and calls
  `tool_registry.register()` for each, using the namespace convention
  `mcp.<alias>.<tool_name>`. Registration persists in
  `_python_registered_tools` across `run_source()` calls.
- **Server role:** `NodusRuntime.tool_registry.list_tools()` is the source
  for the tool list served to MCP clients via `tools/list`.

---

## Decision 4 — Tool namespace convention: `mcp.<alias>.<tool_name>`

**Decision:** MCP tools are registered in the Nodus registry under the name
`mcp.<alias>.<tool_name>`, where `<alias>` is assigned by the user at
discovery time (when calling `mcp.connect(url, alias="srv1")` or similar).
The alias scopes multiple MCP server connections that might expose tools
with the same name.

**Source:** Design conversation (2026-05-28). The Nodus tool registry requires
dotted namespacing with at least one dot; MCP tool names are flat strings.

**Why:** Two MCP servers might both expose a `read_file` tool. `mcp.srv1.read_file`
and `mcp.srv2.read_file` are unambiguous. The alias is user-assigned, not
auto-generated, so scripts can use stable names.

**Implementation implication:** The `mcp.connect()` function (Phase A/C)
accepts an `alias` parameter. All tools discovered from that server are
registered as `mcp.<alias>.<tool_name>`. If `alias` is omitted, a sanitized
version of the server URL is used (deterministic, documented).

---

## Decision 5 — MRTR elicitation is encapsulated inside the Python handler

**Decision:** From the Nodus script's perspective, `tool.invoke("mcp.srv.search",
args)` is a synchronous call that returns a result. The multi-round-trip
elicitation flow (SEP-2322) is handled entirely inside the Python callable
registered as the tool handler. Nodus code does not see `InputRequiredResult`,
`inputRequests`, or `requestState`.

**Source:** Feasibility report M2 analysis; design conversation (2026-05-28).

**Why:** The alternative — surfacing `InputRequiredResult` as a Nodus err
record that scripts handle — would require every Nodus script that calls MCP
tools to handle elicitation explicitly. This creates ceremony for the common
case (tools that never need elicitation) and forces protocol-level concerns
into user code. Encapsulation inside the handler makes elicitation invisible
to scripts while remaining fully supported.

**Implementation implication:** The Python handler callable:

```python
def mcp_tool_handler(args):
    result = transport.send_tools_call(name, args)
    while result.get("resultType") == "input_required":
        user_responses = elicitation_callback(result["inputRequests"])
        result = transport.send_tools_call(
            name, args,
            input_responses=user_responses,
            request_state=result.get("requestState"),
        )
    return result
```

The `elicitation_callback` integration point is settled in Decision 13.

---

## Decision 6 — Elicitation timeout mechanism

**Decision:** If the elicitation callback does not return within a configured
timeout, the handler raises a `tool_error` err record with
`category: "elicitation_timeout"` back to the Nodus script.

**Source:** Design conversation (2026-05-28). The mechanism is settled here;
the default value is settled in Decision 14 (5 minutes, per-invocation configurable).

**Why:** The blocking handler model (Decision 5) requires a safety valve.
Without a timeout, a script waiting for human elicitation input could block
indefinitely. The `tool_error` / `elicitation_timeout` shape is consistent
with the existing `tool_error` err contract from `std:tool`.

**Implementation implication:** The transport layer wraps the elicitation wait
in a timed context. On timeout, it returns a `tool_error` record. The
timeout is configurable per-handler or globally at runtime construction.

---

## Decision 7 — Production elicitation substrate: `_io_channels`

**Decision:** In production (non-test) code, elicitation waiting uses the
Nodus scheduler's `_io_channels` mechanism — the same Channel-backed async
I/O infrastructure used by `std:http` streaming and SSE. The test framework's
`flush_async`/`advance_clock` mechanism is used only in tests.

**Source:** Feasibility report M5 hypothesis (refuted for production, confirmed
for testing); design conversation (2026-05-28).

**Why:** `flush_async` and `advance_clock` are explicitly test-mode-only
primitives ("no-op in production code"). Elicitation waiting is a real async
I/O operation that must work in production. The `_io_channels` mechanism
is already used for async HTTP and handles thread-to-coroutine wakeup
correctly.

**Implementation implication:** The elicitation callback, when implemented as
a coroutine-friendly path, pushes the `inputRequests` into a Channel that the
host application reads. The host writes the `inputResponses` back. The
scheduler's `_drain_io_channels` wakes the blocked coroutine. For testing,
the mock transport can use `flush_async`/`advance_clock` to simulate the
elicitation flow deterministically.

---

## Decision 8 — Per-VM tool registry is the correct home

**Decision:** MCP client-side tool registration uses
`NodusRuntime.tool_registry.register()`, which writes to
`_python_registered_tools` and persists across `run_source()` calls.

**Source:** Feasibility report M2 (per-VM registry confirmed); nodus-lang
embedding.py `ToolRegistry` implementation.

**Why:** Tools discovered from an MCP server persist for the lifetime of the
`NodusRuntime` instance — they survive individual script executions. This is
the correct behavior: a Nodus workflow can call MCP tools discovered in a
previous script execution. The `_python_registered_tools` dict (vs the
ephemeral `vm.tool_registry`) is the right layer because it survives VM resets.

**Implementation implication:** `mcp.connect(url, alias)` (client role)
performs discovery, then calls `runtime.tool_registry.register()` for each
discovered tool. `mcp.disconnect(alias)` calls `runtime.tool_registry.unregister()`
for all tools in that namespace.

---

## Decision 9 — Entry-point contract for `import "nodus-mcp"`

**Decision:** `pip install nodus-mcp` is sufficient for `import "nodus-mcp"`
to work in any Nodus script. No additional setup steps (no `nodus add`, no
`python -m nodus_mcp init`).

**Source:** nodus-lang feasibility pass (Option 3 recommendation); nodus-lang
commit `ea16b10` implementing the `nodus.nd` entry-point resolver;
`docs/guide/library-entry-points.md` in nodus-lang.

**Contract:**

```toml
[project.entry-points."nodus.nd"]
nodus-mcp = "nodus_mcp.nd:get_nd_root"
```

`get_nd_root()` returns `os.path.join(os.path.dirname(__file__), "nd")` —
the directory containing `index.nd` (bare import) and sub-module `.nd` files
(colon form). This is wired and validated by the scaffold's roundtrip test.

---

## Decision 10 — Bidirectional library (client + server)

**Decision:** nodus-mcp v0.1 implements both client role (calling MCP servers)
and server role (exposing Nodus tools as MCP-callable). Neither role is
optional or deferred.

**Source:** Decision 16 (nodus-lang `docs/governance/V4_0_PLAN.md`); rejected
alternative "minimum viable (client only)" explicitly rejected there.

**Why:** "The 'protocols are adapters' commitment in LIBRARY_ECOSYSTEM.md is
validated by shipping with two adapters... One adapter is a coincidence; two
is a pattern." The full bidirectional library validates the Nodus-as-agent-tool
claim; client-only leaves half the story unbuilt.

---

## Decision 11 — Transport scope: stdio + HTTP only (corrects Decision 16)

**Decision:** nodus-mcp v0.1 implements **two transports**: stdio and HTTP.
The original Decision 16 named three (stdio, HTTP, Streamable HTTP), but this
was written against the 2025-11-25 spec. The 2026-07-28 RC's stateless model
collapsed the "HTTP" vs "Streamable HTTP" distinction: with no sessions and no
SSE required for basic calls, what the RC calls the HTTP transport subsumes
what the pre-RC spec called Streamable HTTP. There is no separate "Streamable
HTTP transport" in the RC's model.

**Source:** Design conversation (2026-05-28); validated against feasibility
report RC analysis. This is a **correction**, not a scope cut — the capability
surface is the same, the transport count is two not three because the RC
merged them.

**Why:** The pre-RC transport distinction arose from the need to distinguish
between SSE-streamed responses (Streamable HTTP) and simple request/response
HTTP. The RC eliminates SSE requirements for the base transport; streaming
(e.g. progress notifications) flows through `_meta` and is a message-level
feature, not a transport-level feature.

**Implementation implication:** Phase A (foundation) and Phase B (stdio)
proceed as originally sequenced. The original Phase G (HTTP transports) is
now a single implementation item rather than two. Phase sequencing shifts
accordingly — there is no "Phase G part 2" for a separate Streamable HTTP
transport.

---

## Decision 12 — Feature scope: Tasks deferred; Roots + Sampling in; MCP Apps + Logging out

**Decision:**

| Feature | v0.1 status | Reason |
|---|---|---|
| Tasks | Deferred to v0.2 | Moved from core to extension in RC; extension is still settling |
| MCP Apps | Deferred to v0.2+ | New extension, no production maturity, not in Decision 16 original scope |
| Roots | **Included** | Real interop need with existing servers; 12-month deprecation window is sufficient runway |
| Sampling | **Included** | Real interop need with existing servers; same 12-month window reasoning |
| Logging | Skipped entirely | Least essential of the three deprecated capabilities; host application logging (Python's `logging` module, observability pipelines) covers the use cases. Not worth building on a deprecated foundation. |

**Source:** Design conversation (2026-05-28).

**On the deprecation window:** Roots and Sampling are described in the RC as
deprecated-but-functional for ≥12 months. v0.1 is built knowing they are
scheduled for removal. The 12-month window is sufficient for v0.1's useful
life and for downstream users to migrate. nodus-mcp's own deprecation notice
for these features will land before the protocol removes them.

**Implementation implication:** Phase F (Client advanced) in Decision 16's
original sequencing covered sampling, logging, progress, completion, roots,
and elicitation. In v0.1: phase F implements Roots, Sampling, and Elicitation
(via SEP-2322 MRTR). Logging is removed from Phase F entirely. Tasks is
removed from server phases (H+) and treated as a v0.2 extension.

---

## Decision 13 — Server-side elicitation callback wiring: Python callback primary

**Decision:** Host applications that embed nodus-mcp and need to handle
server-side elicitation (a Nodus tool handler mid-call asking the user for
input) register a Python callback via `runtime.set_elicitation_handler(fn)`.

**Callback contract:**

```python
# Registered on the NodusRuntime instance that owns the MCP server
runtime.set_elicitation_handler(my_elicitation_fn)

# Callback signature:
def my_elicitation_fn(request: dict) -> dict:
    # request is the inputRequest dict from SEP-2322, e.g.:
    # {"method": "elicitation/create", "params": {"message": "...", "requestedSchema": {...}}}
    # Return one of:
    return {"action": "accept", "content": {"field": "value"}}
    # or:
    return {"action": "decline"}
```

If no callback is registered and a tool handler triggers elicitation,
`tool.invoke` returns a `tool_error` err record with
`category: "elicitation_unsupported"` to the Nodus script.

**Source:** Design conversation (2026-05-28). Channel-based wiring is deferred
to v0.2 if real async host applications surface a genuine need for it.

**Why:** The Python callback form is simple, synchronous, and matches how
99% of host applications will handle elicitation (display a prompt, wait for
user input, return the response). The `_io_channels` mechanism (Decision 7)
remains the correct production substrate for the internal async plumbing, but
the host-facing API is the callback. Wrapping channels in a callback adapter
internally is an implementation detail.

**Implementation implication:** `runtime.set_elicitation_handler(fn)` stores
`fn` on the `NodusRuntime` instance. The nodus-mcp server module checks for
it before triggering elicitation; if absent, it short-circuits to
`elicitation_unsupported`. The `fn` is called synchronously in the handler
thread; it blocks until the host returns the response.

---

## Decision 14 — Elicitation timeout default: 5 minutes, per-invocation configurable

**Decision:** The default timeout for elicitation waits (Decision 6 mechanism)
is **5 minutes**. The timeout is configurable per-invocation via the
`tools/call` call options; a per-runtime default can be set at construction.

**Source:** Design conversation (2026-05-28).

**Why 5 minutes:** Short enough that a forgotten elicitation (e.g. a
background script left waiting for human input with no one watching) doesn't
hang a process indefinitely. Long enough to accommodate realistic human-input
latency for interactive use: a user who takes 3 minutes to fill a form is
within the window; a user who took a coffee break and came back is not, and
that's acceptable. 30 seconds would be too tight for any interactive use.
None (no default) creates friction for every caller. 5 minutes is the
idiomatic choice for human-in-the-loop flows across the ecosystem.

**Implementation implication:** The elicitation wait uses a `threading.Event`
or equivalent with a `timeout=300` (300 seconds = 5 minutes). On expiry:
raises `tool_error` / `category: "elicitation_timeout"` (Decision 6). The
per-invocation override is passed through `tool.invoke` call metadata or
the transport's call options dict. Construction: `McpClient(elicitation_timeout_s=120)`
overrides the default for all invocations on that client.

---

## Decision 15 — Authorization: bearer token only in v0.1

**Decision:** nodus-mcp v0.1 supports **bearer token authentication only**.
OAuth2/OIDC is deferred to v0.2. v0.1 cannot connect to MCP servers that
require OAuth2 — this limitation is documented prominently in the README.

**Source:** Decision 16 amendment (nodus-lang `V4_0_PLAN.md`); design
conversation (2026-05-28). Matches nodus-a2a's planned auth posture for
ecosystem consistency.

**Why:** OAuth2 client credentials flow adds significant implementation
complexity (token refresh, PKCE, well-known discovery, error handling for
expired tokens). Bearer tokens cover the common case: API keys and session
tokens, which is how most MCP servers in the current ecosystem authenticate.
The v0.2 timeline is not blocked on v0.1's bearer-only limitation since
the tool call / discovery path is identical regardless of auth scheme —
only the HTTP headers differ.

**Implementation implication:** The HTTP transport adds an `Authorization:
Bearer <token>` header when a token is configured. Token is passed at
connection time: `mcp.connect(url, bearer_token="...")`. The `server/discover`
call and all subsequent tool calls carry the token. No token rotation, no
refresh. The README clearly states: "v0.1 does not support OAuth2. Servers
requiring OAuth authentication are not supported until v0.2."

---

## Decision 16 — std:tools compatibility: separate domains, no conflict

**Decision:** `std:tools` (the existing Nodus v3.x module with `tool_call`,
`tool_available`, `tool_describe` builtins) and nodus-mcp's server role solve
**different problems** and coexist without conflict in v0.1.

- `std:tools` exposes the Nodus runtime to a **Python host application** via
  the embedding API's service registration pattern — this is a Python-to-Nodus
  boundary, not a network boundary.
- nodus-mcp server exposes Nodus tools to **MCP clients over the network** via
  the MCP protocol — this is a network boundary.

They are not in the same domain. A Nodus tool registered via `std:tool.register`
and a `std:tools` service registered by the Python host are different things.
There is no migration needed between them in v0.1.

**Source:** Design conversation (2026-05-28). This resolves the
"semantic-incompatibility-table" question parked in nodus-lang's
TECH_DEBT.md (3C.2 commit notes).

**v0.2 plan:** nodus-mcp v0.2 will add a "runtime-tool registration" feature
that makes the nodus-mcp server automatically expose tools registered in
`std:tool` to MCP clients, without any Python embedding plumbing. At that
point, `std:tools` (which previously served as the mechanism for "host
registers tools that Nodus calls") becomes formally deprecated with the
semantic-incompatibility table from the 3C.2 commit notes as the migration
map. v0.1 does not trigger this deprecation.

**Implementation implication for v0.1:** The server module's `tools/list`
handler enumerates `NodusRuntime.tool_registry.list_tools()` — only tools
registered via `std:tool.register` (Decision 3). It does NOT enumerate
`std:tools` services. The two systems are invisible to each other in v0.1.

---

## Phase 0 complete — Phase 1 plan

All 16 decisions are settled. Phase 0 is complete as of 2026-05-28.

### What Phase 1 covers

Phase 1 produces approximately five focused design docs, one per major
design area, before implementation begins:

1. **Adapter mapping core** — how the stateless request model (no sessions,
   `_meta` per-request, `server/discover`) maps onto the Nodus tool registry.
   The source-of-truth contract between the MCP wire format and `std:tool`
   entry shapes.

2. **Elicitation via `_io_channels`** — the full MRTR loop (SEP-2322),
   callback wiring (Decision 13), timeout (Decision 14), error contract
   (Decision 6), and test substrate (flush_async/advance_clock). This is the
   most complex single design area and warrants its own doc.

3. **Transports** — stdio + HTTP (Decision 11). Shared-core design so that
   switching transports doesn't change the tool-invocation path. Message
   framing, connection lifecycle, error propagation.

4. **Server mode** — tool registry → MCP enumeration, request routing,
   server-side elicitation (Decision 13), bearer auth on the server side
   (Decision 15). Scope explicitly excludes `std:tools` interop until v0.2
   (Decision 16).

5. **Deprecated-feature handling** — Roots and Sampling in v0.1 (Decision 12).
   Implementation strategy for building against deprecated protocol features:
   the deprecation notice the library will carry, the plan for removal when the
   protocol removes them, and how tests are structured to catch when the
   features stop working.

These five docs are Phase 1. Once they exist, Phase A (foundation) can be
implemented against them.
