import pytest
from nodus_mcp_aindy.naming import is_nodus_tool, mcp_to_syscall_name, syscall_to_mcp_name


# ── syscall_to_mcp_name ───────────────────────────────────────────────────────

def test_v1_basic():
    assert syscall_to_mcp_name("sys.v1.memory.read") == "nodus_memory_read"


def test_v1_write():
    assert syscall_to_mcp_name("sys.v1.memory.write") == "nodus_memory_write"


def test_v1_multi_action():
    assert syscall_to_mcp_name("sys.v1.flow.execute_intent") == "nodus_flow_execute_intent"


def test_v2_suffix():
    assert syscall_to_mcp_name("sys.v2.memory.read") == "nodus_memory_read_v2"


def test_domain_override_nodus():
    # "nodus" domain → "script" to avoid "nodus_nodus_"
    assert syscall_to_mcp_name("sys.v1.nodus.execute") == "nodus_script_execute"


def test_single_action_flow_run():
    assert syscall_to_mcp_name("sys.v1.flow.run") == "nodus_flow_run"


def test_event_emit():
    assert syscall_to_mcp_name("sys.v1.event.emit") == "nodus_event_emit"


def test_agent_execute():
    assert syscall_to_mcp_name("sys.v1.agent.execute") == "nodus_agent_execute"


def test_agent_list_recent_runs():
    assert syscall_to_mcp_name("sys.v1.agent.list_recent_runs") == "nodus_agent_list_recent_runs"


def test_job_submit():
    assert syscall_to_mcp_name("sys.v1.job.submit") == "nodus_job_submit"


def test_invalid_raises():
    with pytest.raises(ValueError):
        syscall_to_mcp_name("memory.read")

    with pytest.raises(ValueError):
        syscall_to_mcp_name("sys.memory.read")


# ── mcp_to_syscall_name ───────────────────────────────────────────────────────

def test_reverse_basic():
    assert mcp_to_syscall_name("nodus_memory_read") == "sys.v1.memory.read"


def test_reverse_v2():
    assert mcp_to_syscall_name("nodus_memory_read_v2") == "sys.v2.memory.read"


def test_reverse_domain_override():
    assert mcp_to_syscall_name("nodus_script_execute") == "sys.v1.nodus.execute"


def test_reverse_invalid_raises():
    with pytest.raises(ValueError):
        mcp_to_syscall_name("memory_read")


# ── Round-trip ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("syscall", [
    "sys.v1.memory.read",
    "sys.v1.memory.write",
    "sys.v1.memory.search",
    "sys.v1.flow.run",
    "sys.v1.event.emit",
    "sys.v1.job.submit",
    "sys.v1.agent.execute",
    "sys.v2.memory.read",
    "sys.v1.nodus.execute",
])
def test_round_trip(syscall):
    mcp_name = syscall_to_mcp_name(syscall)
    restored = mcp_to_syscall_name(mcp_name)
    assert restored == syscall, f"{syscall} → {mcp_name} → {restored}"


# ── is_nodus_tool ─────────────────────────────────────────────────────────────

def test_is_nodus_tool_true():
    assert is_nodus_tool("nodus_memory_read") is True


def test_is_nodus_tool_false():
    assert is_nodus_tool("some_other_tool") is False
    assert is_nodus_tool("memory_read") is False
