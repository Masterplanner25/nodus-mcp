"""Phase D + E tests — resources and prompts.

All tests use MockTransport (canned responses). No subprocess, no NodusRuntime.
Zero concurrency; no MRTR loop; no terminal conditions beyond the error table.

Standing assertions:
  - resources/list parses RC descriptor shape correctly (uri, name, mimeType, description)
  - resources/read handles both text and blob content (text/blob distinction is the only
    shape subtlety — confirmed per RC)
  - prompts/list parses arguments (name, description, required)
  - prompts/get parses messages with all three content-block types (text, image, resource)
  - Error paths produce the right ToolErrorCategory from doc 1's table
  - _simple_call is the shared path: D and E use transport.send_request via _simple_call,
    not a parallel mechanism
  - RC shape purity: no pre-RC field names (no `id` on descriptors, no `metadata`
    wrapper, no `file` content-block type)
  - resources/subscribe is absent (confirmed deferred)
"""
import json

import pytest

from nodus_mcp.client import McpClient, _simple_call, _CLIENT_META
from nodus_mcp.protocol.messages import (
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    METHOD_PROMPTS_LIST,
    METHOD_PROMPTS_GET,
    ResourceDescriptor,
    ResourceContent,
    PromptArgument,
    PromptDescriptor,
    PromptMessageContent,
    PromptMessage,
    ToolErrorCategory,
)
from nodus_mcp.transport import McpTransport, TransportError
from nodus_mcp.protocol.jsonrpc import METHOD_NOT_FOUND, INVALID_PARAMS


# ── Mock infrastructure (same pattern as Phase C) ────────────────────────────

class MockTransport(McpTransport):
    def __init__(self, responses: list):
        self._responses = list(responses)
        self._requests: list[tuple[str, dict]] = []

    def send_request(self, method: str, params: dict) -> dict:
        self._requests.append((method, params))
        if not self._responses:
            raise TransportError("no more canned responses")
        return self._responses.pop(0)

    def send_notification(self, method: str, params: dict) -> None:
        pass

    def close(self) -> None:
        pass

    def last_request(self) -> tuple[str, dict]:
        return self._requests[-1]


def _make_client_with_transport(transport: MockTransport, alias: str = "srv") -> McpClient:
    """Build a McpClient with a pre-connected alias without running server/discover."""
    from nodus_mcp.connection import McpConnection
    client = McpClient()
    client._connections[alias] = McpConnection(
        alias=alias,
        url="mock://test",
        transport=transport,
        bearer_token=None,
        server_info={},
        server_capabilities={},
        registered_tools=[],
    )
    return client


# ── _simple_call: shared path check ──────────────────────────────────────────

def test_simple_call_uses_transport_send_request():
    """D+E go through _simple_call → transport.send_request, not a new mechanism."""
    transport = MockTransport([{"result": {"data": 42}}])
    result = _simple_call(transport, "custom/method", {"x": 1})
    assert result == {"data": 42}
    method, params = transport.last_request()
    assert method == "custom/method"
    assert params["x"] == 1


def test_simple_call_transport_error_returns_error_dict():
    class FailTransport(McpTransport):
        def send_request(self, m, p): raise TransportError("pipe broken")
        def send_notification(self, m, p): pass
        def close(self): pass

    result = _simple_call(FailTransport(), "any/method", {})
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.TRANSPORT_ERROR.value


def test_simple_call_rpc_error_not_found():
    transport = MockTransport([{"error": {"code": METHOD_NOT_FOUND, "message": "no"}}])
    result = _simple_call(transport, "x", {})
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.NOT_FOUND.value


def test_simple_call_rpc_invalid_params():
    transport = MockTransport([{"error": {"code": INVALID_PARAMS, "message": "bad"}}])
    result = _simple_call(transport, "x", {})
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.INVALID_PARAMS.value


# ── Phase D: resources/list ───────────────────────────────────────────────────

