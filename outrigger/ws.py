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

from . import events, notebook, platform, settings, tokens
from .protocol import PROTOCOL_VERSION, SUPPORTED_PROTOCOLS, ProtocolError, int_field, one_line, parse_tools, str_field
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


# A reverse proxy or tunnel (cloudflared, nginx, Caddy…) on this machine connects from
# loopback; these headers say the request really came from elsewhere.
PROXY_HEADERS = ("CF-Connecting-IP", "CF-Ray", "CF-Visitor", "True-Client-IP", "X-Forwarded-For",
                 "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded", "Via")


def _is_local(request: web.Request) -> bool:
    """A client on this machine: loopback and not relayed by a proxy."""
    return _is_loopback(request) and not any(h in request.headers for h in PROXY_HEADERS)


def _peer(request: web.Request) -> str:
    """For logs only (never for decisions): the client address a proxy reports, else the socket's."""
    for h in ("CF-Connecting-IP", "True-Client-IP", "X-Real-IP"):
        if request.headers.get(h):
            return one_line(request.headers[h], 64)
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return one_line(xff.split(",")[0], 64)
    return str(request.remote or "?")


OWNER = {"kind": "owner"}


def authenticate(request: web.Request) -> tuple[web.Response | None, dict | None]:
    """(refusal, principal). The principal is the owner (API key or, with no key set,
    loopback) or a paired client token's user. Never logs headers or keys."""
    presented = _presented_key(request)
    local = _is_local(request)
    rec = tokens.verify(presented) if presented.startswith("otr_") else None
    if rec is not None:
        if not local and not settings.get("allow_remote"):
            log.warning("outrigger: refused remote client %s (allow_remote is off)", _peer(request))
            return web.json_response({"error": "forbidden"}, status=403), None
        return None, {"kind": "token", **rec}
    if presented.startswith("otr_"):
        log.warning("outrigger: bad or revoked token from %s", _peer(request))
        return web.json_response({"error": "unauthorized"}, status=401), None
    refusal = authorize(request)
    return refusal, (None if refusal is not None else OWNER)


def authorize(request: web.Request) -> web.Response | None:
    """None if allowed as the owner. Never logs headers or the key."""
    key = _expected_key()
    local = _is_local(request)
    if not key:
        # No credential configured: only a process on this machine (not via a proxy) may attach.
        if not local:
            log.warning("outrigger: refused remote client %s (no key configured)", _peer(request))
            return web.json_response({"error": "forbidden"}, status=403)
        return None
    if not local and not (settings.get("allow_remote") and settings.get("owner_key_remote")):
        # The API key unlocks the whole gateway; by default it never works from outside.
        log.warning("outrigger: refused the API key from remote client %s (owner_key_remote is off; use a paired token)",
                    _peer(request))
        return web.json_response({"error": "forbidden"}, status=403)
    # surrogateescape: a header with undecodable bytes must fail the comparison, not raise.
    if not hmac.compare_digest(_presented_key(request).encode("utf-8", "surrogateescape"),
                               key.encode("utf-8", "surrogateescape")):
        log.warning("outrigger: bad or missing key from %s", _peer(request))
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


def _limited(client: Client) -> bool:
    """A paired non-admin user (rate-limited, narrower toolsets)."""
    p = client.principal
    return p.get("kind") == "token" and not p.get("admin")


_RATE: dict[str, list[float]] = {}


