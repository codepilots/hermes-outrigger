"""F-04: notebook watcher must fail loud, never silently die; tool_edit must refresh
from disk before comparing versions.

Reuses the vault setup from test_notebook.py. WATCH_INTERVAL is shrunk so several
real watcher ticks run inside a test; no per-tick helper API is assumed.
"""
import asyncio
import json
import logging
import os
import time

import pytest

from outrigger import notebook, settings

from test_notebook import vault  # noqa: F401 (fixture)

LOG = "hermes.plugins.outrigger"


class FakeClient:
    """Minimal ws-client stand-in: exactly what broadcast()/who() touch."""

    def __init__(self, loop, cid="fake"):
        self.id = cid
        self.name = cid
        self.label = cid
        self.principal = {"kind": "owner"}
        self.connected_at = 0.0
        self.closed = False
        self.loop = loop
        self.sent = []
        self.ws = self

    async def send_json(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def fast_watcher(monkeypatch):
    monkeypatch.setattr(notebook, "WATCH_INTERVAL", 0.01)
    notebook._watch_task = None
    yield
    t = notebook._watch_task
    notebook._watch_task = None
    if t is not None and not t.done():
        t.cancel()


def _age_lock(doc, client):
    doc.lock = {"by": "hermes", "who": {}, "instruction": "x", "snapshot": None,
                "line": None, "selection": None, "started": time.time() - 100_000,
                "client": client, "session_id": None, "chat_key": None}


def _external_write(vault, text):
    p = vault / "Projects/Garden.md"
    p.write_text(text)
    os.utime(p, ns=(1, time.time_ns() + 10**9))


def _external_write_bytes(vault, data):
    p = vault / "Projects/Garden.md"
    p.write_bytes(data)
    os.utime(p, ns=(1, time.time_ns() + 10**9))


# ---------------------------------------------------------------- watcher survives bad disk


async def test_watcher_survives_non_utf8_write(vault, caplog):
    loop = asyncio.get_running_loop()
    c = FakeClient(loop)
    doc = notebook.open_doc("notes", "Projects/Garden.md", c)
    notebook.ensure_watcher()
    task = notebook._watch_task
    _external_write_bytes(vault, b"\xff\xfe not utf-8 \x80")
    with caplog.at_level(logging.WARNING, logger=LOG):
        await asyncio.sleep(0.08)
        assert not task.done(), "watcher died on a non-UTF-8 external write"
        # lock expiry still runs on later ticks while the note is unreadable
        _age_lock(doc, c)
        await asyncio.sleep(0.08)
        assert doc.lock is None, "locks stopped expiring after the malformed write"
        # a later valid external save is still detected and broadcast
        _external_write(vault, "back to utf-8")
        await asyncio.sleep(0.08)
    assert not task.done()
    assert any(m["type"] == "doc.changed" and m["text"] == "back to utf-8" for m in c.sent)
    assert any(r.levelno >= logging.WARNING for r in caplog.records), "failure was not logged loud"


async def test_bad_doc_does_not_starve_other_docs(vault, monkeypatch, caplog):
    loop = asyncio.get_running_loop()
    c = FakeClient(loop)
    (vault / "Other.md").write_text("# Other\n")
    a = notebook.open_doc("notes", "Projects/Garden.md", c)
    b = notebook.open_doc("notes", "Other.md", c)
    real_read = notebook._read

    def bad_read(doc):
        if doc.doc_id == a.doc_id:
            raise RuntimeError("disk on fire")
        real_read(doc)

    monkeypatch.setattr(notebook, "_read", bad_read)
    notebook.ensure_watcher()
    task = notebook._watch_task
    _external_write_bytes(vault, b"\xff\xfe")  # make a's stat change so _read is attempted
    (vault / "Other.md").write_text("# Other\n\nchanged outside")
    os.utime(vault / "Other.md", ns=(1, time.time_ns() + 10**9))
    with caplog.at_level(logging.WARNING, logger=LOG):
        await asyncio.sleep(0.08)
    assert not task.done(), "one unreadable doc killed the whole watcher"
    assert any(m["type"] == "doc.changed" and m["doc_id"] == b.doc_id for m in c.sent)
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------- lock timeout coercion


@pytest.mark.parametrize("bad", [None, "abc", ""])
def test_bad_lock_timeout_falls_back(vault, bad, caplog):
    c = object()
    doc = notebook.open_doc("notes", "Projects/Garden.md", c)
    doc.clients = set()  # expiry must not need a live client
    settings.SETTINGS["notebook_lock_timeout_s"] = bad
    _age_lock(doc, c)
    with caplog.at_level(logging.WARNING, logger=LOG):
        notebook.expire_locks()  # must not raise TypeError/ValueError
    assert doc.lock is None, "lock did not expire with the fallback timeout"
    assert any("notebook_lock_timeout_s" in r.message for r in caplog.records), "fallback was not logged loud"


async def test_watcher_survives_null_timeout(vault, caplog):
    loop = asyncio.get_running_loop()
    c = FakeClient(loop)
    doc = notebook.open_doc("notes", "Projects/Garden.md", c)
    settings.SETTINGS["notebook_lock_timeout_s"] = None
    _age_lock(doc, c)
    notebook.ensure_watcher()
    task = notebook._watch_task
    with caplog.at_level(logging.WARNING, logger=LOG):
        await asyncio.sleep(0.08)
    assert not task.done(), "null timeout killed the watcher"
    assert doc.lock is None
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------- closed client loop


async def test_closed_client_loop_does_not_kill_watcher(vault, caplog):
    loop = asyncio.get_running_loop()
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    good = FakeClient(loop, "good")
    bad = FakeClient(dead_loop, "bad")
    doc = notebook.open_doc("notes", "Projects/Garden.md", good)
    doc.clients.add(bad)
    notebook.ensure_watcher()
    task = notebook._watch_task
    _external_write(vault, "external save")
    with caplog.at_level(logging.WARNING, logger=LOG):
        await asyncio.sleep(0.08)
    assert not task.done(), "broadcast to a closed loop killed the watcher"
    # the healthy client must still receive the change even though the bad one fails
    assert any(m["type"] == "doc.changed" and m["text"] == "external save" for m in good.sent)
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_broadcast_to_closed_loop_is_loud_not_fatal(vault, caplog):
    loop = asyncio.get_running_loop()
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    good = FakeClient(loop, "good")
    bad = FakeClient(dead_loop, "bad")
    doc = notebook.open_doc("notes", "Projects/Garden.md", good)
    doc.clients.add(bad)
    with caplog.at_level(logging.WARNING, logger=LOG):
        notebook.broadcast(doc, {"type": "doc.changed", "doc_id": doc.doc_id})  # must not raise
        await asyncio.sleep(0.02)
    assert any(m["type"] == "doc.changed" for m in good.sent)
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------- supervision / restart


async def test_watcher_restarts_after_forced_crash(vault, monkeypatch, caplog):
    loop = asyncio.get_running_loop()
    c = FakeClient(loop)
    doc = notebook.open_doc("notes", "Projects/Garden.md", c)

    class _Crash(BaseException):  # escapes any `except Exception` guard: the task really dies
        pass

    real_expire = notebook.expire_locks
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Crash("forced watcher death")
        real_expire()

    monkeypatch.setattr(notebook, "expire_locks", boom)
    notebook.ensure_watcher()
    first = notebook._watch_task
    with caplog.at_level(logging.WARNING, logger=LOG):
        await asyncio.sleep(0.1)
        assert first.done(), "forced crash did not kill the first task (test setup broken)"
        assert notebook._watch_task is not first, "dead watcher was not restarted without a new doc.open"
        assert not notebook._watch_task.done()
        assert any("watcher" in r.message.lower() for r in caplog.records), "restart was not logged loud"
        # the revived watcher still does its job
        _age_lock(doc, c)
        await asyncio.sleep(0.08)
    assert doc.lock is None


# ---------------------------------------------------------------- tool_edit disk refresh


def _locked_doc(vault, monkeypatch, c):
    doc = notebook.open_doc("notes", "Projects/Garden.md", c)
    notebook.start_command(doc, c, doc.version, None, "tidy", selection=(1, 1))
    monkeypatch.setattr(notebook, "_session_chat", lambda: "chat-1")
    notebook.NOTE_CHATS["chat-1"] = doc.doc_id
    return doc


async def test_tool_edit_detects_external_save(vault, monkeypatch):
    c = FakeClient(asyncio.get_running_loop())
    doc = _locked_doc(vault, monkeypatch, c)
    v = doc.version
    _external_write(vault, "external save\n")  # within the watcher interval: no tick has run
    r = json.loads(notebook.tool_edit(
        {"version": v, "edits": [{"from_line": 1, "to_line": 1, "text": "clobber"}]}, session_id="s-1"))
    assert r["ok"] is False, "tool_edit clobbered an external save made within the watcher interval"
    assert r["error"] in ("stale_version", "conflict")
    assert (vault / "Projects/Garden.md").read_text() == "external save\n"


async def test_tool_edit_unreadable_disk_fails_closed(vault, monkeypatch):
    c = FakeClient(asyncio.get_running_loop())
    doc = _locked_doc(vault, monkeypatch, c)
    v = doc.version
    _external_write_bytes(vault, b"\xff\xfe not utf-8")
    r = json.loads(notebook.tool_edit(
        {"version": v, "edits": [{"from_line": 1, "to_line": 1, "text": "clobber"}]}, session_id="s-1"))
    assert r["ok"] is False, "tool_edit must refuse (JSON), not raise or clobber, on unreadable disk"
    assert (vault / "Projects/Garden.md").read_bytes() == b"\xff\xfe not utf-8"


# ---------------------------------------------------------------- normal behavior guards


async def test_watcher_normal_expiry_and_broadcast(vault):
    loop = asyncio.get_running_loop()
    c = FakeClient(loop)
    doc = notebook.open_doc("notes", "Projects/Garden.md", c)
    _age_lock(doc, c)
    notebook.ensure_watcher()
    task = notebook._watch_task
    _external_write(vault, "obsidian save")
    await asyncio.sleep(0.08)
    assert not task.done()
    assert doc.lock is None
    assert any(m["type"] == "doc.changed" and m["text"] == "obsidian save" and m["by"] == "disk" for m in c.sent)


async def test_tool_edit_normal_edit_still_works(vault, monkeypatch):
    c = FakeClient(asyncio.get_running_loop())
    doc = _locked_doc(vault, monkeypatch, c)
    v = doc.version
    r = json.loads(notebook.tool_edit(
        {"version": v, "edits": [{"from_line": 1, "to_line": 1, "text": "# Garden (edited)"}]}, session_id="s-1"))
    assert r["ok"] is True
    assert (vault / "Projects/Garden.md").read_text().startswith("# Garden (edited)")
    assert r["version"] == doc.version != v
    stale = json.loads(notebook.tool_edit(
        {"version": v, "edits": [{"from_line": 1, "to_line": 1, "text": "x"}]}, session_id="s-1"))
    assert stale["ok"] is False and stale["error"] == "stale_version"
    await asyncio.sleep(0)  # let the broadcast task run
