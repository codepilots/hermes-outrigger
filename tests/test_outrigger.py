import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from outrigger import context, dispatch, settings
from outrigger.registry import REGISTRY
from outrigger.ws import ws_handler

pytestmark = pytest.mark.asyncio

from helpers import TOOLS, call, connect, serve_one, start  # noqa: E402


async def test_round_trip(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    server = asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": m["args"]["text"].upper()}))
    out = await call({"tool": "echo", "args": {"text": "hi"}})
    assert out["ok"] and out["content"] == "HI" and out["untrusted"] is True
    assert (await server)["tool"] == "echo"


async def test_audit_written_without_args(aiohttp_client, tmp_path):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "x"}))
    await call({"tool": "echo", "args": {"text": "secret"}})
    line = (tmp_path / "plugin-data/outrigger/audit.jsonl").read_text()
    assert "secret" not in line and '"outcome": "ok"' in line


async def test_auth(aiohttp_client, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "k3y")
    c = await start(aiohttp_client)
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await connect(c, key="wrong")
    assert e.value.status == 401
    ws, _ = await connect(c, key="k3y")
    await ws.close()


async def test_auth_via_subprotocol(aiohttp_client, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "k3y")
    c = await start(aiohttp_client)
    ws = await c.ws_connect("/v1/outrigger/ws",
                            protocols=("hermes-outrigger.v1", "bearer.k3y"))
    await ws.close()


async def test_validation_errors(aiohttp_client):
    c = await start(aiohttp_client)
    await connect(c)
    assert (await call({"tool": "nope", "args": {}}))["error"] == "outrigger_unknown_tool"
    assert (await call({"tool": "echo", "args": {}}))["error"] == "outrigger_bad_args"
    assert (await call({"tool": "echo", "args": {"text": "x"}}, session="other"))["error"] == "outrigger_no_client"


async def test_timeout_sends_cancel(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    out = await call({"tool": "slow", "args": {}})
    assert out["error"] == "outrigger_timeout"
    call_msg = await ws.receive_json()
    cancel = await ws.receive_json()
    assert call_msg["type"] == "call" and cancel["type"] == "cancel"


async def test_disconnect_mid_call(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    task = asyncio.create_task(call({"tool": "slow", "args": {}}))
    await ws.receive_json()
    await ws.close()
    assert (await task)["error"] == "outrigger_disconnected"


async def test_truncation_and_control_chars(aiohttp_client):
    settings.SETTINGS["max_result_chars"] = 10
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "a\x00b" + "z" * 50}))
    out = await call({"tool": "big", "args": {}})
    assert out["truncated"] and len(out["content"]) == 10 and "\x00" not in out["content"]


async def test_tool_error_passthrough(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": "boom"}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is False and out["message"] == "boom"


async def test_call_limit(aiohttp_client):
    settings.SETTINGS["max_calls_per_turn"] = 1
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "x"}))
    assert (await call({"tool": "big", "args": {}}))["ok"]
    assert (await call({"tool": "big", "args": {}}))["error"] == "outrigger_call_limit"
    REGISTRY.end_turn("S1", cancel=False)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "x"}))
    assert (await call({"tool": "big", "args": {}}))["ok"]


async def test_multi_client_ambiguity_and_pick(aiohttp_client):
    c = await start(aiohttp_client)
    ws1, w1 = await connect(c, name="a")
    ws2, w2 = await connect(c, name="b")
    out = await call({"tool": "echo", "args": {"text": "x"}})
    assert out["error"] == "outrigger_ambiguous_client" and len(out["clients"]) == 2
    asyncio.create_task(serve_one(ws2, lambda m: {"ok": True, "content": "from b"}))
    out = await call({"client": w2["client_id"], "tool": "echo", "args": {"text": "x"}})
    assert out["content"] == "from b"


async def test_rebind_and_conversation_id(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c, session=None)
    assert (await call({"tool": "echo", "args": {"text": "x"}}))["error"] == "outrigger_no_client"
    await ws.send_json({"type": "bind", "session_id": "S0"})
    assert (await ws.receive_json())["type"] == "welcome"
    # compression rotated the session id; conversation_id is the original
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "ok"}))
    out = await call({"tool": "echo", "args": {"text": "x"}}, session="S1", conversation_id="S0")
    assert out["ok"]
    assert REGISTRY.for_session("S1")  # now bound to the new id


async def test_tools_update_and_context(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    await ws.send_json({"type": "context.update", "summary": 'tab: "Example"'})
    await ws.send_json({"type": "tools.update", "tools": [
        {"name": "get_page", "description": "Page text", "policy": "auto",
         "parameters": {"type": "object", "properties": {"max_chars": {"type": "integer"}}}}]})
    await asyncio.sleep(0.1)
    text = context.build("S1", None, 2000)
    assert "get_page(max_chars?: int)" in text and "Example" in text and "echo" not in text
    assert context.build("nobody", None, 2000) is None


async def test_context_cap_falls_back_to_names(aiohttp_client):
    many = [{"name": f"tool{i}", "description": "d" * 80, "policy": "auto"} for i in range(40)]
    c = await start(aiohttp_client)
    await connect(c, tools=many)
    text = context.build("S1", None, 500)
    assert len(text) <= 500 and "outrigger_tools" in text


async def test_interrupt_cancels_pending(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    task = asyncio.create_task(call({"tool": "big", "args": {}}))
    await ws.receive_json()
    await asyncio.get_running_loop().run_in_executor(None, dispatch.cancel_session, "S1")
    assert (await task)["error"] == "outrigger_cancelled"
    assert (await ws.receive_json())["type"] == "cancel"


async def test_bad_hello_rejected(aiohttp_client):
    c = await start(aiohttp_client)
    ws = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1",))
    await ws.send_json({"type": "hello", "protocol": 99, "client": {"name": "x"}})
    assert (await ws.receive_json())["code"] == "bad_protocol"


async def test_register_wires_everything():
    from outrigger import register

    class Ctx:
        def __init__(self):
            self.tools, self.hooks, self.platforms, self.unload = {}, {}, {}, []
        def get_config(self, k, d=None): return d
        def register_tool(self, **kw): self.tools[kw["name"]] = kw
        def register_hook(self, n, cb): self.hooks[n] = cb
        def register_platform_handler(self, p, f): self.platforms[p] = f
        def on_unload(self, cb): self.unload.append(cb)
        def emit_to_session(self, *a): return True

    ctx = Ctx()
    register(ctx)
    assert set(ctx.tools) == {"outrigger_call", "outrigger_tools"}
    assert set(ctx.hooks) == {"pre_llm_call", "on_session_end", "subagent_start", "subagent_stop"}
    app = web.Application()
    ctx.platforms["api_server"](app, object())
    assert any(r.resource.canonical == "/v1/outrigger/ws" for r in app.router.routes())
    # Always advertised: a cached check_fn verdict would strand sessions started while
    # the client reconnects. Calls fail fast instead.
    assert ctx.tools["outrigger_call"].get("check_fn") is None


async def test_hot_reload_into_frozen_router_does_not_raise():
    from outrigger import register

    class Ctx:
        platforms = {}
        def get_config(self, k, d=None): return d
        def register_tool(self, **kw): pass
        def register_hook(self, n, cb): pass
        def register_platform_handler(self, p, f): self.platforms[p] = f
        def on_unload(self, cb): pass

    ctx = Ctx()
    register(ctx)
    app = web.Application()
    app.router.freeze()
    ctx.platforms["api_server"](app, object())  # logs a warning instead of raising
