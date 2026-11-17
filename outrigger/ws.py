"""WebSocket control channel: GET /v1/outrigger/ws."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import time

from aiohttp import WSMsgType, web

from . import events, platform, settings
from .protocol import PROTOCOL_VERSION, SUPPORTED_PROTOCOLS, ProtocolError, one_line, parse_tools
from .registry import REGISTRY, Client

log = logging.getLogger("hermes.plugins.outrigger")

SUBPROTOCOL = "hermes-outrigger.v1"
PING_INTERVAL = 20
DEAD_AFTER = 50  # two missed pings
HELLO_TIMEOUT = 10
MAX_MSG = 4 * 1024 * 1024
MAX_CONCURRENCY = 8


def _expected_key() -> str:
    return settings.get("auth_token") or os.environ.get("API_SERVER_KEY", "")


def _presented_key(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for proto in request.headers.get("Sec-WebSocket-Protocol", "").split(","):
        proto = proto.strip()
        if proto.startswith("bearer."):
            return proto[len("bearer."):]
    return ""


def _is_loopback(request: web.Request) -> bool:
    try:
        return ipaddress.ip_address(request.remote or "").is_loopback
    except ValueError:
        return False


def authorize(request: web.Request) -> web.Response | None:
    """None if allowed. Never logs headers or the key."""
    key = _expected_key()
    loopback = _is_loopback(request)
    if not key:
        # No credential configured: only a local process may attach.
        if not loopback:
            log.warning("outrigger: refused non-loopback peer %s (no key configured)", request.remote)
            return web.json_response({"error": "forbidden"}, status=403)
        return None
    if not loopback and not settings.get("allow_remote"):
        log.warning("outrigger: refused non-loopback peer %s (allow_remote is off)", request.remote)
        return web.json_response({"error": "forbidden"}, status=403)
    if not hmac.compare_digest(_presented_key(request).encode(), key.encode()):
        log.warning("outrigger: bad or missing key from %s", request.remote)
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


def _limits() -> dict:
    return {
        "default_timeout_s": settings.get("default_timeout_s"),
        "max_timeout_s": settings.get("max_timeout_s"),
        "max_result_chars": settings.get("max_result_chars"),
        "max_calls_per_turn": settings.get("max_calls_per_turn"),
    }


async def _err(ws, code: str, message: str, close: bool = False) -> None:
    # The peer may already be gone (e.g. it closed before sending hello).
    if not ws.closed:
        try:
            await ws.send_json({"type": "error", "code": code, "message": message})
        except (ConnectionError, RuntimeError):
            pass
    if close and not ws.closed:
        await ws.close()


async def ws_handler(request: web.Request) -> web.StreamResponse:
    denied = authorize(request)
    if denied is not None:
        return denied
    ws = web.WebSocketResponse(protocols=(SUBPROTOCOL,), max_msg_size=MAX_MSG)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()
    client: Client | None = None
    last_seen = time.monotonic()

    async def keepalive():
        while not ws.closed:
            await asyncio.sleep(PING_INTERVAL)
            if time.monotonic() - last_seen > DEAD_AFTER:
                log.info("outrigger: client %s missed pings; dropping", client.id if client else "?")
                await ws.close()
                return
            try:
                await ws.send_json({"type": "ping"})
            except (ConnectionError, RuntimeError):
                return

    ka = asyncio.create_task(keepalive())
    try:
        # hello must come first
        try:
            first = await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            await _err(ws, "hello_timeout", "send hello first", close=True)
            return ws
        msg = _decode(first)
        if not msg or msg.get("type") != "hello":
            await _err(ws, "bad_hello", "first message must be hello", close=True)
            return ws
        try:
            client = _register(msg, ws, loop)
        except ProtocolError as e:
            await _err(ws, e.code, e.message, close=True)
            return ws
        except RuntimeError as e:
            await _err(ws, "too_many_clients", str(e), close=True)
            return ws
        log.info("outrigger: client %s (%s) attached, session %s, %d tool(s)",
                 client.id, client.name, client.session_id, len(client.tools))
        await ws.send_json(_welcome(client, client.session_id))

        async for raw in ws:
            last_seen = time.monotonic()
            m = _decode(raw)
            if m is None:
                await _err(ws, "bad_message", "expected a JSON object")
                continue
            try:
                await _handle(client, m, ws)
            except ProtocolError as e:
                log.info("outrigger: client %s protocol error %s", client.id, e.code)
                await _err(ws, e.code, e.message)
    finally:
        ka.cancel()
        if client is not None:
            REGISTRY.remove(client)
            platform.drop_client(client)
            log.info("outrigger: client %s detached", client.id)
        if not ws.closed:
            await ws.close()
    return ws


def _decode(raw) -> dict | None:
    if raw.type != WSMsgType.TEXT:
        return None
    try:
        data = json.loads(raw.data)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _register(msg: dict, ws, loop) -> Client:
    if msg.get("protocol") not in SUPPORTED_PROTOCOLS:
        raise ProtocolError("bad_protocol", f"supported protocols: {sorted(SUPPORTED_PROTOCOLS)}")
    info = msg.get("client")
    if not isinstance(info, dict) or not isinstance(info.get("name"), str) or not info["name"]:
        raise ProtocolError("bad_hello", "client.name is required")
    session_id = msg.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ProtocolError("bad_hello", "session_id must be a string")
    concurrency = info.get("concurrency", 1)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or not (
        1 <= concurrency <= MAX_CONCURRENCY
    ):
        raise ProtocolError("bad_hello", f"client.concurrency must be 1..{MAX_CONCURRENCY}")
    name = one_line(info["name"], 64)
    if not name:
        raise ProtocolError("bad_hello", "client.name is required")
    client = Client(
        name=name, label=one_line(info.get("label", ""), 120),
        kind=one_line(info.get("kind", ""), 64), version=one_line(info.get("version", ""), 32),
        ws=ws, loop=loop, session_id=session_id or None, tools=parse_tools(msg.get("tools")),
        concurrency=concurrency,
    )
    client.redaction = _parse_redaction(msg.get("redaction"))
    caps = msg.get("capabilities")
    client.capabilities = {one_line(c, 32) for c in caps[:32] if isinstance(c, str)} if isinstance(caps, list) else set()
    old = REGISTRY.add(client)
    if old is not None:
        loop.create_task(old.ws.close())
    return client


def _welcome(client: Client, session_id) -> dict:
    msg = {"type": "welcome", "client_id": client.id, "session_id": session_id, "limits": _limits()}
    if "chat" in client.capabilities:
        # Tells the client whether chat can go through the Outrigger platform right now.
        msg["platform"] = {"name": platform.PLATFORM, "available": platform.available()}
    return msg


_CHAT_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_.:-]{1,128}$")


async def _handle_chat(client: Client, t: str, m: dict, ws) -> None:
    """Outrigger platform (protocol 2): chat messages go to Hermes's messaging pipeline."""
    if "chat" not in client.capabilities:
        raise ProtocolError("no_chat_capability", "declare the 'chat' capability in hello")
    local = m.get("chat_id")
    if not isinstance(local, str) or not _CHAT_ID_RE.match(local):
        raise ProtocolError("bad_chat", "chat_id: 1-128 of A-Z a-z 0-9 _ . : -")
    if not platform.available():
        raise ProtocolError("platform_unavailable", "the outrigger platform is not connected; restart the gateway or check the plugin's platform setting")
    if t == "chat.attach":
        platform.attach(client, local)
        await platform.flush_outbox(client, local)
        return
    if t == "chat.action":
        if not platform.owns(client, local):
            raise ProtocolError("not_your_chat", "attach or send to this chat first")
        kind, pid = m.get("kind"), m.get("id")
        if kind not in ("model", "choice", "clarify", "approval") or not isinstance(pid, str):
            raise ProtocolError("bad_action", "kind: model|choice|clarify|approval; id: string")
        status = await platform.action(client, local, kind, pid, m.get("value"))
        await ws.send_json({"type": "chat.action_result", "chat_id": local, "id": pid, "status": status})
        return
    if t == "chat.stop":
        if not platform.owns(client, local):
            raise ProtocolError("not_your_chat", "attach or send to this chat first")
        await platform.inbound(client, local, "/stop", "ostop_" + __import__("secrets").token_hex(4))
        return
    text = m.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError("bad_chat", "text must be a non-empty string")
    if len(text) > MAX_MSG:
        raise ProtocolError("bad_chat", "text too long")
    mid = m.get("client_msg_id")
    mid = one_line(mid, 64) if isinstance(mid, str) and mid else "oin_" + __import__("secrets").token_hex(6)
    platform.attach(client, local)
    await platform.inbound(client, local, text, mid)
    await ws.send_json({"type": "chat.accepted", "chat_id": local, "client_msg_id": mid})


