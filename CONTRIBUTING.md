# Contributing to nodus-mcp

## Setup

```bash
git clone https://github.com/Masterplanner25/nodus-mcp.git
cd nodus-mcp
pip install -e ".[dev]"
```

Tests require `nodus-lang` source:

```bash
PYTHONPATH="src:C:/dev/Coding Language/src" pytest tests/ -q
```

## Running tests

```bash
# Full suite (note: 2 HTTP transport tests may fail due to port conflicts
# when run together — they pass individually)
PYTHONPATH="src:C:/dev/Coding Language/src" pytest tests/ -q

# Stable subset (excludes known-flaky transport tests)
PYTHONPATH="src:C:/dev/Coding Language/src" pytest tests/ -q \
  --ignore=tests/test_phase_m.py
```

## Test structure

Tests are organised by implementation phase (A–N). Each phase file is
self-contained:

| File | Phase | Coverage |
|---|---|---|
| `test_phase_a.py` | A | Foundation: JSON-RPC, lifecycle |
| `test_phase_b.py` | B | Stdio transport |
| `test_phase_c.py` | C | Client tools |
| `test_phase_de.py` | D–E | Client resources + prompts |
| `test_phase_f.py` | F | Client advanced (sampling, elicitation) |
| `test_phase_g.py` | G | HTTP transports |
| `test_phase_h.py` | H | Server foundation |
| `test_phase_i.py` | I | Server tools |
| `test_phase_jk.py` | J–K | Server resources + prompts |
| `test_phase_l.py` | L | Server advanced |
| `test_phase_m.py` | M | Server transports (has port-conflict sensitivity) |
| `test_invariants.py` | — | Standing architectural assertions |

## Code style

- Python 3.10+ (not 3.11+)
- MCP spec version: 2026-07-28 RC
- `BYTECODE_VERSION` stays at 4 — no new nodus-lang opcodes
- Bearer token auth only in v0.1 — OAuth 2.0 is v0.2

## Submitting changes

1. Fork the repo and create a branch from `main`
2. Add tests for any new behaviour
3. Ensure `pytest tests/ -q` passes
4. Open a pull request with a description of what changes and why
