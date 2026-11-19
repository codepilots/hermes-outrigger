"""F-01 regressions: a client-declared tool schema must never make the gateway
fetch a remote/file reference (SSRF), and schema validation must fail closed
and loud — never silently treat an unvalidatable schema as valid."""
import logging
import sys
import urllib.request

import pytest

from outrigger import protocol
from outrigger.protocol import ProtocolError, ToolDecl, check_schema, validate_args


@pytest.fixture
def no_fetch(monkeypatch):
    """Record and block any gateway-side urlopen: no schema may trigger one."""
    calls = []

    def fake_urlopen(req, *a, **k):
        calls.append(getattr(req, "full_url", str(req)))
        raise AssertionError(f"gateway attempted an outbound fetch: {calls[-1]}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


REMOTE_REFS = [
    "http://169.254.169.254/latest/meta-data/",
    "https://evil.example/s.json",
    "file:///etc/passwd",
    "ftp://internalhost/x",
    "//evil.example/x",            # protocol-relative
    "other.json",                  # relative: resolves against the base URI
    "urn:evil:x",
    "https://evil.example/%73chema.json",   # percent-encoded
    "http://\u0065vil.example/x",           # unicode-escaped (decodes to http)
]


@pytest.mark.parametrize("ref", REMOTE_REFS)
def test_check_schema_rejects_remote_ref(ref):
    err = check_schema({"$ref": ref})
    assert isinstance(err, str) and err, f"check_schema accepted remote $ref {ref!r}"


@pytest.mark.parametrize("key", ["$dynamicRef", "$recursiveRef"])
def test_check_schema_rejects_remote_dynamic_and_recursive_refs(key):
    err = check_schema({key: "https://evil.example/s.json"})
    assert isinstance(err, str) and err


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"a": {"$ref": "file:///etc/passwd"}}},
    {"type": "array", "items": {"$ref": "http://169.254.169.254/"}},
    {"allOf": [{"type": "object"}, {"$ref": "https://evil.example/x"}]},
    {"$defs": {"x": {"$ref": "http://evil.example/"}}, "type": "object"},
])
def test_check_schema_rejects_nested_remote_ref(schema):
    err = check_schema(schema)
    assert isinstance(err, str) and err


@pytest.mark.parametrize("ref", [
    "http://169.254.169.254/latest/meta-data/",
    "https://evil.example/s.json",
    "file:///etc/passwd",
    "ftp://internalhost/x",
])
def test_validate_args_never_fetches_remote_ref(no_fetch, ref):
    err = validate_args({"$ref": ref}, {})
    assert isinstance(err, str) and err          # fail closed: call is refused
    assert no_fetch == [], f"gateway fetched {no_fetch} for $ref {ref!r}"


@pytest.mark.parametrize("key", ["$dynamicRef", "$recursiveRef"])
def test_validate_args_never_fetches_dynamic_ref(no_fetch, key):
    err = validate_args({key: "http://169.254.169.254/x"}, {})
    assert isinstance(err, str) and err
    assert no_fetch == []


def test_local_fragment_refs_still_validate(no_fetch):
    pointer = {"type": "object",
               "properties": {"n": {"$ref": "#/$defs/pos"}},
               "$defs": {"pos": {"type": "integer", "minimum": 1}}}
    anchor = {"type": "object",
              "properties": {"n": {"$ref": "#pos"}},
              "$defs": {"pos": {"$anchor": "pos", "type": "integer", "minimum": 1}}}
    nested = {"type": "array",
              "items": {"$ref": "#/$defs/item"},
              "$defs": {"item": {"type": "object", "required": ["id"],
                                 "properties": {"id": {"type": "string"}}}}}
    for schema in (pointer, anchor, nested):
        assert check_schema(schema) is None
    assert validate_args(pointer, {"n": 3}) is None
    assert isinstance(validate_args(pointer, {"n": 0}), str)
    assert validate_args(anchor, {"n": 3}) is None
    assert isinstance(validate_args(anchor, {"n": -1}), str)
    assert validate_args(nested, [{"id": "a"}]) is None
    assert isinstance(validate_args(nested, [{"id": 1}]), str)
    assert isinstance(validate_args(nested, [{}]), str)
    assert no_fetch == []


def test_fragment_lookalike_ref_never_fetches(no_fetch):
    # '#' + a remote-looking string is an anchor name, not a URL: local-only.
    err = validate_args({"$ref": "#http://169.254.169.254/"}, {})
    assert isinstance(err, str) and err
    assert no_fetch == []


def test_check_schema_fails_closed_and_loud_without_jsonschema(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    with caplog.at_level(logging.WARNING, logger="hermes.plugins.outrigger"):
        for schema in ({"type": 12}, {"type": "object"}, {}):
            err = check_schema(schema)
            assert isinstance(err, str) and err, "check_schema passed with jsonschema absent"
            assert "jsonschema" in err
    assert any(r.levelno >= logging.WARNING and "jsonschema" in r.getMessage()
               for r in caplog.records), "missing jsonschema was not logged loudly"


def test_declaration_refused_without_jsonschema(monkeypatch):
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    with pytest.raises(ProtocolError):
        ToolDecl.parse({"name": "t", "parameters": {"type": "object"}})


@pytest.mark.parametrize("value", ["anything", 123, {}, None, True])
def test_minimal_validate_rejects_unknown_type(value):
    err = protocol._minimal_validate({"type": "str"}, value)
    assert isinstance(err, str) and err


@pytest.mark.parametrize("types,value", [
    (["str", "string"], "x"),   # unknown first: must not pass everything
    (["string", "str"], 5),     # unknown last: must not rescue a mismatch
])
def test_minimal_validate_rejects_unknown_type_in_list(types, value):
    err = protocol._minimal_validate({"type": types}, value)
    assert isinstance(err, str) and err


def test_minimal_validate_known_types_still_work():
    assert protocol._minimal_validate({"type": "string"}, "x") is None
    assert protocol._minimal_validate({"type": ["string", "null"]}, None) is None
    assert isinstance(protocol._minimal_validate({"type": "integer"}, "1"), str)


def test_validate_args_fallback_rejects_unknown_type(monkeypatch):
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    err = validate_args({"type": "str"}, 123)
    assert isinstance(err, str) and err


def test_existing_hardening_preserved():
    # ReDoS tradeoff kept: pattern is still not enforced on the gateway ...
    schema = {"type": "object", "properties": {"q": {"type": "string", "pattern": "^(a+)+$"}}}
    assert check_schema(schema) is None
    assert validate_args(schema, {"q": "a" * 40 + "!"}) is None
    assert isinstance(validate_args(schema, {"q": 5}), str)   # types still enforced
    # ... but patternProperties, depth and size limits still reject.
    assert "patternProperties" in check_schema({"patternProperties": {"(a+)+$": {}}})
    deep = {"type": "object"}
    for _ in range(30):
        deep = {"type": "object", "properties": {"a": deep}}
    assert "deeper" in check_schema(deep)
    assert "larger" in check_schema({"type": "object", "description": "x" * 40_000})
