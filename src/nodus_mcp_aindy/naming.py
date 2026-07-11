"""Syscall ↔ MCP tool name conversion.

Convention
----------
``sys.v1.memory.read``   → ``nodus_memory_read``
``sys.v2.memory.read``   → ``nodus_memory_read_v2``
``sys.v1.nodus.execute`` → ``nodus_script_execute``  (avoids ``nodus_nodus_``)
``sys.v1.flow.run``      → ``nodus_flow_run``

The ``nodus_`` prefix identifies tools that originate from a Nodus runtime
without colliding with MCP tools from other servers.
"""
from __future__ import annotations

import re

# Map domains that would produce awkward names to better alternatives.
_DOMAIN_OVERRIDES: dict[str, str] = {
    "nodus": "script",   # nodus_nodus_execute → nodus_script_execute
}

# Inverse: MCP domain suffix back to syscall domain.
_DOMAIN_REVERSE: dict[str, str] = {v: k for k, v in _DOMAIN_OVERRIDES.items()}

_NODUS_PREFIX = "nodus_"
_SYS_VERSION_RE = re.compile(r"^sys\.(v\d+)\.")


def syscall_to_mcp_name(syscall_name: str) -> str:
    """Convert a syscall name to an MCP tool name.

    Examples::

        syscall_to_mcp_name("sys.v1.memory.read")   == "nodus_memory_read"
        syscall_to_mcp_name("sys.v2.memory.read")   == "nodus_memory_read_v2"
        syscall_to_mcp_name("sys.v1.nodus.execute") == "nodus_script_execute"
        syscall_to_mcp_name("sys.v1.flow.run")      == "nodus_flow_run"

    Raises:
        ValueError: If *syscall_name* does not match the ``sys.vN.*`` pattern.
    """
    # Parse: "sys.v1.memory.read" → version="v1", action="memory.read"
    match = _SYS_VERSION_RE.match(syscall_name)
    if not match:
        raise ValueError(
            f"syscall_name must start with 'sys.vN.', got {syscall_name!r}"
        )
    version = match.group(1)
    action = syscall_name[match.end():]  # "memory.read"

    # Split action into domain + rest: "memory.read" → ("memory", "read")
    parts = action.split(".", 1)
    domain = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    # Apply domain override
    mapped_domain = _DOMAIN_OVERRIDES.get(domain, domain)

    # Build MCP name: nodus_<domain>_<rest>
    action_underscored = rest.replace(".", "_")
    if action_underscored:
        base = f"{_NODUS_PREFIX}{mapped_domain}_{action_underscored}"
    else:
        base = f"{_NODUS_PREFIX}{mapped_domain}"

    # Append version suffix for non-v1 versions
    if version != "v1":
        base = f"{base}_{version}"

    return base


def mcp_to_syscall_name(mcp_name: str, *, version: str = "v1") -> str:
    """Convert an MCP tool name back to a syscall name.

    Examples::

        mcp_to_syscall_name("nodus_memory_read")      == "sys.v1.memory.read"
        mcp_to_syscall_name("nodus_memory_read_v2")   == "sys.v2.memory.read"
        mcp_to_syscall_name("nodus_script_execute")   == "sys.v1.nodus.execute"

    Args:
        mcp_name: The MCP tool name (must start with ``"nodus_"``).
        version:  Default syscall version when the name has no ``_vN`` suffix.

    Raises:
        ValueError: If *mcp_name* does not start with ``"nodus_"``.
    """
    if not mcp_name.startswith(_NODUS_PREFIX):
        raise ValueError(
            f"MCP name must start with '{_NODUS_PREFIX}', got {mcp_name!r}"
        )
    body = mcp_name[len(_NODUS_PREFIX):]  # "memory_read" or "memory_read_v2"

    # Detect version suffix: "memory_read_v2" → version="v2", body="memory_read"
    ver_match = re.search(r"_(v\d+)$", body)
    if ver_match:
        version = ver_match.group(1)
        body = body[: ver_match.start()]

    # Split domain from rest: "memory_read" → ("memory", "read")
    parts = body.split("_", 1)
    domain = parts[0]
    rest = parts[1].replace("_", ".") if len(parts) > 1 else ""

    # Reverse domain override
    syscall_domain = _DOMAIN_REVERSE.get(domain, domain)

    if rest:
        return f"sys.{version}.{syscall_domain}.{rest}"
    return f"sys.{version}.{syscall_domain}"


def is_nodus_tool(mcp_name: str) -> bool:
    """Return True if *mcp_name* looks like a Nodus-generated MCP tool name."""
    return mcp_name.startswith(_NODUS_PREFIX)
