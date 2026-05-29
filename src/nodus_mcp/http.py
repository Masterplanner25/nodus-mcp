"""HttpTransport — MCP client-role HTTP transport (doc 3 B2, C2, D1, D3).

Stateless request/response: each send_request() is one .post() call.
No reader thread. No pending map. No inbound-request routing (TD-007).
Bearer token in Authorization header when configured (Decision 15 / doc 3 C2).

F's server-initiated paths (roots/list, sampling/createMessage, elicitation/create)
are NOT supported over HTTP in v0.1 — there is no persistent channel for the server
to push through. McpClient.connect() suppresses those capabilities in _meta for
HTTP connections even when handlers are configured (TD-007).

C's MRTR elicitation loop (InputRequiredResult in tools/call response) is fully
supported: each elicitation round is a separate .post() carrying requestState in the
body. C's _run_tools_call is transport-agnostic and runs unchanged over HTTP.
"""
from __future__ import annotations

import httpx

from .codec import McpCodec
from .connection import ActiveElicitationRegistry
from .protocol.jsonrpc import next_request_id
from .transport import McpTransport, TransportError


class HttpTransport(McpTransport):
    """MCP client HTTP transport.

    send_request() is a synchronous .post() that blocks until the server
    responds. The HTTP round-trip IS the wait — no waiter, no reader thread,
    no pending map. This is doc 3 B2's core divergence from StdioTransport.

    Construction: HttpTransport(url, bearer_token=...) — no server/discover
    at construction time (that is connect()'s responsibility, Phase C).
    """

    def __init__(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        elicitation_registry: ActiveElicitationRegistry | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._url = url
        self._bearer_token = bearer_token
        self._elicitation_registry = elicitation_registry
        self._codec = McpCodec()
        self._client = httpx.Client(timeout=timeout_s)
        self._closed = False

    # ── McpTransport interface ────────────────────────────────────────────────

    def send_request(self, method: str, params: dict) -> dict:
        """POST one JSON-RPC request and return the response dict.

        The .post() call IS the blocking wait — the HTTP client blocks until
        the server responds. No reader thread, no pending map entry, no
        wake_event. The response JSON-RPC id is echoed back but correlation
        is implicit in the single-POST round-trip (doc 3 B2).

        Raises TransportError on network failure, timeout, or non-200 status.
        """
        if self._closed:
            raise TransportError("Transport is closed")

        req_id = next_request_id()
        raw = self._codec.encode_request(method, params, id=req_id)
        headers = self._build_headers()

        try:
            resp = self._client.post(self._url, content=raw, headers=headers)
        except httpx.TimeoutException as exc:
            raise TransportError(f"HTTP request timed out: {exc}")
        except httpx.TransportError as exc:
            raise TransportError(f"HTTP connection error: {exc}")
        except httpx.HTTPError as exc:
            raise TransportError(f"HTTP error: {exc}")

        if resp.status_code != 200:
            raise TransportError(
                f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        try:
            return self._codec.decode(resp.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransportError(f"Malformed JSON in HTTP response: {exc}")

    def send_notification(self, method: str, params: dict) -> None:
        """POST a JSON-RPC notification (fire-and-forget; response ignored)."""
        if self._closed:
            return
        raw = self._codec.encode_notification(method, params)
        headers = self._build_headers()
        try:
            self._client.post(self._url, content=raw, headers=headers)
        except (httpx.HTTPError, httpx.TransportError):
            pass  # notifications are fire-and-forget

    def close(self) -> None:
        """Tear down the HTTP client (doc 3 D3).

        Calls elicitation_registry.teardown() first (interface symmetry with
        StdioTransport.close()). For HTTP, blocking is inside .post() rather
        than a wake_event, so teardown typically finds the registry empty.
        """
        if self._closed:
            return
        self._closed = True
        if self._elicitation_registry is not None:
            self._elicitation_registry.teardown()
        self._client.close()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    @property
    def is_closed(self) -> bool:
        return self._closed
