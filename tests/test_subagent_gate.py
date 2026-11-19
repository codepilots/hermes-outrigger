"""F-08: the subagent mark IS the allow_subagents deny signal.

At the MAX_TRACKED_SUBAGENTS cap the registry must fail CLOSED: it may never
evict a live mark (that would hand the evicted subagent the owner's clients),
and it may never refuse the new mark (that would hand the NEW subagent the
owner's clients). Every marked session — existing and overflow — stays denied
while allow_subagents=False; normal sessions are unaffected; allow_subagents=True
still permits subagents without rebinding the owner's client.
"""
import asyncio
import json
import logging

import pytest

import outrigger
from outrigger import context, dispatch, registry, settings
from outrigger.registry import REGISTRY, MAX_TRACKED_SUBAGENTS, Client

from helpers import call, connect, serve_one, start

CAP = MAX_TRACKED_SUBAGENTS
LOG = "hermes.plugins.outrigger"


def _owner(session="PARENT"):
    """An owner client bound to `session`, registered directly (no socket needed
    for the registry-level gate). A closed stub loop keeps close_all's teardown
    from touching the absent socket."""
    loop = asyncio.new_event_loop()
    loop.close()
    c = Client(name="owner", label="", kind="browser", version="1",
               ws=None, loop=loop, session_id=session)
    REGISTRY.add(c)
    return c


def _fill_cap(prefix="sub"):
    """Mark CAP active subagents (none finished/unmarked). Returns their ids."""
    ids = [f"{prefix}-{i}" for i in range(CAP)]
    for sid in ids:
        REGISTRY.mark_subagent(sid)
    assert len(REGISTRY.subagents) == CAP
    return ids


# ---- (a) at the cap, marking a new subagent evicts NO existing active mark

def test_cap_does_not_evict_active_marks():
    owner = _owner()
    ids = _fill_cap()
    before = set(REGISTRY.subagents)

    REGISTRY.mark_subagent("overflow")

    # No live mark may be dropped: every original subagent is still marked ...
    assert set(REGISTRY.subagents) >= before, "a live subagent mark was evicted"
    for sid in ids:
        assert REGISTRY.is_subagent(sid), f"{sid} lost its mark at the cap"
        # ... and still gets no clients via the inherited parent conversation.
        assert REGISTRY.for_session(sid, "PARENT", allow_subagent=False) == [], \
            f"evicted/unmarked subagent {sid} gained the owner's clients"
    assert owner.session_id == "PARENT" and owner.sessions == {"PARENT"}


# ---- (b) the new overflow subagent is ALSO denied (and nothing was evicted)

def test_overflow_subagent_denied_and_retained():
    owner = _owner()
    ids = _fill_cap()

    REGISTRY.mark_subagent("overflow")

    assert REGISTRY.is_subagent("overflow")
    assert REGISTRY.for_session("overflow", "PARENT", allow_subagent=False) == []
    # The deny must not come at the cost of evicting an existing mark.
    assert len(REGISTRY.subagents) == CAP + 1
    assert all(REGISTRY.is_subagent(s) for s in ids)
    assert owner.sessions == {"PARENT"}


# ---- (c) an evicted/untracked subagent must never gain clients or steal binding

def test_no_eviction_means_no_untracked_subagent():
    owner = _owner()
    _fill_cap()
    before = set(REGISTRY.subagents)

    REGISTRY.mark_subagent("overflow")

    evicted = before - set(REGISTRY.subagents)
    assert not evicted, f"live marks evicted at cap: {evicted}"
    # Under the old set.pop() policy the evicted session would pass the gate and
    # permanently rebind the owner's client to itself; prove that never happens.
    for sid in evicted:
        assert REGISTRY.for_session(sid, "PARENT", allow_subagent=False) == []
    assert owner.session_id == "PARENT" and owner.sessions == {"PARENT"}


# ---- (d) normal marking/unmarking below the cap is unchanged

