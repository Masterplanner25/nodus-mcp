"""ToolDefinition and ToolRegistry — the portable tool contract for nodus-mcp."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolDefinition:
    """Minimal portable tool descriptor.

    Used by ``NodusServer`` (expose as MCP) and ``MCPClientAdapter`` (ingest
    from MCP).  Framework-agnostic — no dependency on AINDY, MCP, or FastAPI.

    Attributes
    ----------
    name:          MCP tool name (e.g. ``"nodus_memory_read"``).
    description:   Human-readable description shown to AI clients.
    input_schema:  Full JSON Schema: ``{"type": "object", "properties": {...}}``.
    handler:       Callable ``(args: dict) → dict``.  The return value is
                   serialised to a string for MCP clients.
    output_schema: Optional JSON Schema for the return value.
    capability:    AINDY capability tag (e.g. ``"memory.read"``).
    category:      Grouping hint (e.g. ``"memory"``, ``"flow"``).
    risk:          Risk level: ``"low"`` | ``"medium"`` | ``"high"``.
    stable:        Whether the tool's ABI is considered stable.
    deprecated:    When True, clients are advised to migrate away.
    source:        Origin: ``"local"`` or ``"mcp://<server_url>"``.
    original_name: Syscall name before conversion (e.g. ``"sys.v1.memory.read"``).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    output_schema: dict[str, Any] | None = None
    capability: str | None = None
    category: str | None = None
    risk: str = "high"
    stable: bool = True
    deprecated: bool = False
    source: str = "local"
    original_name: str | None = None


class ToolRegistry:
    """Thread-safe registry of ``ToolDefinition`` objects.

    Does not depend on AINDY, MCP, or any web framework.

    Usage::

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="nodus_memory_read",
            description="Recall memory nodes",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            handler=lambda args: {"nodes": [], "count": 0},
        ))
        tool = registry.get("nodus_memory_read")
        assert len(registry) == 1
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._lock = threading.Lock()

    def register(self, tool: ToolDefinition) -> None:
        """Register *tool*.  Overwrites an existing entry with the same name."""
        with self._lock:
            self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        """Return the ``ToolDefinition`` for *name*, or None if not found."""
        with self._lock:
            return self._tools.get(name)

    def list(self, *, include_deprecated: bool = False) -> list[ToolDefinition]:
        """Return all registered tools.

        Args:
            include_deprecated: When False (default), deprecated tools are excluded.
        """
        with self._lock:
            tools = list(self._tools.values())
        if not include_deprecated:
            tools = [t for t in tools if not t.deprecated]
        return tools

    def names(self) -> list[str]:
        """Return the names of all registered tools (including deprecated)."""
        with self._lock:
            return list(self._tools.keys())

    def unregister(self, name: str) -> bool:
        """Remove a tool by name.  Returns True if it existed."""
        with self._lock:
            return self._tools.pop(name, None) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._tools
