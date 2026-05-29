# nodus-mcp Tech Debt

## TD-001: Roots (deprecated RC feature)

**Status:** In v0.1 intentionally (real interop need).  
**Decision:** Decision 12 — included for ≥12-month deprecation window.  
**Remove when:** Protocol removes Roots AND nodus-mcp has shipped an alternative.  
**No earlier than:** 2027-07-28.  
**Doc:** `docs/design/05-deprecated-features.md §C1`.

## TD-002: Sampling (deprecated RC feature)

**Status:** In v0.1 intentionally (real interop need).  
**Decision:** Decision 12 — included for ≥12-month deprecation window.  
**Remove when:** Protocol removes Sampling AND nodus-mcp has shipped an alternative.  
**No earlier than:** 2027-07-28.  
**Doc:** `docs/design/05-deprecated-features.md §C1`.

## TD-003: GAP-ELICIT-TIMEOUT-CUMULATIVE

**Status:** Open, v0.1 known-gap.  
**Description:** Cumulative wall-clock across multiple tool calls in one `run_source()` can exhaust the outer budget mid-elicitation. The outer `run_source()` timer fires, the VM is interrupted, but the elicitation thread wakes to a torn-down run. Mitigated by the teardown sentinel (handler returns `tool_error / elicitation_aborted`), but the timing of which elicitation gets cut is non-deterministic.  
**Doc:** `docs/design/02-elicitation.md §D1 GAP-ELICIT-TIMEOUT-CUMULATIVE`.

## TD-004: No relay loop detection

**Status:** Open, v0.1 known-gap.  
**Description:** When one `NodusRuntime` acts as both MCP client and server, client-discovered tools are re-exposed by the server (implicit relay). If two such runtimes discover each other, relay calls are transitive and can cycle. No loop detection in v0.1; cycles exhaust timeout.  
**Doc:** `docs/design/04-server-mode.md §D3`.

## TD-005: Server-side elicitation not supported for Nodus closure handlers

**Status:** v0.2 target.  
**Description:** `ElicitationRequest` sentinel only works for Python callable handlers. Nodus closure handlers cannot issue server-side elicitation in v0.1. Requires `tool.elicit()` builtin (new std:tool function, no new opcode).  
**Doc:** `docs/design/04-server-mode.md §C2`.

## TD-008: _validate_args enforces a subset of JSON Schema

**Status:** Known v0.1 limitation; documented.  
**Description:** Phase I's inlined `_validate_args` / `_check_arg_type` validates inbound
`tools/call` arguments at the top level only: required-field presence and primitive type
checking (string, integer, number, boolean, object, array). It does NOT enforce deeper
JSON Schema constraints: `minLength`, `maxLength`, `minimum`, `maximum`, `enum`, `pattern`,
`format`, `additionalProperties`, nested `$defs`, conditional (`if`/`then`/`else`), etc.
**Contract for tool authors:** Do not rely on the MCP server to enforce schema constraints
beyond required-fields and top-level types. A tool handler receiving an argument that
passes top-level type validation may still violate deeper schema constraints. Handlers
should validate narrow invariants themselves.  
**Code site:** `server.py::_validate_args`, `server.py::_check_arg_type`.

## TD-009: resource read handler must signal unknown-URI via KeyError

**Status:** Implicit protocol; documented.  
**Description:** Phase J maps `KeyError` raised by the `resource_read_handler` to HTTP -32601
MethodNotFound. This is the adapter's contract with host applications: an unknown URI is
communicated via `KeyError`, not `FileNotFoundError`, `ValueError`, or a custom exception.
Any other exception maps to a generic -32603 InternalError, which the caller cannot
distinguish from a programming error.
**Contract for resource handler authors:** Signal "URI not found" by raising `KeyError(uri)`.
Use other exceptions for genuine errors (read failure, permission denied) — those will
surface as -32603.  
**Code site:** `server.py::_handle_resources_read`.

## TD-010: requestState is visible to the MCP client; never checkpoint secrets

**Status:** By-design constraint; documented.  
**Description:** Phase L's server-issued re-call pattern encodes tool continuation state
into `requestState` and sends it to the calling MCP client in the `InputRequiredResult`
response. The client echoes it back unchanged. The blob is opaque to Nodus scripts (below
the VM boundary) but is NOT private — it travels across the wire to the client and back,
and a hostile client could inspect or modify it.
**Contract for tool authors returning ElicitationRequest/SamplingRequest:** The `state` dict
placed in the sentinel (and encoded into `requestState`) must not contain secrets, tokens,
or credentials. Keep it minimal — tool checkpoint only. The client holds this state between
rounds.  
**Code site:** `server.py::_encode_request_state`.

## TD-007: Server-initiated requests over HTTP deferred to v0.2

**Status:** Deferred to v0.2, known v0.1 asymmetry.  
**Description:** F's server-initiated request paths (roots/list, sampling/createMessage,
elicitation/create) require a persistent channel the server can write into. HTTP in v0.1
is stateless request/response (.post() only); there is no open channel for the server to
push a request to the client. Therefore: (a) roots/sampling capabilities are suppressed
in HTTP clients' outbound _meta even when handlers are configured, (b) inbound request
routing (_inbound_request_handler) is not wired on HttpTransport. Handlers configured on
a McpClient remain functional for stdio connections — the limitation is transport-level,
not config-level. v0.2 path: SSE or long-poll transport enabling bidirectional HTTP.  
**Doc:** `docs/design/03-transports.md §C3` (no SSE in v0.1).

## TD-006: resources/subscribe not implemented

**Status:** Deferred to v0.2.  
**Description:** `resources/subscribe` (server-push resource update notifications) requires a held-stream model. v0.1 dropped SSE (doc 3 C3); server-push notifications are not supported. The subscription flow needs either a persistent notification channel or an SSE stream, neither of which exists in v0.1's stateless HTTP or stdio model (which only reads responses to explicit requests).  
**Doc:** `docs/design/03-transports.md §C3`.
