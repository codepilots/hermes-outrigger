# Comment for NousResearch/hermes-agent#149673

(Our original issue draft duplicated #149673, so this is a comment on it instead.
No API-server route is needed: the Outrigger platform reads the catalog in the
plugin and sends it to clients over its own WebSocket.)

---

A third platform plugin with the same gap: **outrigger**, a platform adapter plus a
tool relay for thin clients (a browser side panel first). It registers with
`ctx.register_platform` and needs the catalog to send to clients for a `/` menu,
so a `gateway_command_catalog()` along these lines would cover it.

Two details from our side, beyond the earlier comment:

- **Scope it to the session's profile and workspace**, as the TUI's
  `commands.catalog` already does: an optional `session_key` (or profile) argument.
  In a multi-profile gateway, or with project-local skills, a process-wide list
  can offer commands the chat's session won't run.
- **Optionally include skill commands** (`include_skills=True`, marked with
  `source: "skill"`), the way `commands.catalog` and Discord's native command list
  do.

`tui_gateway.command_discovery.command_catalog` already builds this data
(registry, plugin and skill commands, scoped), so the public wrapper could reuse it
and return the plain `list[dict]` proposed here rather than the RPC's display
format.

+1 to plain data with `args_hint` verbatim. Happy to help test or review the PR.
