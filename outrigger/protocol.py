"""Wire messages and validation for the outrigger control channel.

Trust boundary: untrusted client tool declarations / args -> gateway. Validates
tool names (\\Z-anchored regex), policies, and JSON Schemas (small, shallow, no
patternProperties, local-only refs — the gateway never fetches a client-chosen
URI, an SSRF backstop), and validates model args against the declared schema.
jsonschema ImportError fails closed (loud) for declarations; args fall back to a
minimal validator that still refuses non-local refs. Deliberately does NOT:
authenticate peers (auth.py) or run tools; it only accepts/rejects declarations
and args. `pattern` is dropped gateway-side (ReDoS) and left to the client.
"""
# Audit: see AUDIT.md#security-sensitive-units (input validation)
from __future__ import annotations

import json
import logging

import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("hermes.plugins.outrigger")

PROTOCOL_VERSION = 2
SUPPORTED_PROTOCOLS = {1, 2}  # 2 adds the Outrigger platform's chat.* messages
POLICIES = ("auto", "confirm", "deny_when_unattended")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ToolDecl:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    policy: str = "confirm"  # safe default: ask the human
    timeout_s: int | None = None
    max_result_chars: int | None = None

    @classmethod
    def parse(cls, raw: Any) -> "ToolDecl":
        """Actor: untrusted client hello/tools.update. Input: one raw tool
        declaration. Control: \\Z-anchored name regex, policy in POLICIES
        (default confirm — ask the human), parameters via check_schema,
        positive-int timeout/result caps (bools refused), description capped
        500. Sink: an accepted ToolDecl in the client catalog. Any failure ->
        ProtocolError bad_tool; the declaration is refused."""
        if not isinstance(raw, dict):
            raise ProtocolError("bad_tool", "tool declaration must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ProtocolError("bad_tool", f"invalid tool name: {name!r}")
        policy = raw.get("policy", "confirm")
        if policy not in POLICIES:
            raise ProtocolError("bad_tool", f"invalid policy for {name}: {policy!r}")
        params = raw.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(params, dict):
            raise ProtocolError("bad_tool", f"parameters for {name} must be an object")
        problem = check_schema(params)
        if problem:
            raise ProtocolError("bad_tool", f"parameters for {name}: {problem}")
        for key in ("timeout_s", "max_result_chars"):
            v = raw.get(key)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v <= 0):
                raise ProtocolError("bad_tool", f"{key} for {name} must be a positive integer")
        return cls(
            name=name,
            description=str(raw.get("description", ""))[:500],
            parameters=params,
            policy=policy,
            timeout_s=raw.get("timeout_s"),
            max_result_chars=raw.get("max_result_chars"),
        )


