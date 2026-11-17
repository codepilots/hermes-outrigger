# Outrigger platform: a messaging platform for outrigger clients (proposal)

Date: 2026-11-16. Status: proposal; open questions answered against the Hermes
v0.23.1 source (section 5). Extends spec.md (protocol v1) and replaces phase 2
of the sidebar's proposal 13.

## 1. Why

An outrigger client today talks to Hermes over two channels:

- **chat** through the API server's Chat Completions endpoint;
- **tools** through the outrigger WebSocket.

Chat Completions runs the agent turn directly. It skips the messaging gateway's
pipeline, so a client gets none of these:
- slash commands (`/model` sent as a message is answered by the model as a
  prompt, checked 2026-11-16);
- Hermes's interactive prompts: model picker, approvals, clarify questions,
  choice pickers;
- per-user command access control.

Discord, Telegram and Slack get all of that because each is a **platform
adapter**: inbound messages go through authorization, session lookup and the
slash-command dispatcher before the agent, and the adapter renders Hermes's
interactive prompts natively.

The **outrigger platform** is one platform adapter, shipped by this plugin, that
any outrigger client can use. A client that speaks the protocol (the browser
sidebar, a phone app, an editor panel, a kiosk) becomes a first-class Hermes
messaging surface. It needs no client-specific adapter, native helper or core
change.

## 2. What the Hermes plugin API provides (checked against v0.23.1 docs)

- **`ctx.register_platform(name=..., adapter_factory=..., ...)`** is the
  documented, recommended path for third-party adapters (`adding-platform-adapters.md`).
  It automatically handles:
  - config parsing and the `hermes status`, `hermes tools` and `hermes gateway setup` listings;
  - user authorization (`allowed_users_env`, `allow_all_env`);
  - the `platform_hint` added to the system prompt;
  - message chunking (`max_message_length`);
  - `display_tier` (streaming and tool progress);
  - cron delivery and the `send_message` routing.
- **Adapter base class** (`gateway.platforms.base.BasePlatformAdapter`, public):
  `connect`, `disconnect`, `send`, `send_typing`, `get_chat_info`. Inbound
  messages are built with `self.build_source(...)` and `MessageEvent`, then
  `await self.handle_message(event)`. From there the gateway's own pipeline,
  slash commands included, takes over.
- **Interactive hooks.** All are optional and fall back to plain text when not
  overridden (`gateway/platforms/ADDING_A_PLATFORM.md`):

  | Hook | Used for |
  |---|---|
  | `send_model_picker(...)` | `/model` with no argument (Telegram, Discord, Slack implement it) |
  | `send_choice_picker(...)` | `/reasoning`, `/fast` and other finite choices |
  | `send_clarify(...)` | the `clarify` tool; answered with `tools.clarify_gateway.resolve_gateway_clarify` |
  | `_send_exec_approval_prompt(prompt)` | dangerous-command approvals; answered with `tools.approval.resolve_gateway_approval_card` |
  | `send_slash_confirm(...)` | Once/Always/Cancel confirmations (`/reload-mcp`); answered with `tools.slash_confirm.resolve` |
  | `edit_message(...)` | progressive edits, which is how replies stream |
  | `send_image`, `send_document`, … | media |

  The resolver imports above are listed as **public helpers for platform
  plugins** in the adapter guide, so this design stays within documented APIs.

## 3. Design

### 3.1 One connection, two roles

The client keeps its single WebSocket to `/v1/outrigger/ws`. Protocol v2 adds
`chat.*` messages next to the existing tool messages; a client opts in with
`capabilities`. A v1 client (tools only, chat over Chat Completions) keeps
working unchanged.

```
client ──WS /v1/outrigger/ws──► outrigger plugin
          hello{capabilities:[chat,…]}   ├─ tool relay (v1): call/result
          chat.send / chat.action        └─ OutriggerAdapter (platform "outrigger")
          ◄ chat.message/edit/approval…        └─ handle_message ► gateway pipeline
                                                   (auth, session, slash commands, agent)
```

