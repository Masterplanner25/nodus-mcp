"""McpCodec — JSON-RPC framing shared by all transports (doc 3 A1).

Transport-agnostic: operates on bytes/dicts only, performs no I/O.
Both StdioTransport (Phase B) and HttpTransport (Phase G) use this codec.
"""
from __future__ import annotations

import json

from .protocol.jsonrpc import (
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    JsonRpcError,
    JsonRpcResponse,
    next_request_id,
)


class McpCodec:
    """Shared JSON-RPC 2.0 framing layer (doc 3 A1).

    encode_request / encode_notification produce newline-terminated UTF-8 bytes
    suitable for both stdio (newline-delimited) and HTTP (body).

    decode / parse_response accept either bytes or str.
    """

    def encode_request(
        self, method: str, params: dict, *, id: int | str | None = None
    ) -> bytes:
        if id is None:
            id = next_request_id()
        msg = {
            "jsonrpc": JSONRPC_VERSION,
            "id": id,
            "method": method,
            "params": params,
        }
        return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")

    def encode_notification(self, method: str, params: dict) -> bytes:
        msg = {
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
            "params": params,
        }
        return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")

    def decode(self, raw: bytes | str) -> dict:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def parse_response(self, raw: bytes | str) -> JsonRpcResponse:
        d = self.decode(raw)
        if "error" in d:
            err = d["error"]
            return JsonRpcResponse(
                id=d.get("id"),
                error=JsonRpcError(
                    code=err.get("code", INTERNAL_ERROR),
                    message=err.get("message", ""),
                    data=err.get("data"),
                ),
            )
        return JsonRpcResponse(id=d.get("id"), result=d.get("result"))

    def make_error_response(
        self,
        code: int,
        message: str,
        id: int | str | None,
        *,
        data: object = None,
    ) -> dict:
        error: dict = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": JSONRPC_VERSION, "id": id, "error": error}

    def make_result_response(self, result: object, id: int | str | None) -> dict:
        return {"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}

    # Convenience wrappers using named constants (doc 1 D-table)

    def make_method_not_found(self, id: int | str | None) -> dict:
        return self.make_error_response(METHOD_NOT_FOUND, "Method not found", id)

    def make_invalid_params(self, message: str, id: int | str | None) -> dict:
        return self.make_error_response(INVALID_PARAMS, message, id)

    def make_internal_error(self, message: str, id: int | str | None) -> dict:
        return self.make_error_response(INTERNAL_ERROR, message, id)

    def encode_response(self, response: dict) -> bytes:
        """Encode a pre-built response dict to newline-terminated UTF-8 bytes."""
        return (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
