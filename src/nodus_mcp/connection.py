"""McpConnection handle and lifecycle/teardown contract (doc 1 A2, doc 2 D1, doc 3 D3).

McpConnection is the opaque handle returned by mcp.connect(). Nodus scripts
hold it but never inspect its fields. The adapter layer reads fields directly.

ActiveElicitationRegistry tracks in-flight client-side elicitation waits so
that run_source() teardown can signal them cleanly (doc 2 D1 teardown sentinel).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .transport import McpTransport

# Sentinel pushed into active-elicitation result boxes on teardown (doc 2 D1).
# Distinct object identity; never equal to any real response.
TEARDOWN_SENTINEL: object = object()


@dataclass
class McpConnection:
    """Opaque per-connection handle (doc 1 A2).

    Fields:
      alias             — namespace prefix for tools registered from this server
      url               — server URL (HTTP) or process spec (stdio)
      transport         — active McpTransport; Phase B/G provide implementations
      bearer_token      — HTTP auth token (None for stdio; stdio is trusted/local)
      server_info       — serverInfo block from server/discover
      server_capabilities — capabilities block from server/discover
      registered_tools  — full mcp.<alias>.<name> keys written to _python_registered_tools;
                          used by disconnect() to unregister cleanly (doc 1 A2)
    """
    alias: str
    url: str
    transport: McpTransport
    bearer_token: str | None
    server_info: dict
    server_capabilities: dict
    registered_tools: list[str] = field(default_factory=list)
    roots: list = field(default_factory=list)  # [{uri, name}] configured at connect time (doc 5 A2)
    stateless_http: bool = False  # True for HttpTransport; suppresses server-initiated caps (TD-007)

    def close(self) -> None:
        """Close the underlying transport (doc 3 D3).

        Callers are responsible for calling
        ActiveElicitationRegistry.teardown() before this method, per the
        teardown sequence: teardown elicitations first, then close transport.
        """
        self.transport.close()


class ActiveElicitationRegistry:
    """Tracks in-flight client-side elicitation waits (doc 2 D1).

    Registered on the runtime-equivalent object so run_source() teardown can
    reach all parked threading.Event instances and signal them with
    TEARDOWN_SENTINEL, preventing orphaned handler threads.

    Usage in a handler:
        result_box = [None]
        wake_event = threading.Event()
        token = registry.register(result_box, wake_event)
        try:
            fired = wake_event.wait(timeout=T)
            if not fired:
                return tool_error(ELICITATION_TIMEOUT, ...)
            if result_box[0] is TEARDOWN_SENTINEL:
                return tool_error(ELICITATION_ABORTED, ...)
            # result_box[0] is the real response
        finally:
            registry.unregister(token)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[int, tuple[list, threading.Event]] = {}
        self._next_token = 0

    def register(self, result_box: list, wake_event: threading.Event) -> int:
        """Register an active elicitation wait. Returns a token for unregister."""
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._active[token] = (result_box, wake_event)
        return token

    def unregister(self, token: int) -> None:
        """Remove a completed elicitation wait (called in finally block)."""
        with self._lock:
            self._active.pop(token, None)

    def teardown(self) -> None:
        """Signal all parked waits with TEARDOWN_SENTINEL (doc 2 D1).

        Called by run_source() teardown before transport.close(). After this
        call every parked handler thread will wake immediately and return
        ToolErrorCategory.ELICITATION_ABORTED to the Nodus script.
        """
        with self._lock:
            items = list(self._active.values())
        for result_box, wake_event in items:
            result_box[0] = TEARDOWN_SENTINEL
            wake_event.set()
