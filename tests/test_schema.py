from nodus_mcp_aindy.schema import json_schema_to_lightweight, lightweight_to_json_schema


# ── lightweight_to_json_schema ────────────────────────────────────────────────

def test_none_returns_empty_object():
    s = lightweight_to_json_schema(None)
    assert s == {"type": "object", "properties": {}}


def test_empty_dict_returns_empty_object():
    s = lightweight_to_json_schema({})
    assert s == {"type": "object", "properties": {}}


def test_basic_properties():
    schema = lightweight_to_json_schema({"properties": {"q": {"type": "string"}}})
    assert schema["type"] == "object"
    assert schema["properties"]["q"]["type"] == "string"


def test_type_normalization_int():
    s = lightweight_to_json_schema({"properties": {"limit": {"type": "int"}}})
    assert s["properties"]["limit"]["type"] == "integer"


def test_type_normalization_list():
    s = lightweight_to_json_schema({"properties": {"tags": {"type": "list"}}})
    assert s["properties"]["tags"]["type"] == "array"


def test_type_normalization_dict():
    s = lightweight_to_json_schema({"properties": {"meta": {"type": "dict"}}})
    assert s["properties"]["meta"]["type"] == "object"


def test_type_normalization_float():
    s = lightweight_to_json_schema({"properties": {"score": {"type": "float"}}})
    assert s["properties"]["score"]["type"] == "number"


def test_type_normalization_bool():
    s = lightweight_to_json_schema({"properties": {"active": {"type": "bool"}}})
    assert s["properties"]["active"]["type"] == "boolean"


def test_required_list_preserved():
    s = lightweight_to_json_schema({
        "required": ["content"],
        "properties": {"content": {"type": "string"}, "tags": {"type": "list"}},
    })
    assert "required" in s
    assert "content" in s["required"]
    assert "tags" not in s.get("required", [])


def test_description_preserved():
    s = lightweight_to_json_schema({
        "properties": {"q": {"type": "string", "description": "Search query"}}
    })
    assert s["properties"]["q"]["description"] == "Search query"


def test_extra_fields_preserved():
    s = lightweight_to_json_schema({
        "properties": {"status": {"type": "string", "enum": ["ok", "fail"]}}
    })
    assert s["properties"]["status"]["enum"] == ["ok", "fail"]


def test_type_already_json_schema_passthrough():
    # Already-normalised types should pass through unchanged
    s = lightweight_to_json_schema({"properties": {"limit": {"type": "integer"}}})
    assert s["properties"]["limit"]["type"] == "integer"


# ── json_schema_to_lightweight ────────────────────────────────────────────────

def test_reverse_basic():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    lt = json_schema_to_lightweight(schema)
    assert lt["properties"]["query"]["type"] == "string"


def test_reverse_integer():
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    lt = json_schema_to_lightweight(schema)
    assert lt["properties"]["limit"]["type"] == "int"


def test_reverse_array():
    schema = {"type": "object", "properties": {"tags": {"type": "array"}}}
    lt = json_schema_to_lightweight(schema)
    assert lt["properties"]["tags"]["type"] == "list"


def test_reverse_required():
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }
    lt = json_schema_to_lightweight(schema)
    assert "content" in lt["required"]


def test_reverse_non_object_returns_empty():
    assert json_schema_to_lightweight({}) == {}
    assert json_schema_to_lightweight({"type": "string"}) == {}


# ── Round-trip ────────────────────────────────────────────────────────────────

def test_round_trip_lightweight_to_json_and_back():
    original = {
        "required": ["content"],
        "properties": {
            "content": {"type": "string", "description": "Text to store"},
            "limit":   {"type": "int"},
            "tags":    {"type": "list"},
        },
    }
    json_schema = lightweight_to_json_schema(original)
    restored = json_schema_to_lightweight(json_schema)

    assert restored["properties"]["content"]["type"] == "string"
    assert restored["properties"]["limit"]["type"] == "int"
    assert restored["properties"]["tags"]["type"] == "list"
    assert "content" in restored["required"]
