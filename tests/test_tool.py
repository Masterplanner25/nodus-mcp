import threading

import pytest

from nodus_mcp_aindy import ToolDefinition, ToolRegistry


def _make_tool(name: str = "test_tool", **kwargs) -> ToolDefinition:
    defaults = dict(
        description="A test tool",
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: {"result": "ok"},
    )
    defaults.update(kwargs)
    return ToolDefinition(name=name, **defaults)


# ── ToolDefinition ────────────────────────────────────────────────────────────

def test_tool_definition_fields():
    t = _make_tool("my_tool")
    assert t.name == "my_tool"
    assert t.description == "A test tool"
    assert t.risk == "high"
    assert t.stable is True
    assert t.deprecated is False
    assert t.source == "local"


def test_tool_handler_callable():
    t = _make_tool(handler=lambda args: {"x": args.get("x", 0) + 1})
    assert t.handler({"x": 5}) == {"x": 6}


# ── ToolRegistry ──────────────────────────────────────────────────────────────

def test_register_and_get():
    reg = ToolRegistry()
    tool = _make_tool("my_tool")
    reg.register(tool)
    assert reg.get("my_tool") is tool


def test_get_unknown_returns_none():
    reg = ToolRegistry()
    assert reg.get("nonexistent") is None


def test_len():
    reg = ToolRegistry()
    assert len(reg) == 0
    reg.register(_make_tool("t1"))
    reg.register(_make_tool("t2"))
    assert len(reg) == 2


def test_register_overwrites():
    reg = ToolRegistry()
    t1 = _make_tool("x", description="v1")
    t2 = _make_tool("x", description="v2")
    reg.register(t1)
    reg.register(t2)
    assert reg.get("x").description == "v2"
    assert len(reg) == 1


def test_list_excludes_deprecated_by_default():
    reg = ToolRegistry()
    reg.register(_make_tool("active"))
    reg.register(_make_tool("old", deprecated=True))
    tools = reg.list()
    assert len(tools) == 1
    assert tools[0].name == "active"


def test_list_include_deprecated():
    reg = ToolRegistry()
    reg.register(_make_tool("active"))
    reg.register(_make_tool("old", deprecated=True))
    all_tools = reg.list(include_deprecated=True)
    assert len(all_tools) == 2


def test_names():
    reg = ToolRegistry()
    reg.register(_make_tool("a"))
    reg.register(_make_tool("b"))
    assert set(reg.names()) == {"a", "b"}


def test_names_includes_deprecated():
    reg = ToolRegistry()
    reg.register(_make_tool("x", deprecated=True))
    assert "x" in reg.names()


def test_contains():
    reg = ToolRegistry()
    reg.register(_make_tool("t"))
    assert "t" in reg
    assert "other" not in reg


def test_unregister():
    reg = ToolRegistry()
    reg.register(_make_tool("t"))
    assert reg.unregister("t") is True
    assert reg.get("t") is None
    assert reg.unregister("t") is False


def test_thread_safe_concurrent_register():
    reg = ToolRegistry()
    errors = []

    def worker(n):
        try:
            for i in range(10):
                reg.register(_make_tool(f"tool_{n}_{i}"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(reg) == 50  # 5 workers × 10 tools each