def parse_tools(raw: Any, limit: int = 64) -> dict[str, ToolDecl]:
    """The client's whole tool catalog: a list of <=64 declarations, each via
    ToolDecl.parse; duplicate names collapse last-wins. None -> {}. Refusals:
    ProtocolError bad_tool (not a list / too many / bad member)."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ProtocolError("bad_tool", "tools must be a list")
    if len(raw) > limit:
        raise ProtocolError("bad_tool", f"too many tools (max {limit})")
    out: dict[str, ToolDecl] = {}
    for item in raw:
        decl = ToolDecl.parse(item)
        out[decl.name] = decl
    return out


MAX_SCHEMA_BYTES = 32_768
MAX_SCHEMA_DEPTH = 24


def _depth(obj: Any) -> int:
    """Nesting depth of a JSON value, without recursion."""
    deepest, stack = 0, [(obj, 1)]
    while stack:
        o, d = stack.pop()
        deepest = max(deepest, d)
        if d > MAX_SCHEMA_DEPTH:
            return d
        if isinstance(o, dict):
            stack.extend((v, d + 1) for v in o.values())
        elif isinstance(o, list):
            stack.extend((v, d + 1) for v in o)
    return deepest


_REF_KEYS = ("$ref", "$dynamicRef", "$recursiveRef")


def _remote_ref(schema: Any) -> str | None:
    """First reference target that is not a local '#' fragment, or None. The gateway
    must never fetch a client-chosen URI (SSRF / local-file read), so hostile schemas
    are refused at declaration; the validator registry refuses retrieval as a backstop.
    Every dict key is scanned: over-rejecting ref-like data (const/enum) is fail-closed."""
    stack = [schema]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k, v in o.items():
                if k in _REF_KEYS and isinstance(v, str) and not v.startswith("#"):
                    return v[:100]
                stack.append(v)
        elif isinstance(o, list):
            stack.extend(o)
    return None


def check_schema(schema: dict) -> str | None:
    """Why a client-declared parameter schema is refused, or None. Hermes's arguments are
    validated against it on the gateway, so it must be small, shallow and regex-free where
    a regex would run on the gateway (patternProperties); `pattern` is left to the client.
    References must be local '#' fragments: the gateway never fetches a client-chosen URI.
    Caps: <=32 KiB serialized, depth <=24. jsonschema ImportError -> ERROR log +
    refusal (fail closed; broken install). SchemaError/RecursionError -> refusal string."""
    if _depth(schema) > MAX_SCHEMA_DEPTH:
        return f"nested deeper than {MAX_SCHEMA_DEPTH}"
    try:
        text = json.dumps(schema)
    except (TypeError, ValueError, RecursionError):
        return "not JSON"
    if len(text) > MAX_SCHEMA_BYTES:
        return f"larger than {MAX_SCHEMA_BYTES} bytes"
    if '"patternProperties"' in text:
        return "patternProperties is not supported"
    remote = _remote_ref(schema)
    if remote is not None:
        return (f"non-local reference not allowed: {one_line(remote, 100)!r} "
                "(only '#' fragments resolve on the gateway)")
    try:
        import jsonschema
        meta = jsonschema.Draft202012Validator(
            jsonschema.Draft202012Validator.META_SCHEMA,
            format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
            registry=_local_only_registry(),
        )
        for error in meta.iter_errors(schema):
            raise jsonschema.SchemaError.create_from(error)
    except ImportError:
        log.error("outrigger: jsonschema is not importable; refusing tool schema declarations "
                  "(broken install: the plugin requires jsonschema>=4,<5)")
        return "schema validation unavailable: jsonschema is not installed"
    except jsonschema.SchemaError as e:
        return f"invalid JSON Schema: {e.message[:200]}"
    except RecursionError:
        return "invalid JSON Schema (recursive)"
    return None


_VALIDATOR = None
_REGISTRY = None


def _local_only_registry():
    """Schema registry that refuses ALL retrieval. Local '#' fragment references resolve
    from the root resource already in the registry, so any URI reaching retrieve is
    non-local by definition; jsonschema's default registry would fetch it with urlopen
    (http/https/file/ftp, no scheme allowlist, no timeout) — an SSRF / local-file read.
    Any retrieval attempt is WARNING-logged and refused (NoSuchResource)."""
    global _REGISTRY
    if _REGISTRY is None:
        import referencing

        def _refuse(uri):
            log.warning("outrigger: refused gateway-side retrieval of schema reference %r",
                        one_line(uri, 200))
            raise referencing.exceptions.NoSuchResource(ref=uri)

        _REGISTRY = referencing.Registry(retrieve=_refuse)
    return _REGISTRY


def _validator_cls():
    """Draft 2020-12 without `pattern`: Python's re has no timeout, so a hostile pattern
    (catastrophic backtracking) could pin a gateway thread. The client validates its own
    input; `pattern` still reaches the model in the schema."""
    global _VALIDATOR
    if _VALIDATOR is None:
        from jsonschema import Draft202012Validator, validators
        checks = {k: v for k, v in Draft202012Validator.VALIDATORS.items() if k != "pattern"}
        _VALIDATOR = validators.create(meta_schema=Draft202012Validator.META_SCHEMA, validators=checks,
                                       type_checker=Draft202012Validator.TYPE_CHECKER,
                                       format_checker=None, version="outrigger-2020-12")
    return _VALIDATOR


def validate_args(schema: dict, args: Any) -> str | None:
    """Return an error string, or None if args satisfy the JSON Schema. Actor:
    model args relayed to a client tool (dispatch). Control: non-local ref
    refused even in the no-jsonschema fallback; jsonschema absent ->
    _minimal_validate; SchemaError / any evaluation failure -> error string
    (fail closed, never a pass). Sink: bad args -> outrigger_bad_args, audited
    by dispatch; nothing reaches the client."""
    remote = _remote_ref(schema)
    if remote is not None:
        # Fail closed even for reference keywords this validator does not evaluate
        # (e.g. $recursiveRef) and in the no-jsonschema fallback, which ignores $ref.
        return f"tool schema declares a non-local reference: {one_line(remote, 100)!r}"
    try:
        import jsonschema
    except ImportError:
        return _minimal_validate(schema, args)
    try:
        err = jsonschema.exceptions.best_match(
            _validator_cls()(schema, registry=_local_only_registry()).iter_errors(args))
    except jsonschema.SchemaError as e:
        return f"tool declares an invalid schema: {e.message}"
    except Exception:  # untrusted schema: recursion, unresolvable $ref, anything else
        return "tool schema could not be evaluated (recursive or unresolvable $ref?)"
    return err.message if err is not None else None


_PY_TYPES = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "null": (type(None),),
}


def _type_ok(value: Any, expected) -> bool:
    """Fallback type check: an unknown type keyword -> False (fail closed, never
    pass everything); bools are not integers/numbers."""
    names = expected if isinstance(expected, list) else [expected]
    if any(name not in _PY_TYPES for name in names):
        return False  # unknown type keyword: fail closed, never pass everything
    for name in names:
        types = _PY_TYPES[name]
        if isinstance(value, bool) and name in ("integer", "number"):
            continue
        if isinstance(value, types):
            return True
    return False


def _minimal_validate(schema: dict, args: Any, path: str = "args") -> str | None:
    """Fallback when jsonschema is not installed: type, enum, required,
    properties and additionalProperties: false, recursing into objects and arrays.
    Deliberately partial (no pattern/minimum/$ref evaluation): check_schema already
    refuses declarations without jsonschema, and validate_args refuses non-local
    refs before this runs — defense in depth, not the primary gate."""
    if not isinstance(schema, dict):
        return None
    if "type" in schema and not _type_ok(args, schema["type"]):
        return f"{path} must be of type {schema['type']}"
    if "enum" in schema and args not in schema["enum"]:
        return f"{path} must be one of {schema['enum']}"
    if isinstance(args, dict):
        for key in schema.get("required", []) or []:
            if key not in args:
                return f"missing required argument: {key}"
        props = schema.get("properties", {}) or {}
        if schema.get("additionalProperties") is False:
            extra = sorted(set(args) - set(props))
            if extra:
                return f"unexpected argument(s): {', '.join(extra)}"
        for key, sub in props.items():
            if key in args:
                err = _minimal_validate(sub, args[key], f"{path}.{key}")
                if err:
                    return err
    if isinstance(args, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(args):
            err = _minimal_validate(schema["items"], item, f"{path}[{i}]")
            if err:
                return err
    return None


_WS_RE = re.compile(r"\s+")


def int_field(value: Any, name: str, lo: int, hi: int, default: int | None = None) -> int:
    """A client-supplied integer in [lo, hi], or ProtocolError. Rejects bools, floats,
    strings and out-of-range values (no int("NaN") surprises, no 10**30 line numbers)."""
    if value is None and default is not None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
        raise ProtocolError("bad_field", f"{name} must be an integer {lo}..{hi}")
    return value


def str_field(value: Any, name: str, max_len: int, default: str | None = None) -> str:
    """A client-supplied string of at most max_len characters, or ProtocolError."""
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or len(value) > max_len:
        raise ProtocolError("bad_field", f"{name} must be a string of at most {max_len} characters")
    return value


def one_line(text: Any, limit: int) -> str:
    """Single line, no control characters: safe to splice into model context.
    Actor: untrusted text (client names, log peers, reviewer reasons). Control:
    control chars -> space, whitespace collapsed, hard cap `limit`."""
    text = _CTRL_RE.sub(" ", str(text))
    return _WS_RE.sub(" ", text).strip()[:limit]


def sanitize_text(text: str, limit: int) -> tuple[str, bool]:
    """Strip control characters and cap length. Returns (text, truncated). The
    untrusted-result bound (F-07): dispatch applies it to client results and
    error detail before the model sees them; the ok path surfaces truncated=True
    so a cut result is visible, never silent."""
    text = _CTRL_RE.sub("", text)
    if len(text) > limit:
        return text[:limit], True
    return text, False
