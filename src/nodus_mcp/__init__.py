"""nodus-mcp — MCP (Model Context Protocol) library for Nodus.

Phase A foundation exports: wire types, codec, transport ABCs, connection handle.
"""
__version__ = "0.1.0.dev0"

from .protocol import (
    # JSON-RPC error codes (doc 1 D-table — reference constants, not literals)
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    # JSON-RPC types
    JsonRpcRequest,
    JsonRpcNotification,
    JsonRpcError,
    JsonRpcResponse,
    next_request_id,
    # MCP method names
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    METHOD_SERVER_DISCOVER,
    METHOD_ROOTS_LIST,
    METHOD_SAMPLING_CREATE_MESSAGE,
    # Error category enum (closed set)
    ToolErrorCategory,
    # Wire message types
    RequestMeta,
    ToolDefinition,
    ToolContent,
    ToolCallResult,
    InputRequiredResult,
    SamplingRequiredResult,
    RootsRequiredResult,
    # Server-side re-call sentinels (doc 4 C2, doc 5 A2/B2)
    ElicitationRequest,
    SamplingRequest,
    RootsRequest,
    # Phase D — resource types
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    ResourceDescriptor,
    ResourceContent,
    # Phase E — prompt types
    METHOD_PROMPTS_LIST,
    METHOD_PROMPTS_GET,
    PromptArgument,
    PromptDescriptor,
    PromptMessageContent,
    PromptMessage,
)
from .codec import McpCodec
from .transport import McpTransport, McpServerTransport, TransportError
from .connection import McpConnection, ActiveElicitationRegistry, TEARDOWN_SENTINEL
from .stdio import StdioTransport
from .http import HttpTransport
from .client import McpClient
from .server import McpServer
from .server_transport import StdioServerTransport, HttpServerTransport

__all__ = [
    "__version__",
    # JSON-RPC
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "JsonRpcRequest",
    "JsonRpcNotification",
    "JsonRpcError",
    "JsonRpcResponse",
    "next_request_id",
    # MCP methods
    "METHOD_TOOLS_CALL",
    "METHOD_TOOLS_LIST",
    "METHOD_SERVER_DISCOVER",
    "METHOD_ROOTS_LIST",
    "METHOD_SAMPLING_CREATE_MESSAGE",
    # Error categories
    "ToolErrorCategory",
    # Wire types
    "RequestMeta",
    "ToolDefinition",
    "ToolContent",
    "ToolCallResult",
    "InputRequiredResult",
    "SamplingRequiredResult",
    "RootsRequiredResult",
    # Sentinels
    "ElicitationRequest",
    "SamplingRequest",
    "RootsRequest",
    # Codec + transport
    "McpCodec",
    "McpTransport",
    "McpServerTransport",
    "TransportError",
    # Connection
    "McpConnection",
    "ActiveElicitationRegistry",
    "TEARDOWN_SENTINEL",
    # Transports
    "StdioTransport",
    "HttpTransport",
    # Client + Server
    "McpClient",
    "McpServer",
    # Server transports (Phase M)
    "StdioServerTransport",
    "HttpServerTransport",
    # Resources (Phase D)
    "METHOD_RESOURCES_LIST",
    "METHOD_RESOURCES_READ",
    "ResourceDescriptor",
    "ResourceContent",
    # Prompts (Phase E)
    "METHOD_PROMPTS_LIST",
    "METHOD_PROMPTS_GET",
    "PromptArgument",
    "PromptDescriptor",
    "PromptMessageContent",
    "PromptMessage",
]