def _rate_check(client: Client) -> None:
    """Per-user limit on messages that start Hermes turns (chat and // commands)."""
    if not _limited(client):
        return
    per_min = int(settings.get("user_messages_per_min") or 0)
    if per_min <= 0:
        return
    now = time.monotonic()
    key = client.principal.get("user_id", "")
    recent = [t for t in _RATE.get(key, []) if now - t < 60]
    if len(recent) >= per_min:
        _RATE[key] = recent
        raise ProtocolError("rate_limited", f"at most {per_min} messages a minute; try again shortly")
    recent.append(now)
    _RATE[key] = recent


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
    denied, principal = authenticate(request)
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
            if principal.get("kind") == "token" and not tokens.is_active(principal["id"]):
                log.info("outrigger: token %s revoked; closing", principal["id"])
                await ws.close(code=4401, message=b"token revoked")
                return
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
            client = _register(msg, ws, loop, principal)
        except ProtocolError as e:
            await _err(ws, e.code, e.message, close=True)
            return ws
        except RuntimeError as e:
            await _err(ws, "too_many_clients", str(e), close=True)
            return ws
        log.info("outrigger: client %s (%s, %s) attached from %s, session %s, %d tool(s)",
                 client.id, client.name,
                 "owner" if principal.get("kind") == "owner" else f"token {principal.get('id')}",
                 _peer(request), client.session_id, len(client.tools))
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
            except (ConnectionError, asyncio.CancelledError):
                raise
            except Exception:
                # Safety net: a message we failed to validate must not drop the connection
                # (or take anything else down). Logged as an error so tests catch it.
                log.exception("outrigger: client %s: unexpected error handling a %r message",
                              client.id, one_line(m.get("type"), 40))
                await _err(ws, "internal_error", "the server could not handle that message")
    finally:
        ka.cancel()
        if client is not None:
            REGISTRY.remove(client)
            platform.drop_client(client)
            notebook.drop_client(client)
            log.info("outrigger: client %s detached", client.id)
        if not ws.closed:
            await ws.close()
    return ws


def _decode(raw) -> dict | None:
    if raw.type != WSMsgType.TEXT:
        return None
    try:
        # NaN/Infinity literals are not JSON; deep nesting raises RecursionError.
        data = json.loads(raw.data, parse_constant=_no_constant)
        # JSON escapes can produce lone UTF-16 surrogates ("\ud800"), which aren't valid
        # Unicode text: they'd fail later when written to a note or sent to the model
        # provider. Reject the whole message here, once, for every field.
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _no_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def _register(msg: dict, ws, loop, principal: dict | None = None) -> Client:
    principal = principal or OWNER
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
        # Token clients serve only their own platform chats, never API sessions (the owner's).
        ws=ws, loop=loop, session_id=(session_id or None) if principal["kind"] == "owner" else None,
        tools=parse_tools(msg.get("tools")),
        concurrency=concurrency,
    )
    client.principal = principal
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
    if len(text) > int(settings.get("max_chat_chars") or MAX_MSG):
        raise ProtocolError("bad_chat", f"text too long (max {settings.get('max_chat_chars')} characters)")
    _rate_check(client)
    mid = m.get("client_msg_id")
    mid = one_line(mid, 64) if isinstance(mid, str) and mid else "oin_" + __import__("secrets").token_hex(6)
    platform.attach(client, local)
    await platform.inbound(client, local, text, mid)
    await ws.send_json({"type": "chat.accepted", "chat_id": local, "client_msg_id": mid})


