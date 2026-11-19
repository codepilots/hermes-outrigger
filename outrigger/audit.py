"""Append-only JSONL audit log. Args are hashed unless audit_args is set."""
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
