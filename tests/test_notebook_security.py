"""F-03: notebook resource exhaustion — open-doc caps + LRU eviction, off-loop bounded
listing, and rate limits on doc.list/doc.open for paired non-admin users.

Reuses the vault/nb_client setup from test_notebook.py. Cap constants are monkeypatched
small (raising=False) so these tests fail on BEHAVIOR before the fix exists, not on a
missing attribute.
"""
import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest

from outrigger import notebook, settings, tokens
from outrigger.notebook import NotebookError

from helpers import start
from test_notebook import CAPS, nb_client, vault  # noqa: F401 (fixture)
from test_platform import next_of

LOG = "hermes.plugins.outrigger"


def _mknotes(root, names):
    for n in names:
        (root / f"{n}.md").write_text(f"# {n}\n")


# ---------------------------------------------------------------- open-doc caps

def test_per_client_open_cap(vault, monkeypatch, caplog):
    monkeypatch.setattr(notebook, "MAX_DOCS_PER_CLIENT", 3, raising=False)
    monkeypatch.setattr(notebook, "MAX_OPEN_DOCS", 100, raising=False)
    _mknotes(vault, [f"n{i}" for i in range(10)])
    c = object()
    docs = [notebook.open_doc("notes", f"n{i}.md", c) for i in range(3)]
    with caplog.at_level(logging.WARNING, logger=LOG):
        with pytest.raises(NotebookError) as e:
            notebook.open_doc("notes", "n3.md", c)
    assert e.value.code == "too_many_docs"
    assert len(notebook.DOCS) == 3
    assert sum(1 for d in notebook.DOCS.values() if c in d.clients) == 3
    # re-opening a note the client already holds is not a new open: still allowed at cap
    assert notebook.open_doc("notes", "n0.md", c) is docs[0]
    assert any("refus" in r.message.lower() or "too many" in r.message.lower() for r in caplog.records)


def test_global_open_cap(vault, monkeypatch, caplog):
    monkeypatch.setattr(notebook, "MAX_OPEN_DOCS", 5, raising=False)
    monkeypatch.setattr(notebook, "MAX_DOCS_PER_CLIENT", 100, raising=False)
    _mknotes(vault, [f"g{i}" for i in range(10)])
    clients = [object() for _ in range(6)]
    held = [notebook.open_doc("notes", f"g{i}.md", clients[i]) for i in range(5)]
    with caplog.at_level(logging.WARNING, logger=LOG):
        with pytest.raises(NotebookError) as e:
            notebook.open_doc("notes", "g5.md", clients[5])
    assert e.value.code == "too_many_docs"
    assert len(notebook.DOCS) == 5
    for i, d in enumerate(held):                      # nothing evicted out from under holders
        assert d.doc_id in notebook.DOCS and clients[i] in d.clients
    # an existing doc can still be re-opened at the global cap (no new retention)
    assert notebook.open_doc("notes", "g0.md", clients[0]) is held[0]
    assert any("refus" in r.message.lower() or "too many" in r.message.lower() for r in caplog.records)


