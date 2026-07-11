"""AINDY syscall / tool registry → ToolDefinition adapters.

All functions accept duck-typed objects to avoid importing from AINDY.
They work with the actual AINDY types (SyscallEntry, TOOL_REGISTRY dict)
as well as SimpleNamespace mocks in tests.
"""
from __future__ import annotations

from typing import Any, Callable

from ..naming import syscall_to_mcp_name
from ..schema import lightweight_to_json_schema
from ..tool import ToolDefinition


def syscall_entry_to_tool(
    syscall_name: str,
    entry: Any,
    *,
    handler: Callable[[dict[str, Any]], Any],
    name_override: str | None = None,
) -> ToolDefinition:
    """Convert a ``SyscallEntry``-like object to a ``ToolDefinition``.

    The handler must be a pre-bound closure that accepts ``(args: dict) → dict``.
    Callers are responsible for constructing a ``SyscallContext`` inside the
    closure — this adapter does not know about AINDY internals.

    Args:
        syscall_name:  Full syscall name (e.g. ``"sys.v1.memory.read"``).
        entry:         ``SyscallEntry``-like object with attributes:
                       ``description``, ``input_schema``, ``output_schema``,
                       ``capability``, ``stable``, ``deprecated``.
        handler:       Callable ``(args: dict) → dict``.
        name_override: Use this MCP name instead of the auto-derived one.

    Returns:
        A ``ToolDefinition`` ready to register in a ``ToolRegistry``.
    """
    mcp_name = name_override or syscall_to_mcp_name(syscall_name)
    raw_input = getattr(entry, "input_schema", None)
    raw_output = getattr(entry, "output_schema", None)
    return ToolDefinition(
        name=mcp_name,
        description=str(getattr(entry, "description", "") or ""),
        input_schema=lightweight_to_json_schema(raw_input),
        output_schema=lightweight_to_json_schema(raw_output) if raw_output else None,
        handler=handler,
        capability=getattr(entry, "capability", None),
        stable=bool(getattr(entry, "stable", True)),
        deprecated=bool(getattr(entry, "deprecated", False)),
        original_name=syscall_name,
        source="local",
    )


def agent_tool_to_definition(
    name: str,
    entry: dict[str, Any],
    *,
    handler: Callable[[dict[str, Any]], Any] | None = None,
) -> ToolDefinition:
    """Convert an AINDY agent tool registry entry to a ``ToolDefinition``.

    Args:
        name:    Tool name (key in AINDY's ``TOOL_REGISTRY``).
        entry:   The dict stored at ``TOOL_REGISTRY[name]``; expected keys:
                 ``fn``, ``description``, ``risk``, ``capability``,
                 ``required_capability``, ``category``, ``egress_scope``.
        handler: Override the handler callable.  Defaults to ``entry["fn"]``.

    Returns:
        A ``ToolDefinition``.  The ``input_schema`` will be an empty object
        schema because AINDY agent tool entries do not store input schemas
        (those live in the syscall layer).  Enrich with a custom
        ``input_schema`` after calling this function if needed.
    """
    fn = handler or entry.get("fn") or (lambda _: {})
    return ToolDefinition(
        name=name,
        description=str(entry.get("description") or ""),
        input_schema={"type": "object", "properties": {}},
        handler=fn,
        capability=entry.get("capability"),
        category=entry.get("category"),
        risk=str(entry.get("risk") or "high"),
        stable=True,
        deprecated=False,
        original_name=None,
        source="local",
    )


def registry_to_tool_list(
    tool_registry: dict[str, dict[str, Any]],
    *,
    syscall_registry: Any | None = None,
) -> list[ToolDefinition]:
    """Convert AINDY's ``TOOL_REGISTRY`` dict into a list of ``ToolDefinition``.

    When *syscall_registry* is provided (an object with ``get(name)`` returning
    a SyscallEntry-like), the input schema is enriched from the matching
    syscall entry.

    Args:
        tool_registry:   AINDY's ``TOOL_REGISTRY`` dict (``{name: entry_dict}``).
        syscall_registry: Optional syscall registry for schema enrichment.

    Returns:
        List of ``ToolDefinition`` objects in registry insertion order.
    """
    results: list[ToolDefinition] = []
    for name, entry in tool_registry.items():
        fn = entry.get("fn")
        if fn is None:
            continue
        tool = agent_tool_to_definition(name, entry, handler=fn)

        # Try to enrich input_schema from the syscall layer.
        if syscall_registry is not None:
            try:
                capability = entry.get("capability", "")
                # Common convention: tool "memory.recall" → syscall "sys.v1.memory.read"
                # There is no guaranteed mapping; skip silently if not found.
                syscall_entry = getattr(syscall_registry, "get", lambda x: None)(
                    f"sys.v1.{name.replace('.', '.')}"
                )
                if syscall_entry is not None:
                    raw = getattr(syscall_entry, "input_schema", None)
                    if raw:
                        tool = ToolDefinition(
                            **{
                                **tool.__dict__,
                                "input_schema": lightweight_to_json_schema(raw),
                            }
                        )
            except Exception:
                pass

        results.append(tool)
    return results