async def _handle_doc(client: Client, t: str, m: dict, ws) -> None:
    """Notebook mode (docs/notebook.md): notes on the Hermes host, `//` commands."""
    if "notebook" not in client.capabilities:
        raise ProtocolError("no_notebook_capability", "declare the 'notebook' capability in hello")
    if not notebook.enabled():
        raise ProtocolError("notebook_off", "no notebook_roots configured in the outrigger plugin settings")
    allowed = notebook.allowed_roots(client)
    if not allowed:
        raise ProtocolError("not_allowed", "no notebook roots for this user (notebook_roots_for)")
    doc_id = m.get("doc_id")
    try:
        if t == "doc.list":
            root = str_field(m.get("root"), "root", 64, default="") or None
            query = str_field(m.get("query"), "query", 200, default="")
            limit = int_field(m.get("limit"), "limit", 1, notebook.LIST_LIMIT, default=50)
            _rate_check(client)
            # The tree walk must not block the shared event loop for other clients (F-03).
            notes = await asyncio.get_running_loop().run_in_executor(
                None, notebook.listing, root, query, limit, allowed)
            await ws.send_json({"type": "doc.list", "roots": sorted(allowed), "notes": notes})
        elif t == "doc.open":
            _rate_check(client)
            doc = notebook.open_doc(str(m.get("root") or ""), str(m.get("path") or ""), client, allowed)
            notebook.ensure_watcher()
            await ws.send_json(notebook.content_frame(doc))
            notebook.broadcast(doc, notebook.presence_frame(doc))
        elif t == "doc.close":
            notebook.close_doc(str(doc_id), client)
        elif t == "doc.update":
            doc = notebook.get(str(doc_id), client)
            text = m.get("text")
            if not isinstance(text, str):
                raise notebook.NotebookError("bad_update", "text must be a string")
            version = notebook.update(doc, str(m.get("base_version")), text)
            await ws.send_json({"type": "doc.saved", "doc_id": doc.doc_id, "version": version})
            notebook.broadcast(doc, {"type": "doc.changed", "doc_id": doc.doc_id, "text": doc.text,
                                     "version": version, "by": "other-client", "who": notebook.who(client)}, exclude=client)
        elif t == "doc.undo":
            doc = notebook.get(str(doc_id), client)
            notebook.undo(doc, str(m.get("snapshot") or ""))
            notebook.broadcast(doc, notebook.content_frame(doc, by="undo"))
        elif t == "doc.command":
            doc = notebook.get(str(doc_id), client)
            if not platform.available():
                raise notebook.NotebookError("platform_unavailable", "// commands need the Outrigger platform")
            if "chat" not in client.capabilities:
                raise notebook.NotebookError("no_chat", "// commands need the 'chat' capability")
            instruction = str(m.get("instruction") or "")
            _rate_check(client)
            sel = m.get("selection")
            selection = None
            if sel is not None:
                if not isinstance(sel, dict):
                    raise notebook.NotebookError("bad_selection", "selection: {from_line, to_line}")
                try:
                    selection = (int_field(sel.get("from_line"), "selection.from_line", 1, 10**6),
                                 int_field(sel.get("to_line"), "selection.to_line", 1, 10**6))
                except ProtocolError as e:
                    raise notebook.NotebookError("bad_selection", e.message)
            try:
                line = None if selection else int_field(m.get("line"), "line", 1, 10**6)
            except ProtocolError as e:
                raise notebook.NotebookError("bad_line", e.message)
            lock = notebook.start_command(doc, client, str(m.get("base_version")), line, instruction, selection)
            local = notebook.note_chat_id(doc)
            key = platform.chat_key(client, local)
            lock["chat_key"] = key
            notebook.NOTE_CHATS[key] = doc.doc_id
            notebook.broadcast(doc, {"type": "doc.lock", "doc_id": doc.doc_id, "locked": True, "by": "hermes", "who": lock["who"],
                                     "instruction": lock["instruction"], "snapshot": lock["snapshot"],
                                     "chat_id": local})
            notebook.broadcast(doc, notebook.content_frame(doc, by="command"))
            try:
                await platform.inbound(client, local, notebook.command_message(doc, line, lock["instruction"], selection),
                                       "ndoc_" + __import__("secrets").token_hex(5))
            except Exception as e:
                notebook.end_command(doc, notice=f"could not start Hermes: {e}")
        else:
            raise ProtocolError("unknown_type", f"unknown message type: {t!r}")
    except notebook.NotebookError as e:
        err = {"type": "doc.error", "doc_id": doc_id, "code": e.code, "message": e.message}
        d = notebook.DOCS.get(str(doc_id))
        if e.code == "conflict" and d is not None:
            err.update(text=d.text, version=d.version)
        await ws.send_json(err)
    except OSError as e:
        await ws.send_json({"type": "doc.error", "doc_id": doc_id, "code": "io_error", "message": str(e)[:200]})


def _parse_redaction(raw) -> dict | None:
    """Optional hello field: the client replaces personal data with ⟦…⟧ placeholders."""
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    cats = raw.get("categories") if isinstance(raw.get("categories"), list) else []
    return {"enabled": True, "categories": [one_line(c, 20) for c in cats[:20] if isinstance(c, str)]}


async def _handle(client: Client, m: dict, ws) -> None:
    t = m.get("type")
    if not isinstance(t, str) or len(t) > 40:
        raise ProtocolError("bad_message", "type must be a short string")
    if t == "pong":
        return
    if t == "bind" and client.principal.get("kind") != "owner":
        raise ProtocolError("not_allowed", "token clients chat through the Outrigger platform; session binding is for the owner")
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
    elif t.startswith("doc."):
        await _handle_doc(client, t, m, ws)
    elif t == "chat.commands":
        if "chat" not in client.capabilities:
            raise ProtocolError("no_chat_capability", "declare the 'chat' capability in hello")
        cat = platform.command_catalog()
        await ws.send_json({"type": "chat.commands", "commands": cat.get("commands", []),
                            "source": cat.get("source", ""), "hermes_version": cat.get("hermes_version", "")})
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
