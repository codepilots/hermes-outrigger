"""Notebook mode: roots and path safety, versions, outside changes, // commands, Hermes's tools."""
import asyncio
import json
import os
import time

import pytest

from outrigger import notebook, settings, tokens
from outrigger.notebook import NotebookError

from helpers import start
from test_platform import ALL_CAPS, HANDLED, SESSION_ENV, adapter, next_of  # noqa: F401 (fixture)

CAPS = ALL_CAPS + ("notebook",)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Projects").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "app.md").write_text("hidden")
    (root / "Projects" / "Garden.md").write_text("# Garden\n\nThe quick brwon fox.\n\nTodo list.")
    (root / "notes.txt").write_text("not markdown")
    outside = tmp_path / "secret.md"; outside.write_text("outside")
    (root / "escape.md").symlink_to(outside)
    settings.SETTINGS["notebook_roots"] = {"notes": str(root)}
    notebook.DOCS.clear(); notebook.NOTE_CHATS.clear()
    yield root
    notebook.DOCS.clear(); notebook.NOTE_CHATS.clear()


# ---------------------------------------------------------------- files

@pytest.mark.parametrize("rel,code", [
    ("../secret.md", "bad_path"), ("/etc/passwd.md", "bad_path"), (".obsidian/app.md", "bad_path"),
    ("notes.txt", "bad_path"), ("escape.md", "bad_path"), ("Projects/../../secret.md", "bad_path")])
def test_path_safety(vault, rel, code):
    with pytest.raises(NotebookError) as e:
        notebook.resolve("notes", rel)
    assert e.value.code == code


def test_unknown_root_and_listing(vault):
    with pytest.raises(NotebookError):
        notebook.resolve("other", "x.md")
    names = [n["path"] for n in notebook.listing()]
    assert "Projects/Garden.md" in names and not any(".obsidian" in n for n in names)
    assert [n["path"] for n in notebook.listing(query="gard")] == ["Projects/Garden.md"]


def test_versions_conflicts_and_outside_changes(vault):
    c = object()
    doc = notebook.open_doc("notes", "Projects/Garden.md", c)
    v1 = doc.version
    v2 = notebook.update(doc, v1, doc.text + "\nMore.")
    assert (vault / "Projects/Garden.md").read_text().endswith("More.")
    with pytest.raises(NotebookError) as e:
        notebook.update(doc, v1, "stale")                 # an old version never overwrites
    assert e.value.code == "conflict"
    (vault / "Projects/Garden.md").write_text("changed in Obsidian")
    os.utime(vault / "Projects/Garden.md", ns=(1, time.time_ns() + 10**9))
    assert notebook._check_disk(doc) and doc.text == "changed in Obsidian" and doc.version != v2
    mode = os.stat(vault / "Projects/Garden.md").st_mode


def test_apply_edits():
    t = "a\nb\nc\nd"
    assert notebook.apply_edits(t, [{"from_line": 2, "to_line": 3, "text": "B\nC\nX"}]) == "a\nB\nC\nX\nd"
    assert notebook.apply_edits(t, [{"from_line": 2, "to_line": 2, "text": ""}]) == "a\nc\nd"
    assert notebook.apply_edits(t, [{"from_line": 1, "to_line": 0, "text": "top"}, {"from_line": 5, "to_line": 4, "text": "end"}]) == "top\na\nb\nc\nd\nend"
    with pytest.raises(NotebookError):
        notebook.apply_edits(t, [{"from_line": 1, "to_line": 2, "text": ""}, {"from_line": 2, "to_line": 3, "text": ""}])
    with pytest.raises(NotebookError):
        notebook.apply_edits(t, [{"from_line": 9, "to_line": 9, "text": ""}])


# ---------------------------------------------------------------- over the websocket

async def nb_client(c, caps=CAPS, key=None, name="sb"):
    protos = ("hermes-outrigger.v1",) + (("bearer." + key,) if key else ())
    w = await c.ws_connect("/v1/outrigger/ws", protocols=protos)
    await w.send_json({"type": "hello", "protocol": 2, "client": {"name": name}, "tools": [], "capabilities": list(caps)})
    assert (await w.receive_json())["type"] == "welcome"
    return w


async def test_open_edit_and_other_clients_follow(aiohttp_client, vault):
    c = await start(aiohttp_client)
    a, b = await nb_client(c, name="a"), await nb_client(c, name="b")
    await a.send_json({"type": "doc.list", "query": "garden"})
    assert [n["path"] for n in (await next_of(a, "doc.list"))["notes"]] == ["Projects/Garden.md"]
    for w in (a, b):
        await w.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    doc = await next_of(a, "doc.content"); await next_of(b, "doc.content")
    await a.send_json({"type": "doc.update", "doc_id": doc["doc_id"], "base_version": doc["version"], "text": "# Garden\n\nEdited."})
    saved = await next_of(a, "doc.saved")
    ch = await next_of(b, "doc.changed")
    assert ch["by"] == "other-client" and ch["text"].endswith("Edited.") and ch["version"] == saved["version"]
    await b.send_json({"type": "doc.update", "doc_id": doc["doc_id"], "base_version": doc["version"], "text": "stale"})
    err = await next_of(b, "doc.error")
    assert err["code"] == "conflict" and err["text"].endswith("Edited.")