def test_resources_list_returns_raw_result():
    resources_resp = {"result": {"resources": [
        {"uri": "file:///a.txt", "name": "A", "mimeType": "text/plain",
         "description": "file A"},
        {"uri": "file:///b.png", "name": "B", "mimeType": "image/png"},
    ]}}
    transport = MockTransport([resources_resp])
    client = _make_client_with_transport(transport)
    result = client.resources_list("srv")
    assert len(result["resources"]) == 2


def test_resources_list_sends_correct_method():
    transport = MockTransport([{"result": {"resources": []}}])
    client = _make_client_with_transport(transport)
    client.resources_list("srv")
    method, params = transport.last_request()
    assert method == METHOD_RESOURCES_LIST


def test_resources_list_includes_meta():
    transport = MockTransport([{"result": {"resources": []}}])
    client = _make_client_with_transport(transport)
    client.resources_list("srv")
    _, params = transport.last_request()
    assert "_meta" in params
    assert "capabilities" in params["_meta"]


# ── ResourceDescriptor parsing ────────────────────────────────────────────────

def test_resource_descriptor_from_dict_full():
    d = {"uri": "file:///x.txt", "name": "X", "mimeType": "text/plain",
         "description": "desc"}
    rd = ResourceDescriptor.from_dict(d)
    assert rd.uri == "file:///x.txt"
    assert rd.name == "X"
    assert rd.mime_type == "text/plain"   # camelCase on wire → snake_case in type
    assert rd.description == "desc"


def test_resource_descriptor_optional_fields_absent():
    d = {"uri": "file:///y.bin", "name": "Y"}
    rd = ResourceDescriptor.from_dict(d)
    assert rd.mime_type is None
    assert rd.description is None


def test_resource_descriptor_to_dict_round_trip():
    rd = ResourceDescriptor(uri="file:///f", name="F", mime_type="text/csv")
    d = rd.to_dict()
    assert d["mimeType"] == "text/csv"   # round-trips to camelCase
    assert "description" not in d        # absent when None


def test_resource_descriptor_no_pre_rc_id_field():
    """RC purity: no `id` field on resource descriptors."""
    d = {"uri": "file:///x", "name": "X", "id": "legacy-id"}  # pre-RC artifact
    rd = ResourceDescriptor.from_dict(d)
    out = rd.to_dict()
    assert "id" not in out              # id not propagated


# ── Phase D: resources/read ───────────────────────────────────────────────────

def test_resources_read_text_content():
    read_resp = {"result": {"contents": [
        {"uri": "file:///a.txt", "text": "hello world"},
    ]}}
    transport = MockTransport([read_resp])
    client = _make_client_with_transport(transport)
    result = client.resources_read("srv", "file:///a.txt")
    assert len(result["contents"]) == 1
    assert result["contents"][0]["text"] == "hello world"


def test_resources_read_blob_content():
    import base64
    blob_data = base64.b64encode(b"\x89PNG").decode()
    read_resp = {"result": {"contents": [
        {"uri": "file:///img.png", "blob": blob_data},
    ]}}
    transport = MockTransport([read_resp])
    client = _make_client_with_transport(transport)
    result = client.resources_read("srv", "file:///img.png")
    assert result["contents"][0]["blob"] == blob_data
    assert "text" not in result["contents"][0]


def test_resources_read_sends_uri_param():
    transport = MockTransport([{"result": {"contents": []}}])
    client = _make_client_with_transport(transport)
    client.resources_read("srv", "file:///target.txt")
    method, params = transport.last_request()
    assert method == METHOD_RESOURCES_READ
    assert params["uri"] == "file:///target.txt"


# ── ResourceContent parsing ───────────────────────────────────────────────────

def test_resource_content_text():
    rc = ResourceContent.from_dict({"uri": "file:///f.txt", "text": "content"})
    assert rc.is_text()
    assert not rc.is_blob()
    assert rc.text == "content"


def test_resource_content_blob():
    rc = ResourceContent.from_dict({"uri": "file:///f.bin", "blob": "abc=="})
    assert rc.is_blob()
    assert not rc.is_text()
    assert rc.blob == "abc=="


