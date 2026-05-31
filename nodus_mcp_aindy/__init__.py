"""nodus-mcp — Model Context Protocol framework for Nodus AI systems.

Expose Nodus tool registries as MCP servers; consume MCP servers as Nodus
tool sources.

Core types (no MCP dependency):
    ToolDefinition    — portable tool descriptor
    ToolRegistry      — thread-safe registry

Name conversion (no MCP dependency):
    syscall_to_mcp_name   — "sys.v1.memory.read" → "nodus_memory_read"
    mcp_to_syscall_name   — reverse
    is_nodus_tool         — predicate

Schema conversion (no MCP dependency):
    lightweight_to_json_schema  — AINDY schema → JSON Schema
    json_schema_to_lightweight  — JSON Schema → AINDY schema

MCP server (requires mcp):
    NodusServer           — expose ToolRegistry as MCP server

MCP client (requires mcp):
    MCPClientAdapter      — persistent client → list + call tools
    discover_tools()      — one-shot discovery

AINDY adapters (duck-typed, no AINDY import needed):
    syscall_entry_to_tool     — SyscallEntry-like → ToolDefinition
    agent_tool_to_definition  — TOOL_REGISTRY entry → ToolDefinition
    registry_to_tool_list     — full TOOL_REGISTRY → list[ToolDefinition]
"""
from .client import MCPClientAdapter, discover_tools
from .naming import is_nodus_tool, mcp_to_syscall_name, syscall_to_mcp_name
from .schema import json_schema_to_lightweight, lightweight_to_json_schema
from .server import NodusServer
from .tool import ToolDefinition, ToolRegistry
from .adapters.syscall import (
    agent_tool_to_definition,
    registry_to_tool_list,
    syscall_entry_to_tool,
)

__all__ = [
    # Core types
    "ToolDefinition",
    "ToolRegistry",
    # Naming
    "syscall_to_mcp_name",
    "mcp_to_syscall_name",
    "is_nodus_tool",
    # Schema
    "lightweight_to_json_schema",
    "json_schema_to_lightweight",
    # MCP server
    "NodusServer",
    # MCP client
    "MCPClientAdapter",
    "discover_tools",
    # Adapters
    "syscall_entry_to_tool",
    "agent_tool_to_definition",
    "registry_to_tool_list",
]