def test_cap_evicts_lru_idle_and_spares_held_and_locked(vault, monkeypatch, caplog):
    monkeypatch.setattr(notebook, "MAX_OPEN_DOCS", 6, raising=False)
    monkeypatch.setattr(notebook, "MAX_DOCS_PER_CLIENT", 100, raising=False)
    _mknotes(vault, ["held", "locked", "idle1", "idle2", "fill1", "fill2", "new1", "new2", "new3"])
    A, D = object(), object()
    H = notebook.open_doc("notes", "held.md", A)
    K = notebook.open_doc("notes", "locked.md", D)
    K.lock = {"by": "hermes", "who": {}, "instruction": "x", "snapshot": None, "line": None,
              "selection": None, "started": int(time.time()), "client": D, "session_id": None, "chat_key": None}
    K.clients.clear()                                 # locked, no clients: must survive eviction
    I1 = notebook.open_doc("notes", "idle1.md", object()); I1.clients.clear()
    I2 = notebook.open_doc("notes", "idle2.md", object()); I2.clients.clear()
    notebook.open_doc("notes", "fill1.md", object())
    notebook.open_doc("notes", "fill2.md", object())
    assert len(notebook.DOCS) == 6                    # exactly at the cap
    with caplog.at_level(logging.WARNING, logger=LOG):
        notebook.open_doc("notes", "new1.md", object())   # pressure: evict the LRU idle note
        assert I1.doc_id not in notebook.DOCS             # oldest idle goes first
        assert I2.doc_id in notebook.DOCS                 # newer idle kept
        assert H.doc_id in notebook.DOCS and A in H.clients
        assert K.doc_id in notebook.DOCS and K.lock is not None
        assert len(notebook.DOCS) <= 6
        notebook.open_doc("notes", "new2.md", object())   # next idle evicted
        assert I2.doc_id not in notebook.DOCS
        with pytest.raises(NotebookError) as e:           # nothing evictable left: refuse
            notebook.open_doc("notes", "new3.md", object())
        assert e.value.code == "too_many_docs"
        assert len(notebook.DOCS) == 6
        assert H.doc_id in notebook.DOCS and A in H.clients and K.doc_id in notebook.DOCS
        assert notebook.get(H.doc_id, A) is H             # the held note is still usable
    assert any("evict" in r.message.lower() for r in caplog.records)
    assert any("refus" in r.message.lower() or "too many" in r.message.lower() for r in caplog.records)


async def test_open_cap_sends_doc_error_frame(aiohttp_client, vault, monkeypatch):
    monkeypatch.setattr(notebook, "MAX_DOCS_PER_CLIENT", 2, raising=False)
    _mknotes(vault, ["b", "c"])
    c = await start(aiohttp_client)
    w = await nb_client(c)
    for p in ("Projects/Garden.md", "b.md"):
        await w.send_json({"type": "doc.open", "root": "notes", "path": p})
        assert (await next_of(w, "doc.content"))["path"] == p
    await w.send_json({"type": "doc.open", "root": "notes", "path": "c.md"})
    while True:                                       # skip trailing presence broadcasts
        m = await w.receive_json(timeout=2)
        if m["type"] in ("doc.error", "doc.content"):
            break
    assert m["type"] == "doc.error" and m["code"] == "too_many_docs"


# ---------------------------------------------------------------- rate limits

async def _responses(w, n):
    """The next n doc.content/error frames, skipping presence broadcasts."""
    out = []
    while len(out) < n:
        m = await w.receive_json(timeout=2)
        if m["type"] in ("doc.content", "error", "doc.error"):
            out.append(m)
    return out


async def test_doc_list_rate_limited_for_paired_user(aiohttp_client, vault):
    settings.SETTINGS["user_messages_per_min"] = 3
    settings.SETTINGS["notebook_roots_for"] = {"ann": ["notes"], "boss": ["notes"]}
    _, tok = tokens.mint("ann")
    _, admin_tok = tokens.mint("boss", admin=True)
    c = await start(aiohttp_client)
    t = await nb_client(c, key=tok, name="ann")
    for _ in range(5):
        await t.send_json({"type": "doc.list"})
    frames = [await t.receive_json(timeout=2) for _ in range(5)]
    assert [f["type"] for f in frames[:3]] == ["doc.list"] * 3
    assert all(f["type"] == "error" and f["code"] == "rate_limited" for f in frames[3:])
    # owner and admin token users are NOT throttled
    for key, name in ((None, "owner"), (admin_tok, "boss")):
        w = await nb_client(c, key=key, name=name)
        for _ in range(5):
            await w.send_json({"type": "doc.list"})
        assert [f["type"] for f in [await w.receive_json(timeout=2) for _ in range(5)]] == ["doc.list"] * 5


