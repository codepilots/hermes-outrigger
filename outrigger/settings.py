"""Plugin settings, filled from plugins.entries.outrigger.settings at register()."""
from __future__ import annotations

DEFAULTS = {
    "enabled_routes": True,
    "allow_remote": False,
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
    "review_policy": "",
    "review_deny": "ask",
    # Notebook mode: {"name": "~/path"} roots whose .md notes the owner's clients may open.
    "notebook_roots": {},
    "notebook_max_bytes": 2 * 1024 * 1024,
    "notebook_lock_timeout_s": 600,
    "platform": True,  # register the Outrigger messaging platform (needs a gateway restart)
    "platform_owner_id": "owner",  # user id for chats from key-authenticated clients
    "platform_owner_name": "",
    # Slash commands paired (non-admin) token users may run, beyond Hermes's /help and /whoami.
    # /model and /reasoning are left out: with --global they rewrite config.yaml.
    "user_allowed_commands": ["status", "new", "stop", "retry", "undo", "title", "usage", "queue", "q",
                              "btw", "save", "context", "commands", "sessions", "resume"],  # ask: a deny goes to the user as a flagged prompt; block: final  # extra trusted rules appended to the reviewer prompt
}

SETTINGS: dict = dict(DEFAULTS)


def load(get_config) -> None:
    """get_config is ctx.get_config(key, default)."""
    for key, default in DEFAULTS.items():
        try:
            SETTINGS[key] = get_config(key, default)
        except Exception:
            SETTINGS[key] = default


def get(key: str):
    return SETTINGS.get(key, DEFAULTS[key])
