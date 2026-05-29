# nodus-mcp Changelog

## [0.1.0] — PREPARED, NOT RELEASED

> **Coordinated launch:** nodus-mcp v0.1.0 is prepared but not published.
> Release waits for nodus-a2a v0.1.0 to exist. All three artifacts
> (nodus-lang 4.0.0, nodus-mcp 0.1.0, nodus-a2a 0.1.0) ship together.

### Summary

First release of nodus-mcp — the MCP (Model Context Protocol) adapter library
for Nodus. Implements the 2026-07-28 RC specification. Bidirectional: both
MCP client (A–G) and MCP server (H–M).

### Features

**Client (Phases A–G)**
- Foundation: JSON-RPC 2.0 core, MCP message types, McpConnection handle,
  ActiveElicitationRegistry with teardown sentinel (Phase A)
- StdioTransport: persistent reader thread, pending map, all five failure
  modes including process-death-fails-waiters (Phase B)
- Client tools: `tools/call` + full MRTR elicitation loop, five terminal
  conditions, alias strip, requestState opaque echo (Phase C)
- Client resources: `resources/list` + `resources/read` (Phase D)
- Client prompts: `prompts/list` + `prompts/get` (Phase E)
- Client advanced: Roots/Sampling servicing, inbound `elicitation/create`
  routing via reader-thread third-case (Phase F)
- HttpTransport: synchronous `.post()`, bearer auth, capability suppression
  for HTTP (server-initiated paths are stdio-only in v0.1, TD-007) (Phase G)

**Server (Phases H–M)**
- McpServer foundation: transport-agnostic stateless dispatch,
  `server/discover` with capability gating, `tools/list` with relay
  enumeration (Phase H)
- Server tools: inbound `tools/call` → registry invoke, validate-before-invoke
  ordering, producer-side error table (Phase I)
- Server resources and prompts: handler-configured (no language-side registry)
  (Phases J, K)
- Server-issued elicitation/sampling: stateless re-call engine via sentinels,
  one engine / two sentinels, requestState encode/decode in one place (Phase L)
- Concrete transports: `StdioServerTransport`, `HttpServerTransport` with
  bearer auth (Phase M)

**CLI** (Phase N)
- `nodus-mcp serve --stdio | --http [--port N] [--bearer-token T]`
- `nodus-mcp connect <url>` (interactive REPL)

**Deprecated features included** (Decision 12)
- Roots (TD-001, functional ≥ 2027-07-28)
- Sampling (TD-002, functional ≥ 2027-07-28)

### Known limitations and documented contracts

- **OAuth not supported** (Decision 15): bearer-token auth only in v0.1.
  Servers requiring OAuth 2.0/OIDC cannot be used until v0.2.
- `resources/subscribe` not implemented (TD-006): server-push deferred.
- Server-initiated requests over HTTP not supported (TD-007): stdio-only.
- `_validate_args` enforces top-level type checking only (TD-008): deeper
  JSON Schema constraints are not enforced server-side.
- Resource read handler must raise `KeyError` for unknown-URI (TD-009).
- `requestState` travels to the client; never checkpoint secrets (TD-010).
- No relay loop detection (TD-004).

### Spec target

2026-07-28 RC (stateless; no session init handshake). `BYTECODE_VERSION` 4.

## [Unreleased]

<!-- Future entries -->
