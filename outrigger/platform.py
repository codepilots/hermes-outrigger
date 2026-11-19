"""The Outrigger platform: outrigger clients as a Hermes messaging platform.

Clients that declare the "chat" capability send `chat.send` over the outrigger
WebSocket. Each becomes a MessageEvent on platform "outrigger", so it goes through
the gateway's own pipeline (authorization, session, slash commands, agent), as a
Discord message does. Replies come back through send/edit_message as
`chat.message` / `chat.edit` frames. See docs/platform.md.

Only the public adapter surface is used: gateway.platforms.base, .event,
gateway.config (public plugin API).

Trust boundary: token user -> Hermes toolsets / slash-command gating. The
admin-scope mitigation (toolsets_for_user / access_extra, F-10) stays in this
module because it reads the live chat registry (CHATS) — see the banner below.
Deliberately does NOT: authenticate the socket (auth.py / ws.py) or sandbox the
agent turn; it narrows toolsets and access lists, the gateway pipeline enforces.
"""
# Audit: see AUDIT.md#security-sensitive-units (platform admin-scope authz / inbound)
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import secrets
import time
from collections import deque

from . import settings
from .registry import REGISTRY, Client

log = logging.getLogger("hermes.plugins.outrigger")

PLATFORM = "outrigger"
OUTBOX_MAX = 50  # replies kept per chat while its client is away
OUTBOX_CHATS_MAX = 500  # chats with queued replies, overall (oldest dropped first)
MAX_CHATS_PER_CLIENT = 256  # chats one connection may route at once
# The platform's home channel: where cron results and cross-platform messages for
# Outrigger go. Shared by the owner's clients (any of them may attach it).
HOME = "home"

# Namespaced chat id ("<client name>:<client chat id>") -> client currently serving it.
CHATS: dict[str, Client] = {}
# Replies for chats whose client is disconnected, delivered on chat.attach.
OUTBOX: dict[str, deque] = {}
ADAPTER = None  # the live adapter, set on connect

# Interactive prompts waiting for the user (P2): id -> {kind, chat, created, ...}.
PENDING: dict[str, dict] = {}
PENDING_TTL = 15 * 60
MAX_PICKER_MODELS = 50
MAX_MEDIA_BYTES = 5 * 1024 * 1024  # files Hermes sends are inlined up to this size
_CATALOG = None


def command_catalog() -> dict:
    """Build-time command list (tools/gen_command_catalog.py); see NousResearch/hermes-agent#149673."""
    global _CATALOG
    if _CATALOG is None:
        path = os.path.join(os.path.dirname(__file__), "data", "commands.json")
        try:
            with open(path, encoding="utf-8") as f:
                _CATALOG = json.load(f)
        except (OSError, ValueError):
            _CATALOG = {"commands": [], "source": "unavailable"}
    return _CATALOG


def _new_prompt(kind: str, chat: str, **data) -> str:
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - v["created"] > PENDING_TTL]:
        PENDING.pop(k, None)
    pid = f"{kind[:2]}_{secrets.token_hex(6)}"
    PENDING[pid] = {"kind": kind, "chat": chat, "created": now, **data}
    return pid


def _prefix(client: Client) -> str:
    """Chat namespace: the owner's clients by name; token clients also by token, so two
    users' clients with the same name never share (or hijack) a chat."""
    p = client.principal
    return f"{client.name}:" if p.get("kind") == "owner" else f"t-{p['id']}:{client.name}:"


def chat_key(client: Client, local_id: str) -> str:
    # The shared home chat (cron results etc.) belongs to the owner.
    if local_id == HOME and client.principal.get("kind") == "owner":
        return HOME
    return _prefix(client) + local_id


def local_id(client: Client, key: str) -> str:
    if key == HOME:
        return HOME
    prefix = _prefix(client)
    return key[len(prefix):] if key.startswith(prefix) else key


def user_of(client: Client) -> tuple[str, str | None]:
    p = client.principal
    if p.get("kind") == "token":
        return p["user_id"], (p.get("user_name") or None)
    return str(settings.get("platform_owner_id") or "owner"), (settings.get("platform_owner_name") or None)


