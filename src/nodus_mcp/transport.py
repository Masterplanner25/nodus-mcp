"""McpTransport and McpServerTransport abstract base classes (doc 3 A1/A3).

Phase A defines the interfaces; implementations are:
  StdioTransport / StdioServerTransport — Phase B
  HttpTransport / HttpServerTransport   — Phase G / Phase M

The seam: everything above (adapter, codec) is transport-agnostic.
Everything below (byte sources) is transport-specific.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


class McpTransport(ABC):
    """Client-role transport interface (doc 3 A1).

    The adapter layer calls send_request() and send_notification() only.
    Byte-level I/O (stdin/stdout pipe, HTTP POST) is hidden below.
    """

    @abstractmethod
    def send_request(self, method: str, params: dict) -> dict:
        """Send one JSON-RPC request; block until response; return response dict.

        Raises TransportError on any unrecoverable I/O failure.
        """

    @abstractmethod
    def send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""

    @abstractmethod
    def close(self) -> None:
        """Shut down the transport cleanly (doc 3 D3).

        Callers must invoke runtime._teardown_active_elicitations() before
        calling close(), per the teardown sequence defined in doc 2 D1 / doc 3 D3.
        """


class McpServerTransport(ABC):
    """Server-role transport interface (doc 4 B3).

    The server adapter calls serve(handler); the transport reads inbound
    requests and dispatches each to handler synchronously.
    """

    @abstractmethod
    def serve(self, handler: Callable[[str, dict, object], dict | None]) -> None:
        """Accept and dispatch requests until close() is called.

        handler(method: str, params: dict, request_id: object) -> dict | None
          Called synchronously for each inbound request.
          Return value is sent as the JSON-RPC result.
          Raise to produce a JSON-RPC error response.
        """

    @abstractmethod
    def send_response(self, response: dict, request_id: object) -> None:
        """Write a pre-built JSON-RPC response dict back to the caller."""

    @abstractmethod
    def close(self) -> None:
        """Shut down the server transport."""


class TransportError(Exception):
    """Unrecoverable transport-layer error.

    Maps to JSON-RPC -32603 INTERNAL_ERROR at the adapter layer (doc 1 D-table,
    doc 3 D2). Raised by transport implementations; caught by the adapter and
    converted to a ToolErrorCategory.TRANSPORT_ERROR result.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
