"""The streamable HTTP transport, driven end to end through the SDK client.

`NodusServer.streamable_http_app()` is served under uvicorn on a free port and
spoken to with the SDK's own `streamable_http_client` + `ClientSession`, through
a real `initialize`, `tools/list` and `tools/call` -- the same shape as
`test_sse_end_to_end.py`, and for the same reason: an app whose routes look
right can still fail every request. nodus-mcp-server's `--http` mode did, twice,
the second time because the session manager was never entered.

Under either SDK major. The server has one implementation and these tests hold
both branches to it; `test_which_branch_ran_is_visible` names the one a run
exercised.
"""

import asyncio
import contextvars
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from nodus_mcp_aindy import NodusServer, ToolDefinition, ToolRegistry
from nodus_mcp_aindy.server import _MCP_AVAILABLE, _SDK_V2

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")
pytestmark = pytest.mark.skipif(not _MCP_AVAILABLE, reason="mcp SDK not installed")

if _MCP_AVAILABLE:
    from mcp import ClientSession

    try:
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:  # mcp 1.x before the rename
        streamable_http_client = None
        from mcp.client.streamable_http import streamablehttp_client

# mcp 2.x's client is built on `httpx2`; 1.x on `httpx`.
try:
    import httpx2 as _httpx
except ImportError:
    import httpx as _httpx


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="echo",
        description="Echo the query back",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        handler=lambda args: {"echoed": args["q"]},
    ))
    reg.register(ToolDefinition(
        name="fail",
        description="Always raises",
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: (_ for _ in ()).throw(RuntimeError("boom")),
    ))
    return reg


class _Served:
    """An ASGI app under uvicorn on a daemon thread, for one test."""

    def __init__(self, app):
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/nothing", timeout=1)
                return self
            except urllib.error.HTTPError:
                return self  # 404 (or a 401 from host middleware): the app is answering
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("uvicorn did not come up")

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(10)

    def url(self, path: str = "/mcp") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", None))


def _schema(tool) -> dict:
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}


async def _with_session(url: str, headers: dict, fn):
    if streamable_http_client is None:
        async with streamablehttp_client(url, headers=headers) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await fn(session)
    async with _httpx.AsyncClient(headers=headers, timeout=30) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await fn(session)


async def _list_and_call(session):
    tools = await session.list_tools()
    ok = await session.call_tool("echo", {"q": "hi"})
    bad = await session.call_tool("fail", {})
    return tools.tools, ok, bad


@pytest.mark.parametrize("stateless", [True, False], ids=["stateless", "stateful"])
def test_discover_and_call_over_streamable_http(stateless):
    seen = []
    server = NodusServer(_registry(), name="e2e", auth_hook=lambda n, a, m: seen.append(m))

    with _Served(server.streamable_http_app(stateless=stateless)) as served:
        tools, ok, bad = asyncio.run(
            _with_session(served.url(), {"X-Probe": "from-the-client"}, _list_and_call)
        )

    names = {t.name for t in tools}
    assert {"echo", "fail"} <= names, names
    echo = next(t for t in tools if t.name == "echo")
    assert _schema(echo).get("required") == ["q"], _schema(echo)

    assert not _is_error(ok) and json.loads(_text(ok)) == {"echoed": "hi"}, ok
    # A handler exception reaches the client as an error result, not a dropped stream.
    assert _is_error(bad) and "boom" in _text(bad), bad

    # A2: the hook gets the same context it gets over SSE, headers included.
    assert seen, "auth_hook never ran"
    assert seen[0].get("session") is not None and "request_id" in seen[0], seen[:1]
    assert seen[0].get("headers", {}).get("x-probe") == "from-the-client", seen[0].get("headers")
    # No host middleware ran, so nothing claims an identity.
    assert "user" not in seen[0] and "auth" not in seen[0], seen[0]


