"""nodus-mcp CLI — thin shell over the library's transport and client APIs.

Commands:
  nodus-mcp serve --stdio            Run as a spawned-child MCP server on stdin/stdout
  nodus-mcp serve --http [--port N] [--bearer-token T]   Run as an HTTP MCP server
  nodus-mcp connect <url> [--bearer-token T]             Connect and enter REPL
  nodus-mcp version                  Print version and exit

Serve starts an empty McpServer (no tools registered). To expose tools, import
nodus_mcp in a Python script, register tools via runtime.tool_registry or
McpServer.set_*_handler(), then call transport.serve(server.dispatch) directly.
The CLI is the transport front door for default/zero-config usage.

The REPL understands: discover, list, call <tool> <json>, help, quit.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .server import McpServer
from .server_transport import StdioServerTransport, HttpServerTransport


def _cmd_serve(args: argparse.Namespace) -> None:
    server = McpServer()
    if args.stdio:
        print(f"[nodus-mcp] serving on stdio (v{__version__})", file=sys.stderr)
        t = StdioServerTransport()
        try:
            t.serve(server.dispatch)
        except KeyboardInterrupt:
            pass
    else:
        port = args.port or 8080
        bearer = args.bearer_token or None
        print(f"[nodus-mcp] serving on http://localhost:{port} (v{__version__})",
              file=sys.stderr)
        if bearer:
            print("[nodus-mcp] bearer auth enabled", file=sys.stderr)
        t = HttpServerTransport("localhost", port, bearer_token=bearer)
        try:
            t.serve(server.dispatch)
        except KeyboardInterrupt:
            t.close()


def _cmd_connect(args: argparse.Namespace) -> None:
    from .http import HttpTransport
    from .client import McpClient

    url = args.url
    bearer = args.bearer_token or None
    transport = HttpTransport(url, bearer_token=bearer)
    client = McpClient()

    print(f"Connecting to {url} …", file=sys.stderr)
    try:
        conn = client.connect(transport, alias="srv", url=url, bearer_token=bearer)
    except Exception as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Connected. Server: {conn.server_info.get('name', '?')} "
          f"{conn.server_info.get('version', '')}. "
          f"Tools: {len(conn.registered_tools)}. Type 'help' for commands.",
          file=sys.stderr)

    _run_repl(client, conn)
    conn.close()


def _run_repl(client: "McpClient", conn: "McpConnection") -> None:
    from .protocol.messages import METHOD_TOOLS_LIST, METHOD_SERVER_DISCOVER
    import readline  # noqa: F401 — enables history on supported platforms

    while True:
        try:
            line = input("mcp> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split(None, 2)
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            break
        elif cmd == "help":
            print("Commands:")
            print("  discover           — call server/discover")
            print("  list               — list tools")
            print("  call <tool> <json> — invoke a tool (json may be omitted for {})")
            print("  quit               — exit")
        elif cmd == "discover":
            resp = conn.transport.send_request(METHOD_SERVER_DISCOVER,
                                               {"_meta": {"capabilities": {}}})
            print(json.dumps(resp.get("result", resp), indent=2))
        elif cmd == "list":
            resp = conn.transport.send_request(METHOD_TOOLS_LIST, {})
            tools = resp.get("result", {}).get("tools", [])
            if not tools:
                print("(no tools)")
            else:
                for t in tools:
                    dep = " [deprecated]" if t.get("annotations", {}).get("deprecated") else ""
                    print(f"  {t['name']}{dep} — {t.get('description', '')}")
        elif cmd == "call":
            if len(parts) < 2:
                print("usage: call <tool> [<json>]")
                continue
            tool_name = parts[1]
            raw_args = parts[2] if len(parts) > 2 else "{}"
            try:
                call_args = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                print(f"Invalid JSON args: {exc}")
                continue
            # Look up the prefixed name in registered_tools
            prefixed = f"mcp.srv.{tool_name}"
            if prefixed not in conn.registered_tools:
                # Try the raw name in case user typed the full namespaced form
                if tool_name not in conn.registered_tools:
                    print(f"Tool '{tool_name}' not found. Use 'list' to see available tools.")
                    continue
                prefixed = tool_name
            try:
                from .client import _run_tools_call, _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ROUNDS, _CLIENT_META
                # Strip the mcp.srv. prefix for the wire call
                raw_name = prefixed.split(".", 2)[-1] if prefixed.startswith("mcp.") else prefixed
                result = _run_tools_call(raw_name, call_args, conn.transport,
                                         client._elicitation_handler, client._registry,
                                         _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ROUNDS,
                                         lambda: _CLIENT_META)
                print(json.dumps(result, indent=2))
            except Exception as exc:
                print(f"Error: {exc}")
        else:
            print(f"Unknown command: {cmd!r}. Type 'help'.")


def _cmd_version(_args: argparse.Namespace) -> None:
    print(f"nodus-mcp {__version__}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nodus-mcp",
        description="MCP (Model Context Protocol) library for Nodus — bidirectional client + server",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    p_serve = sub.add_parser("serve", help="Run an MCP server")
    transport_group = p_serve.add_mutually_exclusive_group(required=True)
    transport_group.add_argument("--stdio", action="store_true",
                                  help="Serve on stdin/stdout (spawned-child mode)")
    transport_group.add_argument("--http", action="store_true",
                                  help="Serve on HTTP")
    p_serve.add_argument("--port", type=int, default=8080,
                          help="HTTP port (default: 8080)")
    p_serve.add_argument("--bearer-token", metavar="TOKEN",
                          help="Require this bearer token on inbound requests")
    p_serve.set_defaults(func=_cmd_serve)

    # connect
    p_connect = sub.add_parser("connect", help="Connect to an MCP server (REPL)")
    p_connect.add_argument("url", help="Server URL, e.g. http://localhost:8080")
    p_connect.add_argument("--bearer-token", metavar="TOKEN",
                            help="Bearer token for authentication")
    p_connect.set_defaults(func=_cmd_connect)

    # version
    p_ver = sub.add_parser("version", help="Print version and exit")
    p_ver.set_defaults(func=_cmd_version)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
