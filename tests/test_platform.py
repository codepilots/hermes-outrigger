"""Outrigger platform (protocol 2) contract tests.

The gateway modules only exist inside Hermes, so minimal stand-ins mirroring the
public signatures (gateway.platforms.base / .event, gateway.config,
gateway.platform_registry, gateway.session_context) are installed in sys.modules.
"""
import asyncio
import dataclasses
import enum
import json
import sys
import types

import pytest

from helpers import TOOLS, call, start

# ---------------------------------------------------------------- gateway stand-ins
SESSION_ENV: dict = {}
HANDLED: list = []


def _install_fake_gateway():
    gw = types.ModuleType("gateway")
    cfg = types.ModuleType("gateway.config")
    cfg.Platform = lambda name: f"Platform({name})"
    cfg.PlatformConfig = type("PlatformConfig", (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})
    reg = types.ModuleType("gateway.platform_registry")

    @dataclasses.dataclass
    class PlatformEntry:
        name: str = ""
        is_connected: object = None
        env_enablement_fn: object = None
        display_tier: str = ""
        display_overrides: dict = dataclasses.field(default_factory=dict)
        allowlist_suppresses_pairing: bool = False
    reg.PlatformEntry = PlatformEntry

    plats = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")

    @dataclasses.dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config, self.platform, self.connected = config, platform, False

        def _mark_connected(self, **kw):
            self.connected = True

        def _mark_disconnected(self):
            self.connected = False

        def build_source(self, chat_id, chat_name=None, chat_type="dm", user_id=None, user_name=None, **kw):
            return {"chat_id": chat_id, "chat_name": chat_name, "chat_type": chat_type,
                    "user_id": user_id, "user_name": user_name, **kw}

        async def handle_message(self, event):
            HANDLED.append(event)
    base.SendResult, base.BasePlatformAdapter = SendResult, BasePlatformAdapter

    ev = types.ModuleType("gateway.platforms.event")

    class MessageType(enum.Enum):
        TEXT = "text"

    @dataclasses.dataclass
    class MessageEvent:
        text: str
        message_type: MessageType = MessageType.TEXT
        source: dict = None
        message_id: str | None = None
    ev.MessageType, ev.MessageEvent = MessageType, MessageEvent

    sc = types.ModuleType("gateway.session_context")
    sc.get_session_env = lambda name, default="": SESSION_ENV.get(name, default)

    for name, mod in {"gateway": gw, "gateway.config": cfg, "gateway.platform_registry": reg,
                      "gateway.platforms": plats, "gateway.platforms.base": base,
                      "gateway.platforms.event": ev, "gateway.session_context": sc}.items():
        sys.modules[name] = mod


@pytest.fixture
def adapter(monkeypatch):
    saved = {k: v for k, v in sys.modules.items() if k == "gateway" or k.startswith("gateway.")}
    _install_fake_gateway()
    SESSION_ENV.clear(); HANDLED.clear()
    from outrigger import platform
    platform.CHATS.clear(); platform.OUTBOX.clear()

    registered = {}

    class Ctx:
        def register_platform(self, **kw): registered.update(kw)

    assert platform.register(Ctx())
    a = registered["adapter_factory"](object())
    a.registered = registered
    yield a
    platform.ADAPTER = None
    for k in [k for k in sys.modules if k == "gateway" or k.startswith("gateway.")]:
        del sys.modules[k]
    sys.modules.update(saved)


async def chat_client(c, name="sb", caps=("chat",), session="S1"):
    ws = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1",))
    await ws.send_json({"type": "hello", "protocol": 2, "client": {"name": name}, "session_id": session,
                        "tools": TOOLS, "capabilities": list(caps)})
    welcome = await ws.receive_json()
    assert welcome["type"] == "welcome", welcome
    return ws, welcome


async def next_of(ws, kind, timeout=2):
    while True:
        m = await ws.receive_json(timeout=timeout)
        if m["type"] == kind:
            return m


# ---------------------------------------------------------------- tests

def test_registration(adapter):
    r = adapter.registered
    assert r["name"] == "outrigger" and r["label"] == "Outrigger"
    assert r["is_connected"](None) is True
    assert r["env_enablement_fn"]() == {"allow_from": ["owner"],
                                        "home_channel": {"chat_id": "home", "name": "Outrigger home"}}
    assert r["display_tier"] == "medium" and "display_overrides" not in r
    assert r["max_message_length"] == 0


def test_not_registered_when_disabled(monkeypatch):
    from outrigger import platform, settings
    settings.SETTINGS["platform"] = False

    class Ctx:
        def register_platform(self, **kw): raise AssertionError("must not register")
    assert platform.register(Ctx()) is False


async def test_welcome_reports_platform(aiohttp_client, adapter):
    c = await start(aiohttp_client)
    _, w = await chat_client(c)
    assert w["platform"] == {"name": "outrigger", "available": False}
    await adapter.connect()
    _, w2 = await chat_client(c, name="sb2")
    assert w2["platform"]["available"] is True


async def test_chat_send_becomes_message_event(aiohttp_client, adapter):
    await adapter.connect()
    c = await start(aiohttp_client)
    ws, _ = await chat_client(c)
    await ws.send_json({"type": "chat.send", "chat_id": "conv-1", "text": "/model", "client_msg_id": "m1"})
    acc = await next_of(ws, "chat.accepted")
    await asyncio.sleep(0.05)
    assert acc["client_msg_id"] == "m1"
    [ev] = HANDLED
    assert ev.text == "/model" and ev.message_id == "m1"
    assert ev.source["chat_id"] == "sb:conv-1" and ev.source["user_id"] == "owner"