# ---------------------------------------------------------------- admin-scope authz (F-10)
# Kept in platform.py (NOT moved to a separate authz module): toolsets_for_user
# reads the live chat registry CHATS below, so it is entangled with platform chat
# state. Moving it would force a platform import back-reference for no cohesion gain.

def access_extra() -> dict:
    """Hermes's own per-platform access lists (gateway allowlist and slash-command gating),
    kept in step with the token store: the owner and admin users get every command; other
    paired users get user_allowed_commands (plus Hermes's /help and /whoami floor).
    F-10: admin is scoped like toolsets_for_user — a user with a live non-admin
    connection is not admin here, even if another of their tokens is admin."""
    from . import tokens
    owner = str(settings.get("platform_owner_id") or "owner")
    users, _ = tokens.active_users()
    admins = {u for u in users if toolsets_for_user(u) is None}
    return {"allow_from": sorted({owner} | users), "allow_admin_from": sorted({owner} | admins),
            "user_allowed_commands": [str(c) for c in settings.get("user_allowed_commands")]}


def toolsets_for_user(user_id: str) -> list[str] | None:
    from . import tokens
    if user_id == str(settings.get("platform_owner_id") or "owner"):
        return None
    ts = settings.get("user_toolsets")
    ts = [str(t) for t in ts] if isinstance(ts, list) else ["web", "outrigger", "no_mcp"]
    # Hermes treats an empty override as "none given" (the full default), so say "nothing".
    restricted = ts or ["no_mcp"]
    # F-10 (admin scope): the gateway hands us only the user id, never the token, so
    # scope by the clients currently serving this user's chats: a live non-admin
    # connection constrains the user's chats even when another of their tokens is
    # admin (fail closed when admin and non-admin connections coexist). With no live
    # connection, fall back to the token store's per-user admin flag (residual:
    # cron/outbox turns; see docs/security-review/auth-trust-boundary.md).
    try:
        live = [c for c in tuple(CHATS.values())
                if not c.closed and c.principal.get("kind") == "token"
                and c.principal.get("user_id") == user_id]
    except RuntimeError:  # CHATS mutated from another loop mid-scan: fail closed
        log.warning("outrigger: chat registry changed mid-scan; restricting toolsets for %s", user_id)
        return restricted
    if any(not c.principal.get("admin") for c in live):
        return restricted
    _, admins = tokens.active_users()
    if user_id in admins:
        return None
    return restricted


def attach(client: Client, local: str) -> str:
    """Route a chat to this client (latest wins, e.g. after a reconnect)."""
    key = chat_key(client, local)
    if CHATS.get(key) is not client and sum(1 for c in CHATS.values() if c is client) >= MAX_CHATS_PER_CLIENT:
        from .protocol import ProtocolError
        raise ProtocolError("too_many_chats", f"at most {MAX_CHATS_PER_CLIENT} chats per connection")
    CHATS[key] = client
    return key


def owns(client: Client, local: str) -> bool:
    return CHATS.get(chat_key(client, local)) is client


def client_for_chat(key: str) -> Client | None:
    c = CHATS.get(key)
    return c if c is not None and not c.closed else None


def available() -> bool:
    return ADAPTER is not None


def drop_client(client: Client) -> None:
    for key in [k for k, c in CHATS.items() if c is client]:
        CHATS.pop(key, None)


async def _send_frame(client: Client, msg: dict) -> bool:
    try:
        if asyncio.get_running_loop() is client.loop:
            await client.ws.send_json(msg)
        else:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(client.ws.send_json(msg), client.loop))
        return True
    except Exception:
        return False