async def test_doc_open_rate_limited_for_paired_user(aiohttp_client, vault):
    settings.SETTINGS["user_messages_per_min"] = 2
    settings.SETTINGS["notebook_roots_for"] = {"ann": ["notes"]}
    _mknotes(vault, ["r1", "r2", "r3", "r4", "r5"])
    _, tok = tokens.mint("ann")
    c = await start(aiohttp_client)
    t = await nb_client(c, key=tok, name="ann")
    for p in ("r1.md", "r2.md", "r3.md"):
        await t.send_json({"type": "doc.open", "root": "notes", "path": p})
    frames = await _responses(t, 3)
    assert [f["type"] for f in frames[:2]] == ["doc.content"] * 2
    assert frames[2]["type"] == "error" and frames[2]["code"] == "rate_limited"
    # the owner may keep opening within the same minute
    o = await nb_client(c, name="owner")
    for p in ("r1.md", "r2.md", "r3.md", "r4.md", "r5.md"):
        await o.send_json({"type": "doc.open", "root": "notes", "path": p})
    assert [f["type"] for f in await _responses(o, 5)] == ["doc.content"] * 5


# ---------------------------------------------------------------- off-loop, bounded listing

async def test_doc_list_runs_off_the_event_loop(aiohttp_client, vault, monkeypatch):
    c = await start(aiohttp_client)
    w = await nb_client(c)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    seen = {}
    real_listing = notebook.listing

    def spy(*a, **k):
        seen["thread"] = threading.get_ident()
        return real_listing(*a, **k)

    monkeypatch.setattr(notebook, "listing", spy)
    dispatched = []
    real_rie = loop.run_in_executor

    def rie_spy(executor, fn, *args):
        dispatched.append(fn)
        return real_rie(executor, fn, *args)

    monkeypatch.setattr(loop, "run_in_executor", rie_spy)
    await w.send_json({"type": "doc.list", "query": "garden"})
    lst = await next_of(w, "doc.list")
    assert [n["path"] for n in lst["notes"]] == ["Projects/Garden.md"]
    assert seen.get("thread") is not None, "listing never ran"
    assert seen["thread"] != loop_thread, "listing ran ON the event loop thread"
    assert any(fn is spy for fn in dispatched), "listing was not dispatched via run_in_executor"


def test_listing_walk_is_bounded(vault, monkeypatch, caplog):
    big = vault / "big"
    for d in range(20):
        sub = big / f"d{d}"
        sub.mkdir(parents=True)
        for i in range(100):
            (sub / f"n{i}.md").write_text("x")          # 2000 matching files
    monkeypatch.setattr(notebook, "LIST_WALK_LIMIT", 50, raising=False)
    stats = []
    real_stat = Path.stat

    def counting_stat(self, *a, **k):
        stats.append(str(self))
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", counting_stat)
    with caplog.at_level(logging.WARNING, logger=LOG):
        out = notebook.listing(limit=200)
    md_stats = [p for p in stats if p.endswith(".md")]
    assert len(md_stats) <= 50, f"walk stat'd {len(md_stats)} files — unbounded"
    assert len(out) <= 50
    assert any("walk" in r.message.lower() or "cap" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------- normal use still works

async def test_normal_listing_and_open_within_limits(aiohttp_client, vault):
    settings.SETTINGS["notebook_roots_for"] = {"ann": ["notes"]}
    _, tok = tokens.mint("ann")
    c = await start(aiohttp_client)
    o = await nb_client(c, name="owner")
    t = await nb_client(c, key=tok, name="ann")
    for w in (o, t):
        await w.send_json({"type": "doc.list", "query": "garden"})
        lst = await next_of(w, "doc.list")
        assert [n["path"] for n in lst["notes"]] == ["Projects/Garden.md"]
        await w.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
        doc = await next_of(w, "doc.content")
        assert doc["text"].startswith("# Garden")