async def test_replies_and_streaming_edits(aiohttp_client, adapter):
    await adapter.connect()
    c = await start(aiohttp_client)
    ws, _ = await chat_client(c)
    await ws.send_json({"type": "chat.send", "chat_id": "conv-1", "text": "hi"})
    await next_of(ws, "chat.accepted")
    r = await adapter.send("sb:conv-1", "Hel")
    msg = await next_of(ws, "chat.message")
    assert msg["chat_id"] == "conv-1" and msg["text"] == "Hel" and msg["message_id"] == r.message_id
    await adapter.edit_message("sb:conv-1", r.message_id, "Hello!", finalize=True)
    ed = await next_of(ws, "chat.edit")
    assert ed == {"type": "chat.edit", "chat_id": "conv-1", "message_id": r.message_id, "text": "Hello!", "final": True}
    await adapter.send_typing("sb:conv-1")
    assert (await next_of(ws, "chat.typing"))["chat_id"] == "conv-1"


async def test_outbox_while_away_then_attach(aiohttp_client, adapter):
    await adapter.connect()
    c = await start(aiohttp_client)
    ws, _ = await chat_client(c)
    await ws.send_json({"type": "chat.send", "chat_id": "conv-1", "text": "hi"})
    await next_of(ws, "chat.accepted")
    await ws.close()
    await asyncio.sleep(0.1)
    r = await adapter.send("sb:conv-1", "answer while you were away")
    assert r.success
    ws2, _ = await chat_client(c)
    await ws2.send_json({"type": "chat.attach", "chat_id": "conv-1"})
    msg = await next_of(ws2, "chat.message")
    assert msg["text"] == "answer while you were away" and msg["chat_id"] == "conv-1"


async def test_stop_and_chat_ownership(aiohttp_client, adapter):
    await adapter.connect()
    c = await start(aiohttp_client)
    a, _ = await chat_client(c, name="a")
    b, _ = await chat_client(c, name="b")
    await a.send_json({"type": "chat.send", "chat_id": "conv-1", "text": "work"})
    await next_of(a, "chat.accepted")
    await b.send_json({"type": "chat.stop", "chat_id": "conv-1"})  # b's own namespace: never sent there
    assert (await next_of(b, "error"))["code"] == "not_your_chat"
    await a.send_json({"type": "chat.stop", "chat_id": "conv-1"})
    await asyncio.sleep(0.1)
    assert [e.text for e in HANDLED] == ["work", "/stop"]
    assert HANDLED[-1].source["chat_id"] == "a:conv-1"


async def test_chat_needs_capability_and_platform(aiohttp_client, adapter):
    c = await start(aiohttp_client)
    ws, _ = await chat_client(c, caps=())
    await ws.send_json({"type": "chat.send", "chat_id": "x", "text": "hi"})
    assert (await next_of(ws, "error"))["code"] == "no_chat_capability"
    ws2, _ = await chat_client(c, name="sb2")
    await ws2.send_json({"type": "chat.send", "chat_id": "x", "text": "hi"})
    assert (await next_of(ws2, "error"))["code"] == "platform_unavailable"
    await adapter.connect()
    await ws2.send_json({"type": "chat.send", "chat_id": "bad id!", "text": "hi"})
    assert (await next_of(ws2, "error"))["code"] == "bad_chat"


async def test_tools_route_by_platform_chat(aiohttp_client, adapter):
    """On a platform turn, outrigger_call finds the client from the chat id alone."""
    from outrigger import context
    from outrigger.registry import REGISTRY
    await adapter.connect()
    c = await start(aiohttp_client)
    ws, _ = await chat_client(c, session=None)          # bound to no session at all
    await ws.send_json({"type": "chat.send", "chat_id": "conv-1", "text": "hi"})
    await next_of(ws, "chat.accepted")
    SESSION_ENV.update({"HERMES_SESSION_PLATFORM": "outrigger", "HERMES_SESSION_CHAT_ID": "sb:conv-1"})
    assert "[Attached outriggers]" in context.build("20261117_x", None, 4500)

    async def answer():
        m = await next_of(ws, "call")
        await ws.send_json({"type": "result", "call_id": m["call_id"], "ok": True, "content": "done"})
    t = asyncio.create_task(answer())
    out = await call({"tool": "big", "args": {}}, session="20261117_x")
    await t
    assert out["ok"] and out["content"] == "done"
    # a different chat on the platform gets nothing
    SESSION_ENV["HERMES_SESSION_CHAT_ID"] = "sb:other"
    assert (await call({"tool": "big", "args": {}}, session="20261117_y"))["error"] == "outrigger_no_client"
    # subagents stay blocked on platform turns too
    SESSION_ENV["HERMES_SESSION_CHAT_ID"] = "sb:conv-1"
    REGISTRY.mark_subagent("child")
    assert (await call({"tool": "big", "args": {}}, session="child"))["error"] == "outrigger_subagent_denied"


async def test_home_chat_is_shared_and_buffered(aiohttp_client, adapter):
    await adapter.connect()
    r = await adapter.send("home", "cron: nightly report")      # no client yet: buffered
    assert r.success
    c = await start(aiohttp_client)
    ws, _ = await chat_client(c, name="phone")
    await ws.send_json({"type": "chat.attach", "chat_id": "home"})
    m = await next_of(ws, "chat.message")
    assert m["chat_id"] == "home" and m["text"] == "cron: nightly report"
