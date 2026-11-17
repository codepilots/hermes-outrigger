"""Per-client tokens (P4): store, authentication, restrictions, Hermes access lists, CLI."""
import argparse
import asyncio
import json
import os
import stat
import types

import aiohttp
import pytest

from outrigger import cli, settings, tokens, ws as wsmod
from outrigger.registry import REGISTRY

from helpers import TOOLS, start
from test_platform import ALL_CAPS, adapter, chat_client, next_of, act, HANDLED  # noqa: F401 (fixture)


# ---------------------------------------------------------------- store

def test_mint_verify_revoke(tmp_path):
    tid, tok = tokens.mint("ann", user_name="Ann", label="phone")
    assert tok.startswith(f"otr_{tid}_") and tokens.TOKEN_RE.match(tok)
    rec = tokens.verify(tok)
    assert rec == {"id": tid, "user_id": "ann", "user_name": "Ann", "label": "phone", "admin": False}
    raw = tokens.path().read_text()
    assert tok.split("_", 2)[2] not in raw                       # only the hash is stored
    assert stat.S_IMODE(os.stat(tokens.path()).st_mode) == 0o600
    assert tokens.verify(tok[:-1] + ("A" if tok[-1] != "A" else "B")) is None
    assert tokens.verify("otr_deadbeef_" + "x" * 32) is None
    assert tokens.verify("not a token") is None
    assert tokens.revoke(tid) and not tokens.revoke(tid)
    assert tokens.verify(tok) is None and not tokens.is_active(tid)
    assert tokens.listing()[0]["revoked"]


def test_user_id_validated():
    with pytest.raises(ValueError):
        tokens.mint("bad id with spaces")


def test_store_changes_are_picked_up(tmp_path):
    tid, tok = tokens.mint("bob")
    data = json.loads(tokens.path().read_text())               # another process revokes it
    data["tokens"][tid]["revoked"] = 1
    tokens.path().write_text(json.dumps(data))
    os.utime(tokens.path(), ns=(1, 10**18))                    # make sure the mtime differs
    assert tokens.verify(tok) is None


# ---------------------------------------------------------------- authentication

async def test_token_authenticates_by_header_and_subprotocol(aiohttp_client, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "owner-key")
    _, tok = tokens.mint("ann")
    c = await start(aiohttp_client)
    for kw in ({"headers": {"Authorization": "Bearer " + tok}, "protocols": ("hermes-outrigger.v1",)},
               {"protocols": ("hermes-outrigger.v1", "bearer." + tok)}):
        w = await c.ws_connect("/v1/outrigger/ws", **kw)
        await w.send_json({"type": "hello", "protocol": 2, "client": {"name": "x"}, "tools": []})
        assert (await w.receive_json())["type"] == "welcome"
        await w.close()
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1", "bearer.otr_00000000_" + "a" * 32))
    assert e.value.status == 401


async def test_token_remote_needs_allow_remote(aiohttp_client, monkeypatch):
    _, tok = tokens.mint("ann")
    monkeypatch.setattr(wsmod, "_is_loopback", lambda r: False)
    c = await start(aiohttp_client)
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1", "bearer." + tok))
    assert e.value.status == 403
    settings.SETTINGS["allow_remote"] = True
    w = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1", "bearer." + tok))
    await w.close()


# ---------------------------------------------------------------- token clients on the platform

async def token_client(c, tok, name="sb", session="OWNER-SESSION"):
    w = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1", "bearer." + tok))
    await w.send_json({"type": "hello", "protocol": 2, "client": {"name": name}, "session_id": session,
                       "tools": TOOLS, "capabilities": list(ALL_CAPS)})
    assert (await w.receive_json())["type"] == "welcome"
    return w


async def test_token_client_restrictions(aiohttp_client, adapter):
    await adapter.connect()
    tid, tok = tokens.mint("ann", user_name="Ann")
    c = await start(aiohttp_client)
    w = await token_client(c, tok)
    # cannot ride on an owner's API session, by hello or by bind
    assert REGISTRY.for_session("OWNER-SESSION") == []
    await w.send_json({"type": "bind", "session_id": "OWNER-SESSION"})
    assert (await next_of(w, "error"))["code"] == "not_allowed"
    # chats are namespaced by token; identity is the token's user
    await w.send_json({"type": "chat.send", "chat_id": "conv-1", "text": "hi"})
    await next_of(w, "chat.accepted"); await asyncio.sleep(0.05)
    ev = HANDLED[-1]
    assert ev.source["chat_id"] == f"t-{tid}:sb:conv-1" and ev.source["user_id"] == "ann" and ev.source["user_name"] == "Ann"
    # the owner's home chat is not theirs
    await w.send_json({"type": "chat.send", "chat_id": "home", "text": "x"})
    await next_of(w, "chat.accepted"); await asyncio.sleep(0.05)
    assert HANDLED[-1].source["chat_id"] == f"t-{tid}:sb:home"


