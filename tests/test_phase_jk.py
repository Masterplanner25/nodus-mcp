"""Phase J + K tests — server resources and server prompts.

J = server resources (resources/list + resources/read)
K = server prompts   (prompts/list + prompts/get)

Both are handler-configured (confirmed: nodus-lang has no std:resource/std:prompt).
Both reuse Phase D/E type definitions (ResourceDescriptor, PromptDescriptor, etc.)
— the same RC shape parsed by the client is now emitted by the server.

Standing assertions:
  J: resources/list emits RC-shape descriptors (uri, name, mimeType?, description?)
  J: resources/read emits text-or-blob contents (one-of invariant from D)
  J: no resource handler → -32601; unknown uri → -32601
  K: prompts/list emits RC-shape descriptors (name, description?, arguments?)
  K: prompts/get emits messages with RC content blocks
  K: missing required argument → -32602 (K-specific validation)
  Both: capability gating — resources/prompts in _meta only with list handlers
  Both: server-emit RC-shape purity (no pre-RC id on resources, no pre-RC metadata)
  J: no resources/subscribe route (standing assertion mirroring TD-006)
  Both: reuse D/E types (server emits, client parses — same dataclass, no drift)
"""
import json

import pytest

from nodus_mcp.server import McpServer
from nodus_mcp.protocol.jsonrpc import METHOD_NOT_FOUND, INVALID_PARAMS
from nodus_mcp.protocol.messages import (
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    METHOD_PROMPTS_LIST,
    METHOD_PROMPTS_GET,
    METHOD_SERVER_DISCOVER,
    ResourceDescriptor,
    ResourceContent,
    PromptDescriptor,
    PromptArgument,
    PromptMessage,
    PromptMessageContent,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _server(*, resource_list=None, resource_read=None,
             prompt_list=None, prompt_get=None) -> McpServer:
    s = McpServer()
    if resource_list:
        s.set_resource_list_handler(resource_list)
    if resource_read:
        s.set_resource_read_handler(resource_read)
    if prompt_list:
        s.set_prompt_list_handler(prompt_list)
    if prompt_get:
        s.set_prompt_get_handler(prompt_get)
    return s


def _resource(uri: str, name: str, *, mime_type: str | None = None,
              description: str | None = None) -> dict:
    d: dict = {"uri": uri, "name": name}
    if mime_type:
        d["mimeType"] = mime_type
    if description:
        d["description"] = description
    return d


def _text_content(uri: str, text: str) -> dict:
    return {"uri": uri, "text": text}


def _blob_content(uri: str, blob: str) -> dict:
    return {"uri": uri, "blob": blob}


def _prompt(name: str, *, description: str | None = None,
            arguments: list | None = None) -> dict:
    d: dict = {"name": name}
    if description:
        d["description"] = description
    if arguments:
        d["arguments"] = arguments
    return d


def _prompt_arg(name: str, *, required: bool = False,
                description: str | None = None) -> dict:
    d: dict = {"name": name, "required": required}
    if description:
        d["description"] = description
    return d


def _text_message(role: str, text: str) -> dict:
    return {"role": role, "content": {"type": "text", "text": text}}


# ── J: resources/list ────────────────────────────────────────────────────────

def test_j_resources_list_returns_descriptors():
    """J: resources/list emits RC-shaped descriptors (same shape D parsed)."""
    resources = [
        _resource("file:///a.txt", "File A", mime_type="text/plain", description="A text file"),
        _resource("file:///b.png", "File B", mime_type="image/png"),
    ]
    s = _server(resource_list=lambda: resources)
    resp = s.dispatch(METHOD_RESOURCES_LIST, {}, "r1")
    assert "result" in resp
    items = resp["result"]["resources"]
    assert len(items) == 2
    assert items[0]["uri"] == "file:///a.txt"
    assert items[0]["name"] == "File A"
    assert items[0]["mimeType"] == "text/plain"    # camelCase in wire
    assert items[0]["description"] == "A text file"
    assert items[1]["uri"] == "file:///b.png"
    assert "description" not in items[1]           # absent when None


def test_j_resources_list_no_handler_returns_32601():
    """J: no handler → -32601 (unsupported, not configured)."""
    s = McpServer()
    resp = s.dispatch(METHOD_RESOURCES_LIST, {}, "r1")
    assert resp.get("error", {}).get("code") == METHOD_NOT_FOUND


def test_j_resources_list_empty_list():
    s = _server(resource_list=lambda: [])
    resp = s.dispatch(METHOD_RESOURCES_LIST, {}, "r1")
    assert resp["result"]["resources"] == []


def test_j_resources_list_no_pre_rc_id_field():
    """RC purity: server-emitted resource descriptors have no pre-RC 'id' field."""
    resources = [{"uri": "file:///x", "name": "X", "id": "legacy-id"}]
    s = _server(resource_list=lambda: resources)
    resp = s.dispatch(METHOD_RESOURCES_LIST, {}, "r1")
    for item in resp["result"]["resources"]:
        assert "id" not in item, "Pre-RC 'id' field must not appear in emitted descriptors"


def test_j_resources_list_reuses_d_type():
    """Reuse check: server emits ResourceDescriptor dicts; client parses same shape."""
    resources = [_resource("file:///f.csv", "CSV", mime_type="text/csv")]
    s = _server(resource_list=lambda: resources)
    resp = s.dispatch(METHOD_RESOURCES_LIST, {}, "r1")
    # Round-trip: server emits → client parses via ResourceDescriptor.from_dict()
    parsed = ResourceDescriptor.from_dict(resp["result"]["resources"][0])
    assert parsed.uri == "file:///f.csv"
    assert parsed.mime_type == "text/csv"          # camelCase↔snake_case handled


# ── J: resources/read ────────────────────────────────────────────────────────

def test_j_resources_read_text_content():
    """J: read handler returns text content → text in response (no blob)."""
    def read(uri: str) -> list:
        if uri == "file:///hello.txt":
            return [_text_content("file:///hello.txt", "Hello world")]
        raise KeyError(uri)

    s = _server(resource_list=lambda: [], resource_read=read)
    resp = s.dispatch(METHOD_RESOURCES_READ, {"uri": "file:///hello.txt"}, "r1")
    contents = resp["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["text"] == "Hello world"
    assert "blob" not in contents[0]


def test_j_resources_read_blob_content():
    """J: blob content passes through as base64 string (opaque)."""
    import base64
    b64 = base64.b64encode(b"\x89PNG\r\n").decode()

    def read(uri):
        if uri == "file:///img.png":
            return [_blob_content("file:///img.png", b64)]
        raise KeyError(uri)

    s = _server(resource_list=lambda: [], resource_read=read)
    resp = s.dispatch(METHOD_RESOURCES_READ, {"uri": "file:///img.png"}, "r1")
    c = resp["result"]["contents"][0]
    assert c["blob"] == b64
    assert "text" not in c


def test_j_resources_read_unknown_uri_returns_32601():
    """J: handler raises KeyError for unknown uri → -32601."""
    def read(uri):
        raise KeyError(uri)

    s = _server(resource_list=lambda: [], resource_read=read)
    resp = s.dispatch(METHOD_RESOURCES_READ, {"uri": "file:///missing"}, "r1")
    assert resp.get("error", {}).get("code") == METHOD_NOT_FOUND


def test_j_resources_read_missing_uri_param_returns_32602():
    """J: missing 'uri' param → -32602."""
    s = _server(resource_list=lambda: [], resource_read=lambda uri: [])
    resp = s.dispatch(METHOD_RESOURCES_READ, {}, "r1")
    assert resp.get("error", {}).get("code") == INVALID_PARAMS


def test_j_resources_read_no_handler_returns_32601():
    s = McpServer()
    resp = s.dispatch(METHOD_RESOURCES_READ, {"uri": "file:///x"}, "r1")
    assert resp.get("error", {}).get("code") == METHOD_NOT_FOUND


def test_j_resources_read_text_blob_invariant():
    """J: ResourceContent ensures exactly one of text or blob (D's invariant, server-side)."""
    def read(uri):
        return [{"uri": "file:///f.txt", "text": "content"}]

    s = _server(resource_list=lambda: [], resource_read=read)
    resp = s.dispatch(METHOD_RESOURCES_READ, {"uri": "file:///f.txt"}, "r1")
    c = resp["result"]["contents"][0]
    # Exactly one set; D's is_text()/is_blob() holds on round-trip
    rc = ResourceContent.from_dict(c)
    assert rc.is_text()
    assert not rc.is_blob()


# ── J: no resources/subscribe ─────────────────────────────────────────────────

def test_j_no_resources_subscribe_route():
    """TD-006 server mirror: resources/subscribe must not be routed (standing assertion)."""
    s = McpServer()
    resp = s.dispatch("resources/subscribe", {}, "r1")
    assert resp.get("error", {}).get("code") == METHOD_NOT_FOUND


# ── J: capability gating ─────────────────────────────────────────────────────

def test_j_resources_in_discover_only_when_list_handler_set():
    """resources capability gated on list handler (H2/F2 rule, extended)."""
    s = McpServer()
    r1 = s.dispatch(METHOD_SERVER_DISCOVER, {}, "d1")
    assert "resources" not in r1["result"]["_meta"]["capabilities"]

    s.set_resource_list_handler(lambda: [])
    r2 = s.dispatch(METHOD_SERVER_DISCOVER, {}, "d2")
    assert "resources" in r2["result"]["_meta"]["capabilities"]


# ── K: prompts/list ──────────────────────────────────────────────────────────

def test_k_prompts_list_returns_descriptors():
    """K: prompts/list emits RC-shaped descriptors (same shape E parsed)."""
    prompts = [
        _prompt("greet", description="Greeting",
                arguments=[_prompt_arg("name", required=True, description="Who to greet"),
                            _prompt_arg("style")]),
        _prompt("farewell"),
    ]
    s = _server(prompt_list=lambda: prompts)
    resp = s.dispatch(METHOD_PROMPTS_LIST, {}, "r1")
    items = resp["result"]["prompts"]
    assert len(items) == 2
    assert items[0]["name"] == "greet"
    assert items[0]["description"] == "Greeting"
    assert len(items[0]["arguments"]) == 2
    assert items[0]["arguments"][0]["name"] == "name"
    assert items[0]["arguments"][0]["required"] is True
    assert items[0]["arguments"][1]["name"] == "style"
    assert "required" not in items[0]["arguments"][1]   # omitted when False
    assert "arguments" not in items[1]                   # absent when empty


def test_k_prompts_list_no_handler_returns_32601():
    s = McpServer()
    resp = s.dispatch(METHOD_PROMPTS_LIST, {}, "r1")
    assert resp.get("error", {}).get("code") == METHOD_NOT_FOUND


def test_k_prompts_list_no_pre_rc_metadata():
    """RC purity: no pre-RC 'metadata' wrapper on emitted prompt descriptors."""
    prompts = [{"name": "p", "metadata": {"legacy": True}}]
    s = _server(prompt_list=lambda: prompts)
    resp = s.dispatch(METHOD_PROMPTS_LIST, {}, "r1")
    for item in resp["result"]["prompts"]:
        assert "metadata" not in item


def test_k_prompts_list_reuses_e_type():
    """Reuse check: server emits PromptDescriptor; client parses same shape."""
    prompts = [_prompt("ask", arguments=[_prompt_arg("q", required=True)])]
    s = _server(prompt_list=lambda: prompts)
    resp = s.dispatch(METHOD_PROMPTS_LIST, {}, "r1")
    parsed = PromptDescriptor.from_dict(resp["result"]["prompts"][0])
    assert parsed.name == "ask"
    assert parsed.arguments[0].required is True


# ── K: prompts/get ───────────────────────────────────────────────────────────

def test_k_prompts_get_text_message():
    """K: prompts/get returns messages with RC content blocks."""
    prompts = [_prompt("greet")]

    def get(name, args):
        return {
            "description": "A greeting",
            "messages": [_text_message("user", f"Hello, {args.get('name', 'world')}!")],
        }

    s = _server(prompt_list=lambda: prompts, prompt_get=get)
    resp = s.dispatch(METHOD_PROMPTS_GET, {"name": "greet", "arguments": {"name": "Alice"}}, "r1")
    result = resp["result"]
    assert result["description"] == "A greeting"
    assert result["messages"][0]["role"] == "user"
    assert "Alice" in result["messages"][0]["content"]["text"]


def test_k_prompts_get_image_content():
    """K: image content-block in message uses RC shape (type, data, mimeType)."""
    import base64
    b64 = base64.b64encode(b"\x89PNG").decode()
    prompts = [_prompt("show")]

    def get(name, args):
        return {"messages": [{"role": "user", "content": {
            "type": "image", "data": b64, "mimeType": "image/png",
        }}]}

    s = _server(prompt_list=lambda: prompts, prompt_get=get)
    resp = s.dispatch(METHOD_PROMPTS_GET, {"name": "show"}, "r1")
    content = resp["result"]["messages"][0]["content"]
    assert content["type"] == "image"
    assert content["data"] == b64
    assert content["mimeType"] == "image/png"     # camelCase preserved


def test_k_prompts_get_unknown_name_returns_32601():
    """K: handler raises KeyError for unknown prompt → -32601."""
    prompts = [_prompt("known")]

    def get(name, args):
        raise KeyError(name)

    s = _server(prompt_list=lambda: prompts, prompt_get=get)
    resp = s.dispatch(METHOD_PROMPTS_GET, {"name": "unknown"}, "r1")
    assert resp.get("error", {}).get("code") == METHOD_NOT_FOUND


def test_k_prompts_get_missing_name_param_returns_32602():
    s = _server(prompt_list=lambda: [], prompt_get=lambda n, a: {"messages": []})
    resp = s.dispatch(METHOD_PROMPTS_GET, {}, "r1")
    assert resp.get("error", {}).get("code") == INVALID_PARAMS


def test_k_prompts_get_no_handler_returns_32601():
    s = McpServer()
    resp = s.dispatch(METHOD_PROMPTS_GET, {"name": "x"}, "r1")
    assert resp.get("error", {}).get("code") == METHOD_NOT_FOUND


# ── K: required-argument validation (-32602) ─────────────────────────────────

def test_k_prompts_get_missing_required_arg_returns_32602():
    """K-specific validation: missing required argument → -32602 (doc 4 B2 / K framing)."""
    prompts = [_prompt("fill", arguments=[_prompt_arg("color", required=True)])]

    def get(name, args):
        return {"messages": [_text_message("user", f"Color: {args['color']}")]}

    s = _server(prompt_list=lambda: prompts, prompt_get=get)
    # Omit required 'color' arg
    resp = s.dispatch(METHOD_PROMPTS_GET, {"name": "fill", "arguments": {}}, "r1")
    assert resp.get("error", {}).get("code") == INVALID_PARAMS


def test_k_prompts_get_optional_arg_absent_is_ok():
    """K: optional arguments may be omitted (only required args are validated)."""
    prompts = [_prompt("say", arguments=[_prompt_arg("style")])]   # style is optional

    def get(name, args):
        return {"messages": [_text_message("user", "said")]}

    s = _server(prompt_list=lambda: prompts, prompt_get=get)
    resp = s.dispatch(METHOD_PROMPTS_GET, {"name": "say", "arguments": {}}, "r1")
    assert "result" in resp
    assert "error" not in resp


def test_k_prompts_get_all_required_args_present_passes():
    prompts = [_prompt("both", arguments=[_prompt_arg("a", required=True),
                                           _prompt_arg("b", required=True)])]

    def get(name, args):
        return {"messages": [_text_message("user", "done")]}

    s = _server(prompt_list=lambda: prompts, prompt_get=get)
    resp = s.dispatch(METHOD_PROMPTS_GET, {"name": "both", "arguments": {"a": "1", "b": "2"}}, "r1")
    assert "result" in resp


# ── K: capability gating ─────────────────────────────────────────────────────

def test_k_prompts_in_discover_only_when_list_handler_set():
    """prompts capability gated on prompt list handler (H2/F2 rule, extended)."""
    s = McpServer()
    r1 = s.dispatch(METHOD_SERVER_DISCOVER, {}, "d1")
    assert "prompts" not in r1["result"]["_meta"]["capabilities"]

    s.set_prompt_list_handler(lambda: [])
    r2 = s.dispatch(METHOD_SERVER_DISCOVER, {}, "d2")
    assert "prompts" in r2["result"]["_meta"]["capabilities"]


# ── Coexistence: H/I/J/K all on the same McpServer ───────────────────────────

def test_jk_all_methods_coexist():
    """All server methods (H+I+J+K) coexist on one McpServer instance."""
    from nodus_mcp.protocol.messages import METHOD_TOOLS_LIST

    class FakeRuntime:
        class FakeRegistry:
            def list_tools(self): return []
            def lookup(self, n): return None
        tool_registry = FakeRegistry()

    s = McpServer(runtime=FakeRuntime())
    s.set_resource_list_handler(lambda: [_resource("file:///r", "R")])
    s.set_resource_read_handler(lambda uri: [_text_content(uri, "ok")])
    s.set_prompt_list_handler(lambda: [_prompt("p")])
    s.set_prompt_get_handler(lambda n, a: {"messages": [_text_message("user", "hi")]})

    # All six routes work
    assert "result" in s.dispatch(METHOD_SERVER_DISCOVER, {}, "r0")
    assert "result" in s.dispatch(METHOD_TOOLS_LIST, {}, "r1")
    assert "result" in s.dispatch(METHOD_RESOURCES_LIST, {}, "r2")
    assert "result" in s.dispatch(METHOD_RESOURCES_READ, {"uri": "file:///r"}, "r3")
    assert "result" in s.dispatch(METHOD_PROMPTS_LIST, {}, "r4")
    assert "result" in s.dispatch(METHOD_PROMPTS_GET, {"name": "p"}, "r5")
