"""Tests for adapters/syscall.py — duck-typed AINDY bridge."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nodus_mcp_aindy import (
    ToolDefinition,
    agent_tool_to_definition,
    registry_to_tool_list,
    syscall_entry_to_tool,
)
from nodus_mcp_aindy.naming import syscall_to_mcp_name


def _entry(**kwargs) -> SimpleNamespace:
    defaults = dict(
        description="Test syscall",
        input_schema={"properties": {"query": {"type": "string"}}},
        output_schema={"properties": {"nodes": {"type": "list"}}, "required": ["nodes"]},
        capability="memory.read",
        stable=True,
        deprecated=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── syscall_entry_to_tool ─────────────────────────────────────────────────────

def test_syscall_to_tool_name():
    e = _entry()
    t = syscall_entry_to_tool("sys.v1.memory.read", e, handler=lambda _: {})
    assert t.name == "nodus_memory_read"


def test_syscall_to_tool_name_override():
    e = _entry()
    t = syscall_entry_to_tool("sys.v1.memory.read", e, handler=lambda _: {}, name_override="custom_name")
    assert t.name == "custom_name"


def test_syscall_to_tool_description():
    e = _entry(description="Recall memory nodes")
    t = syscall_entry_to_tool("sys.v1.memory.read", e, handler=lambda _: {})
    assert t.description == "Recall memory nodes"


def test_syscall_to_tool_schema_converted():
    e = _entry(input_schema={"required": ["content"], "properties": {"content": {"type": "string"}, "limit": {"type": "int"}}})
    t = syscall_entry_to_tool("sys.v1.memory.write", e, handler=lambda _: {})
    assert t.input_schema["type"] == "object"
    assert t.input_schema["properties"]["limit"]["type"] == "integer"
    assert "content" in t.input_schema["required"]


def test_syscall_to_tool_capability():
    e = _entry(capability="memory.write")
    t = syscall_entry_to_tool("sys.v1.memory.write", e, handler=lambda _: {})
    assert t.capability == "memory.write"


def test_syscall_to_tool_stable_deprecated():
    e = _entry(stable=False, deprecated=True)
    t = syscall_entry_to_tool("sys.v1.memory.read", e, handler=lambda _: {})
    assert t.stable is False
    assert t.deprecated is True


def test_syscall_to_tool_original_name():
    e = _entry()
    t = syscall_entry_to_tool("sys.v1.flow.run", e, handler=lambda _: {})
    assert t.original_name == "sys.v1.flow.run"
    assert t.source == "local"


def test_syscall_to_tool_handler_called():
    results = []
    e = _entry()
    t = syscall_entry_to_tool("sys.v1.memory.read", e, handler=lambda a: results.append(a) or {"ok": True})
    t.handler({"query": "test"})
    assert results[0]["query"] == "test"


def test_syscall_to_tool_none_schemas():
    e = _entry(input_schema=None, output_schema=None)
    t = syscall_entry_to_tool("sys.v1.event.emit", e, handler=lambda _: {})
    assert t.input_schema == {"type": "object", "properties": {}}
    assert t.output_schema is None


# ── agent_tool_to_definition ──────────────────────────────────────────────────

def test_agent_tool_basic():
    entry = {
        "fn": lambda args: {"result": "ok"},
        "description": "Recall memory",
        "risk": "low",
        "capability": "tool:memory.recall",
        "required_capability": "read_memory",
        "category": "memory",
        "egress_scope": "internal",
    }
    t = agent_tool_to_definition("memory.recall", entry)
    assert t.name == "memory.recall"
    assert t.description == "Recall memory"
    assert t.risk == "low"
    assert t.capability == "tool:memory.recall"
    assert t.category == "memory"


def test_agent_tool_handler_from_entry():
    called = []
    entry = {"fn": lambda a: called.append(a) or {"x": 1}, "description": "T", "risk": "low"}
    t = agent_tool_to_definition("my_tool", entry)
    t.handler({"q": "test"})
    assert called[0]["q"] == "test"


def test_agent_tool_handler_override():
    override = lambda a: {"overridden": True}
    entry = {"fn": lambda _: {}, "description": "", "risk": "high"}
    t = agent_tool_to_definition("my_tool", entry, handler=override)
    assert t.handler({}) == {"overridden": True}


def test_agent_tool_empty_schema():
    entry = {"fn": lambda _: {}, "description": "", "risk": "low"}
    t = agent_tool_to_definition("x", entry)
    assert t.input_schema == {"type": "object", "properties": {}}


# ── registry_to_tool_list ─────────────────────────────────────────────────────

def test_registry_to_tool_list_basic():
    registry = {
        "memory.recall": {"fn": lambda _: {}, "description": "Recall", "risk": "low"},
        "memory.write":  {"fn": lambda _: {}, "description": "Write",  "risk": "medium"},
    }
    tools = registry_to_tool_list(registry)
    names = {t.name for t in tools}
    assert "memory.recall" in names
    assert "memory.write" in names


def test_registry_to_tool_list_skips_missing_fn():
    registry = {
        "good": {"fn": lambda _: {}, "description": "", "risk": "low"},
        "bad":  {"description": "no fn", "risk": "low"},  # no "fn" key
    }
    tools = registry_to_tool_list(registry)
    assert len(tools) == 1
    assert tools[0].name == "good"
