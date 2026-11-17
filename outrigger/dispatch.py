"""outrigger_call / outrigger_tools handlers.

Tool handlers run on a Hermes worker thread, not the gateway event loop, so these
are plain sync functions that block on a concurrent Future; the websocket side
resolves it from the gateway loop.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time

from . import audit, platform, reviewer, settings
from .protocol import sanitize_text, validate_args
from .registry import REGISTRY, Client, ClientGone

log = logging.getLogger("hermes.plugins.outrigger")


def _err(code: str, message: str = "", **extra) -> str:
    return json.dumps({"ok": False, "error": code, "message": message, **extra})


def platform_chat_clients(session_id) -> list | None:
    """On an Outrigger-platform turn, the client serving this chat (task-local session
    identity via the public get_session_env); None on other platforms."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None
    if get_session_env("HERMES_SESSION_PLATFORM") != platform.PLATFORM:
        return None
    if REGISTRY.is_subagent(session_id) and not settings.get("allow_subagents"):
        return []
    client = platform.client_for_chat(get_session_env("HERMES_SESSION_CHAT_ID"))
    if client is None:
        return []
    if session_id:
        REGISTRY.bind_extra(client, session_id)  # so per-session bookkeeping (cancel, caps) applies
    return [client]


def _clients(session_id, conversation_id):
    on_platform = platform_chat_clients(session_id)
    if on_platform is not None:
        return on_platform
    return REGISTRY.for_session(session_id, conversation_id,
                                allow_subagent=bool(settings.get("allow_subagents")))


def _pick_client(session_id, conversation_id, wanted: str | None):
    clients = _clients(session_id, conversation_id)
    if not clients:
        if REGISTRY.is_subagent(session_id) and not settings.get("allow_subagents"):
            return None, _err("outrigger_subagent_denied",
                              "Subagents may not call attached clients.")
        return None, _err("outrigger_no_client", "No client is attached to this session.")
    if wanted:
        for c in clients:
            if c.id == wanted or c.name == wanted:
                return c, None
        return None, _err(
            "outrigger_unknown_client",
            f"No attached client {wanted!r}.",
            clients=[{"id": c.id, "name": c.name} for c in clients],
        )
    if len(clients) > 1:
        return None, _err(
            "outrigger_ambiguous_client",
            "Several clients are attached; pass 'client'.",
            clients=[{"id": c.id, "name": c.name, "label": c.label} for c in clients],
        )
    return clients[0], None


def outrigger_call(args: dict, session_id: str | None = None,
                   conversation_id: str | None = None, **_ignored) -> str:
    tool = args.get("tool")
    call_args = args.get("args")
    if call_args is None:
        call_args = {}
    sid = session_id or conversation_id or ""

    def reject(client_id, policy, text):
        audit.record(session_id=sid, client=client_id, tool=tool, args=call_args,
                     policy=policy, outcome="rejected:" + json.loads(text)["error"],
                     duration_s=0.0)
        return text

    if not isinstance(tool, str) or not isinstance(call_args, dict):
        return _err("outrigger_bad_request", "'tool' must be a string and 'args' an object.")

    client, problem = _pick_client(session_id, conversation_id, args.get("client"))
    if problem:
        return reject(None, None, problem)
    decl = client.tools.get(tool)
    if decl is None:
        return reject(client.id, None, _err(
            "outrigger_unknown_tool", f"{client.name} has no tool {tool!r}.",
            tools=sorted(client.tools)))
    bad = validate_args(decl.parameters, call_args)
    if bad:
        return reject(client.id, decl.policy, _err("outrigger_bad_args", bad))

    if REGISTRY.count_call(sid, client) > settings.get("max_calls_per_turn"):
        return reject(client.id, decl.policy, _err(
            "outrigger_call_limit", "Per-turn call limit reached for this client."))

    started = time.monotonic()
    review = None
    if decl.policy != "auto" and reviewer.enabled():
        review = reviewer.review(
            tool=tool, description=decl.description, args=call_args, client_name=client.name,
            client_context=client.summary,
            user_request=REGISTRY.intent.get(sid) or REGISTRY.intent.get(conversation_id or "", ""),
            recent_actions=REGISTRY.recent_actions(sid))
        # review_deny "ask" (default): the deny travels with the call and the client must
        # put it to the user, who is usually right there. "block": final, as for terminal
        # smart approvals.
        if review["verdict"] == "deny" and settings.get("review_deny") == "block":
            audit.record(session_id=sid, client=client.id, tool=tool, args=call_args,
                         policy=decl.policy, outcome="rejected:outrigger_review_denied",
                         duration_s=time.monotonic() - started, review=review)
            return _err("outrigger_review_denied",
                        "Blocked by the outrigger reviewer: " + review["reason"]
                        + " Do not retry this action; explain to the user what you wanted to do.")

    timeout = min(decl.timeout_s or settings.get("default_timeout_s"),
                  settings.get("max_timeout_s"))
    outcome, text = _run(client, sid, decl, tool, call_args, timeout, review)
    REGISTRY.note_action(sid, tool, call_args, outcome)
    audit.record(session_id=sid, client=client.id, tool=tool, args=call_args,
                 policy=decl.policy, outcome=outcome, duration_s=time.monotonic() - started,
                 review=review)
    return text