def test_resource_content_text_blob_mutually_exclusive():
    """RC invariant: exactly one of text or blob set per content item."""
    text_rc = ResourceContent.from_dict({"uri": "u", "text": "t"})
    blob_rc = ResourceContent.from_dict({"uri": "u", "blob": "b"})
    assert text_rc.text is not None and text_rc.blob is None
    assert blob_rc.blob is not None and blob_rc.text is None


# ── Phase D: error paths ──────────────────────────────────────────────────────

def test_resources_list_unknown_alias_raises():
    client = McpClient()
    with pytest.raises(KeyError):
        client.resources_list("nonexistent")


def test_resources_read_not_found_error():
    transport = MockTransport([{"error": {"code": METHOD_NOT_FOUND, "message": "uri not found"}}])
    client = _make_client_with_transport(transport)
    result = client.resources_read("srv", "file:///missing.txt")
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.NOT_FOUND.value


# ── Phase E: prompts/list ─────────────────────────────────────────────────────

def test_prompts_list_returns_raw_result():
    prompts_resp = {"result": {"prompts": [
        {"name": "greet", "description": "Greeting prompt",
         "arguments": [{"name": "person", "description": "Who to greet",
                        "required": True}]},
        {"name": "farewell"},
    ]}}
    transport = MockTransport([prompts_resp])
    client = _make_client_with_transport(transport)
    result = client.prompts_list("srv")
    assert len(result["prompts"]) == 2


def test_prompts_list_sends_correct_method():
    transport = MockTransport([{"result": {"prompts": []}}])
    client = _make_client_with_transport(transport)
    client.prompts_list("srv")
    method, _ = transport.last_request()
    assert method == METHOD_PROMPTS_LIST


# ── PromptDescriptor + PromptArgument parsing ─────────────────────────────────

def test_prompt_descriptor_with_arguments():
    d = {"name": "greet", "description": "Greet someone",
         "arguments": [
             {"name": "name", "description": "Person name", "required": True},
             {"name": "style"},  # optional, not required
         ]}
    pd = PromptDescriptor.from_dict(d)
    assert pd.name == "greet"
    assert pd.description == "Greet someone"
    assert len(pd.arguments) == 2
    assert pd.arguments[0].required is True
    assert pd.arguments[1].required is False


def test_prompt_descriptor_no_arguments():
    d = {"name": "simple"}
    pd = PromptDescriptor.from_dict(d)
    assert pd.arguments == []
    assert pd.description is None


def test_prompt_argument_to_dict_required_only_when_true():
    pa_req = PromptArgument(name="x", required=True)
    pa_opt = PromptArgument(name="y", required=False)
    assert pa_req.to_dict().get("required") is True
    assert "required" not in pa_opt.to_dict()  # omit when False


def test_prompt_descriptor_no_pre_rc_metadata():
    """RC purity: no `metadata` wrapper on prompt descriptors."""
    d = {"name": "p", "metadata": {"legacy": True}}  # pre-RC artifact
    pd = PromptDescriptor.from_dict(d)
    out = pd.to_dict()
    assert "metadata" not in out


# ── Phase E: prompts/get ──────────────────────────────────────────────────────

def test_prompts_get_text_message():
    get_resp = {"result": {
        "description": "A greeting",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": "Hello, World!"}},
        ],
    }}
    transport = MockTransport([get_resp])
    client = _make_client_with_transport(transport)
    result = client.prompts_get("srv", "greet", arguments={"name": "World"})
    assert result["description"] == "A greeting"
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][0]["content"]["text"] == "Hello, World!"


def test_prompts_get_sends_name_and_arguments():
    transport = MockTransport([{"result": {"messages": []}}])
    client = _make_client_with_transport(transport)
    client.prompts_get("srv", "greeting", arguments={"lang": "en"})
    method, params = transport.last_request()
    assert method == METHOD_PROMPTS_GET
    assert params["name"] == "greeting"
    assert params["arguments"] == {"lang": "en"}


