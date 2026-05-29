"""JSON-RPC 2.0 core types and error code constants.

Named constants are the authoritative source for all error codes used in the
adapter layer (doc 1 D-table). Phases C, H, and I reference these constants,
never the integer literals.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 reserved error codes (doc 1 D-table)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601    # tool not found, method not supported
INVALID_PARAMS = -32602      # schema validation failure before tool runs
INTERNAL_ERROR = -32603      # transport error, unrecoverable server fault

# Thread-safe request ID counter. B's pending map keys on these integers.
_id_lock = threading.Lock()
_id_counter = 0


def next_request_id() -> int:
    """Return the next monotonically increasing request ID (thread-safe)."""
    global _id_counter
    with _id_lock:
        _id_counter += 1
        return _id_counter


@dataclass
class JsonRpcRequest:
    """An outbound or inbound JSON-RPC 2.0 request (expects a response)."""
    method: str
    params: dict
    id: int | str  # requests always carry an id; None is reserved for responses


@dataclass
class JsonRpcNotification:
    """A JSON-RPC 2.0 notification — no id, no response expected."""
    method: str
    params: dict


@dataclass
class JsonRpcError:
    """A JSON-RPC 2.0 error object embedded in a response."""
    code: int
    message: str
    data: Any = None


@dataclass
class JsonRpcResponse:
    """A JSON-RPC 2.0 response (result or error)."""
    id: int | str | None
    result: Any = None
    error: JsonRpcError | None = None

    def is_error(self) -> bool:
        return self.error is not None