def _run(client: Client, sid: str, decl, tool: str, call_args: dict, timeout: float,
         review: dict | None = None):
    deadline = time.monotonic() + timeout
    # Serialise per client (concurrency 1 by default); queue time counts against the deadline.
    if not client.sem.acquire(timeout=timeout):
        return "timeout", _err("outrigger_timeout", "Client busy; call timed out while queued.")
    try:
        try:
            call_id, fut = REGISTRY.new_call(client, sid, tool)
        except ClientGone:
            return "disconnected", _err("outrigger_disconnected", "Client is no longer connected.")
        try:
            msg_out = {
                "type": "call", "call_id": call_id, "tool": tool, "args": call_args,
                "deadline_ms": int(max(deadline - time.monotonic(), 0) * 1000),
                "turn_id": REGISTRY.turns.get(sid, ""),
            }
            if review:
                msg_out["review"] = review  # advisory; the client decides
            client.send(msg_out)
            msg = fut.result(timeout=max(deadline - time.monotonic(), 0.01))
        except ClientGone:
            REGISTRY.forget(client, call_id)
            return "disconnected", _err("outrigger_disconnected", "Client is no longer connected.")
        except concurrent.futures.TimeoutError:
            p = REGISTRY.forget(client, call_id)
            _try_send(client, {"type": "cancel", "call_id": call_id, "reason": "timeout"})
            extra = {"last_progress": p.last_progress} if p and p.last_progress else {}
            log.info("outrigger: call %s (%s) to %s timed out", call_id, tool, client.id)
            return "timeout", _err("outrigger_timeout", f"No result within {int(timeout)}s.", **extra)
    finally:
        client.sem.release()

    if msg.get("ok"):
        return "ok", _format_ok(msg.get("content"), decl)
    code = msg.get("error") or "outrigger_tool_error"
    if isinstance(code, str) and not code.startswith("outrigger_"):
        detail = sanitize_text(str(code), 2000)[0]
        return "error", _err("outrigger_tool_error", detail)
    return "cancelled" if code == "outrigger_cancelled" else "error", _err(code, str(msg.get("message", "")))


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
    path.write_bytes(raw)
    for old in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime)[:-KEEP_CAPTURES]:
        old.unlink(missing_ok=True)
    return {"image_path": str(path), "bytes": len(raw),
            "hint": "Open it with vision_analyze (image_url = image_path) to look at it."}


def _format_ok(content, decl) -> str:
    if isinstance(content, dict) and "image" in content:
        content = {**{k: v for k, v in content.items() if k != "image"}, **_save_capture(content["image"])}
    if not isinstance(content, str):
        content = json.dumps(content, default=str)
    limit = min(decl.max_result_chars or settings.get("max_result_chars"),
                settings.get("max_result_chars"))
    text, truncated = sanitize_text(content, limit)
    return json.dumps({"ok": True, "untrusted": True, "content": text, "truncated": truncated})


def _try_send(client: Client, msg: dict) -> None:
    try:
        client.send(msg, timeout=2.0)
    except ClientGone:
        pass


def outrigger_tools(args: dict, session_id: str | None = None,
                    conversation_id: str | None = None, **_ignored) -> str:
    clients = _clients(session_id, conversation_id)
    return json.dumps({"clients": [{
        "id": c.id, "name": c.name, "label": c.label, "kind": c.kind, "summary": c.summary,
        "tools": [{"name": t.name, "description": t.description,
                   "parameters": t.parameters, "policy": t.policy}
                  for t in c.tools.values()],
    } for c in clients]})


def cancel_session(session_id: str) -> None:
    for client, call_id in REGISTRY.end_turn(session_id, cancel=True):
        _try_send(client, {"type": "cancel", "call_id": call_id, "reason": "turn_interrupted"})
