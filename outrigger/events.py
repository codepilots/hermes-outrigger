"""Session events to Hermes frontends via the public ctx.emit_to_session.

Trust boundary: gateway -> session viewers (progress notifications). Never raises;
a failed emit is logged at debug and dropped. Deliberately does NOT: carry tool
results or note content to the model, or authenticate — it only forwards progress.
"""
from __future__ import annotations

import logging

log = logging.getLogger("hermes.plugins.outrigger")

_emit = None  # ctx.emit_to_session, when the running Hermes provides it


def configure(ctx) -> None:
    global _emit
    _emit = getattr(ctx, "emit_to_session", None)


def emit(session_id: str, event: str, payload: dict) -> bool:
    """Send plugin.outrigger.<event> to the session's viewers. Never raises."""
    if _emit is None or not session_id:
        return False
    try:
        return bool(_emit(session_id, event, payload))
    except Exception as e:
        log.debug("outrigger: emit_to_session failed: %s", e)
        return False
