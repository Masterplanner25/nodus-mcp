"""MCP message types targeting the 2026-07-28 RC (Decision 1).

RC constraints enforced here:
- No initialize / initialized / Mcp-Session-Id types (Decision 1: stateless).
- Capabilities live in _meta per-request, not in a session (doc 1 C1).
- requestState is opaque bytes/str at this layer — never parsed (doc 1 A1, doc 2 B2).
- server/discover replaces session-init capability exchange (doc 1 C2).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ── RC method name constants ──────────────────────────────────────────────────

METHOD_TOOLS_CALL = "tools/call"
METHOD_TOOLS_LIST = "tools/list"
METHOD_SERVER_DISCOVER = "server/discover"
METHOD_ROOTS_LIST = "roots/list"
METHOD_SAMPLING_CREATE_MESSAGE = "sampling/createMessage"

# ── Result type discriminators ────────────────────────────────────────────────

RESULT_TYPE_SUCCESS = "success"
RESULT_TYPE_INPUT_REQUIRED = "input_required"    # MRTR elicitation (doc 2 B1)
RESULT_TYPE_SAMPLING_REQUIRED = "sampling_required"  # doc 5 B2
RESULT_TYPE_ROOTS_REQUIRED = "roots_required"        # doc 5 A2

# ── Error category enum — closed set (doc 1 D-table + doc 2 D1) ──────────────

class ToolErrorCategory(str, Enum):
    """Closed set of tool error categories.

    All phases reference this enum; no category string may be used outside it.
    Extends to str so values serialize directly in JSON payloads.

    JSON-RPC-level categories (tool never ran):
      NOT_FOUND, INVALID_PARAMS, TRANSPORT_ERROR

    Tool-result isError categories (tool ran, produced a failure):
      EXECUTION_FAILURE, ELICITATION_TIMEOUT, ELICITATION_UNSUPPORTED,
      ELICITATION_ROUNDS_EXCEEDED, ELICITATION_ABORTED (doc 2 D1 fix),
      ROOTS_UNSUPPORTED, SAMPLING_UNSUPPORTED
    """
    # JSON-RPC level (doc 1 D-table)
    NOT_FOUND = "not_found"
    INVALID_PARAMS = "invalid_params"
    TRANSPORT_ERROR = "transport_error"
    # isError categories (doc 1 D-table + doc 2 D1)
    EXECUTION_FAILURE = "execution_failure"
    ELICITATION_TIMEOUT = "elicitation_timeout"
    ELICITATION_UNSUPPORTED = "elicitation_unsupported"
    ELICITATION_ROUNDS_EXCEEDED = "elicitation_rounds_exceeded"
    ELICITATION_ABORTED = "elicitation_aborted"
    # Deprecated-feature errors (doc 5 C3)
    ROOTS_UNSUPPORTED = "roots_unsupported"
    SAMPLING_UNSUPPORTED = "sampling_unsupported"


# ── Per-request _meta (doc 1 C1) ──────────────────────────────────────────────

@dataclass
class RequestMeta:
    """The _meta field present on every RC request/response.

    No session cache: the adapter reads this fresh on each inbound call
    and attaches it to each outbound call (doc 1 C1, Decision 1).
    """
    capabilities: dict = field(default_factory=dict)
    client_info: dict | None = None
    progress_token: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> RequestMeta:
        if not d:
            return cls()
        return cls(
            capabilities=d.get("capabilities") or {},
            client_info=d.get("clientInfo"),
            progress_token=d.get("progressToken"),
        )

    def to_dict(self) -> dict:
        out: dict = {"capabilities": self.capabilities}
        if self.client_info is not None:
            out["clientInfo"] = self.client_info
        if self.progress_token is not None:
            out["progressToken"] = self.progress_token
        return out

    def has_capability(self, name: str) -> bool:
        return name in self.capabilities


# ── Tool wire types ───────────────────────────────────────────────────────────

@dataclass
class ToolDefinition:
    """A tool as it appears in tools/list (doc 1 B2, B3, doc 4 A1–A3).

    inputSchema always has type:object (doc 1 B2 rule applied on construction).
    Deprecated tools carry annotations.deprecated:true (doc 1 B3, doc 4 A2).
    """
    name: str
    description: str
    input_schema: dict
    deprecated: bool = False

    def __post_init__(self) -> None:
        if not self.input_schema or self.input_schema.get("type") != "object":
            self.input_schema = {"type": "object", **self.input_schema}

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.deprecated:
            d["annotations"] = {"deprecated": True}
        return d


@dataclass
class ToolContent:
    """A single content item in a tool result (text, image, etc.)."""
    type: str
    text: str | None = None
    data: Any = None

    def to_dict(self) -> dict:
        d: dict = {"type": self.type}
        if self.text is not None:
            d["text"] = self.text
        elif self.data is not None:
            d["data"] = self.data
        return d

    @classmethod
    def make_text(cls, message: str) -> ToolContent:
        return cls(type="text", text=message)


@dataclass
class ToolCallResult:
    """tools/call response body (doc 1 D-table).

    isError:true with a ToolErrorCategory payload for execution failures.
    isError:false (default) for successful results.
    """
    content: list[ToolContent]
    is_error: bool = False

    def to_dict(self) -> dict:
        d: dict = {"content": [c.to_dict() for c in self.content]}
        if self.is_error:
            d["isError"] = True
        return d

    @classmethod
    def error(cls, category: ToolErrorCategory, message: str) -> ToolCallResult:
        payload = json.dumps({"category": category.value, "message": message})
        return cls(content=[ToolContent.make_text(payload)], is_error=True)

    @classmethod
    def from_exception(cls, exc: Exception) -> ToolCallResult:
        return cls(content=[ToolContent.make_text(str(exc))], is_error=True)

    @classmethod
    def from_python_value(cls, value: Any) -> ToolCallResult:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, default=str)
        return cls(content=[ToolContent.make_text(text)])


# ── MRTR / stateless re-call result types ────────────────────────────────────

@dataclass
class InputRequiredResult:
    """MRTR elicitation response (doc 2 B2).

    requestState is an opaque base64 string round-tripped by the client.
    It is NEVER parsed at this layer (doc 1 A1).
    """
    input_requests: list[dict]
    request_state: str  # opaque base64; client echoes back unchanged

    def to_dict(self) -> dict:
        return {
            "resultType": RESULT_TYPE_INPUT_REQUIRED,
            "inputRequests": self.input_requests,
            "requestState": self.request_state,
        }


@dataclass
class SamplingRequiredResult:
    """Server-issued sampling request (doc 5 B2).

    Structurally identical to InputRequiredResult — doc 4 C1 re-call pattern
    with SamplingRequest sentinel instead of ElicitationRequest.
    """
    messages: list[dict]
    params: dict
    request_state: str  # opaque

    def to_dict(self) -> dict:
        return {
            "resultType": RESULT_TYPE_SAMPLING_REQUIRED,
            "messages": self.messages,
            "samplingParams": self.params,
            "requestState": self.request_state,
        }


@dataclass
class RootsRequiredResult:
    """Server-issued roots request (doc 5 A2).

    Doc 4 C1 re-call pattern with RootsRequest sentinel.
    """
    request_state: str  # opaque

    def to_dict(self) -> dict:
        return {
            "resultType": RESULT_TYPE_ROOTS_REQUIRED,
            "requestState": self.request_state,
        }


# ── Server-side re-call sentinels (doc 4 C2, doc 5 A2/B2) ───────────────────
# Python callable handlers return these; the server dispatcher detects them
# before the result reaches _to_runtime_value(). Not Nodus types; VM-invisible.

@dataclass
class ElicitationRequest:
    """Handler returns this to trigger server-side elicitation (doc 4 C2).

    state is the handler's checkpoint, serialized into requestState by the
    adapter. On round 2 the adapter injects __elicitation_state__ into args.
    """
    input_requests: list[dict]
    state: dict = field(default_factory=dict)


@dataclass
class SamplingRequest:
    """Handler returns this to trigger server-side sampling (doc 5 B2).

    Reuses doc 4 C1 re-call pattern; SamplingRequest is the sentinel type.
    """
    messages: list[dict]
    params: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)


@dataclass
class RootsRequest:
    """Handler returns this to request the calling client's roots (doc 5 A2).

    Reuses doc 4 C1 re-call pattern; RootsRequest is the sentinel type.
    """
    state: dict = field(default_factory=dict)