def _parse_redaction(raw) -> dict | None:
    """Optional hello field: the client replaces personal data with ⟦…⟧ placeholders."""
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    cats = raw.get("categories") if isinstance(raw.get("categories"), list) else []
    return {"enabled": True, "categories": [one_line(c, 20) for c in cats[:20] if isinstance(c, str)]}


async def _handle(client: Client, m: dict, ws) -> None:
    t = m.get("type")
    if t == "pong":
        return
    if t == "bind":
        sid = m.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ProtocolError("bad_bind", "session_id must be a non-empty string")
        REGISTRY.bind(client, sid)
        await ws.send_json(_welcome(client, sid))
    elif t == "tools.update":
        client.tools = parse_tools(m.get("tools"))
    elif t == "context.update":
        client.summary = one_line(m.get("summary", ""), 300)
    elif t == "result":
        call_id = m.get("call_id")
        if not isinstance(call_id, str):
            raise ProtocolError("bad_result", "call_id required")
        ok = bool(m.get("ok"))
        REGISTRY.resolve(client, call_id, {
            "ok": ok, "content": m.get("content"),
            "error": m.get("error") if not ok else None,
        })
    elif t in ("chat.send", "chat.attach", "chat.stop", "chat.action"):
        await _handle_chat(client, t, m, ws)
    elif t == "progress":
        call_id = m.get("call_id")
        if not isinstance(call_id, str):
            raise ProtocolError("bad_progress", "call_id required")
        text = one_line(m.get("message", ""), 300)
        p = REGISTRY.progress(client, call_id, text)
        if p is not None:
            # emit_to_session is sync and may do I/O: keep it off the gateway loop.
            asyncio.get_running_loop().run_in_executor(None, events.emit, p.session_id, "progress", {
                "client_id": client.id, "client": client.name,
                "call_id": call_id, "tool": p.tool, "message": text,
            })
    else:
        raise ProtocolError("unknown_type", f"unknown message type: {t!r}")
