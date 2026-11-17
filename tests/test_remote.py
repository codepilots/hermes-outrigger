"""Remote access hardening (docs/remote-access.md): tunnels, key locality, per-user limits."""
import asyncio

import aiohttp
import pytest

from helpers import connect, start
from outrigger import platform, registry, settings, tokens
from outrigger import ws as wsmod

CF = {"CF-Connecting-IP": "203.0.113.9", "CF-Ray": "8f00-LHR"}


async def ws_with(c, key, headers=None, name="sb", caps=("chat",)):
    return await c.ws_connect("/v1/outrigger/ws", headers=headers or {},
                              protocols=("hermes-outrigger.v1", "bearer." + key))


@pytest.mark.parametrize("hdr", [CF, {"X-Forwarded-For": "198.51.100.4"}, {"Forwarded": "for=198.51.100.4"}, {"X-Real-IP": "198.51.100.4"}])
async def test_tunnelled_requests_are_remote(aiohttp_client, monkeypatch, hdr):
    """cloudflared connects from 127.0.0.1; its headers must not make the request local."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    c = await start(aiohttp_client)
    # No key configured: a real local process gets in, a tunnelled one never does.
    w = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1",)); await w.close()
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await c.ws_connect("/v1/outrigger/ws", headers=hdr, protocols=("hermes-outrigger.v1",))
    assert e.value.status == 403
    settings.SETTINGS["allow_remote"] = True  # still no: keyless is local-only
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await c.ws_connect("/v1/outrigger/ws", headers=hdr, protocols=("hermes-outrigger.v1",))


async def test_api_key_stays_local_tokens_go_remote(aiohttp_client, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "k3y")
    _, tok = tokens.mint("me-phone", admin=True)
    c = await start(aiohttp_client)
    for key in ("k3y", tok):  # allow_remote off: nothing remote
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await ws_with(c, key, CF)
        assert e.value.status == 403
    settings.SETTINGS["allow_remote"] = True
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await ws_with(c, "k3y", CF)
    assert e.value.status == 403                       # the key: local only
    w = await ws_with(c, tok, CF); await w.close()     # a token: fine
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await ws_with(c, "otr_00000000_" + "x" * 32, CF)
    assert e.value.status == 401
    w = await ws_with(c, "k3y"); await w.close()       # the key on this machine: fine


def test_owner_id_is_reserved():
    with pytest.raises(ValueError):
        tokens.mint(settings.get("platform_owner_id"))


def test_toolsets_for_users():
    _, _ = tokens.mint("ann")
    _, _ = tokens.mint("me-phone", admin=True)
    assert platform.toolsets_for_user("owner") is None          # platform default (full)
    assert platform.toolsets_for_user("me-phone") is None       # admin token: full
    assert platform.toolsets_for_user("ann") == ["web", "outrigger", "no_mcp"]
    assert platform.toolsets_for_user("stranger") == ["web", "outrigger", "no_mcp"]
    settings.SETTINGS["user_toolsets"] = []
    assert platform.toolsets_for_user("ann") == ["no_mcp"]      # never "no override"


async def test_per_token_cap_and_owner_not_crowded_out(aiohttp_client, monkeypatch):
    monkeypatch.setattr(registry, "MAX_CLIENTS", 6)
    settings.SETTINGS["max_clients_per_token"] = 2
    toks = [tokens.mint(f"u{i}")[1] for i in range(4)]
    c = await start(aiohttp_client)

    async def hello(key, name):
        w = await ws_with(c, key)
        await w.send_json({"type": "hello", "protocol": 2, "client": {"name": name}, "tools": [], "capabilities": []})
        return w, await w.receive_json()

    open_ = []
    for name in ("a", "b"):
        w, m = await hello(toks[0], name); open_.append(w); assert m["type"] == "welcome"
    w, m = await hello(toks[0], "c"); assert m["code"] == "too_many_clients"   # third for one token
    for t in toks[1:]:
        for name in ("a", "b"):
            w, m = await hello(t, name); open_.append(w)
    assert sum(1 for c_ in registry.REGISTRY.clients.values() if c_.principal["kind"] == "token") == 6
    w, m = await connect(c, name="owner-sb")       # tokens are full, the owner still gets in
    assert m["type"] == "welcome"
    for w_ in open_:
        await w_.close()


async def test_user_rate_limit_and_size(aiohttp_client, monkeypatch):
    from test_platform import HANDLED, _install_fake_gateway
    _install_fake_gateway()
    reg = {}
    import types
    platform.register(types.SimpleNamespace(register_platform=lambda **kw: reg.update(kw)))
    ad = reg["adapter_factory"](types.SimpleNamespace(extra={}))
    await ad.connect()
    settings.SETTINGS.update(user_messages_per_min=3, max_chat_chars=50)
    _, tok = tokens.mint("ann")
    c = await start(aiohttp_client)
    w = await ws_with(c, tok)
    await w.send_json({"type": "hello", "protocol": 2, "client": {"name": "sb"}, "tools": [], "capabilities": ["chat"]})
    await w.receive_json()
    await w.send_json({"type": "chat.send", "chat_id": "c1", "text": "x" * 51})
    assert (await w.receive_json())["message"].startswith("text too long")
    got = []
    for i in range(4):
        await w.send_json({"type": "chat.send", "chat_id": "c1", "text": f"hi {i}"})
        while True:
            m = await w.receive_json()
            if m["type"] in ("chat.accepted", "error"):
                got.append(m.get("code") or m["type"]); break
    assert got == ["chat.accepted"] * 3 + ["rate_limited"]
    o, _ = await connect(c, name="owner")              # the owner is never rate limited
    await w.close(); await o.close()
