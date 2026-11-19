"""F-10 auth trust boundary: credential source, locality heuristic, admin scope.

Mitigation tests for the three deployment-conditional sub-findings
(cand_76d74ef9 credential-source mismatch, cand_e75c7051 locality fail-open,
cand_ed211e05 admin-scope mismatch). See docs/security-review/auth-trust-boundary.md
for what these mitigations do NOT prove (the residuals pinned below).
"""
import asyncio
import logging
import types

import aiohttp
import pytest
from aiohttp import web

from helpers import connect, start
from outrigger import platform, settings, tokens
from outrigger import ws as wsmod
from outrigger.registry import REGISTRY
from outrigger.ws import ws_handler
from test_platform import HANDLED, adapter  # noqa: F401  (fixture reuse; fake gateway)

LOG = "hermes.plugins.outrigger"
CF = {"CF-Connecting-IP": "203.0.113.9", "CF-Ray": "8f00-LHR"}
USER_TS = ["web", "outrigger", "no_mcp"]


@pytest.fixture(autouse=True)
def _boundary_state():
    """State-neutral: the suite has persistent fake-gateway/adapter fixtures."""
    saved = (dict(platform.CHATS), dict(platform.OUTBOX), dict(platform.PENDING), platform.ADAPTER)
    platform.CHATS.clear(); platform.OUTBOX.clear(); platform.PENDING.clear()
    platform.ADAPTER = None
    yield
    platform.CHATS.clear(); platform.CHATS.update(saved[0])
    platform.OUTBOX.clear(); platform.OUTBOX.update(saved[1])
    platform.PENDING.clear(); platform.PENDING.update(saved[2])
    platform.ADAPTER = saved[3]


async def _client(aiohttp_client, adapter_key=None):
    """A harness app; adapter_key simulates the gateway's resolved _api_key
    (extra['key'] / profile-scoped secret / env), exposed as app['api_server_adapter']."""
    app = web.Application()
    if adapter_key is not None:
        app["api_server_adapter"] = types.SimpleNamespace(_api_key=adapter_key)
    app.router.add_get("/v1/outrigger/ws", ws_handler)
    return await aiohttp_client(app)


async def _ws(c, key=None, headers=None):
    protos = ("hermes-outrigger.v1",) + (("bearer." + key,) if key else ())
    return await c.ws_connect("/v1/outrigger/ws", headers=headers or {}, protocols=protos)


async def _refused(c, status, key=None, headers=None):
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await _ws(c, key, headers)
    assert e.value.status == status


async def _chat_client(c, tok, name="sb"):
    w = await _ws(c, tok)
    await w.send_json({"type": "hello", "protocol": 2, "client": {"name": name},
                       "tools": [], "capabilities": ["chat"]})
    m = await w.receive_json()
    assert m["type"] == "welcome", m
    return w


def _fake_chat_client(user_id, admin, tid="abcd1234", name="sb"):
    c = types.SimpleNamespace(name=name, closed=False,
                              principal={"kind": "token", "user_id": user_id, "admin": admin, "id": tid})
    platform.CHATS[platform.chat_key(c, "c1")] = c
    return c


# ---------------------------------------------------------------- (1) credential source

async def test_config_keyed_gateway_refuses_keyless_local_owner(aiohttp_client, monkeypatch):
    """Gateway key only in extra['key'] (adapter resolved it): loopback with no
    credential must NOT become OWNER; the gateway's key unlocks the route."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    c = await _client(aiohttp_client, adapter_key="gw-config-key")
    await _refused(c, 401)                                   # no credential
    w = await _ws(c, "gw-config-key")                        # the gateway's key works
    await w.send_json({"type": "hello", "protocol": 1, "client": {"name": "own"}, "tools": []})
    assert (await w.receive_json())["type"] == "welcome"
    cl = next(x for x in REGISTRY.clients.values() if x.name == "own")
    assert cl.principal["kind"] == "owner"
    await w.close()


async def test_config_keyed_gateway_wrong_key_refused(aiohttp_client, monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    c = await _client(aiohttp_client, adapter_key="gw-config-key")
    await _refused(c, 401, key="not-the-key")


async def test_adapter_resolved_key_wins_over_stale_env(aiohttp_client, monkeypatch):
    """The adapter's resolved key is the gateway's live truth (scoped secrets,
    extra['key']); a different raw env value must not be the expected key."""
    monkeypatch.setenv("API_SERVER_KEY", "env-key")
    c = await _client(aiohttp_client, adapter_key="scoped-key")
    await _refused(c, 401, key="env-key")
    w = await _ws(c, "scoped-key"); await w.close()


async def test_plugin_auth_token_still_accepted_and_keyless_refused(aiohttp_client, monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    settings.SETTINGS["auth_token"] = "plug-key"
    c = await _client(aiohttp_client, adapter_key="gw-config-key")
    await _refused(c, 401)                                   # keyless no longer admitted
    w = await _ws(c, "plug-key"); await w.close()            # the plugin's own key works


async def test_env_key_behavior_unchanged(aiohttp_client, monkeypatch):
    """Regression guard: raw env key (documented quickstart) still required locally."""
    monkeypatch.setenv("API_SERVER_KEY", "k3y")
    c = await start(aiohttp_client)
    await _refused(c, 401)
    w = await _ws(c, "k3y"); await w.close()


async def test_keyless_local_owner_preserved_when_no_key_anywhere(aiohttp_client, monkeypatch):
    """Regression guard: genuine keyless local dev (no gateway adapter, no key)."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    c = await start(aiohttp_client)
    _, welcome = await connect(c, name="local-dev")
    assert welcome["type"] == "welcome"


