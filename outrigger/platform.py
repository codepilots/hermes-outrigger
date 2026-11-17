"""The Outrigger platform: outrigger clients as a Hermes messaging platform.

Clients that declare the "chat" capability send `chat.send` over the outrigger
WebSocket. Each becomes a MessageEvent on platform "outrigger", so it goes through
the gateway's own pipeline (authorization, session, slash commands, agent), as a
Discord message does. Replies come back through send/edit_message as
`chat.message` / `chat.edit` frames. See docs/platform.md.

Only the public adapter surface is used: gateway.platforms.base, .event,
gateway.config (public plugin API).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque

from . import settings
from .registry import REGISTRY, Client

log = logging.getLogger("hermes.plugins.outrigger")

PLATFORM = "outrigger"
OUTBOX_MAX = 50  # replies kept per chat while its client is away
# The platform's home channel: where cron results and cross-platform messages for
# Outrigger go. Shared by the owner's clients (any of them may attach it).
HOME = "home"

# Namespaced chat id ("<client name>:<client chat id>") -> client currently serving it.
CHATS: dict[str, Client] = {}
# Replies for chats whose client is disconnected, delivered on chat.attach.
OUTBOX: dict[str, deque] = {}
ADAPTER = None  # the live adapter, set on connect


def chat_key(client: Client, local_id: str) -> str:
    return HOME if local_id == HOME else f"{client.name}:{local_id}"


def local_id(client: Client, key: str) -> str:
    if key == HOME:
        return HOME
    prefix = client.name + ":"
    return key[len(prefix):] if key.startswith(prefix) else key


def attach(client: Client, local: str) -> str:
    """Route a chat to this client (latest wins, e.g. after a reconnect)."""
    key = chat_key(client, local)
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

        async def get_chat_info(self, chat_id):
            client = client_for_chat(chat_id)
            return {"name": f"{client.label or client.name}" if client else chat_id,
                    "type": "dm", "chat_id": chat_id}

        # ---- inbound (from the websocket route)
        async def receive(self, client: Client, local: str, text: str, message_id: str) -> None:
            key = attach(client, local)
            source = self.build_source(
                chat_id=key, chat_name=client.label or client.name, chat_type="dm",
                user_id=settings.get("platform_owner_id"),
                user_name=settings.get("platform_owner_name") or None,
                message_id=message_id,
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
        "env_enablement_fn": lambda: {"allow_from": [owner],
                                      "home_channel": {"chat_id": HOME, "name": "Outrigger home"}},
        # Streaming follows the global `streaming` switch (a platform can only narrow it).
        "display_tier": "medium",
        "allowlist_suppresses_pairing": True,
    }
    kwargs.update({k: v for k, v in optional.items() if k in known})
    ctx.register_platform(**kwargs)
    return True
