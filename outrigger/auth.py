"""Authentication and admission for the outrigger WebSocket route.

Responsibility: decide WHO may attach — resolve the expected credential
(plugin auth_token > the gateway adapter's resolved key > env API_SERVER_KEY),
read the presented key/token, verify paired tokens, and admit the owner via API
key or (only when no key resolves anywhere AND allow_keyless_local) the locality
heuristic. Trust boundary: untrusted WS peer -> gateway control surface. The
locality check (loopback AND no proxy headers) is a HEURISTIC a header-less
same-host relay can defeat, not verified peer identity (F-10 residual).

Deliberately does NOT: route messages, rate-limit, register clients, or bound
results (those stay in ws.py); it does not sandbox client tool execution and does
not log keys or headers. ws.py re-exports every name here so ws.<name> stays
resolvable and identical.
"""
# Audit: see AUDIT.md#security-sensitive-units (authentication / admission)
from __future__ import annotations

import hmac
import ipaddress
import logging
import os

from aiohttp import web

from . import settings, tokens
from .protocol import one_line
from .registry import Client

log = logging.getLogger("hermes.plugins.outrigger")


def _expected_key(request: web.Request | None = None) -> str:
    """The credential this route expects: the plugin's auth_token, else the gateway's
    OWN resolved key (the live api_server adapter reads extra['key'], profile-scoped
    secrets and env at startup and is exposed as app['api_server_adapter']), else raw
    env API_SERVER_KEY. Consulting the adapter's resolved key keeps a config-keyed
    gateway from falling into keyless local-owner trust (F-10). Never logs the key."""
    token = settings.get("auth_token")
    if token:
        return token
    app = getattr(request, "app", None)
    get = getattr(app, "get", None)
    adapter = get("api_server_adapter") if callable(get) else None
    key = getattr(adapter, "_api_key", "") or ""
    if key:
        return str(key)
    return os.environ.get("API_SERVER_KEY", "")


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
    key = _expected_key(request)
    local = _is_local(request)
    if not key:
        # No credential configured: only a process on this machine (not via a proxy) may attach.
        if not local:
            log.warning("outrigger: refused remote client %s (no key configured)", _peer(request))
            return web.json_response({"error": "forbidden"}, status=403)
        if not settings.get("allow_keyless_local"):
            log.warning("outrigger: refused %s: no key configured and allow_keyless_local is off; "
                        "configure auth_token or API_SERVER_KEY, or pair a token", _peer(request))
            return web.json_response({"error": "forbidden"}, status=403)
        # Fail loud (F-10): this admission rests on a locality heuristic (loopback + no
        # proxy headers) that a header-less same-host relay can defeat; it is not a
        # verified local peer. See docs/security-review/auth-trust-boundary.md.
        log.warning("outrigger: admitted %s as OWNER with no credential (keyless local trust); "
                    "locality is a heuristic a header-less relay can defeat — configure "
                    "auth_token/API_SERVER_KEY or set allow_keyless_local=false", _peer(request))
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
