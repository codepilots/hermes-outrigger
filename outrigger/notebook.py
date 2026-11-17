"""Notebook mode (docs/notebook.md): edit Markdown notes on the Hermes host from a client.

The plugin owns the files: it only touches `.md` files inside the configured roots,
writes atomically against a content version, watches open notes for outside changes
(Obsidian, sync), and turns a `//` line into a Hermes turn in the note's own chat on
the Outrigger platform. Hermes edits through notebook_read / notebook_edit, which work
only on that note during that command; the note is locked for the user meanwhile.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import settings

log = logging.getLogger("hermes.plugins.outrigger")

SNAPSHOTS_KEPT = 10
WATCH_INTERVAL = 2.0
LIST_LIMIT = 200
_HIDDEN = re.compile(r"(^|/)\.")


class NotebookError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def version_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def roots() -> dict[str, Path]:
    raw = settings.get("notebook_roots") or {}
    out = {}
    if isinstance(raw, dict):
        for name, p in raw.items():
            try:
                rp = Path(os.path.expanduser(str(p))).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if rp.is_dir() and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", str(name)):
                out[str(name)] = rp
    return out


def enabled() -> bool:
    return bool(roots())


def resolve(root: str, rel: str) -> Path:
    """The real path of a note inside a root, or NotebookError. No escapes, no hidden parts."""
    base = roots().get(root)
    if base is None:
        raise NotebookError("no_root", f"unknown notebook root {root!r}")
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        raise NotebookError("bad_path", "path must be relative to the root")
    parts = Path(rel).parts
    if any(p in ("..", ".") or p.startswith(".") for p in parts):
        raise NotebookError("bad_path", "no '..', '.' or hidden folders")
    if not rel.lower().endswith(".md"):
        raise NotebookError("bad_path", "only .md notes")
    full = (base / rel).resolve()
    if base != full and base not in full.parents:
        raise NotebookError("bad_path", "path leaves the root")
    return full


def listing(root: str | None = None, query: str = "", limit: int = 50) -> list[dict]:
    q = (query or "").lower().strip()
    found = []
    for name, base in roots().items():
        if root and name != root:
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if not fn.lower().endswith(".md") or fn.startswith("."):
                    continue
                full = Path(dirpath) / fn
                rel = full.relative_to(base).as_posix()
                if q and q not in rel.lower():
                    continue
                try:
                    st = full.stat()
                except OSError:
                    continue
                found.append({"root": name, "path": rel, "title": fn[:-3], "mtime": int(st.st_mtime), "size": st.st_size})
    found.sort(key=lambda d: d["mtime"], reverse=True)
    return found[:max(1, min(int(limit or 50), LIST_LIMIT))]


# ---------------------------------------------------------------- open documents

@dataclass(eq=False)
class Doc:
    doc_id: str
    root: str
    path: str
    full: Path
    text: str = ""
    version: str = ""
    stat: tuple = ()
    clients: set = field(default_factory=set)
    lock: dict | None = None
    mutex: threading.RLock = field(default_factory=threading.RLock)


DOCS: dict[str, Doc] = {}
NOTE_CHATS: dict[str, str] = {}   # platform chat key of a running command -> doc_id
_watch_task = None


def _stat(p: Path) -> tuple:
    st = p.stat()
    return (st.st_mtime_ns, st.st_size)


def _read(doc: Doc) -> None:
    max_bytes = int(settings.get("notebook_max_bytes"))
    if doc.full.stat().st_size > max_bytes:
        raise NotebookError("too_large", f"note larger than {max_bytes // 1024} KB")
    doc.text = doc.full.read_text(encoding="utf-8")
    doc.version = version_of(doc.text)
    doc.stat = _stat(doc.full)


def _write(doc: Doc, text: str) -> None:
    """Atomic write keeping the file mode; updates the doc's version and stat."""
    tmp = doc.full.with_name(f".{doc.full.name}.outrigger-{secrets.token_hex(3)}")
    mode = doc.full.stat().st_mode & 0o777 if doc.full.exists() else 0o644
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, doc.full)
    doc.text, doc.version, doc.stat = text, version_of(text), _stat(doc.full)


