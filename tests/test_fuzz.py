"""Hostile input: every message type and field with wrong types, huge or odd values.
Each must get an error frame (or be ignored); the connection must stay usable and
nothing may raise out of the handler."""
import asyncio
import itertools
import json
import logging
import types

import pytest

from helpers import start
from outrigger import platform, settings, tokens

NASTY = [None, True, -1, 0, 10**30, 1.5, "NaN", float("inf"), "", "x" * 300_000, "\x00", "\ud800", "..", "../../etc/passwd.md",
         [], {}, [None], {"a": None}, "a" * 129, -10**30]
# Raw JSON text the server must survive: NaN/Infinity literals, deep nesting, lone surrogates.
RAW = ['{"type": NaN}', '{"type": "chat.send", "chat_id": "c", "text": Infinity}', "[" * 50_000 + "]" * 50_000,
       '{"type": "context.update", "summary": "\\ud800\\udfff\\ud800"}', '{"type": "' + "\\ud800" * 1000 + '"}',
       "not json", "", "{}", "[]", "1", '"str"', '{"type": 1}', '{"type": null}', '{"type": ["chat.send"]}',
       '{"type": "result", "call_id": "x", "ok": true, "content": ' + "[" * 5000 + "]" * 5000 + "}"]

MSGS = {
    "bind": ["session_id"], "tools.update": ["tools"], "context.update": ["summary"],
    "result": ["call_id", "ok", "content", "error"], "progress": ["call_id", "message"],
    "chat.send": ["chat_id", "text", "client_msg_id"], "chat.attach": ["chat_id"], "chat.stop": ["chat_id"],
    "chat.action": ["chat_id", "kind", "id", "value"], "chat.commands": [],
    "doc.list": ["root", "query", "limit"], "doc.open": ["root", "path"], "doc.close": ["doc_id"],
    "doc.update": ["doc_id", "base_version", "text"], "doc.undo": ["doc_id", "snapshot"],
    "doc.command": ["doc_id", "base_version", "line", "instruction", "selection"],
}
GOOD = {"chat_id": "c1", "text": "hi", "kind": "model", "id": "p1", "root": "notes", "path": "a.md",
        "doc_id": "d1", "call_id": "call_x", "ok": True, "session_id": "S", "tools": [], "summary": "s"}


class Boom(logging.Handler):
    def __init__(self): super().__init__(); self.records = []
    def emit(self, r):
        if r.exc_info or r.levelno >= logging.ERROR: self.records.append(r)


@pytest.fixture
def boom():
    h = Boom(); root = logging.getLogger(); root.addHandler(h)
    yield h
    root.removeHandler(h)


@pytest.fixture
def plat(tmp_path):
    from test_platform import _install_fake_gateway
    _install_fake_gateway()
    reg = {}
    platform.register(types.SimpleNamespace(register_platform=lambda **kw: reg.update(kw)))
    ad = reg["adapter_factory"](types.SimpleNamespace(extra={}))
    (tmp_path / "v").mkdir(); (tmp_path / "v" / "a.md").write_text("# a\n")
    settings.SETTINGS.update(notebook_roots={"notes": str(tmp_path / "v")}, notebook_roots_for={"ann": ["notes"]},
                             user_messages_per_min=0)
    return ad


async def alive(w) -> bool:
    """The connection still answers: an unknown type must come back as an error frame."""
    await w.send_str('{"type": "zz.alive"}')
    for _ in range(50):
        m = await w.receive(timeout=5)
        if m.type.name != "TEXT":
            return False
        if json.loads(m.data).get("code") == "unknown_type" and "zz.alive" in json.loads(m.data).get("message", ""):
            return True
    return False


async def open_client(c, key=None):
    protos = ("hermes-outrigger.v1",) + (("bearer." + key,) if key else ())
    w = await c.ws_connect("/v1/outrigger/ws", protocols=protos, max_msg_size=0)
    await w.send_json({"type": "hello", "protocol": 2, "client": {"name": "fz"}, "tools": [], "capabilities": ["chat", "notebook"]})
    assert (await w.receive_json())["type"] == "welcome"
    return w


