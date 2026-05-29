"""StdioTransport — MCP client-role stdio transport (doc 3 B1, B3, D1, D3).

Concurrency model: one reader thread per connection reads stdout in a loop;
the pending map (dict[id, _Waiter]) routes responses back to blocked
send_request() callers. All pending-map access is under _pending_lock.

Failure modes handled:
  - Process death (EOF on stdout): reader's finally drains all pending waiters
    with TransportError so no send_request() can block forever (FM-1).
  - Malformed frame: treated as process death — fail the connection (FM-2).
  - Teardown race: close() atomically sets _closed + drains pending under lock,
    then teardowns elicitations, then kills the process (FM-3).
  - Reader outliving close(): join with 2-second timeout; force-kill backstop (FM-4).
  - Send after close: checked under lock before registering a waiter (FM-5).

Phase F adds the routing third-case in _dispatch:
  - Response (id present, method absent) → pending waiter [existing]
  - Notification (method present, id absent) → notification_handler [existing]
  - Inbound request (method + id both present) → _handle_inbound_request [F]
  The daemon-thread inbound handler avoids blocking the reader; _write_lock
  prevents concurrent write corruption without deadlock (reader holds no lock
  during handler execution or during the response write).
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field

from .codec import McpCodec
from .connection import ActiveElicitationRegistry
from .protocol.jsonrpc import next_request_id, INTERNAL_ERROR
from .transport import McpTransport, TransportError


@dataclass
class _Waiter:
    """A pending send_request() call waiting for its response."""
    result_box: list  # length-1; filled by reader thread before setting wake_event
    wake_event: threading.Event = field(default_factory=threading.Event)


_CLOSE_ERROR = TransportError("Transport closed")
_EOF_ERROR = TransportError("Stdio process exited unexpectedly")


class StdioTransport(McpTransport):
    """MCP client stdio transport.

    Spawns a subprocess and owns its stdin (write) / stdout (read via reader thread).
    Implements McpTransport so the adapter layer above is transport-agnostic.

    Construction: StdioTransport(argv) spawns the process and starts the reader.
    The first send_request() typically sends server/discover (Phase C's job).
    """

    def __init__(
        self,
        argv: list[str],
        *,
        elicitation_registry: ActiveElicitationRegistry | None = None,
    ) -> None:
        self._codec = McpCodec()
        self._elicitation_registry = elicitation_registry

        self._pending: dict[int | str, _Waiter] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._notification_handler = None  # optional hook for server notifications
        self._inbound_request_handler = None  # callable(method, params) → dict; Phase F

        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            raise TransportError(f"Failed to spawn stdio server: {exc}")

        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name="mcp-stdio-reader",
            daemon=True,
        )
        self._reader_thread.start()

    # ── McpTransport interface ────────────────────────────────────────────────

    def send_request(self, method: str, params: dict) -> dict:
        """Write a JSON-RPC request and block until the reader thread delivers
        the matching response. Raises TransportError on process death or close.

        Safe to call from multiple threads concurrently — each call gets its
        own _Waiter keyed by a unique request id.
        """
        req_id = next_request_id()
        result_box: list = [None]
        waiter = _Waiter(result_box=result_box)

        # Register under lock — checked atomically against _closed so we never
        # register a waiter after close() has already drained the pending map.
        with self._pending_lock:
            if self._closed:
                raise TransportError("Transport is closed")
            self._pending[req_id] = waiter

        try:
            raw = self._codec.encode_request(method, params, id=req_id)
            with self._write_lock:
                if self._proc.stdin is None or self._proc.stdin.closed:
                    raise TransportError("Stdin pipe is closed")
                self._proc.stdin.write(raw)
                self._proc.stdin.flush()
        except TransportError:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TransportError(f"Write failed: {exc}")

        # Block until the reader thread delivers a response (or dies / we close).
        # Process death unblocks via _read_loop's finally → _fail_all_pending.
        # close() unblocks via its explicit pending drain before termination.
        waiter.wake_event.wait()

        result = result_box[0]
        if isinstance(result, TransportError):
            raise result
        if isinstance(result, Exception):
            raise TransportError(str(result))
        # result is the decoded JSON-RPC response dict
        return result  # type: ignore[return-value]

    def send_notification(self, method: str, params: dict) -> None:
        """Write a JSON-RPC notification (no id, no response expected)."""
        with self._pending_lock:
            if self._closed:
                raise TransportError("Transport is closed")
        raw = self._codec.encode_notification(method, params)
        try:
            with self._write_lock:
                self._proc.stdin.write(raw)
                self._proc.stdin.flush()
        except OSError as exc:
            raise TransportError(f"Write failed: {exc}")

    def close(self) -> None:
        """Shut down the transport (doc 3 D3 teardown order).

        1. Atomically mark closed + collect pending waiters (prevents new
           registrations from racing with our drain).
        2. Teardown elicitation registry first (doc 3 D3 order: elicitations
           before transport close).
        3. Fail all in-flight send_request() waiters with TransportError.
        4. Terminate subprocess + close stdin (makes reader see EOF).
        5. Join reader thread with 2-second backstop, then force-kill.
        6. Reap exit code.
        """
        # Step 1 — atomic: mark closed + collect waiters
        with self._pending_lock:
            if self._closed:
                return
            self._closed = True
            waiters_to_fail = list(self._pending.values())
            self._pending.clear()

        # Step 2 — teardown elicitation handlers (doc 3 D3: first)
        if self._elicitation_registry is not None:
            self._elicitation_registry.teardown()

        # Step 3 — fail in-flight request waiters
        for waiter in waiters_to_fail:
            waiter.result_box[0] = _CLOSE_ERROR
            waiter.wake_event.set()

        # Step 4 — kill subprocess + close stdin so reader sees EOF
        try:
            self._proc.terminate()
        except OSError:
            pass
        try:
            self._proc.stdin.close()
        except OSError:
            pass

        # Step 5 — join reader thread; force-kill if it doesn't exit promptly
        self._reader_thread.join(timeout=2.0)
        if self._reader_thread.is_alive():
            try:
                self._proc.kill()
            except OSError:
                pass
            self._reader_thread.join(timeout=1.0)

        # Step 6 — reap exit code
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
            except OSError:
                pass

    # ── Internal: reader thread ───────────────────────────────────────────────

    def _read_loop(self) -> None:
        """Reader thread: reads stdout line-by-line, routes responses to waiters.

        Exits on EOF (normal process exit or after close()) or on a malformed
        frame (treat as process death — fail the connection).

        The finally block is the single guaranteed path to wake all blocked
        callers, whether exit is clean, abrupt, or from a decode error.
        """
        try:
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    break           # EOF: process closed stdout
                line = line.strip()
                if not line:
                    continue        # blank line: skip
                try:
                    msg = self._codec.decode(line)
                except (ValueError, UnicodeDecodeError):
                    # Malformed frame — fail the connection (not recoverable
                    # per-message; a misbehaving server cannot be trusted).
                    break
                self._dispatch(msg)
        finally:
            # Guarantee: every blocked send_request() caller is unblocked.
            # This runs whether we exited cleanly, on EOF, or on a decode error.
            # close() may have already drained pending (idempotent via clear()).
            with self._pending_lock:
                self._closed = True     # prevent new registrations
                waiters = list(self._pending.values())
                self._pending.clear()

            for waiter in waiters:
                waiter.result_box[0] = _EOF_ERROR
                waiter.wake_event.set()

            # Also tear down parked elicitation handlers if process died
            # spontaneously (without close() being called). Idempotent if
            # close() already called teardown().
            if self._elicitation_registry is not None:
                self._elicitation_registry.teardown()

    def _dispatch(self, msg: dict) -> None:
        """Route an inbound message to the correct handler.

        Three cases (Phase F adds the third):
          method absent, id present  → response to our request   (pending waiter)
          method present, id absent  → server notification        (notification_handler)
          method present, id present → server-initiated request   (_inbound_request_handler)
        """
        has_method = "method" in msg
        msg_id = msg.get("id")

        if not has_method:
            # Response to one of our send_request() calls
            if msg_id is not None:
                with self._pending_lock:
                    waiter = self._pending.pop(msg_id, None)
                if waiter is not None:
                    waiter.result_box[0] = msg
                    waiter.wake_event.set()
        elif msg_id is None:
            # Server-sent notification (method present, no id)
            handler = self._notification_handler
            if handler is not None:
                try:
                    handler(msg)
                except Exception:
                    pass
        else:
            # Inbound server-initiated request (method + id both present)
            self._handle_inbound_request(msg_id, msg["method"], msg.get("params") or {})

    def _handle_inbound_request(self, req_id: object, method: str, params: dict) -> None:
        """Dispatch an inbound server-initiated request.

        Spawns a daemon thread so the reader loop is not blocked while the
        handler runs (sampling LLM calls, elicitation waiting for a human).
        The daemon thread calls _send_raw_response when done.
        """
        handler = self._inbound_request_handler
        if handler is None:
            # No handler wired — nothing to respond with; send a minimal error
            # so the server knows we can't service this method.
            resp = self._codec.make_internal_error("No inbound request handler configured", req_id)
            self._send_raw_response(resp)
            return
        threading.Thread(
            target=self._execute_inbound,
            args=(req_id, method, params, handler),
            daemon=True,
        ).start()

    def _execute_inbound(self, req_id: object, method: str, params: dict, handler) -> None:
        """Run handler synchronously in a daemon thread, then write the response."""
        try:
            result = handler(method, params)
            response = self._codec.make_result_response(result, req_id)
        except Exception as exc:
            response = self._codec.make_error_response(INTERNAL_ERROR, str(exc), req_id)
        self._send_raw_response(response)

    def _send_raw_response(self, response: dict) -> None:
        """Write a pre-built response dict to stdin without registering a waiter.

        Used by the inbound-request path (F) to send responses back to the
        server. Thread-safe: acquires _write_lock. No deadlock risk because
        the reader thread never holds _write_lock.
        """
        if self._closed:
            return
        raw = self._codec.encode_response(response)
        try:
            with self._write_lock:
                self._proc.stdin.write(raw)
                self._proc.stdin.flush()
        except OSError:
            pass

    # ── Introspection (used in tests and diagnostics) ─────────────────────────

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def process(self) -> subprocess.Popen:
        return self._proc
