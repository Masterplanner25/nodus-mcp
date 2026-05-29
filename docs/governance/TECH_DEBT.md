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