@pytest.mark.parametrize("who", ["owner", "token"])
async def test_fuzz_messages(aiohttp_client, plat, boom, who):
    await plat.connect()
    key = tokens.mint("ann")[1] if who == "token" else None
    c = await start(aiohttp_client)
    w = await open_client(c, key)
    from outrigger import notebook
    sent = 0

    async def fresh_doc():
        for d in list(notebook.DOCS.values()):
            if d.lock:
                notebook.end_command(d)
        await w.send_json({"type": "doc.open", "root": "notes", "path": "a.md"})
        while True:
            m = json.loads((await w.receive(timeout=5)).data)
            if m["type"] == "doc.content":
                return m

    for t, fields in MSGS.items():
        cases = [{f: v} for f in fields for v in NASTY] + [{"_whole": v} for v in NASTY[:6]]
        for case in cases:
            good = dict(GOOD)
            if t.startswith("doc.") and t not in ("doc.list", "doc.open"):
                d = await fresh_doc()
                good.update(doc_id=d["doc_id"], base_version=d["version"], line=1, instruction="fix",
                            text="# a\n", snapshot="")
            msg = {"type": t, **{f: good.get(f) for f in fields}}
            if "_whole" in case:
                msg = case["_whole"] if not isinstance(case["_whole"], dict) else {"type": t}
                await w.send_str(json.dumps(msg) if not isinstance(msg, str) else msg)
            else:
                msg.update(case)
                await w.send_str(json.dumps(msg, allow_nan=True))
            sent += 1
            if sent % 25 == 0:
                assert await alive(w), f"connection died after {t} {str(case)[:80]}"
    for raw in RAW:
        await w.send_str(raw)
        assert await alive(w), f"connection died after raw {raw[:60]!r}"
    assert await alive(w)
    await w.close()
    assert not boom.records, [(r.getMessage()[:200], r.exc_info and repr(r.exc_info[1])[:200]) for r in boom.records[:5]]


@pytest.mark.parametrize("hello", [
    {"type": "hello"}, {"type": "hello", "protocol": "2"}, {"type": "hello", "protocol": 2, "client": None},
    {"type": "hello", "protocol": 2, "client": {"name": "\ud800"}}, {"type": "hello", "protocol": 2, "client": {"name": "x" * 10**6}},
    {"type": "hello", "protocol": 2, "client": {"name": "a", "concurrency": 10**9}},
    {"type": "hello", "protocol": 2, "client": {"name": "a"}, "tools": [{"name": "t", "parameters": {"type": "object"}}] * 65},
    {"type": "hello", "protocol": 2, "client": {"name": "a"}, "tools": "x"},
    {"type": "hello", "protocol": 2, "client": {"name": "a"}, "capabilities": [None, 1, "x" * 10**4] * 100},
    {"type": "hello", "protocol": 2, "client": {"name": "a"}, "redaction": {"enabled": True, "categories": [{}] * 10**4}},
    {"type": "hello", "protocol": 2, "client": {"name": "a"}, "session_id": ["S"]},
])
async def test_fuzz_hello(aiohttp_client, boom, hello):
    c = await start(aiohttp_client)
    w = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1",))
    await w.send_str(json.dumps(hello))
    m = await w.receive(timeout=5)
    assert m.type.name in ("TEXT", "CLOSE", "CLOSED", "CLOSING")
    if m.type.name == "TEXT":
        assert json.loads(m.data)["type"] in ("welcome", "error")
    await w.close()
    assert not boom.records, [r.getMessage()[:200] for r in boom.records[:3]]


async def test_preauth_headers(aiohttp_client, boom, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "k3y")
    c = await start(aiohttp_client)
    import aiohttp
    for hdrs, protos in [({"Authorization": "Bearer " + "x" * 7000}, ("hermes-outrigger.v1",)),
                         ({}, ("hermes-outrigger.v1", "bearer." + "y" * 7000)),
                         ({"Authorization": "Bearer otr_" + "z" * 7000}, ("hermes-outrigger.v1",)),
                         ({}, tuple(f"p{i}" for i in range(500))),
                         ({"Authorization": "Bearer éÿ"}, ("hermes-outrigger.v1",)),
                         ({"Authorization": "Bearer"}, ("hermes-outrigger.v1",)),
                         ({"Authorization": "Basic abc"}, ("hermes-outrigger.v1",))]:
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await c.ws_connect("/v1/outrigger/ws", headers=hdrs, protocols=protos)
        assert e.value.status in (400, 401, 403, 431)
    assert not boom.records, [r.getMessage()[:200] for r in boom.records[:3]]


