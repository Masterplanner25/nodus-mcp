# nodus-mcp Phase 0 Decisions

**Cycle:** nodus-mcp v0.1
**Phase 0 date:** 2026-05-28
**Status:** Partially locked. See "Open decisions" section below.
**Maintainer:** Shawn Knight (Masterplanner25)

---

## Purpose

This document captures the design decisions resolved during Phase 0 of
nodus-mcp v0.1. It follows the structure of nodus-lang's
`docs/design/v4/00-phase-0-decisions.md`. Each decision records: what
was decided, the source of the decision, and implementation implications.

Phase 0 is complete when all items in the "Open decisions" section are
resolved. Phase 1 (design docs for each Phase A–N implementation phase)
begins after.

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

The `elicitation_callback` integration point is an open decision (see
"Open decisions" §M3).

---

## Decision 6 — Elicitation timeout mechanism

**Decision:** If the elicitation callback does not return within a configured
timeout, the handler raises a `tool_error` err record with
`category: "elicitation_timeout"` back to the Nodus script.

**Source:** Design conversation (2026-05-28). The mechanism is settled; the
default value is open (see "Open decisions").

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

## Open decisions — NOT yet settled, require chat resolution before Phase 1 design

The following items have not been decided. No defaults are assumed. Phase 1
design docs cannot be written until these are resolved.

---

### M1 — Transport scope for v0.1

**Question:** The original Decision 16 listed three transports: stdio, HTTP,
and Streamable HTTP (in that implementation order, Phases B and G). The RC's
stateless model may have collapsed some distinctions:

- stdio transport is still a distinct, well-defined transport (piped
  process communication, e.g. Claude Desktop's model)
- HTTP transport in the RC is stateless (no session headers, no SSE
  required for basic calls) — what exactly is the "HTTP" transport vs
  the "Streamable HTTP" transport in the stateless model?
- Does the RC effectively reduce the transport distinction to
  stdio (stateful connection) vs HTTP (stateless per-request)?

**Specific question:** Which transports does v0.1 implement, and in what
order? Is the Phase B + Phase G structure still correct, or does the RC
suggest a different decomposition?

---

### M2 — Feature scope: Tasks, MCP Apps, Roots, Sampling, Logging

**Question:** Several MCP capabilities have changed status between 2025-11-25
and the 2026-07-28 RC:

- **Tasks** moved from core protocol to an official extension (ext-apps). The
  original Decision 16 had Tasks in Phase H+ (server-side). Does v0.1 include
  the Tasks extension? Or is Tasks a v0.2 item?
- **MCP Apps** is a new official extension in the RC (servers ship HTML
  interfaces). No mention in Decision 16. v0.1 or v0.2?
- **Roots, Sampling, Logging** are described in the RC as
  "deprecated-but-functional for 12+ months." Decision 16's Phase F (Client
  advanced) included all three. Options:
  - Implement them (they work for ≥12 months, interop is valid)
  - Skip them (they're on a deprecation path, why build on them now?)
  - Implement Logging only (most practically useful for observability)?

**Specific question:** Which of {Tasks, MCP Apps, Roots, Sampling, Logging}
are in v0.1 scope, which are v0.2, and which are explicitly out?

---

### M3 — Server-side elicitation callback wiring

**Question:** When nodus-mcp is embedded via `NodusRuntime`, and a Nodus tool
handler triggers MCP elicitation (the server receives a call, its implementation
needs user input), how does the host application provide that input?

The mechanism is settled (elicitation is encapsulated in the Python handler;
Decision 5). The wiring is not. Options:

1. **Python callback registered at construction:**
   `NodusRuntime(elicitation_handler=my_fn)` — simple, synchronous.
2. **Channel passed at construction:**
   `NodusRuntime(elicitation_channel=channel)` — async-native, matches
   the `_io_channels` substrate (Decision 7).
3. **Both, caller chooses:** The callback form wraps into a Channel
   internally; the Channel form is the primitive.
4. **Not in v0.1 server role:** Server-side elicitation is deferred; v0.1
   server only handles tools that don't need elicitation.

**Specific question:** Which form, and does v0.1 server support elicitation
at all?

---

### M4 — Elicitation timeout default value

**Question:** Decision 6 settles the mechanism (elicitation_timeout err record).
The default value is not settled. Candidates:

- `30s` — tight for human interaction, right for automated pipelines
- `5m` — reasonable for interactive use, may be too long for pipelines
- `None` (no default timeout, caller must set one) — explicit but friction-heavy
- `configurable at construction with a sensible default`

**Specific question:** What is the default timeout value, and is it
per-invocation, per-handler, or per-runtime?

---

### M5 — Authorization depth

**Question:** The 2026-07-28 RC hardened authorization (the Decision 16
amendment mentioned bearer tokens as the v0.1 scope, with OAuth2/OIDC/mTLS
deferred to v0.2). The RC added explicit authorization-related capability
flags and may have changed the bearer token flow. Options:

- Bearer token only, exactly as Decision 16 scoped
- Bearer token + API key (the RC's "HTTP auth" mechanisms, which are low-cost
  to add alongside bearer)
- Full OAuth2 client credentials (the RC seems to have explicit support for
  this; adds significant complexity)

**Specific question:** What authorization schemes does v0.1 implement? Is the
Decision 16 "bearer only" scope still correct given the RC's changes, or does
the RC's authorization structure warrant expanding slightly?

---

### M6 — std:tools compatibility surface

**Question:** The existing `std:tools` module (Nodus v3.x) exposes
`tool_call`, `tool_available`, `tool_describe` builtins that make the Nodus
runtime MCP-callable via a service registration pattern. nodus-mcp's server
role does similar work through the new `std:tool` registry.

This was parked as "semantic-incompatibility-table belongs in Phase 4 docs
in nodus-lang" — it's now a real nodus-mcp design question. Options:

1. **Coexist:** `std:tools` and nodus-mcp server mode both work, addressing
   different use cases. `std:tools` is for embedding-API tool injection;
   nodus-mcp server is for MCP-protocol exposure. No migration needed.
2. **nodus-mcp supersedes:** nodus-mcp's server mode handles everything
   `std:tools` does and more. Publish a migration guide; `std:tools` becomes
   a compatibility shim.
3. **Separate domains:** `std:tools` exposes Nodus to the runtime host
   (Python embedding); nodus-mcp exposes Nodus to MCP clients (over the
   network). They're not the same thing — no conflict, no migration needed.

**Specific question:** Are `std:tools` and nodus-mcp server mode solving the
same problem or different problems? If different, what's the boundary?

---

## Phase 1 gate

Phase 1 begins when all six open decisions (M1–M6) are resolved in a chat
session that produces an amendment to this document. Each resolved decision
gets a new numbered entry in the "settled" section above. Phase 1 design docs
then spec out the first implementation phases.
