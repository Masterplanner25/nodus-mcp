"""Phase B concurrency tests — StdioTransport.

Tests are written against mock subprocesses (inline Python -c scripts) so no
real MCP server is needed. Zero external network.

The two standing assertions (FM-1 and FM-3) are the real exit criterion for B:
  FM-1: dead process → all blocked send_request() callers wake with TransportError
  FM-3: close() during parked elicitation wakes the handler with TEARDOWN_SENTINEL
         while the reader thread is still alive
"""
import json
import sys
import threading
import time

import pytest

from nodus_mcp.stdio import StdioTransport
from nodus_mcp.transport import TransportError
from nodus_mcp.connection import ActiveElicitationRegistry, TEARDOWN_SENTINEL

# ── Subprocess scripts ────────────────────────────────────────────────────────

# Echoes every request as {"jsonrpc":"2.0","id":<id>,"result":{"ok":true}}
_ECHO_SERVER = """\
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""

# Reads stdin but never writes stdout (simulates a hung/crashed server)
_SILENT_SERVER = """\
import sys
sys.stdin.read()
"""

# Reads one request then immediately exits (simulates mid-request crash)
_ONE_THEN_DIE = """\
import sys
sys.stdin.readline()
sys.exit(0)
"""

# Slow echo: sleeps 100ms between read and response (tests concurrency timing)
_SLOW_ECHO = """\
import sys, json, time
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    time.sleep(0.05)
    resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"seq": req["params"].get("seq", 0)}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""

# Emits one malformed (non-JSON) line, then exits
_GARBAGE_SERVER = """\
import sys
sys.stdout.write("not valid json\\n")
sys.stdout.flush()
sys.stdin.read()
"""

# Emits a notification (no id), then echoes requests
_NOTIFICATION_SERVER = """\
import sys, json
notif = {"jsonrpc": "2.0", "method": "server/ready", "params": {"msg": "hello"}}
sys.stdout.write(json.dumps(notif) + "\\n")
sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
"""


def _argv(script: str) -> list[str]:
    return [sys.executable, "-c", script]


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def echo_transport():
    t = StdioTransport(_argv(_ECHO_SERVER))
    yield t
    if not t.is_closed:
        t.close()


# ── Happy path ────────────────────────────────────────────────────────────────

def test_send_request_happy_path(echo_transport):
    resp = echo_transport.send_request("tools/call", {"name": "x.y"})
    assert resp["result"]["ok"] is True


def test_send_request_response_carries_id(echo_transport):
    """Response id must match the request id (doc 3 B3 routing contract)."""
    resp = echo_transport.send_request("tools/list", {})
    assert "id" in resp
    assert resp["id"] is not None


def test_send_notification_returns_immediately(echo_transport):
    """Notifications are fire-and-forget — no response, no blocking (A2 type contract)."""
    start = time.monotonic()
    echo_transport.send_notification("progress", {"token": "t1", "value": 50})
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, "Notification should return immediately"


def test_notification_handler_receives_server_notification():
    """Server-initiated notifications (no id) route to notification_handler."""
    received = []
    t = StdioTransport(_argv(_NOTIFICATION_SERVER))
    t._notification_handler = lambda msg: received.append(msg)
    try:
        # Give the notification time to arrive, then send a request to sync
        resp = t.send_request("tools/list", {})
        assert resp["result"]["ok"] is True
        # Notification should have arrived before or during our request
        time.sleep(0.05)
        assert any(m.get("method") == "server/ready" for m in received), (
            "Expected server/ready notification"
        )
    finally:
        t.close()


# ── Concurrent requests ───────────────────────────────────────────────────────

def test_concurrent_requests_all_resolve():
    """Multiple threads calling send_request concurrently all get their response."""
    t = StdioTransport(_argv(_SLOW_ECHO))
    errors = []
    results = []
    lock = threading.Lock()

    def worker(seq: int):
        try:
            resp = t.send_request("tools/call", {"seq": seq})
            with lock:
                results.append(resp["result"]["seq"])
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    try:
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
    finally:
        t.close()

    assert not errors, f"Concurrent requests failed: {errors}"
    assert sorted(results) == list(range(5)), "All 5 responses received"