async def test_keyless_admission_is_loud(aiohttp_client, monkeypatch, caplog):
    """Fail-loud directive: every keyless-local-owner admission warns, actionably."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    caplog.set_level(logging.WARNING, logger=LOG)
    c = await start(aiohttp_client)
    await connect(c, name="local-dev")
    msgs = [r.getMessage() for r in caplog.records if r.name == LOG and r.levelno >= logging.WARNING]
    assert any("keyless" in m and "allow_keyless_local" in m for m in msgs), msgs


async def test_gateway_keyless_adapter_local_still_admitted(aiohttp_client, monkeypatch):
    """Adapter present but itself keyless (unsupported manual wiring): local trust
    stays available, loudly warned; remote still refused."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    c = await _client(aiohttp_client, adapter_key="")
    await connect(c, name="local-dev")
    await _refused(c, 403, headers=CF)


async def test_allow_keyless_local_off_refuses_local_owner(aiohttp_client, monkeypatch, caplog):
    """Operator control: disable keyless local trust entirely (shared hosts)."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    settings.SETTINGS["allow_keyless_local"] = False
    caplog.set_level(logging.WARNING, logger=LOG)
    c = await start(aiohttp_client)
    await _refused(c, 403)
    assert any("allow_keyless_local" in r.getMessage() for r in caplog.records if r.name == LOG)
    # paired tokens are unaffected by the keyless switch
    _, tok = tokens.mint("ann")
    w = await _ws(c, tok); await w.close()


async def test_allow_keyless_local_off_does_not_touch_keyed_auth(aiohttp_client, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "k3y")
    settings.SETTINGS["allow_keyless_local"] = False
    c = await start(aiohttp_client)
    w = await _ws(c, "k3y"); await w.close()
    _, tok = tokens.mint("ann")
    w = await _ws(c, tok); await w.close()


async def test_remote_keyless_refused_even_with_adapter_key(aiohttp_client, monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    c = await _client(aiohttp_client, adapter_key="gw-config-key")
    await _refused(c, 403, headers=CF)


def test_stub_request_without_app_still_authenticates(monkeypatch):
    """Guard: authenticate() tolerates request objects with no .app (test_fuzz shape)."""
    monkeypatch.setenv("API_SERVER_KEY", "k3y")
    req = types.SimpleNamespace(headers={"Authorization": "Bearer \udcff\udcfe"}, remote="127.0.0.1")
    denied, principal = wsmod.authenticate(req)
    assert denied is not None and principal is None


# ---------------------------------------------------------------- (2) locality heuristic

async def test_headerless_relay_gets_no_keyless_owner_when_gateway_keyed(aiohttp_client, monkeypatch):
    """A header-less same-host relay (ssh -L / socat / bare proxy_pass) is
    indistinguishable from a local process; with the gateway's key visible it
    must still present the key — no credential-less OWNER."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    c = await _client(aiohttp_client, adapter_key="gw-config-key")
    await _refused(c, 401)                                   # relayed peer, no headers, no key
    settings.SETTINGS["allow_remote"] = True                 # remote switches don't help keyless
    await _refused(c, 401)


async def test_headerless_relay_residual_pinned(aiohttp_client, monkeypatch):
    """RESIDUAL PIN (e75c7051): with no key anywhere and keyless local trust on,
    a header-less relayed peer IS admitted as owner — the heuristic cannot tell
    it from a local process. Only a configured key or allow_keyless_local=false
    closes this; see auth-trust-boundary.md."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    c = await start(aiohttp_client)
    await connect(c, name="relay-peer")                      # admitted: documented limit
    settings.SETTINGS["allow_keyless_local"] = False
    await _refused(c, 403)                                   # operator control closes it


async def test_proxy_headers_still_remote_with_keyless_switch_off(aiohttp_client, monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    settings.SETTINGS["allow_keyless_local"] = False
    c = await start(aiohttp_client)
    await _refused(c, 403, headers={"X-Forwarded-For": "198.51.100.4"})


# ---------------------------------------------------------------- (3) admin scope

def test_nonadmin_connection_with_idle_admin_token_restricted():
    """The exploit: ann holds an admin token (idle) and connects with a non-admin
    token; ann's chats must get user_toolsets, not the full default."""
    tid, _ = tokens.mint("ann")
    tokens.mint("ann", admin=True)
    _fake_chat_client("ann", admin=False, tid=tid)
    assert platform.toolsets_for_user("ann") == USER_TS


