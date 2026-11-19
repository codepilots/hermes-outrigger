"""Append-only JSONL audit log. Args are hashed unless audit_args is set.

Responsibility: the audit trail for client tool calls (record) and the capture
artifact writer (_save_capture) that saves a client-returned image to a 0600 file
for Hermes's vision tool. Trust boundary: gateway -> owner-only audit artifacts.
A lost write (OSError) never breaks a tool call but is logged loud and counted
(_write_failures). Deliberately does NOT: cap or hash-chain the trail (it grows
unbounded; offline edits are undetectable), or audit notebook edits / platform
chat (those are outside audit.jsonl scope). dispatch.py calls record / _save_capture.
"""
# Audit: see AUDIT.md#security-sensitive-units (audit trail / capture artifacts)
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from . import settings

log = logging.getLogger("hermes.plugins.outrigger")

_lock = threading.Lock()
# Fail-loud counter of lost audit writes (OSError). A lost entry never breaks a tool
# call, but it is a security event: every loss is logged loud and counted here.
_write_failures = 0


def _path() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / "outrigger" / "audit.jsonl"


def record(*, session_id, client, tool, args, policy, outcome, duration_s, review=None,
           principal=None) -> None:
    entry = {
        "ts": round(time.time(), 3),
        "session": session_id,
        "client": client,
        "tool": tool,
        "args_sha256": hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "policy": policy,
        "outcome": outcome,
        "duration_s": round(duration_s, 3),
    }
    if principal is not None:
        entry["principal"] = principal  # stable token/user identity (client.principal)
    if review:
        entry["review"] = review
    if settings.get("audit_args"):
        entry["args"] = args
    p = None
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            # Created 0600 like the token store: the trail is owner-only by default.
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:
        # Auditing must never break a tool call — but never silently either: log the
        # path and reason loud and count the loss so a vanished trail is observable.
        global _write_failures
        with _lock:
            _write_failures += 1
            n = _write_failures
        log.error("outrigger: AUDIT WRITE FAILED (%d so far) to %s: %s — entry for tool %r "
                  "(session %s, outcome %s) was NOT recorded; tool calls continue but the "
                  "audit trail is incomplete", n, p, e, tool, session_id, outcome)


# ---------------------------------------------------------------- capture artifacts

CAPTURE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
KEEP_CAPTURES = 20


def _save_capture(image) -> dict:
    """A client tool returned an image ({mime, data: base64}). A plugin tool's result is text
    to the model, so the image is saved as a file and Hermes is told to open it with its
    vision_analyze tool (which shows it natively on vision-capable models, else describes it)."""
    import base64
    import os
    import secrets
    import time
    from pathlib import Path
    mime = str(image.get("mime", "")).lower() if isinstance(image, dict) else ""
    ext = CAPTURE_TYPES.get(mime)
    if not ext or not isinstance(image.get("data"), str):
        return {"image_error": "unsupported image (png, jpeg or webp, base64)"}
    try:
        raw = base64.b64decode(image["data"], validate=True)
    except Exception:
        return {"image_error": "invalid base64"}
    if len(raw) > MAX_CAPTURE_BYTES:
        return {"image_error": f"image larger than {MAX_CAPTURE_BYTES // (1024 * 1024)} MB"}
    # The declared type must match the bytes (they're handed to Hermes's vision tool).
    magic = {".png": raw[:8] == b"\x89PNG\r\n\x1a\n", ".jpg": raw[:3] == b"\xff\xd8\xff",
             ".webp": raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"}
    if not magic.get(ext, False):
        return {"image_error": f"data is not a {mime} image"}
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    folder = home / "plugin-data" / "outrigger" / "captures"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}{ext}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    for old in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime)[:-KEEP_CAPTURES]:
        old.unlink(missing_ok=True)
    return {"image_path": str(path), "bytes": len(raw),
            "hint": "Open it with vision_analyze (image_url = image_path) to look at it."}