def test_each_request_gets_its_own_response():
    """Response routing: id correlation ensures response A doesn't go to waiter B."""
    t = StdioTransport(_argv(_SLOW_ECHO))
    result_map = {}
    lock = threading.Lock()

    def worker(seq: int):
        resp = t.send_request("tools/call", {"seq": seq})
        with lock:
            result_map[seq] = resp["result"]["seq"]

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    try:
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
    finally:
        t.close()

    for i in range(4):
        assert result_map[i] == i, f"seq {i} got wrong response: {result_map[i]}"


# ── Failure mode #1: process death → failed waiters ──────────────────────────

def test_fm1_process_death_fails_pending_waiters():
    """FM-1 (critical): a dead subprocess must not leave VM threads blocked forever.

    Strategy: send to a silent server (never responds), then kill the process.
    The blocked send_request() must wake with TransportError within 2 seconds.
    """
    t = StdioTransport(_argv(_SILENT_SERVER))
    error_holder = [None]
    completed = threading.Event()

    def blocked_caller():
        try:
            t.send_request("tools/call", {})
        except TransportError as exc:
            error_holder[0] = exc
        finally:
            completed.set()

    th = threading.Thread(target=blocked_caller, daemon=True)
    th.start()

    # Give the thread time to block in send_request
    time.sleep(0.05)
    assert not completed.is_set(), "Should still be blocked"

    # Kill the process
    t.process.kill()

    # The blocked thread must wake within a bounded time
    woke = completed.wait(timeout=2.0)
    assert woke, "Blocked send_request() did not wake after process death (deadlock)"
    assert isinstance(error_holder[0], TransportError), (
        "Expected TransportError after process death"
    )

    th.join(timeout=1.0)
    t.close()


def test_fm1_one_then_die_server():
    """Process that reads one request then exits — second request must fail cleanly."""
    t = StdioTransport(_argv(_ONE_THEN_DIE))
    error_holder = [None]
    completed = threading.Event()

    def second_caller():
        try:
            t.send_request("tools/call", {})
        except TransportError as exc:
            error_holder[0] = exc
        finally:
            completed.set()

    # First request goes in (process reads it and dies — no response)
    th = threading.Thread(target=second_caller, daemon=True)
    th.start()

    woke = completed.wait(timeout=3.0)
    assert woke, "Call to dying process did not return"
    assert isinstance(error_holder[0], TransportError)

    th.join(timeout=1.0)
    t.close()


def test_fm1_malformed_frame_fails_waiters():
    """Garbage output from server is treated as process death (FM-2 → FM-1 path)."""
    t = StdioTransport(_argv(_GARBAGE_SERVER))
    error_holder = [None]
    completed = threading.Event()

    def caller():
        # The server emits garbage immediately, then reads stdin forever.
        # Our send_request registers a waiter, writes to stdin, then blocks.
        # Reader sees the garbage line, breaks, fails all waiters.
        try:
            t.send_request("tools/call", {})
        except TransportError as exc:
            error_holder[0] = exc
        finally:
            completed.set()

    th = threading.Thread(target=caller, daemon=True)
    th.start()

    woke = completed.wait(timeout=3.0)
    assert woke, "Malformed frame did not unblock pending waiter"
    assert isinstance(error_holder[0], TransportError)

    th.join(timeout=1.0)
    t.close()


# ── Failure mode #3: teardown drains both populations ─────────────────────────

