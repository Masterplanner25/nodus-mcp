# nodus-mcp

Model Context Protocol (MCP) library for [Nodus](https://github.com/Masterplanner25/Nodus) — bidirectional client and server, implementing the [2026-07-28 RC specification](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/) including stateless transport (no session initialization), Multi Round-Trip Requests (SEP-2322) for server-side elicitation, and the extensions framework (SEP-2133).

**Status: pre-implementation.** The repo is scaffolded and the entry-point contract is wired; Phase 1 design docs are pending open-decision resolution. See `docs/design/00-decisions.md` for what is settled and what is not.

**Development install:** `pip install -e . --no-deps` (nodus-lang 4.0.0 is not yet on PyPI; set `PYTHONPATH` to the nodus-lang dev source). Once nodus-lang 4.0.0 ships, `pip install nodus-mcp` will be sufficient.
