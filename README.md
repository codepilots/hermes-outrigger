# hermes-outrigger

A Hermes gateway plugin that lets a thin client attach to a Hermes session and
offer tools that run on the client, not on the Hermes host. Hermes calls them as
ordinary tool calls; the plugin relays each call to the client and returns the
client's result as the tool result, inside the same turn.

Status: spec only. No code yet.

- docs/spec.md: design, protocol, security model, open questions
- docs/scenarios.md: where an outrigger fits, where it doesn't (vs MCP, vs a plugin)

The first client will be the Hermes Sidebar Chromium extension
(github.com/codepilots/hermes-sidebar), exposing page tools such as run_js.

Name: an outrigger is a float rigged alongside a hull that extends a boat's
reach and steadies it. The plugin does the same for Hermes: a light attachment
that lets it act where the hull cannot go.

License: MIT. See LICENSE.