async def test_gates(aiohttp_client, vault, monkeypatch):
    c = await start(aiohttp_client)
    w = await nb_client(c, caps=("chat",))
    await w.send_json({"type": "doc.list"})
    assert (await next_of(w, "error"))["code"] == "no_notebook_capability"
    _, tok = tokens.mint("ann")
    t = await nb_client(c, key=tok, name="tok")
    await t.send_json({"type": "doc.list"})
    assert (await next_of(t, "error"))["code"] == "not_allowed"         # owner only
    settings.SETTINGS["notebook_roots"] = {}
    w2 = await nb_client(c, name="w2")
    await w2.send_json({"type": "doc.list"})
    assert (await next_of(w2, "error"))["code"] == "notebook_off"


async def test_command_flow(aiohttp_client, vault, adapter):
    await adapter.connect()
    c = await start(aiohttp_client)
    w = await nb_client(c)
    await w.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    doc = await next_of(w, "doc.content")
    text = doc["text"].replace("The quick brwon fox.", "The quick brwon fox.\n//correct my spelling")
    await w.send_json({"type": "doc.update", "doc_id": doc["doc_id"], "base_version": doc["version"], "text": text})
    ver = (await next_of(w, "doc.saved"))["version"]
    await w.send_json({"type": "doc.command", "doc_id": doc["doc_id"], "base_version": ver, "line": 4, "instruction": "correct my spelling"})
    lock = await next_of(w, "doc.lock")
    after = await next_of(w, "doc.content")
    assert lock["locked"] and lock["instruction"] == "correct my spelling" and after["locked"]
    assert "//correct" not in after["text"] and "//correct" not in (vault / "Projects/Garden.md").read_text()
    await asyncio.sleep(0.05)
    ev = HANDLED[-1]
    assert "correct my spelling" in ev.text and "line 4" in ev.text and ev.source["chat_id"].startswith("sb:note-")
    # editing is paused
    await w.send_json({"type": "doc.update", "doc_id": doc["doc_id"], "base_version": after["version"], "text": "x"})
    assert (await next_of(w, "doc.error"))["code"] == "locked"

    # Hermes's turn: tools only in that chat
    loop = asyncio.get_running_loop()
    assert json.loads(notebook.tool_read({}, session_id="s-1"))["error"] == "no_command"     # not that chat
    SESSION_ENV.update({"HERMES_SESSION_PLATFORM": "outrigger", "HERMES_SESSION_CHAT_ID": ev.source["chat_id"]})
    notebook.turn_started("s-1")
    r = json.loads(await loop.run_in_executor(None, lambda: notebook.tool_read({}, session_id="s-1")))
    assert r["ok"] and "3\tThe quick brwon fox." in r["text"]
    bad = json.loads(await loop.run_in_executor(None, lambda: notebook.tool_edit({"version": "old", "edits": [{"from_line": 3, "to_line": 3, "text": "x"}]}, session_id="s-1")))
    assert bad["error"] == "stale_version"
    ok = json.loads(await loop.run_in_executor(None, lambda: notebook.tool_edit(
        {"version": r["version"], "edits": [{"from_line": 3, "to_line": 3, "text": "The quick brown fox."}]}, session_id="s-1")))
    assert ok["ok"]
    live = await next_of(w, "doc.content")
    assert live["by"] == "hermes" and "brown fox" in live["text"] and live["locked"]

    # turn ends: unlocked, snapshot offered, undo restores
    notebook.turn_ended("s-1")
    unlock = await next_of(w, "doc.lock")
    assert unlock["locked"] is False and unlock["snapshot"] == lock["snapshot"]
    final = await next_of(w, "doc.content")
    assert final["by"] == "hermes" and not final["locked"]
    assert json.loads(notebook.tool_edit({"version": "x", "edits": []}, session_id="s-1"))["error"] == "no_command"
    await w.send_json({"type": "doc.undo", "doc_id": doc["doc_id"], "snapshot": lock["snapshot"]})
    restored = await next_of(w, "doc.content")
    assert restored["by"] == "undo" and "//correct my spelling" in restored["text"]


async def test_command_line_must_match(aiohttp_client, vault, adapter):
    await adapter.connect()
    c = await start(aiohttp_client)
    w = await nb_client(c)
    await w.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    doc = await next_of(w, "doc.content")
    await w.send_json({"type": "doc.command", "doc_id": doc["doc_id"], "base_version": doc["version"], "line": 1, "instruction": "x"})
    assert (await next_of(w, "doc.error"))["code"] == "bad_line"
    assert (vault / "Projects/Garden.md").read_text().startswith("# Garden")


def test_lock_timeout(vault):
    c = object()
    doc = notebook.open_doc("notes", "Projects/Garden.md", c)
    doc.text  # noqa
    notebook.DOCS[doc.doc_id].clients = set()
    (vault / "Projects/Garden.md").write_text("x\n//do it")
    notebook._check_disk(doc)
    notebook.start_command(doc, c, doc.version, 2, "do it")
    doc.lock["started"] -= 10_000
    notebook.expire_locks()
    assert doc.lock is None


def test_subagents_cannot_edit(vault):
    from outrigger.registry import REGISTRY
    REGISTRY.mark_subagent("child")
    assert json.loads(notebook.tool_read({}, session_id="child"))["error"] == "subagent"
