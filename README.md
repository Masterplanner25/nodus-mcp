# nodus-mcp

Model Context Protocol (MCP) library for [Nodus](https://github.com/Masterplanner25/Nodus)
— bidirectional client and server, implementing the 2026-07-28 RC specification.

**Status: v0.1.0 — published on [PyPI](https://pypi.org/project/nodus-mcp/).**

---

## Authentication warning

**v0.1 does not support OAuth 2.0.** Only bearer-token authentication is implemented.
Servers that require OAuth 2.0 / OIDC authentication (including many production MCP
deployments) cannot be used with nodus-mcp v0.1. OAuth support is planned for v0.2.

To connect to a server with a bearer token:
```python
from nodus_mcp import HttpTransport, McpClient

client = McpClient()
transport = HttpTransport("https://server/mcp", bearer_token="sk-...")
conn = client.connect(transport, alias="srv")
```

---

## Installation

```bash
# Once nodus-lang 4.0.0 is on PyPI:
pip install nodus-mcp

# Development install (both repos checked out):
pip install -e . --no-deps
# Set PYTHONPATH to nodus-lang source:
# PYTHONPATH="path/to/nodus-lang/src" python ...
```

---

## Quick start

### Run an MCP server

```bash
# Spawned-child stdio mode (parent process connects to our stdin/stdout)
nodus-mcp serve --stdio

# HTTP mode
nodus-mcp serve --http --port 8080 --bearer-token my-api-key
```

To expose tools, use the Python API before serving:

```python
from nodus_mcp import McpServer, HttpServerTransport

server = McpServer()
server.set_resource_list_handler(lambda: [
    {"uri": "file:///data", "name": "Data Directory"}
])

transport = HttpServerTransport("localhost", 8080)
transport.serve(server.dispatch)
```

### Connect to a server

```bash
nodus-mcp connect http://localhost:8080 --bearer-token my-api-key
```

Interactive REPL:
```
Connected. Server: my-server 1.0. Tools: 3. Type 'help' for commands.
mcp> list
  my.tool — Does something useful
mcp> call my.tool {"x": 42}
{"content": [{"type": "text", "text": "done"}]}
mcp> quit
```

### Use from a Nodus script

```nodus
import "nodus-mcp"

// Discover and call tools (requires McpClient.connect() in host Python code)
let result = tool.invoke("mcp.srv.my_tool", {x: 42})
```

---

## Entry-point contract

```python
# nodus-mcp registers itself in the nodus.nd entry-point group:
# nodus-mcp = "nodus_mcp.nd:get_nd_root"
#
# After pip install nodus-mcp, any Nodus script can:
#   import "nodus-mcp"
# and get the adapter module without additional configuration.
# See docs/guide/library-entry-points.md in nodus-lang for the contract.
```

---

## Deprecated features (Roots + Sampling)

nodus-mcp v0.1 includes Roots and Sampling despite their deprecation in the
2026-07-28 RC, because existing MCP servers in the ecosystem use them (real
interop need) and the RC's 12-month deprecation window extends to at least
2027-07-28.

**Do not remove Roots or Sampling before:**
1. The protocol has actually removed them, AND
2. nodus-mcp has shipped a replacement

The removal gate is 2027-07-28 at the earliest. See `docs/governance/TECH_DEBT.md`
(TD-001, TD-002) and `docs/design/05-deprecated-features.md §C1`.

---

## Known limitations

| Limitation | Details |
|---|---|
| No OAuth support | Bearer token only; see auth warning above |
| `resources/subscribe` not implemented | Server-push deferred to v0.2 |
| Server-initiated requests (HTTP) | `roots/list`, `sampling/createMessage`, `elicitation/create` are stdio-only; HTTP has no push channel (TD-007) |
| Partial JSON Schema validation | `_validate_args` checks required fields and primitive types; `enum`, `pattern`, `minLength` etc. are not enforced server-side (TD-008) |
| Resource handler `KeyError` convention | Resource read handlers must raise `KeyError(uri)` for unknown URIs (TD-009) |
| `requestState` is on the wire | Server-issued elicitation state travels to the client; never checkpoint secrets (TD-010) |

---

## Spec target

[2026-07-28 RC](https://spec.modelcontextprotocol.io/) — stateless (no session
initialization handshake, no `Mcp-Session-Id`). Capabilities travel in `_meta`
per-request. `server/discover` replaces capability exchange.
