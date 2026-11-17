# Comment for NousResearch/hermes-agent#20950

---

I've been building this as a plugin, using only the public plugin API, in case it
helps anyone who needs client-provided tools today:
[hermes-outrigger](https://github.com/codepilots/hermes-outrigger). Disclosure: I
wrote it.

**How it works:**

- **The route.** `register_platform_handler("api_server", …)` adds a WebSocket
  route, `/v1/outrigger/ws`, to the API server. A client connects and declares
  its tools at `hello`: name, description, JSON Schema, and a policy (`auto` or
  `confirm`).
- **The tool.** One static tool, `outrigger_call(tool, args)`, is registered.
  A `pre_llm_call` hook adds a compact catalog of the attached client's tools
  to the turn's context.
- **A call.** When the model calls a tool, the handler validates the arguments
  against the client's schema, sends a `call` frame over the socket and blocks
  until the client answers (or the timeout, kept under Hermes's 420 s tool
  limit). The result comes back in the same turn, wrapped as untrusted and
  size-capped.
- **Binding.** A client binds to a session with `session_id`. The binding
  follows `conversation_id`, so it survives rotating compression.

The first client is a Chrome side panel that offers page tools: read, snapshot
with element refs, click, fill and so on.

**Where it falls short of what this issue asks.** The model sees one generic
`outrigger_call(tool, args)` rather than each client tool as a native function
with its own schema. A plugin can't add tools to a running session:
`register_tool` is static, and `check_fn` verdicts are cached. Native per-request
tools, as in the OpenAI `tools` field, would still need core support. If that
lands, the transport above could feed it.