async def test_same_client_name_different_users_are_isolated(aiohttp_client, adapter):
    await adapter.connect()
    _, tok_a = tokens.mint("ann")
    _, tok_b = tokens.mint("bob")
    c = await start(aiohttp_client)
    a = await token_client(c, tok_a)
    b = await token_client(c, tok_b)
    await a.send_json({"type": "chat.send", "chat_id": "conv-1", "text": "ann's"})
    await next_of(a, "chat.accepted")
    await b.send_json({"type": "chat.attach", "chat_id": "conv-1"})      # same name, same local id
    key_a = HANDLED[-1].source["chat_id"] if HANDLED else None
    await asyncio.sleep(0.05)
    await adapter.send(HANDLED[-1].source["chat_id"], "for ann only")
    assert (await next_of(a, "chat.message"))["text"] == "for ann only"
    with pytest.raises(asyncio.TimeoutError):
        await next_of(b, "chat.message", timeout=0.3)


async def test_approval_actor_is_the_token_user(aiohttp_client, adapter):
    import sys
    await adapter.connect()
    _, tok = tokens.mint("ann")
    c = await start(aiohttp_client)
    w = await token_client(c, tok)
    await w.send_json({"type": "chat.send", "chat_id": "conv-1", "text": "hi"})
    await next_of(w, "chat.accepted"); await asyncio.sleep(0.05)
    prompt = types.SimpleNamespace(chat_id=HANDLED[-1].source["chat_id"], session_key="sk", text="", command="ls",
                                   description="", smart_denied=False, request_id="r1",
                                   actions=[("Approve", "once", "primary"), ("Deny", "deny", "danger")])
    await adapter._send_exec_approval_prompt(prompt)
    card = await next_of(w, "chat.approval")
    assert await act(w, "approval", card["id"], "once") == "ok"
    assert sys.modules["tools.approval"].CALLS[-1][3] == "ann"


async def test_hermes_access_lists_follow_the_token_store(adapter):
    adapter.config = types.SimpleNamespace(extra={})
    await adapter.connect()
    assert adapter.config.extra["allow_from"] == ["owner"]
    tid, _ = tokens.mint("ann")
    tokens.mint("root2", admin=True)
    adapter.sync_access()
    ex = adapter.config.extra
    assert ex["allow_from"] == ["ann", "owner", "root2"] and ex["allow_admin_from"] == ["owner", "root2"]
    assert "model" not in ex["user_allowed_commands"] and "new" in ex["user_allowed_commands"]
    tokens.revoke(tid); adapter.sync_access()
    assert "ann" not in adapter.config.extra["allow_from"]


async def test_revoking_closes_live_connection(aiohttp_client, monkeypatch):
    monkeypatch.setattr(wsmod, "PING_INTERVAL", 0.05)
    tid, tok = tokens.mint("ann")
    c = await start(aiohttp_client)
    w = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1", "bearer." + tok))
    await w.send_json({"type": "hello", "protocol": 2, "client": {"name": "x"}, "tools": []})
    await w.receive_json()
    tokens.revoke(tid)
    async with asyncio.timeout(2):
        while True:
            m = await w.receive()
            if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                break
    assert w.close_code == 4401


# ---------------------------------------------------------------- CLI

def test_cli_pair_list_revoke(capsys):
    parser = argparse.ArgumentParser(); cli.setup(parser)
    assert cli.handle(parser.parse_args(["pair", "--user", "ann", "--label", "phone", "--admin"])) == 0
    out = capsys.readouterr().out
    tok = next(w for w in out.split() if w.startswith("otr_"))
    assert tokens.verify(tok)["admin"] is True
    assert cli.handle(parser.parse_args(["tokens"])) == 0
    listing = capsys.readouterr().out
    assert "ann" in listing and "admin" in listing and "phone" in listing and tok not in listing
    tid = tok.split("_")[1]
    assert cli.handle(parser.parse_args(["revoke", tid])) == 0
    assert cli.handle(parser.parse_args(["revoke", tid])) == 1
    assert cli.handle(parser.parse_args(["pair", "--user", "bad id"])) == 2
