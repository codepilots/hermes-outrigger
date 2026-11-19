"""Cohesion restructure (step 2): the security surface lives in named units.

These assertions pin the *shape* of the refactor, not behavior:
- every moved name stays resolvable on its original module AND is the identical
  object on the new module (re-export identity, so existing callers/tests keep
  working and `ws.authenticate is auth.authenticate`);
- the dependency direction is one-way (auth does not import ws; notebook does not
  import notebook_tools at load; audit does not import dispatch);
- every security-sensitive module carries the inline AUDIT.md hub pointer.

State-neutral: reads source and compares object identity only; no global mutation.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import outrigger
from outrigger import audit, auth, dispatch, notebook, notebook_tools, ws

PKG = Path(outrigger.__file__).parent

# (original module, new module, names that must be identical re-exports)
REEXPORTS = [
    (ws, auth, ["_expected_key", "_presented_key", "_is_loopback", "PROXY_HEADERS",
                "_is_local", "_peer", "OWNER", "authenticate", "authorize", "_limited"]),
    (notebook, notebook_tools, ["doc_for_tool", "tool_read", "tool_edit", "apply_edits",
                                "READ_SCHEMA", "EDIT_SCHEMA"]),
    (dispatch, audit, ["_save_capture"]),
]

SECURITY_MODULES = ["auth", "notebook", "notebook_tools", "protocol", "reviewer",
                    "tokens", "audit", "registry", "settings", "dispatch", "platform", "ws"]


def _source(name: str) -> str:
    return (PKG / f"{name}.py").read_text(encoding="utf-8")


def _toplevel_imports(name: str) -> list[str]:
    """Module names imported at load time (top-level Import/ImportFrom only)."""
    tree = ast.parse(_source(name))
    out = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            out.append(mod)
            out += [f"{mod}.{a.name}" for a in node.names]
    return out


def test_reexport_identity():
    for original, new, names in REEXPORTS:
        for n in names:
            assert hasattr(original, n), f"{original.__name__}.{n} no longer resolvable"
            assert hasattr(new, n), f"{new.__name__}.{n} missing"
            assert getattr(original, n) is getattr(new, n), \
                f"{original.__name__}.{n} is not {new.__name__}.{n}"


def test_auth_does_not_import_ws():
    assert not any(m == "ws" or m.endswith(".ws") for m in _toplevel_imports("auth"))


def test_notebook_does_not_import_notebook_tools_at_load():
    assert not any("notebook_tools" in m for m in _toplevel_imports("notebook"))


def test_audit_does_not_import_dispatch():
    assert not any(m == "dispatch" or m.endswith(".dispatch") for m in _toplevel_imports("audit"))


def test_notebook_tools_uses_module_access_not_from_imports():
    src = _source("notebook_tools")
    assert "from . import notebook" in src
    # shared state/helpers a test monkeypatches must be reached via notebook.<name>,
    # never bound by a from-import (which would silently defeat the patch).
    assert "from .notebook import" not in src


def test_security_modules_carry_audit_pointer():
    for name in SECURITY_MODULES:
        lines = _source(name).splitlines()
        assert any(ln.startswith("# Audit: see AUDIT.md#") for ln in lines), \
            f"{name}.py lacks the inline AUDIT.md hub pointer"


def test_tool_handlers_are_the_same_callable():
    assert notebook.tool_edit is notebook_tools.tool_edit
    assert notebook.tool_read is notebook_tools.tool_read
    assert inspect.isfunction(notebook_tools.tool_edit)