The adapter does not open its own listener. Its `connect()` marks it connected,
and it routes frames for the clients registered on the WebSocket route.
Transport, auth and keepalive are shared with v1.

### 3.2 Protocol v2 additions

`hello` gains:

```json
{ "protocol": 2,
  "capabilities": ["chat", "edits", "model_picker", "choices", "clarify", "approvals", "slash_confirm", "media"],
  "user": { "id": "owner", "name": "Autumn" } }
```

Client → server:

| type | fields | maps to |
|---|---|---|
| `chat.send` | `chat_id`, `text`, `client_msg_id`, `attachments?` | `MessageEvent` (text or media) → `handle_message`. `/…` text goes through the gateway's command dispatch. |
| `chat.action` | `kind` (`model` / `choice` / `clarify` / `approval` / `slash_confirm`), `id`, `value` | the matching resolver or picker callback |
| `chat.stop` | `chat_id` | `/stop` for that chat |
| `chat.history` | `chat_id`, `limit?` | recent messages for a reopened panel (sessions API) |

Server → client:

| type | fields | from |
|---|---|---|
| `chat.message` | `chat_id`, `message_id`, `text` (Markdown), `reply_to?` | `send` |
| `chat.edit` | `chat_id`, `message_id`, `text` | `edit_message` (streaming) |
| `chat.typing` | `chat_id` | `send_typing` |
| `chat.model_picker` | `chat_id`, `id`, `current`, `providers: [{slug, name, models: [...]}]` | `send_model_picker` |
| `chat.choice` | `chat_id`, `id`, `title`, `options`, `current?` | `send_choice_picker` |
| `chat.clarify` | `chat_id`, `id`, `question`, `choices`, `multi` | `send_clarify` |
| `chat.approval` | `chat_id`, `id`, `title`, `detail` (the full command), `choices` | `_send_exec_approval_prompt` |
| `chat.confirm` | `chat_id`, `id`, `title`, `message` | `send_slash_confirm` |
| `chat.media` | `chat_id`, `kind`, `name`, `mime`, `data` (base64, capped) or `url` | `send_image`, `send_document`, … |

Clients render what they declared in `capabilities`. The adapter only overrides
a hook when the connected client supports it; otherwise the base class's text
fallback is used, so a minimal client still gets something readable.

### 3.3 Identity, sessions and authorization