def test_normal_lifecycle_below_cap():
    owner = _owner()
    REGISTRY.mark_subagent("child")
    assert REGISTRY.is_subagent("child")
    assert REGISTRY.for_session("child", "PARENT", allow_subagent=False) == []
    assert owner.sessions == {"PARENT"}  # denied subagent never rebinds

    # A normal (unmarked) session sharing the conversation still gets the client
    # and binds, exactly as before.
    got = REGISTRY.for_session("NORMAL", "PARENT", allow_subagent=False)
    assert got == [owner]
    assert "NORMAL" in owner.sessions

    # subagent_stop unmarks; the session is then treated as normal again.
    REGISTRY.unmark_subagent("child")
    assert not REGISTRY.is_subagent("child")
    assert REGISTRY.for_session("child", "PARENT", allow_subagent=False) == [owner]


def test_normal_session_allowed_at_cap():
    owner = _owner()
    _fill_cap()
    REGISTRY.mark_subagent("overflow")

    # Requirement: a normal session must NOT be wrongly denied at/over the cap.
    assert REGISTRY.for_session("NORMAL", "PARENT", allow_subagent=False) == [owner]
    # A directly-bound normal session is unaffected too.
    owner2 = _owner(session="S1")
    assert REGISTRY.for_session("S1", allow_subagent=False) == [owner2]


def test_allow_subagents_true_at_cap_permits_without_rebinding():
    owner = _owner()
    ids = _fill_cap()
    REGISTRY.mark_subagent("overflow")

    for sid in ("overflow", ids[0], ids[-1]):
        got = REGISTRY.for_session(sid, "PARENT", allow_subagent=True)
        assert got == [owner], f"allow_subagent=True must permit {sid} at the cap"
    # Permitting never moves the owner's binding onto the child.
    assert owner.session_id == "PARENT" and owner.sessions == {"PARENT"}


def test_remark_existing_at_cap_is_noop():
    _owner()
    ids = _fill_cap()
    before = set(REGISTRY.subagents)

    REGISTRY.mark_subagent(ids[0])  # already marked

    assert set(REGISTRY.subagents) == before  # no eviction, no growth


def test_cap_reached_logs_warning(caplog):
    _owner()
    for i in range(CAP - 1):
        REGISTRY.mark_subagent(f"sub-{i}")
    with caplog.at_level(logging.WARNING, logger=LOG):
        REGISTRY.mark_subagent("overflow")  # crosses the cap
    assert any(r.levelno == logging.WARNING and "subagent" in r.getMessage().lower()
               for r in caplog.records), "cap reached must log a loud WARNING"


# ---- end-to-end through the real dispatch/context gates at the cap

async def test_cap_overflow_denied_end_to_end(aiohttp_client):
    c = await start(aiohttp_client)
    ws, welcome = await connect(c, session="PARENT")
    owner = REGISTRY.for_session("PARENT")[0]

    ids = _fill_cap()
    outrigger._subagent_start(child_session_id="OVERFLOW")

    # No live mark lost (deterministic RED under the old arbitrary eviction).
    assert all(REGISTRY.is_subagent(s) for s in ids)
    assert REGISTRY.is_subagent("OVERFLOW")

    # Existing active subagent: denied through dispatch, context and tools.
    out = await call({"tool": "echo", "args": {"text": "x"}},
                     session=ids[0], conversation_id="PARENT")
    assert out["error"] == "outrigger_subagent_denied"
    assert context.build(ids[0], "PARENT", 2000) is None

    # Overflow subagent: denied through every gate too.
    out = await call({"tool": "echo", "args": {"text": "x"}},
                     session="OVERFLOW", conversation_id="PARENT")
    assert out["error"] == "outrigger_subagent_denied"
    assert context.build("OVERFLOW", "PARENT", 2000) is None
    assert json.loads(dispatch.outrigger_tools({}, session_id="OVERFLOW",
                                               conversation_id="PARENT"))["clients"] == []

    # The owner's binding was never stolen by any child.
    assert owner.session_id == "PARENT" and "OVERFLOW" not in owner.sessions
    assert ids[0] not in owner.sessions

    # The owner (normal session) can still call at the cap.
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "owner-ok"}))
    out = await call({"tool": "echo", "args": {"text": "x"}}, session="PARENT")
    assert out["ok"] and out["content"] == "owner-ok"

    outrigger._subagent_stop(child_session_id="OVERFLOW")
    assert not REGISTRY.is_subagent("OVERFLOW")