def test_host_routes_and_middleware_share_the_app():
    """The runtime's shape: its OAuth routes beside /mcp, its bearer check
    wrapping both, a 401 on /mcp before any MCP message is read, and what the
    middleware authenticated reaching the hook as context["user"/"auth"]."""
    from starlette.authentication import (
        AuthCredentials,
        AuthenticationBackend,
        AuthenticationError,
        SimpleUser,
    )
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.requests import HTTPConnection
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    class Bearer(AuthenticationBackend):
        async def authenticate(self, conn: HTTPConnection):
            header = conn.headers.get("authorization", "")
            if conn.url.path != "/mcp":
                return None
            if header != "Bearer good-token":
                raise AuthenticationError("missing or bad bearer token")
            return AuthCredentials(["mcp:tools"]), SimpleUser("alice")

    def on_error(conn, exc):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer resource_metadata="/.well-known/oauth-protected-resource"'},
        )

    async def metadata(request):
        return JSONResponse({"resource": "http://example.test/mcp"})

    seen = []
    server = NodusServer(_registry(), name="e2e", auth_hook=lambda n, a, m: seen.append(m))
    app = server.streamable_http_app(
        routes=[Route("/.well-known/oauth-protected-resource", metadata)],
        middleware=[Middleware(AuthenticationMiddleware, backend=Bearer(), on_error=on_error)],
    )

    with _Served(app) as served:
        # The host route is served by the same app.
        with urllib.request.urlopen(served.url("/.well-known/oauth-protected-resource"), timeout=5) as r:
            assert json.loads(r.read()) == {"resource": "http://example.test/mcp"}

        # No token: 401 + WWW-Authenticate from /mcp itself.
        req = urllib.request.Request(
            served.url(), data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 401
        assert "resource_metadata" in err.value.headers.get("WWW-Authenticate", "")
        assert seen == [], "a refused request reached the tool layer"

        # With the token: the call goes through, and the hook sees the identity.
        tools, ok, _ = asyncio.run(
            _with_session(served.url(), {"Authorization": "Bearer good-token"}, _list_and_call)
        )

    assert json.loads(_text(ok)) == {"echoed": "hi"}
    assert seen[0]["user"].username == "alice", seen[0].get("user")
    assert seen[0]["auth"].scopes == ["mcp:tools"], seen[0].get("auth")


# The handoff asked whether a contextvar set by request middleware is visible to
# the tool handler -- i.e. whether the SDK's `get_access_token()` could carry
# the identity instead. Settled here on whichever major is installed; the
# answer is what the test asserts, so a change in SDK behaviour shows up.
_probe_var: contextvars.ContextVar = contextvars.ContextVar("nodus_mcp_probe", default=None)


@pytest.mark.parametrize("stateless", [True, False], ids=["stateless", "stateful"])
def test_middleware_contextvar_vs_scope(stateless):
    """Each HTTP request gets a fresh id, set both as a contextvar and in the
    ASGI scope. For each tool call, compare what the handler sees through the
    contextvar with the id of the request that actually carried the call."""
    import itertools

    counter = itertools.count(1)

    class Tag:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                rid = next(counter)
                _probe_var.set(rid)
                scope["probe_id"] = rid
            await self.app(scope, receive, send)

    from starlette.middleware import Middleware

    seen = []
    server = NodusServer(
        _registry(), name="e2e",
        auth_hook=lambda n, a, m: seen.append((_probe_var.get(), m["request"].scope.get("probe_id"))),
    )
    app = server.streamable_http_app(stateless=stateless, middleware=[Middleware(Tag)])
    with _Served(app) as served:
        asyncio.run(_with_session(served.url(), {}, _list_and_call))

    print(f"\n[probe] stateless={stateless} (contextvar, scope) per call: {seen}")
    assert len(seen) == 2, seen
    (var1, scope1), (var2, scope2) = seen
    # The scope is always the carrying request's, one per call -- that is what
    # context["user"/"auth"] is read from, on every major and mode.
    assert scope1 is not None and scope2 is not None and scope1 != scope2, seen

    if stateless or _SDK_V2:
        # The contextvar is the carrying request's too.
        assert (var1, var2) == (scope1, scope2), seen
    else:
        # mcp 1.x, stateful: the session's server task is started inside the
        # request that opened the session and keeps that request's context.
        # Every later call sees the *opener's* value -- not missing, stale. An
        # identity carried this way would be whoever opened the session.
        assert var1 == var2 and var1 not in (scope1, scope2), seen


def test_security_settings_pass_through():
    from mcp.server.transport_security import TransportSecuritySettings

    refuse = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=["mcp.example.com"]
    )
    allow = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*"]
    )
    server = NodusServer(_registry(), name="e2e")

    with _Served(server.streamable_http_app(security_settings=refuse)) as served:
        req = urllib.request.Request(
            served.url(), data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code in (400, 403, 421), err.value.code

    with _Served(server.streamable_http_app(security_settings=allow)) as served:
        _, ok, _ = asyncio.run(_with_session(served.url(), {}, _list_and_call))
    assert json.loads(_text(ok)) == {"echoed": "hi"}


def test_custom_path():
    server = NodusServer(_registry(), name="e2e")
    with _Served(server.streamable_http_app(path="/rpc")) as served:
        _, ok, _ = asyncio.run(_with_session(served.url("/rpc"), {}, _list_and_call))
    assert json.loads(_text(ok)) == {"echoed": "hi"}


def _app_run_would_serve(monkeypatch, **run_kwargs):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    NodusServer(_registry(), name="e2e").run(transport="streamable-http", **run_kwargs)
    (app, kw), = calls
    return app, kw


def _raw_post(url: str, **headers) -> int:
    """Status of a bare MCP-shaped POST with the given extra headers."""
    import http.client
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "probe", "version": "0"}},
    })
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    hdrs.update(headers)
    conn.request("POST", parts.path, body=body, headers=hdrs)
    status = conn.getresponse().status
    conn.close()
    return status


