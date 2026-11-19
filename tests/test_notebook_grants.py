"""F-05: stale root grants on open docs + doc-identity aliasing (resolved-file keying).

Reuses the vault/two_roots fixtures and ws helpers from test_notebook / test_platform.
"""
import pytest

from outrigger import notebook, settings, tokens
from outrigger.notebook import NotebookError

from helpers import start
from test_notebook import CAPS, nb_client, two_roots, vault  # noqa: F401 (fixtures)
from test_platform import adapter, next_of  # noqa: F401 (fixture)


class Stub:
    """Minimal client stand-in for unit-level notebook calls (no websocket)."""

    def __init__(self, user_id=None, cid="c1"):
        self.id = cid
        self.principal = ({"kind": "owner"} if user_id is None
                          else {"kind": "token", "user_id": user_id})


async def _lock_via(w, doc, instruction="expand"):
    """Add a // line, run doc.command on it, return the locked frame's doc_id."""
    text = doc["text"].rstrip("\n") + "\n//" + instruction
    await w.send_json({"type": "doc.update", "doc_id": doc["doc_id"],
                       "base_version": doc["version"], "text": text})
    ver = (await next_of(w, "doc.saved"))["version"]
    line = len(text.split("\n"))
    await w.send_json({"type": "doc.command", "doc_id": doc["doc_id"],
                       "base_version": ver, "line": line, "instruction": instruction})
    lock = await next_of(w, "doc.lock")
    assert lock["locked"]
    return ver


async def _alias_blocked(w, alias_doc, ver):
    """Edits and a second // command through the alias spelling must hit the lock."""
    await w.send_json({"type": "doc.update", "doc_id": alias_doc["doc_id"],
                       "base_version": ver, "text": "alias write"})
    assert (await next_of(w, "doc.error"))["code"] == "locked"
    await w.send_json({"type": "doc.command", "doc_id": alias_doc["doc_id"],
                       "base_version": ver, "line": 1, "instruction": "again"})
    assert (await next_of(w, "doc.error"))["code"] == "locked"


# ---------------------------------------------------------------- (A) stale grants

def test_get_rechecks_narrowed_grants(two_roots):
    settings.SETTINGS["notebook_roots_for"] = {"ann": ["notes", "shared"]}
    ann, owner = Stub("ann"), Stub()
    allowed = notebook.allowed_roots(ann)
    garden = notebook.open_doc("notes", "Projects/Garden.md", ann, allowed)
    plan = notebook.open_doc("shared", "Plan.md", ann, allowed)
    owner_doc = notebook.open_doc("notes", "Projects/Garden.md", owner)
    assert notebook.get(garden.doc_id, ann) is garden
    with pytest.raises(NotebookError) as e:
        notebook.get("notes:missing.md", ann)
    assert e.value.code == "not_open"

    settings.SETTINGS["notebook_roots_for"] = {"ann": ["shared"]}   # narrow mid-session
    with pytest.raises(NotebookError) as e:
        notebook.get(garden.doc_id, ann)
    assert e.value.code == "not_allowed"
    assert notebook.get(plan.doc_id, ann) is plan                   # still-granted root
    assert notebook.get(owner_doc.doc_id, owner) is owner_doc       # owner keeps all
    notebook.update(plan, plan.version, plan.text + "\nStill hers.")

    settings.SETTINGS["notebook_roots_for"] = {"ann": ["notes", "shared"]}  # re-widen
    assert notebook.get(garden.doc_id, ann) is garden