async def test_oversized_message_closes_cleanly(aiohttp_client, boom):
    c = await start(aiohttp_client)
    w = await open_client(c)
    try:
        await w.send_str('{"type": "context.update", "summary": "' + "x" * (5 * 1024 * 1024) + '"}')
        m = await w.receive(timeout=5)
        assert m.type.name in ("CLOSE", "CLOSED", "CLOSING", "ERROR")
    except ConnectionError:
        pass
    assert not [r for r in boom.records if "outrigger" in r.name]
    w2 = await open_client(c)  # the server is fine
    await w2.close()


# ---- client-declared schemas and image results

import time as _time

from outrigger.protocol import ProtocolError, ToolDecl, check_schema, validate_args


def test_schema_limits():
    deep = {"type": "object"}
    for _ in range(30):
        deep = {"type": "object", "properties": {"a": deep}}
    assert "deeper" in check_schema(deep)
    assert "larger" in check_schema({"type": "object", "description": "x" * 40_000})
    assert "patternProperties" in check_schema({"type": "object", "patternProperties": {"(a+)+$": {}}})
    assert "invalid" in check_schema({"type": 12})
    with pytest.raises(ProtocolError):
        ToolDecl.parse({"name": "t", "parameters": deep})
    assert check_schema({"type": "object", "properties": {"q": {"type": "string", "pattern": "^a"}}}) is None


def test_redos_pattern_is_not_run_on_the_gateway():
    schema = {"type": "object", "properties": {"q": {"type": "string", "pattern": "^(a+)+$"}}}
    t = _time.monotonic()
    assert validate_args(schema, {"q": "a" * 40 + "!"}) is None   # would take minutes with re
    assert _time.monotonic() - t < 1
    assert validate_args(schema, {"q": 5}) is not None              # types still enforced


def test_recursive_ref_is_a_clean_error():
    schema = {"$ref": "#", "allOf": [{"$ref": "#"}]}
    assert isinstance(validate_args(schema, {"a": 1}), str)
    assert isinstance(validate_args({"$ref": "https://example.com/s.json"}, {}), str)  # no fetching


def test_image_must_match_its_type(tmp_path, monkeypatch):
    import base64
    from outrigger import dispatch
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake = {"mime": "image/png", "data": base64.b64encode(b"#!/bin/sh\necho hi\n").decode()}
    assert "not a image/png" in dispatch._save_capture(fake)["image_error"]
    png = {"mime": "image/png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 20).decode()}
    assert dispatch._save_capture(png)["image_path"].endswith(".png")


async def test_chat_and_outbox_caps(aiohttp_client, plat, monkeypatch):
    await plat.connect()
    monkeypatch.setattr(platform, "MAX_CHATS_PER_CLIENT", 5)
    monkeypatch.setattr(platform, "OUTBOX_CHATS_MAX", 3)
    c = await start(aiohttp_client)
    w = await open_client(c, tokens.mint("ann")[1])
    codes = []
    for i in range(7):
        await w.send_json({"type": "chat.attach", "chat_id": f"c{i}"})
    await w.send_str('{"type": "zz.alive"}')
    while True:
        m = await w.receive_json(timeout=5)
        if m.get("type") == "error":
            codes.append(m["code"])
            if m["code"] == "unknown_type":
                break
    assert codes == ["too_many_chats", "too_many_chats", "unknown_type"]
    await w.close(); await asyncio.sleep(0.05)
    for i in range(5):  # Hermes replies while the client is away
        await plat.send(f"t-x:sb:k{i}", "reply")
    assert len(platform.OUTBOX) == 3


def test_undecodable_key_bytes_fail_cleanly(monkeypatch):
    from outrigger import ws as wsmod
    monkeypatch.setenv("API_SERVER_KEY", "k3y")
    # What aiohttp hands over for undecodable header bytes (surrogate-escaped text).
    req = types.SimpleNamespace(headers={"Authorization": "Bearer \udcff\udcfe"}, remote="127.0.0.1")
    denied, principal = wsmod.authenticate(req)
    assert denied is not None and principal is None
