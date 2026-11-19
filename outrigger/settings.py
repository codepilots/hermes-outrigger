"""Plugin settings, filled from plugins.entries.outrigger.settings at register().

Trust boundary: operator config -> trust switches. Bool-typed settings are
coerced via _as_bool (quoted "false"/0/1 normalized); an unrecognized value logs
loud and uses the declared default (fail closed for refusal switches such as
allow_keyless_local / allow_remote / owner_key_remote). Deliberately does NOT:
authenticate or enforce — it only resolves the switches the security modules read.
"""
# Audit: see AUDIT.md#security-sensitive-units (trust settings / bool coercion)
from __future__ import annotations

import logging

log = logging.getLogger("hermes.plugins.outrigger")

# Bool-typed settings may arrive as quoted YAML strings ("false") or 0/1 from a
# UI/env path. Raw truthiness would leave a refusal switch ON for "false" (fail
# open), so normalize them; unrecognized values log loud and use the default.
_BOOL_TRUE = {"true", "1", "yes", "on"}
_BOOL_FALSE = {"false", "0", "no", "off", ""}


def _as_bool(key: str, value, default: bool) -> bool:
    """Fail-closed bool coercion for trust switches. Actor: operator config
    (possibly quoted YAML strings / 0/1). Control: true/false-ish strings and
    0/1 normalized; anything unrecognized -> loud WARNING naming key/value/
    default, then the DECLARED default — so a quoted "false" can never leave
    allow_keyless_local / allow_remote / owner_key_remote on."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _BOOL_TRUE:
            return True
        if v in _BOOL_FALSE:
            return False
    elif isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    log.warning("outrigger: setting %s=%r is not a boolean; using default %r "
                "(write true/false without quotes in config.yaml)", key, value, default)
    return default

DEFAULTS = {
    "enabled_routes": True,
    # Remote access (docs/remote-access.md). A request is "local" only if it comes from
    # loopback AND carries no reverse-proxy headers: a tunnel (cloudflared) also connects
    # from 127.0.0.1, so its requests count as remote.
    "allow_remote": False,  # accept remote clients at all (paired tokens)
    "owner_key_remote": False,  # also accept the API key from remote clients (prefer admin tokens)
    # F-10: admit a credential-less loopback peer as OWNER when no key exists anywhere
    # (plugin auth_token, the gateway's resolved key, env API_SERVER_KEY). Locality is a
    # heuristic a header-less same-host relay can defeat; off refuses every keyless
    # owner attach. See docs/security-review/auth-trust-boundary.md.
    "allow_keyless_local": True,
    # Toolsets for paired non-admin users' chats (Hermes's toolsets_for_source); the owner
    # and admin tokens keep the platform default. "no_mcp" keeps MCP servers out.
    "user_toolsets": ["web", "outrigger", "no_mcp"],
    "max_clients_per_token": 4,
    "user_messages_per_min": 20,  # chat messages and // commands per paired non-admin user
    "max_chat_chars": 100_000,
    "auth_token": "",
    "default_timeout_s": 60,
    "max_timeout_s": 400,  # Hermes abandons tool calls at 420s
    "max_result_chars": 20000,
    "max_calls_per_turn": 20,
    "context_max_chars": 6000,
    "audit_args": False,
    "allow_subagents": False,
    "review": "smart",  # smart: LLM reviewer for non-auto calls; off: none
    "review_timeout_s": 30,
    "review_policy": "",  # extra trusted rules appended to the reviewer prompt
    "review_deny": "ask",  # ask: a deny goes to the user as a flagged prompt; block: final
    # Notebook mode: {"name": "~/path"} roots whose .md notes the owner's clients may open.
    "notebook_roots": {},
    "notebook_roots_for": {},  # paired users' roots: {"ann": ["shared"]} or {"ann": "*"}; the owner gets all
    "notebook_max_bytes": 2 * 1024 * 1024,
    "notebook_lock_timeout_s": 600,
    "platform": True,  # register the Outrigger messaging platform (needs a gateway restart)
    "platform_owner_id": "owner",  # user id for chats from key-authenticated clients
    "platform_owner_name": "",
    # Slash commands paired (non-admin) token users may run, beyond Hermes's /help and /whoami.
    # /model and /reasoning are left out: with --global they rewrite config.yaml.
    "user_allowed_commands": ["status", "new", "stop", "retry", "undo", "title", "usage", "queue", "q",
                              "btw", "save", "context", "commands", "sessions", "resume"],
}

SETTINGS: dict = dict(DEFAULTS)


def load(get_config) -> None:
    """get_config is ctx.get_config(key, default). Reads every DEFAULTS key; a
    raising get_config falls back to the declared default; bool-typed keys go
    through _as_bool (unrecognized -> WARNING + default, fail closed)."""
    for key, default in DEFAULTS.items():
        try:
            raw = get_config(key, default)
        except Exception:
            raw = default
        SETTINGS[key] = _as_bool(key, raw, default) if isinstance(default, bool) else raw


def get(key: str):
    """The resolved switch value; falls back to the declared default when a key
    was never loaded. Security modules read every trust switch through this."""
    return SETTINGS.get(key, DEFAULTS[key])