def test_run_streamable_http_serves_the_app(monkeypatch):
    app, kw = _app_run_would_serve(monkeypatch, port=9123)
    assert kw == {"host": "127.0.0.1", "port": 9123}
    paths = [getattr(r, "path", None) for r in app.routes]
    assert paths == ["/mcp"], paths


def test_run_on_loopback_turns_dns_rebinding_protection_on(monkeypatch):
    app, _ = _app_run_would_serve(monkeypatch)
    with _Served(app) as served:
        # A rebinding attack arrives with the attacker's name in Host, or from
        # the attacker's page as Origin. Both are refused.
        assert _raw_post(served.url(), Host="evil.example.com") in (400, 403, 421)
        assert _raw_post(served.url(), Origin="http://evil.example.com") in (400, 403, 421)
        # A loopback client, by either name, is not.
        assert _raw_post(served.url(), Host=f"localhost:{served.port}") == 200
        _, ok, _ = asyncio.run(_with_session(served.url(), {}, _list_and_call))
    assert json.loads(_text(ok)) == {"echoed": "hi"}


def test_run_off_loopback_leaves_protection_to_the_caller(monkeypatch):
    # Bound to all interfaces the public name is unknown, so nothing is
    # guessed: a request under any Host is served unless settings say otherwise.
    app, kw = _app_run_would_serve(monkeypatch, host="0.0.0.0")
    assert kw["host"] == "0.0.0.0"
    with _Served(app) as served:
        assert _raw_post(served.url(), Host="mcp.example.com") == 200

    # And explicit settings win over the loopback default.
    from mcp.server.transport_security import TransportSecuritySettings

    open_ = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app, _ = _app_run_would_serve(monkeypatch, security_settings=open_)
    with _Served(app) as served:
        assert _raw_post(served.url(), Host="evil.example.com") == 200


def test_which_branch_ran_is_visible():
    from importlib.metadata import version

    branch = "2.x add_request_handler" if _SDK_V2 else "1.x decorators"
    print(f"\n[nodus-mcp] mcp {version('mcp')} -- server branch: {branch}")
