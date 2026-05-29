"""Standing-assertions inventory — Phase N consolidation.

Each test here documents which phase's standing assertion protects which
design decision. A future maintainer who sees one of these fail should read
the referenced decision or design doc before concluding the test is wrong.

This file does not test new behavior — it catalogs that the standing
assertions from each phase are present and names what each protects.
"""
import importlib
import inspect


def test_a_rc_purity_gate_exists():
    """Phase A: no session-init types (no initialize/Mcp-Session-Id).
    Protects: Decision 1 (target spec 2026-07-28 RC; stateless-first).
    Standing assertion: test_phase_a.py::test_no_session_init_method_constants
    """
    import tests.test_phase_a as m
    assert hasattr(m, "test_no_session_init_method_constants"), (
        "Phase A RC-purity gate missing — protects Decision 1 stateless model"
    )


def test_b_fm1_process_death_test_exists():
    """Phase B: process death → failed waiters (FM-1).
    Protects: reader thread must not create hung VM threads on process death.
    Standing assertion: test_phase_b.py::test_fm1_process_death_fails_pending_waiters
    """
    import tests.test_phase_b as m
    assert hasattr(m, "test_fm1_process_death_fails_pending_waiters")


def test_b_fm3_teardown_drains_both_populations():
    """Phase B: close() drains both elicitation registry AND pending waiters (FM-3).
    Protects: doc 3 D3 teardown order; doc 2 D1 teardown sentinel.
    Standing assertion: test_phase_b.py::test_fm3_close_wakes_elicitation_while_reader_running
    """
    import tests.test_phase_b as m
    assert hasattr(m, "test_fm3_close_wakes_elicitation_while_reader_running")


def test_c_five_terminals_exist():
    """Phase C: five distinct MRTR terminal conditions.
    Protects: doc 2's loop contract; C's five terminals → five distinct wire shapes.
    """
    import tests.test_phase_c as m
    for terminal in ["tc1", "tc2", "tc3", "tc4", "tc5"]:
        names = [n for n in dir(m) if f"_{terminal}_" in n.lower()]
        assert names, f"Phase C terminal {terminal} has no standing test"


def test_g_no_sse_client_assertion_exists():
    """Phase G: HttpTransport has no persistent reader / server-push path.
    Protects: doc 3 C3 (SSE excluded from v0.1); TD-006/TD-007.
    Standing assertion: test_phase_g.py::test_http_transport_no_reader_thread
    """
    import tests.test_phase_g as m
    assert hasattr(m, "test_http_transport_no_reader_thread")
    assert hasattr(m, "test_http_transport_no_inbound_request_handler")


def test_g_capability_suppression_exists():
    """Phase G: HTTP connections suppress roots/sampling capabilities (TD-007).
    Protects: TD-007 — server-initiated paths are stdio-only over HTTP.
    """
    import tests.test_phase_g as m
    assert hasattr(m, "test_http_capability_suppression_with_handlers")


def test_h_no_session_gate_exists():
    """Phase H: McpServer has no session object / per-connection state.
    Protects: Decision 1 / Decision 2 stateless discipline on the server side.
    Standing assertion: test_phase_h.py::test_h_no_session_object_in_module
    """
    import tests.test_phase_h as m
    assert hasattr(m, "test_h_no_session_object_in_module")


def test_h_initialize_not_routed_exists():
    """Phase H: 'initialize' method returns -32601 (server RC purity gate).
    Protects: server foundation is stateless-first; no session-init handshake.
    """
    import tests.test_phase_h as m
    assert hasattr(m, "test_h_initialize_method_not_routed")


def test_i_validate_before_invoke_ordering_exists():
    """Phase I: invoke() never called with invalid args (I3 ordering invariant).
    Protects: doc 1 D2 producer side; validate-vs-execute distinct wire shapes.
    """
    import tests.test_phase_i as m
    assert hasattr(m, "test_i3_validate_before_invoke_invoke_never_called")


def test_l_no_thread_parks_exists():
    """Phase L: server-issued elicitation uses re-call, not parked thread.
    Protects: doc 4 C1 re-call-not-block; the inversion of C's blocking model.
    Standing assertion: test_phase_l.py::test_l_no_thread_parks_during_sentinel_handling
    """
    import tests.test_phase_l as m
    assert hasattr(m, "test_l_no_thread_parks_during_sentinel_handling")


def test_l_one_engine_two_sentinels_exists():
    """Phase L: ElicitationRequest and SamplingRequest go through one engine.
    Protects: doc 5 B2 anti-drift; same dispatch loop, type switch, not parallel engines.
    Standing assertion: test_phase_l.py::test_l_elicitation_and_sampling_use_same_code_path
    """
    import tests.test_phase_l as m
    assert hasattr(m, "test_l_elicitation_and_sampling_use_same_code_path")


def test_m_no_sse_server_assertion_exists():
    """Phase M: HttpServerTransport has no SSE / server-push path.
    Protects: TD-006/TD-007 server side; v0.1 dropped SSE.
    """
    import tests.test_phase_m as m
    assert hasattr(m, "test_m2_no_sse_push_path")


def test_m_integration_l_recall_over_http_exists():
    """Phase M: L's re-call works over real HTTP POSTs (the keystone integration proof).
    Protects: response-folded design; validates that the mock and reality agree.
    """
    import tests.test_phase_m as m
    assert hasattr(m, "test_m_integration_l_recall_over_real_http")


def test_tech_debt_has_three_implicit_contract_notes():
    """N2: the three implicit-contract notes from the build are documented.
    TD-008 (validate scope), TD-009 (KeyError convention), TD-010 (requestState).
    """
    import pathlib
    tech_debt = pathlib.Path(__file__).parent.parent / "docs/governance/TECH_DEBT.md"
    content = tech_debt.read_text()
    assert "TD-008" in content, "TD-008 (validate scope) must be in TECH_DEBT.md"
    assert "TD-009" in content, "TD-009 (KeyError convention) must be in TECH_DEBT.md"
    assert "TD-010" in content, "TD-010 (requestState visibility) must be in TECH_DEBT.md"


def test_readme_has_oauth_warning():
    """N2: README contains the OAuth warning (Decision 15 deliverable)."""
    import pathlib
    readme = pathlib.Path(__file__).parent.parent / "README.md"
    content = readme.read_text()
    assert "OAuth" in content, "README must contain OAuth warning (Decision 15)"
    assert "bearer" in content.lower(), "README must mention bearer-token auth"


def test_version_is_not_dev():
    """N4: version is 0.1.0 (not dev0) — release-ready but not published."""
    from nodus_mcp import __version__
    assert __version__ == "0.1.0", (
        f"Expected version 0.1.0 for release prep; got {__version__!r}"
    )
    assert "dev" not in __version__, "dev version must be removed before release prep"


def test_cli_entry_point_declared():
    """N1: CLI entry point is declared in pyproject.toml."""
    import pathlib
    pyproject = pathlib.Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject.read_text()
    assert "nodus-mcp = " in content, "CLI entry point must be in pyproject.toml"
    assert "nodus_mcp.cli:main" in content, "CLI entry point must point to cli:main"