- **Chats.** `chat_id` is chosen by the client and is per conversation (the
  sidebar's "New conversation" makes a new one). The adapter namespaces it as
  `<client name>:<chat_id>`, so two clients cannot collide. The gateway derives
  the session key from the `SessionSource` (platform `outrigger`, chat, user),
  as for any platform. `/new` and `/reset` behave as on Discord.
- **Users.** With the shared API key, every client is the owner: `user.id` is
  fixed by config (`OUTRIGGER_OWNER_ID`, default `owner`), not taken from the
  client. Per-client identities and limited users come with spec milestone M4
  (pairing tokens). The token then carries the user id, so the gateway's command
  access control (`allow_admin_from`, `user_allowed_commands`, as documented for
  Discord) applies per client.
- **Allowlist.** `allowed_users_env="OUTRIGGER_ALLOWED_USERS"`, with
  `allowlist_suppresses_pairing=True`. Never `trusted_inbound`: these are human
  chat clients.
- **Tool binding** (v1 tools on platform chats). No binding step is needed.
  On an outrigger-platform turn, `outrigger_call` and `pre_llm_call` read
  `HERMES_SESSION_PLATFORM` and `HERMES_SESSION_CHAT_ID` through the public
  `gateway.session_context.get_session_env`, and the namespaced chat id names
  the client and conversation directly (answer 2).
- **Actions are scoped to the client's own chats.** `chat.action` and
  `chat.stop` are accepted only for chat ids in the sending client's namespace,
  so one client cannot answer another client's approval or picker.

### 3.4 Registration and packaging

`ctx.register_platform` fields:

| Field | Value |
|---|---|
| `name` | `"outrigger"` |
| `label` | `"Outrigger"` |
| `emoji` | `"🛶"` |
| `max_message_length` | `0`: clients render Markdown and need no chunking |
| `display_tier` | `"high"`: streaming edits and tool progress, feature-detected as the guide shows |
| `platform_hint` | "You are chatting through an Outrigger client (a thin app such as a browser side panel). Replies render as Markdown." The client's kind and label are added per session through the existing context block. |
| `allowed_users_env`, `allow_all_env` | as in 3.3 |
| `check_fn` | true when routes are enabled |

`hermes tools enable --platform outrigger outrigger` enables the outrigger
toolset on outrigger chats, plus whatever else the owner wants there.

### 3.5 What stays the same

- **Tools.** The v1 tool relay, the reviewer, approvals on the client, redaction
  and `reveal` work on platform chats exactly as on API chats.
- **Chat Completions** stays supported for clients that only want chat plus tools.

## 4. Client side (the sidebar as first client)

- **Chat:** send `chat.send` instead of `POST /v1/chat/completions`. Render
  `chat.message` and `chat.edit` with the existing Markdown pipeline, with
  redaction applied outbound and placeholders rehydrated inbound as today.
- **Commands:** `/…` goes straight through. The autocomplete menu lists the
  sidebar's own commands plus Hermes's command catalog (open question 3).
- **Cards:** `chat.model_picker` renders as a two-step provider/model card (like
  Discord's dropdowns). `chat.choice`, `chat.clarify`, `chat.approval` and
  `chat.confirm` render as cards in the same style as the browser-tool approval
  cards, and answers go back as `chat.action`.
- **Model choice:** `/model` becomes Hermes's own, a server-side override that
  persists for the session. The phase-1 header picker can stay as a shortcut that
  sends `/model <provider:model>`.
- **History:** `chat.history` reloads a conversation when the panel reopens
  (sidebar proposal 5).

## 5. Open questions, answered (2026-11-16, Hermes v0.23.1 source)

1. **Can this (eagerly loaded) plugin call `ctx.register_platform`? Yes.**
   - `PluginContext.register_platform` (`hermes_cli/plugins_context.py:585`)
     doesn't check the plugin's kind. It puts a `PlatformEntry` in the same
     `platform_registry` that `kind: platform` plugins use, and
     `gateway/config.py` looks platforms up there after plugin discovery.
   - So the adapter ships inside this plugin; no second plugin is needed.
   - The platform connects when `gateway.platforms.outrigger.enabled: true` is
     set and the entry's `validate_config` (or `is_connected`) returns true.
     `env_enablement_fn` can enable it from an env var instead.
2. **How do tools find the chat? Through the session context, no correlation needed.**
   - The public `gateway.session_context.get_session_env` (listed in
     `plugins/public-api.md`) reads the task-local `HERMES_SESSION_PLATFORM`,
     `HERMES_SESSION_CHAT_ID`, `HERMES_SESSION_USER_ID`, `HERMES_SESSION_KEY`
     and `HERMES_SESSION_MESSAGE_ID`.
   - The gateway binds them per turn (`set_session_vars`), and the tool executor
     copies the context into worker threads (`propagate_context_to_thread`,
     `agent/tool_executor.py:945`).
   - So `outrigger_call` and `pre_llm_call` read the chat id directly, which
     works with several chats per client.
   - The alternatives were worse: `post_gateway_admission` has `session_key`
     and `source` but no `session_id`; `pre_llm_call` has `session_id` but no
     chat id.
   - To confirm live in P1: the values are present in subagent turns, which
     stay blocked through `subagent_start` anyway.
3. **Is there a public command catalog? No.**
   - The registry (`hermes_cli.commands.COMMAND_REGISTRY`) is not in the public
     plugin API. The bundled Discord adapter imports it as an internal.
   - The API server has no command route.
   - Public alternatives:
     - The gateway's `/help` (filterable, `/help <text>`) and `/commands`
       (paged, includes skills) reply with text through any platform. The
       client renders that reply.
     - Autocomplete uses a static list generated at build time from the
       published slash-command reference, plus the client's own commands.
   - Upstream: already requested in NousResearch/hermes-agent#149673 (public
     `gateway_command_catalog()` for platform plugins); our comment is in
     `docs/upstream/command-catalog-comment-149673.md`. Switch to it when it lands.
4. **What are the picker signatures and callbacks? Confirmed from the Discord
   adapter and the call sites (`gateway/slash_commands_model.py`).**
   - **Model picker:** `send_model_picker(chat_id, providers, current_model,
     current_provider, session_key, on_model_selected, metadata=None) ->
     SendResult`.
     - `providers` comes from `list_picker_providers(max_models=50, …)`:
       `[{slug, name, models, total_models, is_current}]`.
     - The selection: `await on_model_selected(chat_id, model_id,
       provider_slug)` returns the confirmation text to send. The gateway does
       the switch and persists it for the session.
     - Discord's 25-option limit is Discord's own; clients page or search.
   - **Choice picker:** `send_choice_picker(chat_id, title, choices,
     session_key, on_choice_selected, metadata=None)`.
     - `choices` are `[{value, label, is_current}]`.
     - The selection: `await on_choice_selected(chat_id, value)` returns text.
   - Caveat: these two hooks are documented in the Hermes repo's
     `gateway/platforms/ADDING_A_PLATFORM.md` ("Interactive UX"), not on the
     docs site's signature table. Feature-detect and keep the text fallback.
5. **Who may answer an approval? It is enforced by Hermes.**
   - Approvals arrive as `ExecApprovalPrompt` (`chat_id`, `session_key`,
     `text`, `command`, `description`, `smart_denied`, `request_id`, and
     `actions` as `(label, choice, style)` with choice `once` / `session` /
     `always` / `deny`).
   - The adapter answers with the public `resolve_gateway_approval_card(
     session_key, choice, request_id, actor_id=…, extra=…)`. It resolves only
     that card's request, checks the answerer through `approval_refusal`
     (requester or configured approvers), and keeps the request deny-only until
     the card has shown the whole command.
   - With the shared key the actor is always the owner. With M4 tokens it is
     the client's user id, and Hermes enforces requester-only.
   - The adapter adds its own rule: actions only for the client's own chat
     ids (3.3).

## 6. Milestones

- **P1, chat:** adapter registration, `chat.send` / `chat.message` / `chat.edit` /
  `chat.typing` / `chat.stop`, owner identity, tool binding on platform chats.
  Slash commands then work as text, with text replies.
- **P2, interactive:** model picker, choice picker, clarify, approvals, slash
  confirm, with client cards in the sidebar.
- **P3, polish:** command catalog autocomplete, `chat.history`, media both ways.
- **P4, identities:** per-client tokens (M4) and per-user command access control.

## 7. Testing

- **Contract tests** with a fake gateway runner: `chat.send` produces a
  `MessageEvent` with the right source; each interactive hook emits its frame;
  each `chat.action` calls the documented resolver.
- **Live checks:**
  - `/model` with no argument shows the picker; choosing a model changes the
    model on the next turn (and in `GET /api/sessions/{id}`).
  - `/reasoning` shows a choice card.
  - A dangerous terminal command shows an approval card that resolves the
    gateway's approval.
  - `/new` starts a new session.
  - Browser tools still bind and run on a platform chat.
- **Compatibility:** a v1 client (tools only) is unaffected, and Chat Completions
  clients keep working.
