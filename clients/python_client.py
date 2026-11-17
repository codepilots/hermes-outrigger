"""Reference outrigger client. Serves a couple of demo tools until Ctrl-C.

    python clients/python_client.py --url ws://127.0.0.1:8642/v1/outrigger/ws \
        --key $API_SERVER_KEY --session SESSION_ID
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import aiohttp

TOOLS = [
    {"name": "get_time", "description": "Current local time on the client", "policy": "auto",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "echo", "description": "Return the given text", "policy": "auto",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"]}},
]

HANDLERS = {
    "get_time": lambda a: time.strftime("%Y-%m-%d %H:%M:%S"),
    "echo": lambda a: a["text"],
}


async def run(url: str, key: str, session: str | None, handlers=HANDLERS, tools=TOOLS,
              name: str = "py-demo", stop: asyncio.Event | None = None) -> None:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(url, headers=headers, protocols=("hermes-outrigger.v1",)) as ws:
            await ws.send_json({
                "type": "hello", "protocol": 1,
                "client": {"name": name, "version": "0.1", "kind": "python", "label": "demo"},
                "session_id": session, "tools": tools,
            })
            async for raw in ws:
                msg = json.loads(raw.data)
                if msg["type"] == "ping":
                    await ws.send_json({"type": "pong"})
                elif msg["type"] == "call":
                    try:
                        out = handlers[msg["tool"]](msg["args"])
                        await ws.send_json({"type": "result", "call_id": msg["call_id"],
                                            "ok": True, "content": out})
                    except Exception as e:
                        await ws.send_json({"type": "result", "call_id": msg["call_id"],
                                            "ok": False, "error": str(e)})
                if stop is not None and stop.is_set():
                    break


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:8642/v1/outrigger/ws")
    p.add_argument("--key", default="")
    p.add_argument("--session")
    a = p.parse_args()
    asyncio.run(run(a.url, a.key, a.session))
