"""Connected clients, session bindings and pending-call bookkeeping.

Trust boundary: delegated child agents (subagents) -> clients / notes. The
subagent gate (mark_subagent / unmark_subagent / is_subagent, banner below) is a
security surface: the mark IS the allow_subagents deny signal — marked sessions
get no clients (for_session) and no notebook tools. Marks are never evicted and
never refused (overflow retained, loud WARNING); a missed stop leaks memory, not
authority. Deliberately does NOT: authenticate peers (auth.py) or decide tool
policy; it tracks membership, caps and per-turn state.
"""
# Audit: see AUDIT.md#security-sensitive-units (subagent gate / membership caps)
from __future__ import annotations

import asyncio
import json
import concurrent.futures
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from .protocol import ToolDecl

log = logging.getLogger("hermes.plugins.outrigger")

MAX_CLIENTS = 16
# Loud-warning threshold for tracked subagent marks, NOT an eviction bound:
# marks are never evicted (see mark_subagent), so the set may exceed this.
MAX_TRACKED_SUBAGENTS = 1024


class ClientGone(Exception):
    """The client disconnected or could not be written to."""


@dataclass
class Pending:
    future: concurrent.futures.Future
    session_id: str
    tool: str
    last_progress: str = ""


@dataclass(eq=False)
class Client:
    name: str
    label: str
    kind: str
    version: str
    ws: object  # aiohttp.web.WebSocketResponse
    loop: asyncio.AbstractEventLoop
    id: str = field(default_factory=lambda: "c" + secrets.token_hex(4))
    session_id: str | None = None  # most recent session id this client serves
    tools: dict[str, ToolDecl] = field(default_factory=dict)
    summary: str = ""
    concurrency: int = 1
    redaction: dict | None = None
    capabilities: set = field(default_factory=set)  # hello.capabilities, e.g. {"chat"}
    principal: dict = field(default_factory=lambda: {"kind": "owner"})  # owner, or a paired token's user  # {"enabled": True, "categories": [...]} when the client redacts
    pending: dict[str, Pending] = field(default_factory=dict)
    connected_at: float = field(default_factory=time.time)
    closed: bool = False

    def __post_init__(self):
        self.sem = threading.BoundedSemaphore(self.concurrency)
        # Every session id that names this client's conversation: the bound id plus
        # ids a rotating compression moved it to.
        self.sessions: set[str] = {self.session_id} if self.session_id else set()

    def send(self, msg: dict, timeout: float = 5.0) -> None:
        """Thread-safe send onto the gateway loop. Must not be called on that loop."""
        if self.closed:
            raise ClientGone()
        fut = asyncio.run_coroutine_threadsafe(self.ws.send_json(msg), self.loop)
        try:
            fut.result(timeout)
        except Exception as e:  # connection reset, timeout, closed
            raise ClientGone() from e