def _make_adapter_class():
    """Built lazily: the gateway modules exist only inside the Hermes gateway."""
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.platforms.event import MessageEvent, MessageType

    class OutriggerAdapter(BasePlatformAdapter):
        def __init__(self, config):
            super().__init__(config, Platform(PLATFORM))
            self._loop = None

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            global ADAPTER
            self._loop = asyncio.get_running_loop()
            ADAPTER = self
            self.sync_access()
            self._mark_connected()
            log.info("outrigger: platform adapter connected")
            return True

        async def disconnect(self) -> None:
            global ADAPTER
            if ADAPTER is self:
                ADAPTER = None
            self._mark_disconnected()

        # ---- outbound
        async def _deliver(self, chat_id: str, msg: dict) -> bool:
            client = client_for_chat(chat_id)
            if client is not None:
                msg = {**msg, "chat_id": local_id(client, chat_id)}
                if await _send_frame(client, msg):
                    return True
            if chat_id not in OUTBOX and len(OUTBOX) >= OUTBOX_CHATS_MAX:
                OUTBOX.pop(next(iter(OUTBOX)), None)  # drop the oldest chat's queue
            box = OUTBOX.setdefault(chat_id, deque(maxlen=OUTBOX_MAX))
            box.append(msg)
            return False

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            message_id = "om_" + secrets.token_hex(6)
            await self._deliver(chat_id, {"type": "chat.message", "message_id": message_id,
                                          "text": content or "", "reply_to": reply_to})
            # Kept in the outbox when the client is away, so the turn still counts as delivered.
            return SendResult(success=True, message_id=message_id)

        async def edit_message(self, chat_id, message_id, content, *, finalize=False):
            await self._deliver(chat_id, {"type": "chat.edit", "message_id": message_id,
                                          "text": content or "", "final": bool(finalize)})
            return SendResult(success=True, message_id=message_id)

        async def send_typing(self, chat_id, metadata=None) -> None:
            client = client_for_chat(chat_id)
            if client is not None:
                await _send_frame(client, {"type": "chat.typing", "chat_id": local_id(client, chat_id)})

        # ---- interactive prompts (P2): rendered as cards by the client
        def _supports(self, chat_id: str, capability: str) -> bool:
            client = client_for_chat(chat_id)
            return client is not None and capability in client.capabilities

        async def send_model_picker(self, chat_id, providers, current_model, current_provider,
                                    session_key, on_model_selected, metadata=None):
            if not self._supports(chat_id, "model_picker"):
                return SendResult(success=False, error="client has no model picker")
            rows = [{"slug": str(p.get("slug", "")), "name": str(p.get("name") or p.get("slug", "")),
                     "is_current": bool(p.get("is_current")),
                     "models": [str(m) for m in (p.get("models") or [])][:MAX_PICKER_MODELS],
                     "total_models": int(p.get("total_models") or len(p.get("models") or []))}
                    for p in providers if isinstance(p, dict) and p.get("slug")]
            pid = _new_prompt("model", chat_id, callback=on_model_selected)
            ok = await self._deliver(chat_id, {"type": "chat.model_picker", "id": pid,
                                               "current": {"model": current_model, "provider": current_provider},
                                               "providers": rows})
            return SendResult(success=ok, message_id=pid)

        async def send_choice_picker(self, chat_id, title, choices, session_key, on_choice_selected,
                                     metadata=None):
            if not self._supports(chat_id, "choices"):
                return SendResult(success=False, error="client has no choice picker")
            opts = [{"value": str(c.get("value")), "label": str(c.get("label") or c.get("value")),
                     "is_current": bool(c.get("is_current"))} for c in choices if isinstance(c, dict)]
            pid = _new_prompt("choice", chat_id, callback=on_choice_selected,
                              values={o["value"] for o in opts})
            ok = await self._deliver(chat_id, {"type": "chat.choice", "id": pid, "title": title or "",
                                               "options": opts})
            return SendResult(success=ok, message_id=pid)

        async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
            if not choices or not self._supports(chat_id, "clarify"):
                # Open-ended questions, or a client without cards: the gateway's text flow.
                return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)
            from tools.clarify_gateway import get_pending
            entry = get_pending(clarify_id)
            multi = bool(getattr(entry, "multi_select", False))
            pid = _new_prompt("clarify", chat_id, clarify_id=clarify_id, entry=entry,
                              choices=[str(c) for c in choices], multi=multi)
            ok = await self._deliver(chat_id, {"type": "chat.clarify", "id": pid, "question": question,
                                               "choices": [str(c) for c in choices], "multi": multi})
            return SendResult(success=ok, message_id=pid)

        async def _send_exec_approval_prompt(self, prompt):
            if not self._supports(prompt.chat_id, "approvals"):
                return SendResult(success=False, error="client has no approval cards")
            pid = _new_prompt("approval", prompt.chat_id, session_key=prompt.session_key,
                              request_id=prompt.request_id, choices={a[1] for a in prompt.actions})
            ok = await self._deliver(prompt.chat_id, {
                "type": "chat.approval", "id": pid, "text": prompt.text, "command": prompt.command,
                "description": prompt.description, "smart_denied": bool(prompt.smart_denied),
                "actions": [{"label": a[0], "choice": a[1], "style": a[2]} for a in prompt.actions]})
            return SendResult(success=ok, message_id=pid)

        async def answer(self, client: Client, local: str, kind: str, pid: str, value) -> str:
            """Resolve a prompt the client answered. Returns a short status for the client."""
            key = chat_key(client, local)
            p = PENDING.get(pid)
            if p is None or p["kind"] != kind or p["chat"] != key:
                return "expired"
            PENDING.pop(pid, None)
            if kind == "model":
                if (not isinstance(value, dict) or not isinstance(value.get("model"), str)
                        or not isinstance(value.get("provider"), str) or not value["model"] or not value["provider"]
                        or len(value["model"]) > 200 or len(value["provider"]) > 100):
                    return "bad_value"
                text = await p["callback"](key, str(value["model"]), str(value["provider"]))
                if text:
                    await self.send(key, str(text))
                return "ok"
            if kind == "choice":
                if str(value) not in p["values"]:
                    return "bad_value"
                text = await p["callback"](key, str(value))
                if text:
                    await self.send(key, str(text))
                return "ok"
            if kind == "clarify":
                from tools.clarify_gateway import get_pending, mark_awaiting_text, resolve_gateway_clarify
                if get_pending(p["clarify_id"]) is not p["entry"]:
                    return "expired"
                if value == "__other__":   # the user will type the answer as their next message
                    mark_awaiting_text(p["clarify_id"])
                    return "ok"
                picked = value if isinstance(value, list) else [value]
                picked = [str(v) for v in picked if str(v) in p["choices"]]
                if not picked:
                    return "bad_value"
                answer = json.dumps(picked) if p["multi"] else picked[0]
                return "ok" if resolve_gateway_clarify(p["clarify_id"], answer) else "expired"
            if kind == "approval":
                from tools.approval import resolve_gateway_approval_card
                if value not in p["choices"]:
                    return "bad_value"
                n = resolve_gateway_approval_card(
                    p["session_key"], str(value), p["request_id"],
                    actor_id=user_of(client)[0], extra=getattr(self.config, "extra", None))
                return "ok" if n else "expired"
            return "bad_kind"

        # ---- media (P3): images and files Hermes sends, for clients with "media"
        async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None):
            if not self._supports(chat_id, "media") or not str(image_url).startswith(("https://", "http://")):
                return await super().send_image(chat_id, image_url, caption, reply_to, metadata)
            mid = "om_" + secrets.token_hex(6)
            await self._deliver(chat_id, {"type": "chat.media", "message_id": mid, "kind": "image",
                                          "url": str(image_url), "caption": caption or ""})
            return SendResult(success=True, message_id=mid)

        async def _send_file(self, chat_id, path, kind, caption, file_name, reply_to, metadata, fallback):
            if not self._supports(chat_id, "media"):
                return await fallback()
            safe = self.validate_media_delivery_path(path)  # Hermes's own media path checks
            if not safe or not os.path.isfile(safe) or os.path.getsize(safe) > MAX_MEDIA_BYTES:
                return await fallback()
            with open(safe, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            name = file_name or os.path.basename(safe)
            mid = "om_" + secrets.token_hex(6)
            await self._deliver(chat_id, {"type": "chat.media", "message_id": mid, "kind": kind, "name": name,
                                          "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                                          "data": data, "caption": caption or ""})
            return SendResult(success=True, message_id=mid)

        async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
            return await self._send_file(chat_id, image_path, "image", caption, None, reply_to, metadata,
                                         lambda: super(OutriggerAdapter, self).send_image_file(
                                             chat_id, image_path, caption, reply_to, metadata, **kwargs))

        async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None,
                                metadata=None, **kwargs):
            return await self._send_file(chat_id, file_path, "file", caption, file_name, reply_to, metadata,
                                         lambda: super(OutriggerAdapter, self).send_document(
                                             chat_id, file_path, caption, file_name, reply_to, metadata, **kwargs))

        async def get_chat_info(self, chat_id):
            client = client_for_chat(chat_id)
            return {"name": f"{client.label or client.name}" if client else chat_id,
                    "type": "dm", "chat_id": chat_id}

        def toolsets_for_source(self, source):
            """Hermes's per-source toolset override (BasePlatformAdapter, public): paired
            non-admin users get settings.user_toolsets instead of the platform default
            (which includes terminal and file tools). None keeps the default."""
            return toolsets_for_user(str(getattr(source, "user_id", "") or ""))

        def sync_access(self) -> None:
            """Update this platform's live config extra (the gateway reads the same object)."""
            extra = getattr(self.config, "extra", None)
            if isinstance(extra, dict):
                extra.update(access_extra())

        # ---- inbound (from the websocket route)
        async def receive(self, client: Client, local: str, text: str, message_id: str) -> None:
            self.sync_access()  # pairing/revoking takes effect without a restart
            key = attach(client, local)
            user_id, user_name = user_of(client)
            source = self.build_source(
                chat_id=key, chat_name=client.label or client.name, chat_type="dm",
                user_id=user_id, user_name=user_name, message_id=message_id,
            )
            event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                                 message_id=message_id)
            await self.handle_message(event)

    return OutriggerAdapter


async def inbound(client: Client, local: str, text: str, message_id: str) -> None:
    """Hand a client's chat message to the adapter, on the adapter's own loop."""
    adapter = ADAPTER
    if adapter is None:
        raise RuntimeError("the outrigger platform is not connected")
    coro = adapter.receive(client, local, text, message_id)
    if adapter._loop is None or adapter._loop is asyncio.get_running_loop():
        asyncio.get_running_loop().create_task(coro)
    else:
        asyncio.run_coroutine_threadsafe(coro, adapter._loop)


async def action(client: Client, local: str, kind: str, pid: str, value) -> str:
    """Run a client's answer to a prompt on the adapter's loop."""
    adapter = ADAPTER
    if adapter is None:
        return "platform_unavailable"
    coro = adapter.answer(client, local, kind, pid, value)
    if adapter._loop is None or adapter._loop is asyncio.get_running_loop():
        return await coro
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, adapter._loop))


