"""Per-client tokens (spec M4 / platform P4).

A token lets one client (a phone, another browser, another person) connect without
the gateway's API key. It identifies a user on the Outrigger platform, so Hermes's
own allowlist and slash-command gating apply to that user. Tokens are minted with
`hermes outrigger pair`, shown once, and stored only as SHA-256 hashes in
$HERMES_HOME/plugin-data/outrigger/tokens.json (mode 0600). The gateway re-reads the
file when it changes, so pairing and revoking need no restart.

Token format: otr_<id>_<secret>  (id: 8 hex chars; secret: 32 url-safe chars)

Trust boundary: owner CLI -> token store -> gateway admission. Only SHA-256
hashes are stored (0600); verify is constant-time (dummy compare on unknown id);
revoke sets a timestamp and live connections close on the keepalive check.
Deliberately does NOT: store secrets, log tokens, or decide remote admission
(auth.py reads allow_remote); it only mints/verifies/revokes scoped identities.
"""
# Audit: see AUDIT.md#security-sensitive-units (credentials / token pairing)
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_cache: dict = {"key": None, "data": {"tokens": {}}}  # key: (path, mtime_ns)
TOKEN_RE = re.compile(r"^otr_([0-9a-f]{8})_([A-Za-z0-9_-]{32})\Z")
USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}\Z")


def path() -> Path:
    """The token store: $HERMES_HOME/plugin-data/outrigger/tokens.json
    (owner-only; _save always leaves it 0600)."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / "outrigger" / "tokens.json"


def _load() -> dict:
    """Read the store, cached by (path, mtime_ns) so pairing/revoking needs no
    restart. Missing file -> empty store. Unreadable or dict-shaped corrupt JSON ->
    empty store (fail closed: no token verifies). Non-dict JSON raises instead
    (also fail closed: the handshake errors out, no token verifies); routing it
    through the empty-store path is a logged hardening follow-up. Hashes only —
    no secret is read from disk."""
    p = path()
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except FileNotFoundError:
        _cache.update(key=None, data={"tokens": {}})
        return _cache["data"]
    if key != _cache["key"]:
        try:
            data = json.loads(p.read_text())
            if not isinstance(data.get("tokens"), dict):
                raise ValueError("bad token file")
        except (OSError, ValueError):
            data = {"tokens": {}}
        _cache.update(key=key, data=data)
    return _cache["data"]


def _save(data: dict) -> None:
    """Persist hash-only records atomically: temp file created 0600 then
    os.replace (the store is 0600 after every save), cache invalidated so the
    next _load re-reads. No secret is ever written."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)
    _cache.update(key=None)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint(user_id: str, *, user_name: str = "", label: str = "", admin: bool = False) -> tuple[str, str]:
    """Create a token; returns (token_id, token). The token is not stored and can't be shown again.
    Actor: owner CLI (hermes outrigger pair). Control: USER_RE id, not the
    owner's platform_owner_id, random id/secret (secrets module). Sink: SHA-256
    hash + metadata in the 0600 store (shown-once invariant: the secret exists
    only in the returned string). Refusals: ValueError (bad/owner id), printed
    by cli; nothing is stored."""
    if not USER_RE.match(user_id or ""):
        raise ValueError("user id: 1-64 of A-Z a-z 0-9 _ . @ -")
    from . import settings
    if user_id == str(settings.get("platform_owner_id") or "owner"):
        raise ValueError(f"{user_id!r} is the owner's id (platform_owner_id); pick another, or use --admin")
    with _lock:
        data = _load()
        tid = secrets.token_hex(4)
        while tid in data["tokens"]:
            tid = secrets.token_hex(4)
        secret = secrets.token_urlsafe(24)[:32]
        while len(secret) < 32:
            secret += secrets.token_urlsafe(8)
        secret = secret[:32]
        data["tokens"][tid] = {"hash": _hash(secret), "user_id": user_id, "user_name": user_name[:64],
                               "label": label[:64], "admin": bool(admin), "created": int(time.time()),
                               "revoked": None}
        _save(data)
    return tid, f"otr_{tid}_{secret}"


def revoke(token_id: str) -> bool:
    """Actor: owner CLI. Sets the revoked timestamp (hash record kept) and saves;
    False for unknown/already-revoked ids. Live connections close on the <=20s
    keepalive check (ws PING_INTERVAL=20); new handshakes fail immediately via
    verify. Revocation is a timestamp, not a delete — listing still shows it."""
    with _lock:
        data = _load()
        rec = data["tokens"].get(token_id)
        if rec is None or rec.get("revoked"):
            return False
        rec["revoked"] = int(time.time())
        _save(data)
        return True


def listing() -> list[dict]:
    with _lock:
        return [{"id": tid, **{k: v for k, v in rec.items() if k != "hash"}}
                for tid, rec in sorted(_load()["tokens"].items(), key=lambda kv: kv[1].get("created", 0))]


def verify(token: str) -> dict | None:
    """The token's record ({id, user_id, user_name, label, admin}) if valid and active.
    Actor: untrusted presented token at handshake. Control: \\Z-anchored TOKEN_RE,
    then constant-time hmac.compare_digest of the SHA-256 hash — including a
    dummy compare on unknown/revoked ids so timing does not reveal which ids
    exist. Sink: the admission principal (auth.py). Bad/revoked -> None (auth
    WARNING-logs + 401); never logs the token."""
    m = TOKEN_RE.match(token or "")
    if not m:
        return None
    tid, secret = m.groups()
    with _lock:
        rec = _load()["tokens"].get(tid)
    if rec is None or rec.get("revoked"):
        # Still compare, so timing doesn't reveal which ids exist.
        hmac.compare_digest(_hash(secret), "0" * 64)
        return None
    if not hmac.compare_digest(_hash(secret), rec["hash"]):
        return None
    return {"id": tid, "user_id": rec["user_id"], "user_name": rec.get("user_name", ""),
            "label": rec.get("label", ""), "admin": bool(rec.get("admin"))}


def is_active(token_id: str) -> bool:
    """Keepalive predicate (ws every PING_INTERVAL=20s): record exists and not
    revoked. Re-reads the store on mtime change, so revoke propagates without a
    restart (~20s worst case for live connections, by design)."""
    with _lock:
        rec = _load()["tokens"].get(token_id)
    return rec is not None and not rec.get("revoked")


def active_users() -> tuple[set[str], set[str]]:
    """(user ids with an active token, those whose token is admin). Read by
    platform.access_extra / toolsets_for_user to sync Hermes's allowlists;
    revoked tokens drop out on the next call."""
    with _lock:
        recs = [r for r in _load()["tokens"].values() if not r.get("revoked")]
    return {r["user_id"] for r in recs}, {r["user_id"] for r in recs if r.get("admin")}
