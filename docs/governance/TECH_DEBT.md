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
