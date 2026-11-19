"""F-09 audit hardening: fail-loud write failures, stable principal in entries,
0600 plugin artifacts, and an audit entry for malformed outrigger_call requests.

State-neutral: the module-level failure counter is saved/reset/restored per test.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from outrigger import audit, dispatch, notebook, reviewer, settings, tokens
from outrigger.protocol import parse_tools
from outrigger.registry import REGISTRY, Client

from helpers import TOOLS, call, connect, serve_one, start

LOG = "hermes.plugins.outrigger"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def _neutral_audit_counter():
    saved = getattr(audit, "_write_failures", 0)
    audit._write_failures = 0
    yield
    audit._write_failures = saved


def _audit_path(tmp_path):
    return tmp_path / "plugin-data" / "outrigger" / "audit.jsonl"


def _entries(tmp_path):
    p = _audit_path(tmp_path)
    assert p.exists(), "expected an audit entry to be written"
    return [json.loads(line) for line in p.read_text().splitlines()]


def _break_audit(tmp_path):
    """audit.jsonl as a directory: every append raises IsADirectoryError (an OSError),
    without monkeypatching any internal — the real write path fails."""
    p = _audit_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.mkdir()
    return p


def _loud(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith(LOG)]


# ---------------------------------------------------------------- (a) fail loud


def test_audit_write_failure_is_loud_and_counted(tmp_path, caplog):
    p = _break_audit(tmp_path)
    caplog.set_level(logging.WARNING, logger=LOG)
    for i in range(3):
        audit.record(session_id="S1", client="c1", tool="echo", args={"text": "x"},
                     policy="auto", outcome="ok", duration_s=0.01)
        assert audit._write_failures == i + 1, "each failed write must increment the counter"
        loud = _loud(caplog)
        assert len(loud) >= i + 1, "each failed write must log a loud warning/error"
        msg = loud[-1].getMessage()
        assert str(p) in msg, "the loud log must name the audit path"
        assert "Is a directory" in msg, "the loud log must carry the failure reason"


async def test_tool_call_succeeds_when_audit_write_fails(aiohttp_client, tmp_path, caplog):
    p = _break_audit(tmp_path)
    caplog.set_level(logging.WARNING, logger=LOG)
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "HI"}))
    out = await call({"tool": "echo", "args": {"text": "hi"}})
    assert out["ok"] and out["content"] == "HI", "auditing failure must never break a tool call"
    assert audit._write_failures >= 1
    assert any(str(p) in r.getMessage() for r in _loud(caplog))


def test_rejection_survives_audit_failure(tmp_path, caplog):
    _break_audit(tmp_path)
    caplog.set_level(logging.WARNING, logger=LOG)
    out = json.loads(dispatch.outrigger_call({"tool": "echo", "args": {"text": "x"}},
                                             session_id="nobody"))
    assert out["error"] == "outrigger_no_client", "rejection must still be returned"
    assert audit._write_failures == 1
    assert _loud(caplog)


# ---------------------------------------------------------------- (b) stable identity


async def test_owner_principal_in_audit_entry(aiohttp_client, tmp_path):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "HI"}))
    out = await call({"tool": "echo", "args": {"text": "hi"}})
    assert out["ok"]
    entry = _entries(tmp_path)[-1]
    assert "principal" in entry, "audit entry must carry the caller's stable principal"
    assert entry["principal"]["kind"] == "owner"


def test_token_principal_in_audit_entry(tmp_path):
    tid, tok = tokens.mint("ann")
    rec = tokens.verify(tok)
    assert rec is not None
    # Exactly the principal ws._register builds for a paired token; the client serves
    # session S1 and answers calls in-process (no socket needed for the audit path).
    client = Client(name="sb", label="phone", kind="browser", version="1", ws=None, loop=None,
                    session_id="S1", tools=parse_tools(TOOLS), principal={"kind": "token", **rec})
    REGISTRY.add(client)
    client.send = lambda msg, timeout=5.0: REGISTRY.resolve(
        client, msg["call_id"], {"ok": True, "content": "ok"})
    out = json.loads(dispatch.outrigger_call({"tool": "echo", "args": {"text": "x"}}, session_id="S1"))
    assert out["ok"]
    entry = _entries(tmp_path)[-1]
    assert "principal" in entry, "audit entry must carry the caller's stable principal"
    assert entry["principal"]["kind"] == "token"
    assert entry["principal"]["user_id"] == "ann"
    assert entry["principal"]["id"] == tid
    assert entry["client"] == client.id


async def test_unknown_tool_rejection_carries_principal(aiohttp_client, tmp_path):
    c = await start(aiohttp_client)
    await connect(c)
    out = await call({"tool": "nope", "args": {}})
    assert out["error"] == "outrigger_unknown_tool"
    entry = _entries(tmp_path)[-1]
    assert entry["outcome"] == "rejected:outrigger_unknown_tool"
    assert "principal" in entry and entry["principal"]["kind"] == "owner"


async def test_review_denied_carries_principal(aiohttp_client, tmp_path):
    settings.SETTINGS["review_deny"] = "block"
    reviewer._llm = SimpleNamespace(complete_structured=lambda **kw: SimpleNamespace(
        parsed={"verdict": "deny", "reason": "not asked for"}, text="{}"))
    c = await start(aiohttp_client)
    await connect(c, tools=[{"name": "click", "policy": "confirm", "description": "Click",
                             "parameters": {"type": "object",
                                            "properties": {"selector": {"type": "string"}}}}])
    out = await call({"tool": "click", "args": {"selector": "#go"}})
    assert out["error"] == "outrigger_review_denied"
    entry = _entries(tmp_path)[-1]
    assert entry["outcome"] == "rejected:outrigger_review_denied"
    assert "principal" in entry and entry["principal"]["kind"] == "owner"
    assert entry["review"]["verdict"] == "deny"


# ---------------------------------------------------------------- (c) 0600 artifacts


def test_audit_file_created_0600(tmp_path):
    audit.record(session_id="S1", client="c1", tool="echo", args={}, policy="auto",
                 outcome="ok", duration_s=0.0)
    p = _audit_path(tmp_path)
    assert p.exists()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_capture_created_0600(tmp_path):
    out = dispatch._save_capture({"mime": "image/png", "data": base64.b64encode(PNG).decode()})
    assert "image_path" in out, out
    p = Path(out["image_path"])
    assert p.read_bytes() == PNG
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_snapshot_created_0600(tmp_path):
    doc = notebook.Doc(doc_id="notes:Garden.md", root="notes", path="Garden.md",
                       full=tmp_path / "Garden.md", text="# Garden\nprivate note")
    sid = notebook.snapshot(doc)
    snap = notebook._snap_dir(doc) / f"{sid}.md"
    assert snap.read_text() == doc.text
    assert stat.S_IMODE(snap.stat().st_mode) == 0o600


def test_note_file_mode_preserved(tmp_path):
    """Regression guard: notebook._write keeps the user's note-file mode (not plugin-owned)."""
    f = tmp_path / "note.md"
    f.write_text("old")
    os.chmod(f, 0o640)
    doc = notebook.Doc(doc_id="d", root="r", path="note.md", full=f, text="old")
    notebook._write(doc, "new")
    assert f.read_text() == "new"
    assert stat.S_IMODE(f.stat().st_mode) == 0o640