def open_doc(root: str, rel: str, client) -> Doc:
    full = resolve(root, rel)
    if not full.is_file():
        raise NotebookError("not_found", f"no note {rel}")
    doc_id = f"{root}:{Path(rel).as_posix()}"
    doc = DOCS.get(doc_id)
    if doc is None:
        doc = Doc(doc_id=doc_id, root=root, path=Path(rel).as_posix(), full=full)
        _read(doc)
        DOCS[doc_id] = doc
    else:
        with doc.mutex:
            _check_disk(doc)
    doc.clients.add(client)
    return doc


def close_doc(doc_id: str, client) -> None:
    doc = DOCS.get(doc_id)
    if doc is None:
        return
    doc.clients.discard(client)
    if not doc.clients and not doc.lock:
        DOCS.pop(doc_id, None)


def drop_client(client) -> None:
    for doc in list(DOCS.values()):
        close_doc(doc.doc_id, client)


def get(doc_id: str, client) -> Doc:
    doc = DOCS.get(doc_id)
    if doc is None or client not in doc.clients:
        raise NotebookError("not_open", "open the note first")
    return doc


def _check_disk(doc: Doc) -> bool:
    """Re-read if the file changed outside; True when the text changed."""
    try:
        st = _stat(doc.full)
    except OSError:
        return False
    if st == doc.stat:
        return False
    old = doc.version
    _read(doc)
    return doc.version != old


def update(doc: Doc, base_version: str, text: str) -> str:
    with doc.mutex:
        if doc.lock:
            raise NotebookError("locked", "Hermes is working on this note")
        _check_disk(doc)
        if base_version != doc.version:
            raise NotebookError("conflict", "the note changed; merge first")
        if len(text.encode("utf-8")) > int(settings.get("notebook_max_bytes")):
            raise NotebookError("too_large", "note too large")
        if text != doc.text:
            _write(doc, text)
        return doc.version


# ---------------------------------------------------------------- snapshots / undo

def _snap_dir(doc: Doc) -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / "outrigger" / "notebook-snapshots" / hashlib.sha256(doc.doc_id.encode()).hexdigest()[:16]


def snapshot(doc: Doc) -> str:
    d = _snap_dir(doc)
    d.mkdir(parents=True, exist_ok=True)
    sid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    (d / f"{sid}.md").write_text(doc.text, encoding="utf-8")
    for old in sorted(d.glob("*.md"))[:-SNAPSHOTS_KEPT]:
        old.unlink(missing_ok=True)
    return sid


def undo(doc: Doc, sid: str) -> str:
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", sid or ""):
        raise NotebookError("bad_snapshot", "unknown snapshot")
    src = _snap_dir(doc) / f"{sid}.md"
    if not src.is_file():
        raise NotebookError("bad_snapshot", "snapshot no longer kept")
    with doc.mutex:
        if doc.lock:
            raise NotebookError("locked", "Hermes is working on this note")
        _check_disk(doc)
        snapshot(doc)  # the undo itself can be undone
        _write(doc, src.read_text(encoding="utf-8"))
        return doc.version


# ---------------------------------------------------------------- broadcasting

def _frame(doc: Doc, kind: str, **extra) -> dict:
    return {"type": kind, "doc_id": doc.doc_id, **extra}


def content_frame(doc: Doc, by: str = "open") -> dict:
    return _frame(doc, "doc.content", root=doc.root, path=doc.path, text=doc.text, version=doc.version,
                  locked=bool(doc.lock), by=by, lock=_lock_info(doc))


def _lock_info(doc: Doc) -> dict | None:
    if not doc.lock:
        return None
    return {k: doc.lock.get(k) for k in ("instruction", "snapshot", "started")}


def broadcast(doc: Doc, msg: dict, *, exclude=None) -> None:
    """Send to every client with the note open; safe from the loop or a worker thread."""
    for c in list(doc.clients):
        if c is exclude or c.closed:
            continue
        coro = c.ws.send_json(msg)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is c.loop:
            running.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, c.loop)