def test_prompts_get_no_arguments_omits_field():
    transport = MockTransport([{"result": {"messages": []}}])
    client = _make_client_with_transport(transport)
    client.prompts_get("srv", "plain")
    _, params = transport.last_request()
    assert "arguments" not in params


# ── PromptMessage + PromptMessageContent parsing ──────────────────────────────

def test_prompt_message_text_content():
    m = PromptMessage.from_dict({
        "role": "user",
        "content": {"type": "text", "text": "hello"},
    })
    assert m.role == "user"
    assert m.content.type == "text"
    assert m.content.text == "hello"


def test_prompt_message_image_content():
    m = PromptMessage.from_dict({
        "role": "assistant",
        "content": {"type": "image", "data": "abc123=", "mimeType": "image/png"},
    })
    assert m.content.type == "image"
    assert m.content.data == "abc123="
    assert m.content.mime_type == "image/png"   # camelCase → snake_case


def test_prompt_message_resource_content():
    m = PromptMessage.from_dict({
        "role": "user",
        "content": {"type": "resource",
                    "resource": {"uri": "file:///doc.txt", "text": "doc contents"}},
    })
    assert m.content.type == "resource"
    assert m.content.resource["uri"] == "file:///doc.txt"
    assert m.content.resource["text"] == "doc contents"


def test_prompt_message_content_types_are_rc_set():
    """RC purity: content block types are text/image/resource — not pre-RC `file`."""
    valid_types = {"text", "image", "resource"}
    # Verify our dataclass handles exactly these without error
    for t in valid_types:
        c = PromptMessageContent.from_dict({"type": t})
        assert c.type == t
    # pre-RC `file` type is not a special case — treated as unknown passthrough
    c_unknown = PromptMessageContent.from_dict({"type": "file", "text": "data"})
    assert c_unknown.type == "file"  # passes through without error, caller handles


# ── Phase E: error paths ──────────────────────────────────────────────────────

def test_prompts_get_not_found_error():
    transport = MockTransport([{"error": {"code": METHOD_NOT_FOUND, "message": "no prompt"}}])
    client = _make_client_with_transport(transport)
    result = client.prompts_get("srv", "missing")
    assert result.get("isError") is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["category"] == ToolErrorCategory.NOT_FOUND.value


def test_prompts_list_unknown_alias_raises():
    client = McpClient()
    with pytest.raises(KeyError):
        client.prompts_list("nonexistent")


# ── RC purity: no pre-RC shapes ───────────────────────────────────────────────

def test_no_resources_subscribe_method_constant():
    """resources/subscribe is deferred to v0.2 (TD-006); must not exist in v0.1."""
    import nodus_mcp.protocol.messages as m
    assert not hasattr(m, "METHOD_RESOURCES_SUBSCRIBE"), (
        "resources/subscribe is deferred to v0.2 — it should not be a method constant"
    )


def test_resource_method_names_are_rc_strings():
    assert METHOD_RESOURCES_LIST == "resources/list"
    assert METHOD_RESOURCES_READ == "resources/read"


def test_prompt_method_names_are_rc_strings():
    assert METHOD_PROMPTS_LIST == "prompts/list"
    assert METHOD_PROMPTS_GET == "prompts/get"


def test_resource_descriptor_rc_field_names():
    """RC uses camelCase mimeType — verify round-trip preserves it."""
    d = {"uri": "u", "name": "n", "mimeType": "text/plain"}
    rd = ResourceDescriptor.from_dict(d)
    assert rd.mime_type == "text/plain"          # internal: snake_case
    assert rd.to_dict()["mimeType"] == "text/plain"  # wire: camelCase


def test_prompt_message_content_rc_mimetypes_camelcase():
    """RC uses camelCase mimeType in image content blocks."""
    d = {"type": "image", "data": "b64", "mimeType": "image/jpeg"}
    c = PromptMessageContent.from_dict(d)
    assert c.mime_type == "image/jpeg"
    assert c.to_dict()["mimeType"] == "image/jpeg"
