"""Append-only JSONL audit log. Args are hashed unless audit_args is set."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from . import settings

_lock = threading.Lock()


def _path() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / "outrigger" / "audit.jsonl"


def record(*, session_id, client, tool, args, policy, outcome, duration_s, review=None) -> None:
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
    if review:
        entry["review"] = review
    if settings.get("audit_args"):
        entry["args"] = args
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock, p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass  # auditing must never break a tool call