# ---------------------------------------------------------------- // commands

def _strip_command_line(text: str, line: int, instruction: str) -> str:
    lines = text.split("\n")
    if not (1 <= line <= len(lines)):
        raise NotebookError("bad_line", "line out of range")
    got = lines[line - 1]
    m = re.match(r"^\s*//(.*)$", got)
    if not m or m.group(1).strip() != instruction.strip():
        raise NotebookError("bad_line", "that line is not this // command")
    del lines[line - 1]
    return "\n".join(lines)


def note_chat_id(doc: Doc) -> str:
    return "note-" + hashlib.sha256(doc.doc_id.encode()).hexdigest()[:16]


def start_command(doc: Doc, client, base_version: str, line: int, instruction: str) -> dict:
    """Remove the // line, snapshot, lock. The caller then starts the Hermes turn."""
    instruction = (instruction or "").strip()
    if not instruction:
        raise NotebookError("bad_command", "empty instruction")
    with doc.mutex:
        if doc.lock:
            raise NotebookError("locked", "Hermes is already working on this note")
        _check_disk(doc)
        if base_version != doc.version:
            raise NotebookError("conflict", "the note changed; merge first")
        sid = snapshot(doc)
        _write(doc, _strip_command_line(doc.text, line, instruction))
        doc.lock = {"by": "hermes", "instruction": instruction[:300], "snapshot": sid, "line": line,
                    "started": int(time.time()), "client": client, "session_id": None, "chat_key": None}
    return doc.lock


def command_message(doc: Doc, line: int, instruction: str) -> str:
    return (f"The user is editing the note `{doc.doc_id}` and wrote an inline instruction at line {line} "
            f"(the line has been removed): \"{instruction}\".\n"
            "Decide from the instruction, where it was written and the note itself what it applies to. "
            "Use the notebook_read and notebook_edit tools (via tool_call if they are not directly "
            "available) to change this note; keep the user's voice and Markdown. Then reply with one "
            "short line saying what you changed.")


def end_command(doc: Doc, notice: str = "") -> None:
    with doc.mutex:
        if not doc.lock:
            return
        sid = doc.lock.get("snapshot")
        NOTE_CHATS.pop(doc.lock.get("chat_key") or "", None)
        doc.lock = None
    broadcast(doc, _frame(doc, "doc.lock", locked=False, snapshot=sid, notice=notice))
    broadcast(doc, content_frame(doc, by="hermes"))


def turn_started(session_id: str) -> None:
    """pre_llm_call: tie a note command's lock to the session that runs it."""
    chat = _session_chat()
    doc = DOCS.get(NOTE_CHATS.get(chat or "", ""))
    if doc and doc.lock and not doc.lock.get("session_id"):
        doc.lock["session_id"] = session_id


def turn_ended(session_id: str) -> None:
    """on_session_end: a command's turn finished (or was stopped): unlock its note."""
    for doc in list(DOCS.values()):
        if doc.lock and doc.lock.get("session_id") == session_id:
            end_command(doc)


def expire_locks() -> None:
    limit = int(settings.get("notebook_lock_timeout_s"))
    for doc in list(DOCS.values()):
        if doc.lock and time.time() - doc.lock["started"] > limit:
            end_command(doc, notice=f"Hermes took longer than {limit // 60} min; editing resumed")


def _session_chat() -> str | None:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None
    if get_session_env("HERMES_SESSION_PLATFORM") != "outrigger":
        return None
    return get_session_env("HERMES_SESSION_CHAT_ID") or None


def doc_for_tool(session_id: str | None) -> Doc:
    """The note a notebook_* call may touch: the one whose // command is running in this chat."""
    from .registry import REGISTRY
    if REGISTRY.is_subagent(session_id):
        raise NotebookError("subagent", "subagents may not edit notes")
    doc = DOCS.get(NOTE_CHATS.get(_session_chat() or "", ""))
    if doc is None or not doc.lock:
        raise NotebookError("no_command", "notebook tools work only while a // note command is running")
    return doc


# ---------------------------------------------------------------- watcher

