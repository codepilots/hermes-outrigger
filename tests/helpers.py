"""Shared test helpers: a real aiohttp server with the outrigger route and a client."""
import asyncio
import json

from aiohttp import web

from outrigger import dispatch
from outrigger.ws import ws_handler

TOOLS = [
    {"name": "echo", "description": "Return text", "policy": "auto",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"]}},
    {"name": "slow", "description": "Never answers", "policy": "auto", "timeout_s": 1},
    {"name": "big", "description": "Large result", "policy": "auto"},
]


async def start(aiohttp_client):
    app = web.Application()
    app.router.add_get("/v1/outrigger/ws", ws_handler)
    return await aiohttp_client(app)


async def connect(client, session="S1", name="t1", key=None, tools=TOOLS, **info):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    ws = await client.ws_connect("/v1/outrigger/ws", headers=headers,
                                 protocols=("hermes-outrigger.v1",))
    await ws.send_json({"type": "hello", "protocol": 1, "client": {"name": name, **info},
                        "session_id": session, "tools": tools})
    welcome = await ws.receive_json()
    assert welcome["type"] == "welcome", welcome
    return ws, welcome


async def serve_one(ws, reply):
    """Answer exactly one call with reply(call_msg) -> result message fields."""
    msg = await ws.receive_json()
    assert msg["type"] == "call"
    await ws.send_json({"type": "result", "call_id": msg["call_id"], **reply(msg)})
    return msg


async def call(args, session="S1", **kw):
    return json.loads(await asyncio.get_running_loop().run_in_executor(
        None, lambda: dispatch.outrigger_call(args, session_id=session, **kw)))