async def flush_outbox(client: Client, local: str) -> int:
    key = chat_key(client, local)
    box = OUTBOX.pop(key, None)
    sent = 0
    while box:
        msg = box.popleft()
        if await _send_frame(client, {**msg, "chat_id": local}):
            sent += 1
    return sent


def register(ctx) -> bool:
    """Register the platform when this Hermes supports plugin platforms. Returns True if done."""
    if not settings.get("platform") or not hasattr(ctx, "register_platform"):
        return False
    try:
        import dataclasses
        from gateway.platform_registry import PlatformEntry
        adapter_cls = _make_adapter_class()
    except Exception as e:  # not running inside a gateway-capable Hermes
        log.info("outrigger: platform not registered (%s)", e)
        return False

    owner = str(settings.get("platform_owner_id") or "owner")
    kwargs = dict(
        name=PLATFORM, label="Outrigger", adapter_factory=adapter_cls,
        # Passive probes: no deps to install; "connected" means the plugin setting is on.
        check_fn=lambda: True,
        validate_config=lambda config: True,
        emoji="\U0001f6f6",
        max_message_length=0,
        platform_hint=("You are chatting through an Outrigger client: a thin app such as a "
                       "browser side panel. Replies render as Markdown."),
    )
    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    optional = {
        "is_connected": lambda config: bool(settings.get("platform")),
        # The websocket is already authenticated with the API key: the one human behind it
        # is the owner, authorized through the adapter's own allow list.
        # A home channel also stops the gateway's one-time "no home channel" prompt that
        # would otherwise open every new conversation.
        "env_enablement_fn": lambda: {**access_extra(),
                                      "home_channel": {"chat_id": HOME, "name": "Outrigger home"}},
        # Streaming follows the global `streaming` switch (a platform can only narrow it).
        "display_tier": "medium",
        "allowlist_suppresses_pairing": True,
    }
    kwargs.update({k: v for k, v in optional.items() if k in known})
    ctx.register_platform(**kwargs)
    return True
