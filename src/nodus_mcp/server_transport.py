"""Concrete MCP server transports (Phase M).

Wires H–L's transport-agnostic dispatch core to real I/O.
No new dispatch logic, invocation, or re-call engine — this file is pure I/O.

StdioServerTransport  — we are the spawned child; read our own stdin/stdout.
HttpServerTransport   — we bind a port; each POST is one stateless request.

Statelessness enforced at the transport boundary:
  - No per-connection/per-client state retained between requests.
  - HTTP server holds config only (host, port, bearer_token).
  - Each POST is handled independently by a fresh BaseHTTPRequestHandler instance.
  - HTTP frameworks default to session middleware; none is used here.
  - StdioServerTransport holds only I/O references; no request history.

Shutdown discipline:
  StdioServerTransport: parent-stdin-EOF = clean shutdown signal.
    No waiters to drain (L parks no threads); just stop reading + flush.
  HttpServerTransport:  close() calls httpd.shutdown() from any thread.
    serve_forever()/shutdown() thread-safety guaranteed by HTTPServer.

Bearer auth (Decision 15, server side, doc 3 C2):
  HttpServerTransport validates inbound Authorization: Bearer <token>.
  Missing or wrong token → HTTP 401. StdioTransport is trusted/local (no auth).
"""
from __future__ import annotations

import http.server
import json
import sys
import threading
from typing import Callable, Any

from .codec import McpCodec
from .transport import McpServerTransport


# ── M1: StdioServerTransport ─────────────────────────────────────────────────

class StdioServerTransport(McpServerTransport):
    """MCP server transport for spawned-child stdio mode.

    The parent process spawned us; our stdin is its request pipe, our stdout
    is our response pipe. This is the inversion of B's StdioTransport (which
    spawned a child and read from the child's stdout).

    serve() blocks until EOF on stdin (parent closed the pipe → clean shutdown).
    No elicitation waiters to drain on shutdown; L parks nothing server-side.
    """

    def __init__(self, *, stdin=None, stdout=None) -> None:
        self._stdin = stdin if stdin is not None else sys.stdin.buffer
        self._stdout = stdout if stdout is not None else sys.stdout.buffer
        self._codec = McpCodec()
        self._write_lock = threading.Lock()
        self._closed = False

    def serve(self, handler: Callable[[str, dict, Any], dict]) -> None:
        """Read newline-delimited JSON frames from stdin; dispatch; write responses.

        Blocks until EOF on stdin (doc 3 D3 inverted: parent-stdin-EOF = shutdown).
        Each request is handled synchronously in the calling thread.
        Malformed frames are skipped (logged, not fatal — parent may send more).
        """
        while not self._closed:
            try:
                line = self._stdin.readline()
            except OSError:
                break
            if not line:
                break  # EOF — parent closed stdin; clean shutdown
            line = line.strip()
            if not line:
                continue
            try:
                msg = self._codec.decode(line)
            except (ValueError, UnicodeDecodeError):
                # Malformed frame — skip; server continues accepting
                continue

            method = msg.get("method", "")
            params = msg.get("params") or {}
            req_id = msg.get("id")

            response = handler(method, params, req_id)
            self._write_response(response)

    def send_response(self, response: dict, request_id: Any) -> None:
        """Write a pre-built response dict (used for unsolicited / multi-step flows)."""
        self._write_response(response)

    def close(self) -> None:
        self._closed = True

    def _write_response(self, response: dict) -> None:
        raw = self._codec.encode_response(response)
        with self._write_lock:
            self._stdout.write(raw)
            self._stdout.flush()


# ── M2: HttpServerTransport ───────────────────────────────────────────────────

class HttpServerTransport(McpServerTransport):
    """MCP server transport for HTTP mode.

    Binds a TCP port; each inbound POST is one stateless request dispatched
    through H–L's transport-agnostic core. The POST handler IS the request
    cycle — no persistent connection, no reader thread, no session.

    Statelessness enforced: BaseHTTPRequestHandler is instantiated fresh per
    request; HttpServerTransport holds only config, not request history.

    Bearer auth: if bearer_token is set, every inbound POST must carry
    Authorization: Bearer <token>. Mismatch → HTTP 401. (Decision 15, doc 3 C2.)
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 0,          # 0 = OS assigns a free port
        *,
        bearer_token: str | None = None,
    ) -> None:
        self._host = host
        self._port = port        # 0 until serve() runs; then the actual port
        self._bearer_token = bearer_token
        self._codec = McpCodec()
        self._handler: Callable | None = None
        self._httpd: http.server.HTTPServer | None = None
        self._closed = False

    @property
    def port(self) -> int:
        """Actual bound port (available after serve() has started the server)."""
        if self._httpd is not None:
            return self._httpd.server_address[1]
        return self._port

    def serve(self, handler: Callable[[str, dict, Any], dict]) -> None:
        """Bind the port and serve requests until close() is called.

        Blocks in serve_forever(). Call close() from another thread to stop.
        L's re-call works over HTTP by construction: the tool's InputRequiredResult
        is the POST response, and the client's continuation arrives as a new POST
        carrying requestState — two discrete POSTs, no held connection.
        """
        self._handler = handler
        transport = self  # captured by inner class

        class _McpHandler(http.server.BaseHTTPRequestHandler):

            def do_POST(self) -> None:
                # M2: Bearer auth (Decision 15 server side, doc 3 C2)
                if transport._bearer_token:
                    auth = self.headers.get("Authorization", "")
                    expected = f"Bearer {transport._bearer_token}"
                    if auth != expected:
                        self._send_json(401, {"error": "Unauthorized"})
                        return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)

                try:
                    msg = transport._codec.decode(body)
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"error": "Malformed JSON-RPC request"})
                    return

                method = msg.get("method", "")
                params = msg.get("params") or {}
                req_id = msg.get("id")

                # Dispatch through H–L's transport-agnostic core
                response = transport._handler(method, params, req_id)

                self._send_json(200, response)

            def _send_json(self, status: int, body: dict) -> None:
                raw = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: Any) -> None:
                pass  # suppress server access logs in tests

        self._httpd = http.server.HTTPServer((self._host, self._port), _McpHandler)
        self._httpd.serve_forever()

    def send_response(self, response: dict, request_id: Any) -> None:
        """Not applicable for HTTP — responses are returned in-request."""
        pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._httpd is not None:
            self._httpd.shutdown()