async def test_narrowed_grants_block_open_doc_ops(aiohttp_client, vault, two_roots, adapter):
    await adapter.connect()
    settings.SETTINGS["notebook_roots_for"] = {"ann": ["notes", "shared"]}
    _, tok = tokens.mint("ann", user_name="Ann")
    c = await start(aiohttp_client)
    ann = await nb_client(c, key=tok, name="ann-sb")
    owner = await nb_client(c, name="owner-sb")
    await ann.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    g = await next_of(ann, "doc.content")
    await ann.send_json({"type": "doc.open", "root": "shared", "path": "Plan.md"})
    p = await next_of(ann, "doc.content")
    await owner.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    og = await next_of(owner, "doc.content")

    settings.SETTINGS["notebook_roots_for"] = {"ann": ["shared"]}   # owner narrows; ws stays up
    before = (vault / "Projects/Garden.md").read_text()
    for op, extra in (("doc.update", {"base_version": g["version"], "text": "revoked write"}),
                      ("doc.undo", {"snapshot": "20260101-000000-0000"}),
                      ("doc.command", {"base_version": g["version"], "line": 1, "instruction": "x"})):
        await ann.send_json({"type": op, "doc_id": g["doc_id"], **extra})
        err = await next_of(ann, "doc.error")
        assert err["code"] == "not_allowed", (op, err)
    assert (vault / "Projects/Garden.md").read_text() == before     # nothing was written

    # the still-granted root stays fully editable: update, undo, // command
    await ann.send_json({"type": "doc.update", "doc_id": p["doc_id"],
                         "base_version": p["version"], "text": "# Plan\n\nAnn's idea."})
    assert (await next_of(ann, "doc.saved"))["doc_id"] == p["doc_id"]
    sid = notebook.snapshot(notebook.DOCS[p["doc_id"]])
    await ann.send_json({"type": "doc.undo", "doc_id": p["doc_id"], "snapshot": sid})
    restored = await next_of(ann, "doc.content")
    assert restored["by"] == "undo"
    await ann.send_json({"type": "doc.update", "doc_id": p["doc_id"],
                         "base_version": restored["version"], "text": "# Plan\n\n//expand"})
    ver = (await next_of(ann, "doc.saved"))["version"]
    await ann.send_json({"type": "doc.command", "doc_id": p["doc_id"],
                         "base_version": ver, "line": 3, "instruction": "expand"})
    assert (await next_of(ann, "doc.lock"))["locked"]
    notebook.end_command(notebook.DOCS[p["doc_id"]])

    # the owner is untouched by the narrowing
    await owner.send_json({"type": "doc.update", "doc_id": og["doc_id"],
                           "base_version": og["version"], "text": "# Garden\n\nOwner edit."})
    assert (await next_of(owner, "doc.saved"))["doc_id"] == og["doc_id"]


async def test_full_revocation_still_blocks(aiohttp_client, two_roots):
    settings.SETTINGS["notebook_roots_for"] = {"ann": ["notes", "shared"]}
    _, tok = tokens.mint("ann")
    c = await start(aiohttp_client)
    ann = await nb_client(c, key=tok, name="ann-sb")
    await ann.send_json({"type": "doc.open", "root": "shared", "path": "Plan.md"})
    p = await next_of(ann, "doc.content")
    settings.SETTINGS["notebook_roots_for"] = {}                    # empty: entry gate denies
    for op, extra in (("doc.update", {"base_version": p["version"], "text": "x"}),
                      ("doc.undo", {"snapshot": "20260101-000000-0000"}),
                      ("doc.command", {"base_version": p["version"], "line": 1, "instruction": "x"})):
        await ann.send_json({"type": op, "doc_id": p["doc_id"], **extra})
        assert (await next_of(ann, "error"))["code"] == "not_allowed", op


# ---------------------------------------------------------------- (B) alias identity

async def test_in_root_symlink_aliases_share_one_lock(aiohttp_client, vault, adapter):
    await adapter.connect()
    target = vault / "Projects" / "Garden.md"
    (vault / "alias.md").symlink_to(target)
    c = await start(aiohttp_client)
    w = await nb_client(c)
    await w.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    a = await next_of(w, "doc.content")
    await w.send_json({"type": "doc.open", "root": "notes", "path": "alias.md"})
    b = await next_of(w, "doc.content")
    assert b["doc_id"] == a["doc_id"] == "notes:Projects/Garden.md"
    assert sum(1 for d in notebook.DOCS.values() if d.full == target.resolve()) == 1
    ver = await _lock_via(w, a)
    await _alias_blocked(w, b, ver)
    notebook.end_command(notebook.DOCS[a["doc_id"]])


async def test_overlapping_root_names_share_one_lock(aiohttp_client, vault, adapter):
    await adapter.connect()
    settings.SETTINGS["notebook_roots"] = {"notes": str(vault), "mirror": str(vault)}
    c = await start(aiohttp_client)
    w = await nb_client(c)
    await w.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    a = await next_of(w, "doc.content")
    await w.send_json({"type": "doc.open", "root": "mirror", "path": "Projects/Garden.md"})
    b = await next_of(w, "doc.content")
    assert b["doc_id"] == a["doc_id"] == "notes:Projects/Garden.md"   # stable canonical id
    assert len(notebook.DOCS) == 1
    ver = await _lock_via(w, a)
    await _alias_blocked(w, b, ver)
    notebook.end_command(notebook.DOCS[a["doc_id"]])


