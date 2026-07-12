# nodus-mcp Changelog

## [0.1.2] — 2026-07-12

### Fixed

- **`NodusServer.run_sse_app()` omitted the `/messages/` POST mount** (#7). The
  SSE transport is two-endpoint — clients open the GET event stream at `/sse`
  and POST messages back to `/messages/` — but only `/sse` was mounted, so a
  client's post-back 404'd and the session never initialised. Now mounts
  `sse.handle_post_message` at `/messages/`. Server-side SSE only; the client
  adapter was unaffected.
- **`auth_hook` received an empty context `{}`** (#8), so it could not
  distinguish callers or map a session to an identity. It now receives a
  best-effort per-call context built from the MCP SDK's `RequestContext`:
  `request_id`, `session` (the MCP `ServerSession`), client `_meta`, and — over
  SSE/HTTP — the transport `request` and its `headers` (e.g. the bearer token).
  Unblocks per-session identity mapping for multi-tenant MCP servers. `{}` is
  still returned when no request context is active (e.g. stdio pre-session).

## [0.1.1] — 2026-07-11

### Fixed

- **Packaging: `nodus_mcp_aindy` adapters were excluded from the built wheel**
  (#5). The AINDY adapter layer (`tool`, `naming`, `schema`, `adapters.syscall`,
  `client`, `server`) lived at the repo root, but `[tool.setuptools.packages.find]`
  only discovers packages under `where = ["src"]`, so `pip install nodus-mcp`
  shipped the MCP wire stack without the adapters. Moved `nodus_mcp_aindy/` under
  `src/` so it is discovered by the existing configuration and included in the
  wheel. Unblocks downstream consumers (e.g. `aindy-runtime`'s client-side MCP
  plugin) that depend on `nodus_mcp_aindy` as a pip dependency. No API changes.

## [0.1.0] — 2026-06-10


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
