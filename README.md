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

The first client will be the Hermes Sidebar Chromium extension
(github.com/codepilots/hermes-sidebar), exposing page tools such as run_js.

Name: an outrigger is a float rigged alongside a hull that extends a boat's
reach and steadies it. The plugin does the same for Hermes: a light attachment
that lets it act where the hull cannot go.

License: MIT. See LICENSE.