async def test_nested_root_names_share_one_lock(aiohttp_client, vault, adapter):
    await adapter.connect()
    settings.SETTINGS["notebook_roots"] = {"notes": str(vault), "proj": str(vault / "Projects")}
    c = await start(aiohttp_client)
    w = await nb_client(c)
    await w.send_json({"type": "doc.open", "root": "proj", "path": "Garden.md"})
    a = await next_of(w, "doc.content")
    await w.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    b = await next_of(w, "doc.content")
    assert b["doc_id"] == a["doc_id"] == "notes:Projects/Garden.md"
    assert len(notebook.DOCS) == 1
    ver = await _lock_via(w, a)
    await _alias_blocked(w, b, ver)
    notebook.end_command(notebook.DOCS[a["doc_id"]])


async def test_config_reorder_keeps_one_doc(aiohttp_client, vault, adapter):
    """A settings reload that reorders/renames roots must not split a held file's identity."""
    await adapter.connect()
    settings.SETTINGS["notebook_roots"] = {"notes": str(vault), "mirror": str(vault)}
    c = await start(aiohttp_client)
    w = await nb_client(c)
    await w.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    a = await next_of(w, "doc.content")
    settings.SETTINGS["notebook_roots"] = {"mirror": str(vault), "renamed": str(vault)}
    await w.send_json({"type": "doc.open", "root": "renamed", "path": "Projects/Garden.md"})
    b = await next_of(w, "doc.content")
    assert b["doc_id"] == a["doc_id"] and len(notebook.DOCS) == 1
    ver = await _lock_via(w, a)
    await _alias_blocked(w, b, ver)
    notebook.end_command(notebook.DOCS[a["doc_id"]])


async def test_alias_root_grant_still_editable(aiohttp_client, vault):
    """A user granted only the alias root name keeps access to the coalesced doc."""
    settings.SETTINGS["notebook_roots"] = {"notes": str(vault), "mirror": str(vault)}
    settings.SETTINGS["notebook_roots_for"] = {"ann": ["mirror"]}
    _, tok = tokens.mint("ann")
    c = await start(aiohttp_client)
    ann = await nb_client(c, key=tok, name="ann-sb")
    owner = await nb_client(c, name="owner-sb")
    await ann.send_json({"type": "doc.open", "root": "mirror", "path": "Projects/Garden.md"})
    d = await next_of(ann, "doc.content")
    await ann.send_json({"type": "doc.update", "doc_id": d["doc_id"],
                         "base_version": d["version"], "text": "# Garden\n\nVia mirror."})
    saved = await next_of(ann, "doc.saved")
    assert saved["doc_id"] == d["doc_id"]
    # the owner opens the canonical spelling: same doc, ann's alias grant still counts
    await owner.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    od = await next_of(owner, "doc.content")
    assert od["doc_id"] == d["doc_id"]
    await ann.send_json({"type": "doc.update", "doc_id": d["doc_id"],
                         "base_version": saved["version"], "text": "# Garden\n\nStill via mirror."})
    assert (await next_of(ann, "doc.saved"))["doc_id"] == d["doc_id"]


# ---------------------------------------------------------------- (d) normal access

async def test_normal_open_get_update_within_grants(aiohttp_client, two_roots):
    settings.SETTINGS["notebook_roots_for"] = {"ann": ["shared"]}
    _, tok = tokens.mint("ann")
    c = await start(aiohttp_client)
    ann = await nb_client(c, key=tok, name="ann-sb")
    await ann.send_json({"type": "doc.open", "root": "shared", "path": "Plan.md"})
    d = await next_of(ann, "doc.content")
    assert d["doc_id"] == "shared:Plan.md" and d["root"] == "shared"
    await ann.send_json({"type": "doc.update", "doc_id": d["doc_id"],
                         "base_version": d["version"], "text": "# Plan\n\nNormal edit."})
    assert (await next_of(ann, "doc.saved"))["doc_id"] == d["doc_id"]
    assert (two_roots / "Plan.md").read_text().endswith("Normal edit.")
    await ann.send_json({"type": "doc.open", "root": "notes", "path": "Projects/Garden.md"})
    assert (await next_of(ann, "doc.error"))["code"] == "no_root"   # ungranted root refused