async def _watch() -> None:
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        expire_locks()
        for doc in list(DOCS.values()):
            try:
                with doc.mutex:
                    changed = _check_disk(doc)
            except (OSError, NotebookError):
                continue
            if changed:
                broadcast(doc, _frame(doc, "doc.changed", text=doc.text, version=doc.version, by="disk"))


def ensure_watcher() -> None:
    global _watch_task
    if _watch_task is None or _watch_task.done():
        _watch_task = asyncio.get_running_loop().create_task(_watch())


# ---------------------------------------------------------------- Hermes's tools

READ_SCHEMA = {
    "name": "notebook_read",
    "description": ("Read the note the user is editing (only during a // note command): numbered lines "
                    "and the version notebook_edit needs."),
    "parameters": {"type": "object", "properties": {
        "from_line": {"type": "integer", "minimum": 1}, "to_line": {"type": "integer", "minimum": 1}}},
}
EDIT_SCHEMA = {
    "name": "notebook_edit",
    "description": ("Edit the note the user is editing (only during a // note command). Each edit replaces "
                    "lines from_line..to_line (inclusive) with text; text '' deletes them; to insert before "
                    "line N use from_line=N, to_line=N-1. Line numbers refer to the version you read; all "
                    "edits apply at once. Returns the new version."),
    "parameters": {"type": "object", "required": ["version", "edits"], "properties": {
        "version": {"type": "string"},
        "edits": {"type": "array", "minItems": 1, "maxItems": 50, "items": {
            "type": "object", "required": ["from_line", "to_line", "text"], "properties": {
                "from_line": {"type": "integer", "minimum": 1}, "to_line": {"type": "integer", "minimum": 0},
                "text": {"type": "string"}}}}}},
}


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def tool_read(args: dict, session_id: str | None = None, **_) -> str:
    try:
        doc = doc_for_tool(session_id)
    except NotebookError as e:
        return _json({"ok": False, "error": e.code, "message": e.message})
    with doc.mutex:
        lines = doc.text.split("\n")
        a = max(1, int(args.get("from_line") or 1))
        b = min(len(lines), int(args.get("to_line") or len(lines)))
        body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(a, b + 1))
        return _json({"ok": True, "note": doc.doc_id, "version": doc.version, "total_lines": len(lines),
                      "lines": f"{a}-{b}", "text": body})


def apply_edits(text: str, edits: list) -> str:
    lines = text.split("\n")
    n = len(lines)
    spans = []
    for e in edits:
        a, b = int(e["from_line"]), int(e["to_line"])
        if not (1 <= a <= n + 1) or not (a - 1 <= b <= n):
            raise NotebookError("bad_edit", f"lines {a}-{b} outside 1-{n}")
        spans.append((a, b, str(e["text"])))
    spans.sort(key=lambda s: (s[0], s[1]))
    for (_, b1, _), (a2, _, _) in zip(spans, spans[1:]):
        if a2 <= b1:
            raise NotebookError("bad_edit", "edits overlap")
    for a, b, t in reversed(spans):
        new = [] if t == "" else t.split("\n")
        lines[a - 1:b] = new
    return "\n".join(lines)


def tool_edit(args: dict, session_id: str | None = None, **_) -> str:
    try:
        doc = doc_for_tool(session_id)
        with doc.mutex:
            if args.get("version") != doc.version:
                raise NotebookError("stale_version", f"the note is now version {doc.version}: notebook_read again")
            new = apply_edits(doc.text, args.get("edits") or [])
            if len(new.encode("utf-8")) > int(settings.get("notebook_max_bytes")):
                raise NotebookError("too_large", "the result would be too large")
            _write(doc, new)
            result = {"ok": True, "version": doc.version, "total_lines": len(new.split("\n"))}
    except NotebookError as e:
        return _json({"ok": False, "error": e.code, "message": e.message})
    except (KeyError, TypeError, ValueError) as e:
        return _json({"ok": False, "error": "bad_edit", "message": str(e)})
    broadcast(doc, content_frame(doc, by="hermes"))
    return _json(result)