def test_admin_connection_keeps_full_toolsets():
    tid, _ = tokens.mint("ann", admin=True)
    _fake_chat_client("ann", admin=True, tid=tid)
    assert platform.toolsets_for_user("ann") is None


def test_mixed_connections_fail_closed():
    """Admin + non-admin connections of one user live at once: the gateway only
    gives us the user id, so the narrower scope wins (fail closed)."""
    t1, _ = tokens.mint("ann")
    t2, _ = tokens.mint("ann", admin=True)
    _fake_chat_client("ann", admin=False, tid=t1, name="phone")
    _fake_chat_client("ann", admin=True, tid=t2, name="laptop")
    assert platform.toolsets_for_user("ann") == USER_TS


def test_access_extra_excludes_live_nonadmin_user():
    tid, _ = tokens.mint("ann")
    tokens.mint("ann", admin=True)
    tokens.mint("root2", admin=True)
    _fake_chat_client("ann", admin=False, tid=tid)
    ex = platform.access_extra()
    assert ex["allow_from"] == ["ann", "owner", "root2"]
    assert ex["allow_admin_from"] == ["owner", "root2"]      # ann: live non-admin connection


def test_access_extra_no_connections_unchanged():
    """Guard: with no live connections the token store's per-user admin stands."""
    tokens.mint("ann")
    tokens.mint("root2", admin=True)
    ex = platform.access_extra()
    assert ex["allow_from"] == ["ann", "owner", "root2"]
    assert ex["allow_admin_from"] == ["owner", "root2"]


def test_detached_nonadmin_falls_back_to_user_admin():
    """RESIDUAL PIN (ed211e05): turns with no live connection (cron, outbox
    continuations) still evaluate admin per-user from the token store."""
    tid, _ = tokens.mint("ann")
    tokens.mint("ann", admin=True)
    c = _fake_chat_client("ann", admin=False, tid=tid)
    assert platform.toolsets_for_user("ann") == USER_TS
    platform.drop_client(c)
    assert platform.toolsets_for_user("ann") is None         # no live connection: per-user


def test_revoking_admin_token_narrows_authority():
    tid_a, _ = tokens.mint("ann", admin=True)
    tokens.mint("ann")
    assert platform.toolsets_for_user("ann") is None
    tokens.revoke(tid_a)
    assert platform.toolsets_for_user("ann") == USER_TS
    assert "ann" not in platform.access_extra()["allow_admin_from"]


def test_owner_and_stranger_unchanged():
    tokens.mint("ann", admin=True)
    assert platform.toolsets_for_user("owner") is None
    assert platform.toolsets_for_user("stranger") == USER_TS
    settings.SETTINGS["user_toolsets"] = []
    assert platform.toolsets_for_user("stranger") == ["no_mcp"]


async def test_e2e_nonadmin_chat_gets_restricted_toolsets(aiohttp_client, monkeypatch, adapter):
    """Full chat routing: a non-admin token's chat.send, while the same user's
    admin token sits idle, resolves restricted toolsets through the adapter hook."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    adapter.config = types.SimpleNamespace(extra={})
    await adapter.connect()
    _, tok = tokens.mint("ann")
    tokens.mint("ann", admin=True)
    c = await start(aiohttp_client)
    w = await _chat_client(c, tok)
    await w.send_json({"type": "chat.send", "chat_id": "c1", "text": "hi"})
    assert (await w.receive_json())["type"] == "chat.accepted"
    for _ in range(100):
        if HANDLED:
            break
        await asyncio.sleep(0.01)
    assert HANDLED, "chat never reached the platform"
    source = types.SimpleNamespace(**HANDLED[-1].source)
    assert source.user_id == "ann"
    assert adapter.toolsets_for_source(source) == USER_TS
    assert "ann" not in adapter.config.extra["allow_admin_from"]
    await w.close()


async def test_e2e_admin_chat_keeps_full_toolsets(aiohttp_client, monkeypatch, adapter):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    adapter.config = types.SimpleNamespace(extra={})
    await adapter.connect()
    _, tok = tokens.mint("ann", admin=True)
    c = await start(aiohttp_client)
    w = await _chat_client(c, tok)
    await w.send_json({"type": "chat.send", "chat_id": "c1", "text": "hi"})
    assert (await w.receive_json())["type"] == "chat.accepted"
    for _ in range(100):
        if HANDLED:
            break
        await asyncio.sleep(0.01)
    assert HANDLED
    source = types.SimpleNamespace(**HANDLED[-1].source)
    assert adapter.toolsets_for_source(source) is None
    assert "ann" in adapter.config.extra["allow_admin_from"]
    await w.close()
