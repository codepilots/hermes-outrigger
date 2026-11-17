# hermes-outrigger

A Hermes gateway plugin that lets a thin client attach to a Hermes session and
offer tools that run on the client, not on the Hermes host. Hermes calls them as
ordinary tool calls; the plugin relays each call to the client and returns the
client's result as the tool result, inside the same turn.

Status: plugin side of M1-M3 complete (protocol, multi-client, subagent
blocking, progress events, audit log, Python reference client, 51 tests). Verified
end to end on Hermes v0.23.1 (2026-11-16): a client tool called mid-turn with
its result in the same reply, audit entry written, and delegated subagents
refused. M2's browser UI lives in the Hermes
Sidebar repo; M4 (pairing tokens) is not started.

Install (dev):

```
ln -s $PWD/outrigger ~/.hermes/plugins/outrigger
hermes plugins enable outrigger
hermes tools enable --platform api_server outrigger   # plugin toolsets are opt-in
systemctl --user restart hermes-gateway                # routes mount only at start
```

The plugin also registers the **Outrigger platform** (on by default; setting
`platform`): clients that declare the `chat` capability chat through Hermes's
messaging pipeline, so slash commands such as `/model` and `/new` work as on
Discord. See docs/platform.md. Streaming follows Hermes's global `streaming`
setting. The `/` autocomplete list is generated from Hermes's docs; after
upgrading Hermes, run `python tools/gen_command_catalog.py`.

**Other devices or people** get their own client token instead of the API key:
`hermes outrigger pair --user <id> [--label phone] [--admin]` (shown once),
`hermes outrigger tokens`, `hermes outrigger revoke <id>`. A token client chats
only through the Outrigger platform, as its own user. Non-admin users can run the
slash commands in the `user_allowed_commands` setting, plus `/help` and `/whoami`.
For a gateway on another machine, also set `allow_remote: true`.

**Notebook mode** (off until configured): set `notebook_roots` in the plugin
settings, e.g. `plugins.entries.outrigger.settings.notebook_roots: {notes:
~/vaults/notes}`. Clients can then open `.md` notes from those folders, edit them
with autosave, and send `//` lines to Hermes. Paired token users get only the
roots listed for them in `notebook_roots_for` (e.g. `{ann: [shared]}`), and
everyone with a note open sees who else is in it. See docs/notebook.md.

`outrigger_call` is offered in every session once the toolset is enabled, so a
client can attach before or after the conversation starts; with no client
attached, calls fail fast.
Set `API_SERVER_KEY` (or `plugins.entries.outrigger.settings.auth_token`); with
neither set, only loopback clients may attach.
Tests: `python -m venv .venv && .venv/bin/pip install aiohttp jsonschema pytest-aiohttp && .venv/bin/pytest`.
Reference client: `clients/python_client.py`.

- docs/spec.md: design, protocol, security model, open questions
- docs/scenarios.md: where an outrigger fits, where it doesn't (vs MCP, vs a plugin)
- docs/platform.md: proposal for the Outrigger platform, which makes clients a full Hermes messaging platform (slash commands, model picker, approvals)
- docs/notebook.md: proposal for Notebook mode: edit Markdown notes on the Hermes host from a client, with `//` lines as commands to Hermes

The first client will be the Hermes Sidebar Chromium extension
(github.com/codepilots/hermes-sidebar), exposing page tools such as run_js.

Name: an outrigger is a float rigged alongside a hull that extends a boat's
reach and steadies it. The plugin does the same for Hermes: a light attachment
that lets it act where the hull cannot go.

License: MIT. See LICENSE.
