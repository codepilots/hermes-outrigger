# Comment for NousResearch/hermes-agent#141790

---

A data point for this: `toolsets_for_source()` works well as the mechanism. I
implemented it in a plugin platform adapter
([hermes-outrigger](https://github.com/codepilots/hermes-outrigger)), so paired
non-admin users get a narrow toolset while the owner keeps the platform default.
I verified it live on 0.23.1:

- The paired user's turn offered only `web_search`, `web_extract`, the plugin's
  own tools and the Tool Search bridge (`tool_search`, `tool_describe`,
  `tool_call`).
- `tool_search` found no terminal, file, memory or email tools.
- `tool_call` refused `terminal` and `memory` with "not available in this
  execution context". The bridge respects the narrowed set, so it doesn't
  bypass it.

Three pitfalls for anyone wiring this into Discord (or any adapter):

1. **An empty list isn't "no tools".** `_resolve_enabled_toolsets_for_source`
   only applies a truthy override, so `[]` falls back to the full platform
   default, terminal included. Return something like `["no_mcp"]` for "nothing".
2. **MCP servers are added unless excluded.** Every globally enabled MCP server
   is merged into the list unless it contains `no_mcp`.
3. **The default is broad.** A plugin platform with no `platform_toolsets`
   entry gets the implicit `hermes-<platform>` bundle: the core tools,
   including `terminal`, `write_file` and `execute_code`.

So for this issue, the Discord adapter could override `toolsets_for_source()`,
keyed on `source.user_id` or `chat_type == "dm"`. That gives the owner's DMs
`discord_admin` writes and keeps shared channels read-only. The validation path
through `_get_platform_tools` is already there.