class Registry:
    def __init__(self):
        self._lock = threading.RLock()
        self.clients: dict[str, Client] = {}
        self.turns: dict[str, str] = {}  # session_id -> current turn_id
        self.call_counts: dict[tuple[str, str], int] = {}  # (session, client) -> n
        self.subagents: set[str] = set()  # session ids of delegated child agents
        self.intent: dict[str, str] = {}  # session_id -> user's message this turn (for the reviewer)
        self.actions: dict[str, list] = {}  # session_id -> this turn's client calls (for the reviewer)

    # ---- membership
    def add(self, client: Client) -> Client | None:
        """Register; returns a replaced duplicate (same name + session + principal) if any.
        Actor: authenticated hello (_register). Control: per-token cap
        max_clients_per_token (default 4), all tokens share MAX_CLIENTS=16, the
        owner has MAX_CLIENTS of their own — paired tokens never crowd out the
        owner. Sink: REGISTRY membership. Refusal: RuntimeError (too many
        connections) -> ws closes with too_many_clients."""
        with self._lock:
            replaced = None
            for c in list(self.clients.values()):
                if c.name == client.name and c.session_id == client.session_id and c.principal == client.principal:
                    replaced = c
                    self._drop_locked(c, "outrigger_replaced")
            # Paired tokens can't crowd out the owner: each token has its own cap, all
            # tokens together share MAX_CLIENTS, and the owner has MAX_CLIENTS of their own.
            from . import settings
            p = client.principal
            if p.get("kind") == "token":
                mine = sum(1 for c in self.clients.values() if c.principal.get("id") == p.get("id"))
                if mine >= int(settings.get("max_clients_per_token") or 4):
                    raise RuntimeError("too many connections for this token")
                if sum(1 for c in self.clients.values() if c.principal.get("kind") == "token") >= MAX_CLIENTS:
                    raise RuntimeError("too many outrigger clients")
            elif sum(1 for c in self.clients.values() if c.principal.get("kind") != "token") >= MAX_CLIENTS:
                raise RuntimeError("too many outrigger clients")
            self.clients[client.id] = client
            return replaced

    def remove(self, client: Client, reason: str = "outrigger_disconnected") -> None:
        with self._lock:
            self._drop_locked(client, reason)

    def _drop_locked(self, client: Client, reason: str) -> None:
        client.closed = True
        self.clients.pop(client.id, None)
        for p in client.pending.values():
            if not p.future.done():
                p.future.set_result({"ok": False, "error": reason})
        client.pending.clear()

    def close_all(self) -> None:
        """Fail in-flight calls and close every socket (plugin unload)."""
        with self._lock:
            clients = list(self.clients.values())
            for c in clients:
                self._drop_locked(c, "outrigger_shutdown")
        for c in clients:
            try:
                if not c.loop.is_closed():
                    asyncio.run_coroutine_threadsafe(c.ws.close(), c.loop)
            except Exception:  # loop gone, socket already closed
                pass

    # ---- subagents (security gate: the mark IS the allow_subagents deny signal)
    def mark_subagent(self, session_id: str) -> None:
        """Actor: subagent_start hook (trusted gateway). The mark IS the
        allow_subagents (default false) deny signal: marked sessions get no
        clients (for_session) and no notebook tools. Never evicts, never refuses
        — overflow marks retained with a loud WARNING at
        MAX_TRACKED_SUBAGENTS=1024 (a warning threshold, not a bound); a missed
        subagent_stop leaks memory, never authority."""
        with self._lock:
            if session_id in self.subagents:
                return
            # Fail closed: the mark IS the allow_subagents deny signal. Evicting
            # any member (the old set.pop()) could unmark a LIVE subagent, which
            # then inherits the owner's conversation_id and steals the attached
            # clients; refusing the new mark would fail open the same way for the
            # new child. So never evict and never refuse — retain overflow marks
            # too, and warn loudly once the tracked count reaches the cap. Marks
            # are reclaimed only by unmark_subagent (subagent_stop); there is no
            # safe liveness oracle here, so a missed stop leaks memory, never
            # authority.
            self.subagents.add(session_id)
            if len(self.subagents) >= MAX_TRACKED_SUBAGENTS:
                log.warning(
                    "outrigger: subagent mark cap %d reached (%d tracked); retaining "
                    "overflow mark for %s (fail closed). Missed subagent_stop hooks "
                    "leak marks; every marked session stays denied while "
                    "allow_subagents=False.",
                    MAX_TRACKED_SUBAGENTS, len(self.subagents), session_id)

    def unmark_subagent(self, session_id: str) -> None:
        """subagent_stop hook: reclaim a mark. The only removal path — there is no
        liveness oracle, so marks are never evicted automatically."""
        with self._lock:
            self.subagents.discard(session_id)

    def is_subagent(self, session_id: str | None) -> bool:
        """The deny predicate: True for a marked session id (None/empty -> False).
        Read by dispatch, notebook_tools and the rotate guard."""
        with self._lock:
            return bool(session_id) and session_id in self.subagents

    # ---- lookup
    def any_attached(self) -> bool:
        with self._lock:
            return bool(self.clients)

    def bind(self, client: Client, session_id: str) -> None:
        with self._lock:
            client.session_id = session_id
            client.sessions = {session_id}

    def bind_extra(self, client: Client, session_id: str) -> None:
        """Also serve this session id (Outrigger platform chats), keeping existing bindings."""
        with self._lock:
            if not client.closed:
                client.sessions.add(session_id)

    def rotate(self, old_session_id: str, new_session_id: str) -> None:
        """A rotating compression moved the conversation to a new session id.
        Guard: a new id already marked subagent is never adopted — the mark IS the
        deny signal; a child must not inherit the parent's clients."""
        with self._lock:
            if new_session_id in self.subagents:
                return
            for c in self.clients.values():
                if old_session_id in c.sessions:
                    c.sessions.add(new_session_id)
                    c.session_id = new_session_id

    def for_session(self, session_id: str | None, conversation_id: str | None = None,
                    allow_subagent: bool = False) -> list[Client]:
        """Clients serving this session.

        A tool call carries `conversation_id`, the conversation's first session id,
        which survives rotating compression; a client bound to it follows the
        conversation to the new id. A delegated subagent carries its parent's
        conversation_id too, so subagent sessions (seen via subagent_start) get no
        clients unless allow_subagent (mirrors settings.allow_subagents, default
        false), and never move the binding.
        """
        with self._lock:
            sub = bool(session_id) and session_id in self.subagents
            if sub and not allow_subagent:
                return []
            out = []
            for c in self.clients.values():
                if session_id and session_id in c.sessions:
                    out.append(c)
                elif conversation_id and conversation_id in c.sessions:
                    if session_id and not sub:
                        c.sessions.add(session_id)
                        c.session_id = session_id
                    out.append(c)
            return out

    # ---- calls
    def new_call(self, client: Client, session_id: str, tool: str) -> tuple[str, concurrent.futures.Future]:
        call_id = "call_" + secrets.token_hex(6)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            if client.closed:
                raise ClientGone()
            client.pending[call_id] = Pending(fut, session_id, tool)
        return call_id, fut

    def resolve(self, client: Client, call_id: str, msg: dict) -> bool:
        with self._lock:
            p = client.pending.pop(call_id, None)
        if not p or p.future.done():
            return False
        p.future.set_result(msg)
        return True

    def progress(self, client: Client, call_id: str, message: str) -> Pending | None:
        with self._lock:
            p = client.pending.get(call_id)
            if p:
                p.last_progress = message
            return p

    def forget(self, client: Client, call_id: str) -> Pending | None:
        with self._lock:
            return client.pending.pop(call_id, None)

    def note_action(self, session_id: str, tool: str, args: dict, outcome: str) -> None:
        with self._lock:
            lst = self.actions.setdefault(session_id, [])
            lst.append({"tool": tool, "args": json.dumps(args, default=str)[:300], "outcome": outcome})
            del lst[:-12]

    def recent_actions(self, session_id: str) -> list:
        with self._lock:
            return list(self.actions.get(session_id, []))

    def count_call(self, session_id: str, client: Client) -> int:
        """Per-turn call counter keyed (session, client): dispatch refuses with
        outrigger_call_limit once it exceeds max_calls_per_turn (default 20).
        Reset by end_turn; the count is the per-turn cap's input, not a bound
        itself."""
        with self._lock:
            key = (session_id, client.id)
            self.call_counts[key] = self.call_counts.get(key, 0) + 1
            return self.call_counts[key]

    def end_turn(self, session_id: str, cancel: bool) -> list[tuple[Client, str]]:
        """Reset per-turn state; if cancel, resolve the session's leftover calls."""
        cancelled = []
        with self._lock:
            self.turns.pop(session_id, None)
            self.intent.pop(session_id, None)
            self.actions.pop(session_id, None)
            for key in [k for k in self.call_counts if k[0] == session_id]:
                del self.call_counts[key]
            if cancel:
                for c in self.clients.values():
                    for call_id, p in list(c.pending.items()):
                        if p.session_id == session_id and not p.future.done():
                            p.future.set_result({"ok": False, "error": "outrigger_cancelled"})
                            cancelled.append((c, call_id))
                            del c.pending[call_id]
        return cancelled


REGISTRY = Registry()
