# Public API for the slash-command catalog (for platform plugins and HTTP clients)

**Hermes version:** v0.23.1 (2026.9.24), `3b4215785882`

## Summary

Make the slash-command catalog (built-in, plugin and skill commands, with
descriptions, argument hints and per-platform availability) readable through the
public plugin API, and optionally over the API server. Today only bundled
adapters (via internals) and local clients on the TUI gateway (via
`commands.catalog`) can list commands, so a third-party platform adapter cannot
offer command autocomplete or a native command menu.

## Motivation

I'm building a third-party platform adapter plugin. It registers with
`ctx.register_platform`, as the adapter guide recommends, for thin clients such as
a browser side panel. Slash commands work there through `handle_message`, but the
client cannot show the `/` menu that Discord, Telegram and Slack users get,
because the list of commands isn't available to plugins:

- **Discord's native commands come from internals.**
  `plugins/platforms/discord/adapter.py` (around line 4569) builds them from
  `hermes_cli.commands.COMMAND_REGISTRY` plus the private helpers
  `_is_gateway_available`, `_resolve_config_gates` and
  `_iter_plugin_command_entries`. None of these are in
  `hermes_cli/data/plugin_api.json`, so `hermes plugins validate` flags the same
  imports in a third-party plugin, and the rules on public surfaces ask plugins
  not to use them.
- **The TUI gateway already has the right data.** The `commands.catalog` RPC
  (`tui_gateway/methods_tools.py`, backed by
  `tui_gateway.command_discovery.command_catalog`) returns a categorized catalog
  of registry, plugin and skill commands, scoped to the session's profile and
  workspace. It is only reachable over `/api/ws` with a single-use ticket from the
  local control socket, which neither a gateway plugin nor a remote HTTP client
  can use.
- **The API server has no command route,** and Chat Completions does not dispatch
  slash commands. `/model` and `/help` sent as messages are answered by the
  model as ordinary prompts.

The text replies of `/help` and `/commands` work through any platform, but they
are for humans, not menus: no structure, no argument hints, no availability flags.

## Proposal

### 1. A public function

For example in a new public module `hermes_cli.command_catalog` (or by adding
`command_catalog` to the public list):

```python
def gateway_command_catalog(platform: str | None = None, *,
                            session_key: str | None = None,
                            include_skills: bool = True) -> list[CommandInfo]: ...

@dataclass(frozen=True)
class CommandInfo:
    name: str                 # canonical, without "/"
    description: str
    category: str             # "Session", "Configuration", "Info", ..., "Plugin", "Skill"
    args_hint: str = ""       # "<prompt>", "[name]"
    aliases: tuple[str, ...] = ()
    subcommands: tuple[str, ...] = ()
    source: str = "builtin"   # "builtin" | "plugin" | "skill"
    plugin: str | None = None # owning plugin for plugin commands
```

- **Gateway availability, without private helpers.** The result only includes
  commands available to the gateway: config gates resolved and `cli_only`
  excluded, i.e. what `_is_gateway_available(cmd, _resolve_config_gates())`
  decides today.
- **Profile and workspace scope,** as `commands.catalog` does today, so plugin
  and project-local skill commands match what the session would actually run.
  `session_key`, when given, picks the session's scope.
- **Platform filtering.** When `platform` is given, apply that platform's own
  filters where they exist, so the list matches what the platform would accept.
- **Access control is out of scope.** `user_allowed_commands` /
  `allow_admin_from` can stay with the adapter, which keeps enforcing on dispatch;
  the catalog is for display.

A thin wrapper over `tui_gateway.command_discovery.command_catalog` would do, as
long as the result is a stable, documented shape rather than the RPC's display
format (`pairs`, `canon`, `sub`).

### 2. Optional: an HTTP route on the API server

```
GET /api/commands?session_id=<id>
Authorization: Bearer <API_SERVER_KEY>
```

It would return the same list as JSON, scoped to the session's profile, and be
advertised in `GET /v1/capabilities` (for example `"commands_catalog": true`).
This would let HTTP clients (Open WebUI-style frontends, extensions) build
command menus without a platform adapter. The natural companion is a dispatch
route for API-owned sessions running the same reviewed set as `slash.exec`, but
that can be a separate issue.

## Acceptance criteria

- The function (and `CommandInfo`) is listed in `plugins/public-api.md` /
  `plugin_api.json`, so `hermes plugins validate` accepts it.
- Its output matches what the bundled Discord adapter registers today, with the
  same gating, plus skills when requested.
- It is profile- and workspace-scoped in a multi-profile gateway, like
  `commands.catalog`.
- Bundled adapters can migrate to it, which removes their private imports.
- Optional route: authenticated with the API server key, scoped to the session,
  advertised in `/v1/capabilities`.

## Alternatives considered

- **Importing `hermes_cli.commands` internals from the plugin.** It works today
  but breaks the public-surface rule and would break silently on refactors.
- **Parsing `/help` or `/commands` replies.** They are localized text meant for
  people; parsing is fragile and loses argument hints and availability.
- **A build-time static list from the docs.** This is my current fallback. It
  goes stale between releases and can't include the user's plugin commands or
  skills.
- **Using `commands.catalog` over `/api/ws`.** That needs a ticket from the local
  control socket, so it isn't available inside a plugin or to remote clients.

## Context

This is for the open-source "outrigger" plugin: a platform adapter plus a tool
relay for thin clients. I'm happy to send a PR for part 1 if the shape above
looks right, or to adapt it to whatever shape you'd prefer.