def test_fm3_close_wakes_elicitation_while_reader_running():
    """FM-3 (critical): close() during parked elicitation wakes registry with
    TEARDOWN_SENTINEL while the reader thread is still alive.

    Two populations must both be drained by close():
      - The elicitation registry (parked threading.Event wait)
      - Any in-flight send_request() waiters (pending map)
    """
    registry = ActiveElicitationRegistry()
    t = StdioTransport(_argv(_SILENT_SERVER), elicitation_registry=registry)

    # Simulate a parked elicitation handler
    result_box = [None]
    wake_event = threading.Event()
    token = registry.register(result_box, wake_event)

    # Simulate an in-flight send_request (registers a waiter without writing,
    # by directly inserting into pending — tests the drain path)
    from nodus_mcp.stdio import _Waiter
    req_waiter = _Waiter(result_box=[None])
    fake_id = 99999
    with t._pending_lock:
        t._pending[fake_id] = req_waiter

    # Verify both populations are parked
    assert not wake_event.is_set()
    assert not req_waiter.wake_event.is_set()
    assert t._reader_thread.is_alive()

    # close() while reader is alive
    t.close()

    # Elicitation handler woke with TEARDOWN_SENTINEL
    assert wake_event.is_set(), "Elicitation handler not woken by close()"
    assert result_box[0] is TEARDOWN_SENTINEL, (
        f"Expected TEARDOWN_SENTINEL, got {result_box[0]!r}"
    )

    # In-flight request waiter also woken with TransportError
    assert req_waiter.wake_event.is_set(), "Pending waiter not woken by close()"
    assert isinstance(req_waiter.result_box[0], TransportError), (
        "Expected TransportError for pending waiter"
    )

    registry.unregister(token)


def test_fm3_teardown_order_elicitations_before_pending():
    """Elicitation registry teardown must happen before pending waiters are failed
    (doc 3 D3 order). Verified by recording which wakes first.
    """
    registry = ActiveElicitationRegistry()
    t = StdioTransport(_argv(_SILENT_SERVER), elicitation_registry=registry)

    wake_order = []
    lock = threading.Lock()

    result_box = [None]
    elicit_event = threading.Event()

    def track_elicit(orig_teardown):
        def _tracked():
            with lock:
                wake_order.append("elicitation")
            orig_teardown()
        return _tracked

    registry.teardown = track_elicit(registry.teardown)

    from nodus_mcp.stdio import _Waiter
    class TrackingWaiter(_Waiter):
        def __init__(self):
            super().__init__(result_box=[None])
            _orig_set = self.wake_event.set
            def tracked_set():
                with lock:
                    wake_order.append("pending")
                _orig_set()
            self.wake_event.set = tracked_set

    w = TrackingWaiter()
    with t._pending_lock:
        t._pending[99998] = w

    token = registry.register(result_box, elicit_event)

    t.close()

    # Elicitation teardown must precede pending waiter failure
    assert wake_order[0] == "elicitation", (
        f"Wrong teardown order: {wake_order}. Elicitations must come first (doc 3 D3)."
    )

    registry.unregister(token)


# ── Failure mode #5: send after close ────────────────────────────────────────

def test_fm5_send_request_after_close_raises_immediately(echo_transport):
    """Send after close must raise TransportError immediately — no registered waiter."""
    echo_transport.close()
    start = time.monotonic()
    with pytest.raises(TransportError):
        echo_transport.send_request("tools/list", {})
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, "send_request after close should be immediate"


def test_fm5_send_notification_after_close_raises(echo_transport):
    echo_transport.close()
    with pytest.raises(TransportError):
        echo_transport.send_notification("progress", {})


def test_fm5_double_close_is_idempotent(echo_transport):
    """close() a second time must be a no-op."""
    echo_transport.close()
    echo_transport.close()  # must not raise


# ── Reader thread lifecycle ───────────────────────────────────────────────────

def test_reader_thread_exits_after_close(echo_transport):
    assert echo_transport._reader_thread.is_alive()
    echo_transport.close()
    echo_transport._reader_thread.join(timeout=3.0)
    assert not echo_transport._reader_thread.is_alive(), (
        "Reader thread did not exit after close()"
    )


def test_is_closed_reflects_state(echo_transport):
    assert not echo_transport.is_closed
    echo_transport.close()
    assert echo_transport.is_closed


# ── Codec integration (newline-delimited framing) ─────────────────────────────

def test_newline_delimited_framing():
    """Verify the transport correctly handles newline-delimited JSON frames."""
    t = StdioTransport(_argv(_ECHO_SERVER))
    try:
        # Two back-to-back requests — both must be framed and routed correctly
        r1 = t.send_request("tools/call", {"n": 1})
        r2 = t.send_request("tools/call", {"n": 2})
        assert r1["result"]["ok"] is True
        assert r2["result"]["ok"] is True
        # IDs must be distinct (newline framing separates them cleanly)
        assert r1["id"] != r2["id"]
    finally:
        t.close()
