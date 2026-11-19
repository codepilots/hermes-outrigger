"""outrigger_call / outrigger_tools handlers.

Tool handlers run on a Hermes worker thread, not the gateway event loop, so these
are plain sync functions that block on a concurrent Future; the websocket side
resolves it from the gateway loop.

Trust boundary: model -> gateway -> client device -> model. Selects the client
(subagent gate via registry), validates args, runs the advisory reviewer, relays
the call, and bounds/sanitizes the untrusted result before the model sees it
(F-07). Every outcome is audited (audit.record): principal=client.principal
once a client is picked, None for pre-client rejections. One documented
exception: an OSError in the capture writer (audit._save_capture) propagates
and skips that call's audit record — fail loud via the ws handler safety net;
hardening follow-up logged. Capture images go to audit._save_capture. Deliberately does NOT: execute the tool (the client does),
or make the reviewer's deny final unless review_deny="block" (default advisory).
"""
# Audit: see AUDIT.md#security-sensitive-units (dispatch relay / result bounding)
from __future__ import annotations

import concurrent.futures
import json
import logging
import time

from . import audit, platform, reviewer, settings
# Re-export: tests call dispatch._save_capture; audit.py owns the capture writer.
from .audit import _save_capture
from .protocol import sanitize_text, validate_args
from .registry import REGISTRY, Client, ClientGone

log = logging.getLogger("hermes.plugins.outrigger")


def _err(code: str, message: str = "", **extra) -> str:
    return json.dumps({"ok": False, "error": code, "message": message, **extra})


def platform_chat_clients(session_id) -> list | None:
    """On an Outrigger-platform turn, the client serving this chat (task-local session
    identity via the public get_session_env); None on other platforms. Subagent
    gate applies here too: marked session + allow_subagents=false (default) -> []
    (no client). Binds the session so per-turn bookkeeping (cancel, caps) applies."""
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
    """Select the client for a call. Subagent gate: a marked session with
    allow_subagents=false (default) -> outrigger_subagent_denied (the mark IS
    the deny signal). No client / unknown / ambiguous -> JSON refusal listing
    attached clients; only clients bound to this session/conversation."""
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
    """Actor: Hermes model (worker thread). Input: tool + args (+client).
    Control, in order: shape check, client pick (subagent gate), declared-tool
    lookup, validate_args BEFORE relay, per-turn cap max_calls_per_turn
    (default 20), advisory reviewer for policy != auto (review=smart + LLM;
    deny final only with review_deny="block", opt-in). Sink: relay to the
    client device via _run; every outcome — including pre-client rejections —
    audited with client.principal. Refusals are JSON error codes (bad_request/
    no_client/subagent_denied/unknown_tool/bad_args/call_limit/review_denied)."""
    tool = args.get("tool")
    call_args = args.get("args")
    if call_args is None:
        call_args = {}
    sid = session_id or conversation_id or ""

    def reject(client_id, policy, text, principal=None):
        """Audit-and-return a pre-client refusal: every rejection (bad request,
        no/unknown client, unknown tool, bad args, call limit) is recorded with
        outcome rejected:<code> before the error JSON goes back to the model."""
        audit.record(session_id=sid, client=client_id, tool=tool, args=call_args,
                     policy=policy, outcome="rejected:" + json.loads(text)["error"],
                     duration_s=0.0, principal=principal)
        return text

    if not isinstance(tool, str) or not isinstance(call_args, dict):
        return reject(None, None, _err("outrigger_bad_request",
                                       "'tool' must be a string and 'args' an object."))

    client, problem = _pick_client(session_id, conversation_id, args.get("client"))
    if problem:
        return reject(None, None, problem)
    decl = client.tools.get(tool)
    if decl is None:
        return reject(client.id, None, _err(
            "outrigger_unknown_tool", f"{client.name} has no tool {tool!r}.",
            tools=sorted(client.tools)), principal=client.principal)
    bad = validate_args(decl.parameters, call_args)
    if bad:
        return reject(client.id, decl.policy, _err("outrigger_bad_args", bad),
                      principal=client.principal)

    if REGISTRY.count_call(sid, client) > settings.get("max_calls_per_turn"):
        return reject(client.id, decl.policy, _err(
            "outrigger_call_limit", "Per-turn call limit reached for this client."),
            principal=client.principal)

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
                         duration_s=time.monotonic() - started, review=review,
                         principal=client.principal)
            return _err("outrigger_review_denied",
                        "Blocked by the outrigger reviewer: " + review["reason"]
                        + " Do not retry this action; explain to the user what you wanted to do.")

    timeout = min(decl.timeout_s or settings.get("default_timeout_s"),
                  settings.get("max_timeout_s"))
    outcome, text = _run(client, sid, decl, tool, call_args, timeout, review)
    REGISTRY.note_action(sid, tool, call_args, outcome)
    audit.record(session_id=sid, client=client.id, tool=tool, args=call_args,
                 policy=decl.policy, outcome=outcome, duration_s=time.monotonic() - started,
                 review=review, principal=client.principal)
    return text


def _run(client: Client, sid: str, decl, tool: str, call_args: dict, timeout: float,
         review: dict | None = None):
    """Relay one call and bound the result. Control: per-client semaphore
    (concurrency 1..8; queue time counts against the deadline), caller-computed
    timeout min(decl.timeout_s or default_timeout_s=60, max_timeout_s=400) —
    timeout sends cancel + logs INFO; ClientGone -> disconnected. Sink:
    client.send(call) then the pending future. Untrusted result handling (F-07):
    ok -> _format_ok (untrusted label + cap); error detail coerced/sanitized/
    capped again here (ingress already bounded it); short gateway codes pass
    through so the cancelled status survives. Returns (outcome, JSON) for audit."""
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
    # F-07: outrigger_-prefixed strings and non-string errors are client-controlled
    # detail too. Coerce, strip control chars and cap with the same bound as the ok
    # path; short gateway codes (outrigger_cancelled, outrigger_timeout, …) pass
    # through the cap unchanged, so their meaning and the cancelled status survive.
    if not isinstance(code, str):
        code = json.dumps(code, default=str)
    limit = min(decl.max_result_chars or settings.get("max_result_chars"),
                settings.get("max_result_chars"))
    code, _ = sanitize_text(code, limit)
    message = sanitize_text(str(msg.get("message", "")), limit)[0]
    return "cancelled" if code == "outrigger_cancelled" else "error", _err(code, message)


def _format_ok(content, decl) -> str:
    """Bound an ok result before the model: capture images go to
    audit._save_capture (0600 file + vision hint), content coerced to text,
    sanitize_text caps at min(decl.max_result_chars, max_result_chars=20000).
    Invariant: every ok result is labeled untrusted: True and truncated is
    surfaced — client output is data, never instructions."""
    if isinstance(content, dict) and "image" in content:
        content = {**{k: v for k, v in content.items() if k != "image"}, **audit._save_capture(content["image"])}
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
    """Turn interrupted: end_turn resolves leftover futures as outrigger_cancelled
    and a best-effort cancel frame goes to each client (_try_send swallows
    ClientGone — the future already carries the outcome)."""
    for client, call_id in REGISTRY.end_turn(session_id, cancel=True):
        _try_send(client, {"type": "cancel", "call_id": call_id, "reason": "turn_interrupted"})