# ---------------------------------------------------------------- (d) malformed audited


@pytest.mark.parametrize("bad", [
    {"tool": 123},
    {"tool": "echo", "args": ["x"]},
    {"tool": "echo", "args": "x"},
    {},
])
def test_malformed_request_is_audited(tmp_path, bad):
    out = json.loads(dispatch.outrigger_call(bad, session_id="S1"))
    assert out["error"] == "outrigger_bad_request"
    entries = _entries(tmp_path)
    assert entries[-1]["outcome"] == "rejected:outrigger_bad_request"
    assert entries[-1]["client"] is None
    assert entries[-1]["session"] == "S1"


# ---------------------------------------------------------------- (e) normal auditing


async def test_normal_auditing_still_works(aiohttp_client, tmp_path):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "HI"}))
    out = await call({"tool": "echo", "args": {"text": "hi"}})
    assert out["ok"]
    entry = _entries(tmp_path)[-1]
    assert entry["session"] == "S1" and entry["tool"] == "echo"
    assert entry["outcome"] == "ok" and entry["policy"] == "auto"
    assert entry["args_sha256"] == hashlib.sha256(
        json.dumps({"text": "hi"}, sort_keys=True).encode()).hexdigest()
    assert "args" not in entry and "hi" not in json.dumps(entry)
    assert audit._write_failures == 0
    settings.SETTINGS["audit_args"] = True
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "HI"}))
    await call({"tool": "echo", "args": {"text": "raw"}})
    assert _entries(tmp_path)[-1]["args"] == {"text": "raw"}
