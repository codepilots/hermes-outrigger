# Outrigger audit map

Outrigger is a Hermes gateway plugin (Python 3.14, manifest v2) that lets a thin
client — a browser side panel, a phone, an add-in — attach to the gateway over a
WebSocket and offer tools that run **on the client's own device**, plus a
notebook mode (edit `.md` notes on the host) and an "Outrigger" messaging
platform (client chats go through Hermes's own pipeline). It mounts one route,
`GET /v1/outrigger/ws`, on the `api_server` gateway app
(`outrigger/__init__.py:11`, wired at `outrigger/__init__.py:105-114`); plugin
routes get no gateway auth middleware, so the route authenticates itself
(`outrigger/ws.py:89-137`). The privileged sinks are: the client device executing
a tool call, the host note files, and the Hermes agent turn (which for the
owner/admin carries terminal/file tools).

**Read first:** this map, then `docs/security-review/auth-trust-boundary.md`
(the F-10 residual threat model — what the auth mitigations do and do not prove)
and `docs/security-review/remediation.md` (the F-01..F-11 ledger). The ledger
descriptions are claims from the remediation campaign; the anchors below were
re-verified against the current tree at branch `refactor/human-audit` (from main
at merge `27c07ba`). Everything is behavior-preserving documentation; no code
changed. Where a control only *mitigates* (F-10), this map says mitigates +
residual, never "fixed".

## Trust boundaries

Zones: the WS peer and any collaborator-written note text are **untrusted**; the
gateway plugin is the control surface; the **owner device** is where client tools
actually execute (the plugin relays, it does not sandbox — `docs/spec.md` §3);
the **Hermes platform** runs the agent turn. Untrusted data crosses into
privileged execution at three points: tool args → client execution, note body →
the owner's `//` turn, and chat text → the Hermes pipeline.

```mermaid
flowchart LR
  subgraph untrusted["Untrusted client / page"]
    F["WS frames: tool args, chat text, doc text"]
    N["Collaborator-written note body"]
  end
  subgraph gateway["Gateway plugin (outrigger)"]
    A["authenticate / authorize<br/>outrigger/ws.py:89-137"]
    V["validation<br/>outrigger/protocol.py:120-213<br/>outrigger/ws.py:270-282 (_decode)"]
    R["reviewer (advisory unless review_deny=block)<br/>outrigger/reviewer.py:103-163"]
    D["dispatch relay<br/>outrigger/dispatch.py:77-192"]
    NB["notebook access control<br/>outrigger/notebook.py:268-280 (get)<br/>outrigger/notebook.py:504-521 (doc_for_tool)"]
    P["platform inbound<br/>outrigger/platform.py:394-404"]
    NF["host .md notes (atomic write)<br/>outrigger/notebook.py:181-189"]
  end
  subgraph owner["Owner device"]
    T["client tool execution<br/>(client policy + user approval)"]
  end
  subgraph hermes["Hermes platform"]
    H["agent turn / model"]
  end
  F --> A --> V
  V --> R --> D --> T
  T -->|"result: untrusted, capped"| D --> H
  F -->|"chat.send"| P --> H
  N -->|"// turn: body labeled untrusted"| P
  V -->|"doc.update / doc.command"| NB
  H -->|"outrigger_call"| D
  H -->|"notebook_edit via doc_for_tool"| NB
  NB --> NF
```

Notes on the crossings:
- Tool args reach the client only after `validate_args`
  (`outrigger/dispatch.py:103-106`) and, for non-`auto` policy with the reviewer
  on, an advisory review (`outrigger/dispatch.py:114-131`). The `confirm` /
  `deny_when_unattended` policy is **binding on the client, advisory to Hermes**
  (`docs/spec.md` §7; `outrigger/context.py:13-21`); the plugin does not itself
  pop a confirmation prompt — the client UI does.
- The note body is framed as untrusted data in the `//` turn message
  (`outrigger/notebook.py:432-442`) and `notebook_read` results carry
  `untrusted: True` (`outrigger/notebook.py:614`). This is labeling, not
  isolation: the model still reads collaborator text.
- Client tool results are labeled `untrusted: True` and size-capped before the
  model sees them (`outrigger/dispatch.py:237-245`).

## Entry points → controls → sinks

Every control claim names the config that enables it and whether it is
default or opt-in. "Refuses" = the code path returns an error / closes; it does
not imply the control holds across a whole deployment.

| Entry | Boundary | Control (invariant) | Sink | Code anchor |
|-------|----------|---------------------|------|-------------|
| WS handshake / auth | untrusted → gateway | `authenticate` then `authorize`: paired `otr_` token (constant-time hash compare) or owner via API key; keyless-local owner only when no key resolves anywhere AND `allow_keyless_local` (default **true**, opt-out) AND locality heuristic holds. Remote token needs `allow_remote` (default **false**); remote API key needs `allow_remote` AND `owner_key_remote` (both default **false**). Locality = loopback AND no `PROXY_HEADERS` — a heuristic a header-less relay can defeat (F-10 residual). | session bind / upgrade | `outrigger/ws.py:89-137`, `outrigger/ws.py:28-43`, `outrigger/ws.py:66-72`, `outrigger/tokens.py:108-123` |
| Key source resolution | gateway config → route | `_expected_key` precedence: plugin `auth_token` > gateway adapter's resolved `_api_key` (`app['api_server_adapter']`) > env `API_SERVER_KEY`. A config-keyed gateway does not fall into keyless mode. Residual: gateways without the adapter hook fall back to plugin+env view. | owner admission | `outrigger/ws.py:28-43` |
| Token pairing / revocation | owner CLI → store → gateway | `mint` stores SHA-256 hash only, file mode 0600, shown once; `verify` constant-time (dummy compare on unknown id); `revoke` sets timestamp; live connections close on the ≤20 s keepalive check; store re-read on mtime change (no restart). | scoped client access | `outrigger/tokens.py:68-99`, `outrigger/tokens.py:53-61`, `outrigger/tokens.py:108-129`, `outrigger/ws.py:197-203`, `outrigger/cli.py:23-54` |
| Protocol / tool-decl validation | untrusted → gateway | `ToolDecl.parse`: name regex `_NAME_RE` (`\Z` anchor), policy in {auto,confirm,deny_when_unattended} (default confirm), schema via `check_schema`. `parse_tools` ≤64 tools. Schema: ≤32 KiB, depth ≤24, no `patternProperties`, non-local `$ref`/`$dynamicRef`/`$recursiveRef` refused; local-only registry refuses all retrieval (SSRF backstop); `jsonschema` ImportError fails closed + ERROR. | accepted tool catalog | `outrigger/protocol.py:16`, `outrigger/protocol.py:36-77`, `outrigger/protocol.py:120-156`, `outrigger/protocol.py:102-117`, `outrigger/protocol.py:163-178` |
| Args validation | model → gateway → client | `validate_args` against the declared schema; non-local ref refused even in the no-jsonschema fallback; `pattern` keyword dropped gateway-side (ReDoS); unknown type keyword fails closed; fallback `_minimal_validate` when jsonschema absent. Bad args → `outrigger_bad_args`, audited. | client tool call | `outrigger/protocol.py:195-213`, `outrigger/protocol.py:181-192`, `outrigger/protocol.py:227-268`, `outrigger/dispatch.py:103-106` |
| Name / chat-id regexes | untrusted → gateway | `_NAME_RE` and `_CHAT_ID_RE` use `\Z` (trailing LF rejected, F-11); `TOKEN_RE`/`USER_RE` likewise. | tool routing / chat routing | `outrigger/protocol.py:16`, `outrigger/ws.py:333`, `outrigger/tokens.py:26-27` |
| Reviewer approval | model → gateway → client | For policy ≠ auto AND `review="smart"` (default) with an LLM available: args serialized first; over `ARGS_BUDGET`=8000 or unserializable → forced `escalate` before any model call (fail closed). Verdicts approve/deny/escalate are **advisory** and travel with the call; a `deny` is final gateway-side **only** when `review_deny="block"` (opt-in; default `"ask"`). Reviewer failure / no verdict → escalate. | call relay or refusal | `outrigger/reviewer.py:99-163`, `outrigger/reviewer.py:32-39`, `outrigger/dispatch.py:114-131`, `outrigger/settings.py:56-59` |
| Dispatch relay + error cap + audit | gateway → client → model | Per-client semaphore (concurrency 1..8), per-turn call cap `max_calls_per_turn` (default 20), timeout `min(decl, max_timeout_s=400)`. Ok results labeled untrusted + capped `min(decl.max_result_chars, max_result_chars=20000)`; error field coerced/sanitized/capped at ingress and again in `_run` (F-07). Every outcome incl. pre-client rejections audited with `client.principal`. | model tool result | `outrigger/dispatch.py:77-192`, `outrigger/dispatch.py:237-245`, `outrigger/ws.py:491-506`, `outrigger/audit.py:27-65` |
| Notebook open / get / update / command | untrusted → host files | `allowed_roots` (owner: all; token user: `notebook_roots_for` grant, default `{}` = none). `resolve` blocks `..`/absolute/hidden/out-of-root symlink, `.md` only. `get()` re-checks the resolved location against granted roots' directories **every operation** (F-05 narrowing revokes client writes). Doc keyed by resolved file identity (one mutex/lock/version). Caps `MAX_OPEN_DOCS`=64 / `MAX_DOCS_PER_CLIENT`=16; LRU evicts only clientless+unlocked, else refuses. Lock + version conflict checks; `doc.list` walk bounded (5000) off-loop; rate limit on list/open/command for non-admin. | host `.md` write | `outrigger/notebook.py:59-69`, `outrigger/notebook.py:86-101`, `outrigger/notebook.py:268-280`, `outrigger/notebook.py:201-243`, `outrigger/notebook.py:192-198`, `outrigger/notebook.py:296-307`, `outrigger/ws.py:376-462` |
| Notebook tools (`notebook_read`/`notebook_edit`) | model → host files | `doc_for_tool`: only the note whose `//` command is running in this chat; subagents denied (the mark is the deny signal). Authorization decided at command start (via `get()`); **decided behavior** (owner, 19 Nov 2026): mid-turn grant narrowing does NOT abort the in-flight command — its tool calls continue until `end_command`; client writes are revoked immediately. `tool_edit` refreshes from disk before the version compare (no clobber); unreadable → JSON refusal. | host `.md` write | `outrigger/notebook.py:504-521`, `outrigger/notebook.py:604-657`, `outrigger/notebook.py:403-453` |
| Subagent gate | delegated child → clients/notes | `subagent_start` marks the child session; the mark IS the `allow_subagents` (default **false**) deny signal — `mark_subagent` never evicts and never refuses (overflow retained, loud WARNING at `MAX_TRACKED_SUBAGENTS`=1024, a warning threshold not a bound). Marked sessions get no clients (`for_session`, dispatch) and no notebook tools. `allow_subagents=true` permits client calls but notebook tools still deny subagents. | client call / note edit denial | `outrigger/registry.py:132-160`, `outrigger/registry.py:188-211`, `outrigger/__init__.py:71-78`, `outrigger/dispatch.py:34-35`, `outrigger/dispatch.py:55-57`, `outrigger/notebook.py:516-517` |
| Settings bool coercion | config → trust switches | `settings.load` coerces bool-typed settings via `_as_bool` (true/false-ish strings, 0/1); unrecognized → loud WARNING + declared default (fail closed). Fixes quoted `"false"` leaving `allow_keyless_local`/`allow_remote`/`owner_key_remote` on. | trust switches | `outrigger/settings.py:15-28`, `outrigger/settings.py:77-84`, `outrigger/settings.py:35-41` |
| Admin scope (live-connection narrowing) | token user → Hermes toolsets/slash | `toolsets_for_user`: owner → full; a live non-admin connection of the user narrows their chats to `user_toolsets` even if another token is admin; registry race → restricted (fail closed); no live connection → per-user admin fallback. `access_extra` derives `allow_admin_from` from the same function. Residual (API gap): gateway identifies callers by user_id only — mixed connections narrow both, connectionless turns use per-user admin, first `doc.command` sync race. | Hermes toolset / slash gating | `outrigger/platform.py:115-141`, `outrigger/platform.py:101-112`, `outrigger/platform.py:381-391`, `outrigger/platform.py:394-404` |
| Chat namespace / prompt isolation | token user → other chats | Token chats prefixed by token id (`_prefix`); `home` is owner-only; `chat.action`/`chat.stop` require `owns`; prompt answers require the pending prompt's chat to match. | cross-chat access denial | `outrigger/platform.py:73-84`, `outrigger/platform.py:154-155`, `outrigger/platform.py:295-300`, `outrigger/ws.py:349-362` |
| Session bind (owner-only) | token user → API session | `bind` refused for non-owner; token clients' `session_id` forced None at register. | API session binding | `outrigger/ws.py:479-486`, `outrigger/ws.py:310-311` |

## Key workflows

Each node/edge below maps to a real code path (anchors in the tables above and
inline). Async/concurrent flows (watcher, keepalive) are kept minimal.

### (a) Tool-call round trip

```mermaid
sequenceDiagram
  participant C as Client (owner device)
  participant W as ws.py
  participant R as registry
  participant D as dispatch.py
  participant V as reviewer.py
  participant A as audit.py
  participant H as Hermes model
  C->>W: HTTP upgrade (Bearer key / otr_ token)
  W->>W: authenticate / authorize (ws.py:89-137)
  W-->>C: 401/403 refusal OR upgrade
  C->>W: hello (within 10s)
  W->>R: _register / add (ws.py:289-322)
  W-->>C: welcome (limits, platform)
  H->>D: outrigger_call{tool,args} (worker thread)
  D->>R: pick client and count_call (dispatch.py:95-111)
  D->>D: validate_args (dispatch.py:103-106)
  D->>V: review if policy not auto and review=smart (dispatch.py:114-120)
  V-->>D: verdict (advisory, deny with block rejects)
  D->>C: call{args, review} via client.send (dispatch.py:155-162)
  C->>C: client policy prompt then execute
  C->>W: result (ws.py:491-506)
  W->>R: resolve future
  R-->>D: result
  D->>D: _format_ok / error cap, untrusted (dispatch.py:176-192)
  D->>A: record(principal, outcome) (dispatch.py:137-139)
  D-->>H: tool result JSON
```

### (b) Token pairing → scoped access → revoke

```mermaid
sequenceDiagram
  participant O as Owner (CLI)
  participant T as tokens.py store
  participant C as Client
  participant W as ws.py
  participant P as platform.py
  O->>T: mint (hermes outrigger pair) (tokens.py:68-88)
  T-->>O: token shown once, SHA-256 hash stored 0600
  O->>C: paste token out-of-band
  C->>W: upgrade Bearer otr_...
  W->>T: verify, constant-time (tokens.py:108-123)
  W-->>C: welcome (own chats, user_toolsets)
  C->>P: chat.send (receive calls sync_access) (platform.py:394-404)
  O->>T: revoke (tokens.py:91-99)
  W->>T: keepalive is_active every 20s (ws.py:197-203)
  W-->>C: close 4401 within ~20s
```

Revocation latency is ~20 s by design (the keepalive interval,
`outrigger/ws.py:21` `PING_INTERVAL=20`); new handshakes fail immediately via
`verify`, and the next `receive` re-syncs the access lists
(`outrigger/platform.py:395`). This is a documented design choice, not a bug
(ledger: rejected revocation-latency findings).

### (c) Approval decision tree

```mermaid
flowchart TD
  S["outrigger_call: decl.policy"] --> Auto{"policy == auto?"}
  Auto -->|yes| Dispatch["dispatch call (no review)<br/>client runs per its policy"]
  Auto -->|no| Enabled{"reviewer.enabled?<br/>review=smart (default) & LLM present"}
  Enabled -->|no| Dispatch
  Enabled -->|yes| Budget{"args serializable AND<br/>len <= ARGS_BUDGET (8000)?"}
  Budget -->|no| Esc["forced escalate (fail closed,<br/>before any model call)"]
  Budget -->|yes| Model["model verdict:<br/>approve / deny / escalate<br/>(failure or unclear => escalate)"]
  Esc --> Adv
  Model --> Deny{"verdict == deny AND<br/>review_deny == block (opt-in)?"}
  Deny -->|yes| Block["reject gateway-side, audited<br/>(dispatch.py:124-131)"]
  Deny -->|no| Adv["dispatch call with review advisory<br/>(default review_deny=ask)"]
  Adv --> Client["client decides: confirm prompt /<br/>deny_when_unattended focus check"]
```

The only gateway-side final refusal here is `deny` + `review_deny="block"`
(opt-in). With the default `review_deny="ask"`, every verdict — including deny —
is advisory metadata the client must surface to the user
(`outrigger/dispatch.py:121-131`, `outrigger/dispatch.py:160-161`).

### (d) Locality / auth decision

```mermaid
flowchart TD
  Req["request"] --> Tok{"presented key starts otr_?"}
  Tok -->|yes| Ver{"tokens.verify ok?"}
  Ver -->|no| R401["401 bad/revoked token"]
  Ver -->|yes| RemT{"local OR allow_remote (default false)?"}
  RemT -->|no| R403t["403 remote token refused"]
  RemT -->|yes| Token["admit token principal"]
  Tok -->|no| Key{"_expected_key non-empty?<br/>auth_token > adapter key > env"}
  Key -->|no| Loc1{"local? (loopback AND no PROXY_HEADERS)"}
  Loc1 -->|no| R403r["403 remote keyless refused"]
  Loc1 -->|yes| AKL{"allow_keyless_local (default true)?"}
  AKL -->|no| R403k["403 keyless refused (loud)"]
  AKL -->|yes| Owner1["admit OWNER + WARNING<br/>(heuristic; header-less relay can defeat)"]
  Key -->|yes| Loc2{"local OR (allow_remote AND owner_key_remote)?"}
  Loc2 -->|no| R403key["403 remote API key refused"]
  Loc2 -->|yes| Cmp{"hmac.compare_digest(presented, key)?"}
  Cmp -->|no| R401k["401 bad/missing key"]
  Cmp -->|yes| Owner2["admit OWNER"]
```

`local` is the heuristic `_is_local` (`outrigger/ws.py:70-72`): loopback peer AND
none of `PROXY_HEADERS` (`outrigger/ws.py:66-67`). It is **not** verified local
peer identity — a header-less same-host relay (`ssh -L`, `socat`, bare
`proxy_pass`) looks local. See the residual section below.

### (e) Notebook grant recheck + decided in-flight window

```mermaid
flowchart TD
  Open["doc.open"] --> Allow{"root in allowed_roots(client)?<br/>(notebook_roots_for grant)"}
  Allow -->|no| NoRoot["no_root / not_allowed"]
  Allow -->|yes| Resolve["resolve path + canonical identity<br/>caps / eviction (notebook.py:201-243)"]
  Resolve --> Held["Doc held by client"]
  Held --> Op["client op: update / undo / command"]
  Op --> Get{"get(): client holds doc AND<br/>resolved location still under a granted root?"}
  Get -->|no| NotAllowed["not_allowed (write revoked)"]
  Get -->|yes| Proceed["lock / version checks -> write"]
  Op -->|doc.command| Start["start_command locks note<br/>-> platform.inbound // turn"]
  Start --> Mid{"grants narrowed mid-turn?"}
  Mid -->|yes| Window["client writes via get() refused;<br/>in-flight notebook_read/edit (doc_for_tool,<br/>authorized at start) continue to end_command<br/>(DECIDED behavior, owner 19 Nov 2026)"]
  Mid -->|no| Run["turn runs"]
  Window --> End["end_command (turn end / lock timeout)<br/>-> unlock -> doc_for_tool no_command"]
  Run --> End
```

The in-flight window is **decided behavior, not an open question**: a mid-turn
narrowing does not abort the running `//` command (authorized at start via
`get()`); its `notebook_read`/`notebook_edit` calls keep working until
`end_command`. Client-initiated writes are revoked immediately. Pinned by
`tests/test_notebook_grants.py::test_inflight_command_completes_after_midturn_narrowing`
(ledger F-05 close-out). Residual: a narrowed user still receives `doc.changed`
broadcasts on a held-open doc until close/disconnect (`broadcast` does not
re-check grants, `outrigger/notebook.py:361-382`).

## Security-sensitive units

Module-per-concern as it stands **today** (input to the upcoming cohesion
restructure). Function names + line ranges are current. Concerns are interleaved
in the large modules — noted explicitly so the restructure knows what to separate.

| Concern | Where it lives (function : lines) |
|---------|-----------------------------------|
| Authentication (authn) | `outrigger/ws.py`: `_expected_key` 28-43, `_presented_key` 46-54, `_is_loopback` 57-61, `PROXY_HEADERS` 66-67, `_is_local` 70-72, `authenticate` 89-104, `authorize` 107-137. `outrigger/tokens.py`: `verify` 108-123, `is_active` 126-129. |
| Authorization / grants (authz) | `outrigger/notebook.py`: `allowed_roots` 59-69, `resolve` 86-101, `open_doc` 201-243, `get` 268-280, `doc_for_tool` 504-521. `outrigger/platform.py`: `toolsets_for_user` 115-141, `access_extra` 101-112, `_prefix` 73-77, `chat_key` 80-84, `owns` 154-155. `outrigger/registry.py`: `for_session` 188-211, `add` caps 83-104. `outrigger/ws.py`: `_limited` 140-143, owner-only `bind` 479-486. |
| Input validation | `outrigger/protocol.py`: `ToolDecl.parse` 36-63, `parse_tools` 66-77, `check_schema` 120-156, `_remote_ref` 102-117, `_local_only_registry` 163-178, `_validator_cls` 181-192, `validate_args` 195-213, `_type_ok` 227-237, `_minimal_validate` 240-268, `int_field`/`str_field`/`one_line`/`sanitize_text` 274-304. `outrigger/ws.py`: `_decode` 270-282, `_CHAT_ID_RE` 333. `outrigger/notebook.py`: `resolve` 86-101, `apply_edits` 618-634. |
| Audit | `outrigger/audit.py`: `record` 27-65, `_write_failures` 19. `outrigger/dispatch.py`: `reject` 85-89 + audit sites 125-128, 137-139. (Notebook edits and platform chat are **outside** audit.jsonl scope — no audit calls in `notebook.py`/`platform.py`.) |
| Approval / review | `outrigger/reviewer.py`: `configure` 80-96, `enabled` 99-100, `review` 103-163, budgets/defang 32-39. `outrigger/dispatch.py`: review gate + block 114-131. |
| Credentials | `outrigger/tokens.py`: `path` 30-32, `_load` 35-50, `_save` 53-61, `mint` 68-88, `revoke` 91-99, `active_users` 132-136. `outrigger/cli.py`: `setup` 9-20, `handle` 23-54. |
| Subagent gate | `outrigger/registry.py`: `mark_subagent` 132-152, `unmark_subagent` 154-156, `is_subagent` 158-160, `for_session` subagent branch 199-201, `rotate` guard 181-182. `outrigger/__init__.py`: hooks 71-78. `outrigger/dispatch.py`: 34-35, 55-57. `outrigger/notebook.py`: 516-517. |
| Trust settings | `outrigger/settings.py`: `_as_bool` 15-28, `DEFAULTS` 30-72, `load` 77-84, `get` 87-88. `outrigger/plugin.yaml`: `config_schema` 10-38. |
| Relay / capture / result bounding | `outrigger/dispatch.py`: `_run` 143-192, `_format_ok` 237-245, `_save_capture` 200-234. `outrigger/ws.py`: result ingress 491-506. |
| Collaboration machinery (notebook) | `outrigger/notebook.py`: `broadcast` 361-382, watcher `_watch`/`_watch_done`/`ensure_watcher` 526-571, snapshots/undo 317-341, locks/commands 403-491, tool handlers `tool_read`/`tool_edit` 604-657. |

**Interleaving (what the cohesion restructure must separate):**
- `outrigger/ws.py` (530 lines) mixes **authn** (28-137) with **message routing**
  (`_handle` 473-530, `_handle_chat` 336-373, `_handle_doc` 376-462), **rate
  limiting** (`_rate_check` 149-164) and **ingress result bounding** (491-506).
- `outrigger/notebook.py` (657 lines) mixes **access control**
  (`allowed_roots`/`resolve`/`get`/`doc_for_tool`) with **collaboration machinery**
  (broadcast, watcher, snapshots, locks) and the **Hermes tool handlers**
  (`tool_read`/`tool_edit`).
- `outrigger/dispatch.py` (268 lines) mixes **relay** (`_run`) with **audit
  recording** (`reject` + record sites), **capture** (`_save_capture`) and
  **client selection / subagent gate** (`_pick_client`, `platform_chat_clients`).
- `outrigger/platform.py` (481 lines) mixes **admin-scope authz**
  (`toolsets_for_user`/`access_extra`) with the **messaging adapter** (receive,
  send, prompt cards, media).
- `outrigger/registry.py` (277 lines) mixes **membership/caps** with the
  **subagent gate** and **per-turn bookkeeping**.

## Invariants & failure contracts

Fail-loud / fail-closed inventory: what is logged, what is refused, what
continues. Residual limits are stated honestly (from
`docs/security-review/auth-trust-boundary.md` + the ledger).

| Failure point | Logged | Refused | Continues | Anchor |
|---------------|--------|---------|-----------|--------|
| Audit write loss (OSError) | ERROR with path, reason, tool/session/outcome; `_write_failures` counter incremented under lock | nothing — the entry is lost | the tool call (auditing never breaks a call, but loss is observable) | `outrigger/audit.py:56-65`, `outrigger/audit.py:19` |
| Watcher task death | ERROR + restart (`_watch_done`); cancelled → WARNING | — | watcher restarted without waiting for a doc.open; per-tick/per-doc/per-client guards keep it alive through bad writes | `outrigger/notebook.py:550-571`, `outrigger/notebook.py:526-547` |
| Schema validation unavailable (jsonschema ImportError) | ERROR at `check_schema` | tool schema declarations refused (fail closed); `validate_args` falls back to `_minimal_validate` (still refuses non-local refs) | — | `outrigger/protocol.py:148-151`, `outrigger/protocol.py:195-205` |
| Reviewer over-budget / unserializable args | WARNING "ESCALATE (fail closed)" | a silent approve on a truncated prefix is avoided | forced `escalate` before any model call; the user is asked | `outrigger/reviewer.py:111-121` |
| Reviewer failure / unclear verdict | WARNING on exception | — | `escalate` (advisory) | `outrigger/reviewer.py:158-162` |
| Subagent mark cap reached | WARNING per overflow mark (not throttled) | nothing — marks never evicted, never refused | every marked session stays denied while `allow_subagents=false`; missed `subagent_stop` leaks memory, not authority | `outrigger/registry.py:132-152` |
| Settings bool unrecognized | WARNING naming key/value/default | the bad value is not used | declared default applied (fail closed for refusal switches) | `outrigger/settings.py:15-28` |
| Keyless-local-owner admission | WARNING naming peer, heuristic limit, remedies | — | owner admitted on the locality heuristic (default `allow_keyless_local=true`) | `outrigger/ws.py:120-126` |
| Keyless attach with `allow_keyless_local=false` | WARNING | 403 every credential-less owner attach | paired-token / keyed auth untouched | `outrigger/ws.py:116-119` |
| Chat-registry scan raced (admin scope) | WARNING "restricting toolsets" | full toolsets withheld | restricted toolsets (fail closed) | `outrigger/platform.py:129-135` |
| Notebook lock expiry fails on a doc | WARNING | that doc stays locked (fail closed) | other docs still expire | `outrigger/notebook.py:482-491` |
| `tool_edit` disk refresh fails | JSON refusal `unreadable` | the write (no clobber) | the note is preserved | `outrigger/notebook.py:641-644` |
| Bad `notebook_lock_timeout_s` config | WARNING | — | default 600 s used | `outrigger/notebook.py:471-479` |

### Residual limits (honest — mitigated, not "fixed")

- **Locality heuristic (F-10 / e75c7051, OPEN fundamental limit).** Over TCP
  there is no in-process signal distinguishing a header-less relay from a genuine
  local process; `_is_local` (`outrigger/ws.py:70-72`) is loopback + header
  absence, not verified peer identity. With no key anywhere and
  `allow_keyless_local=true`, a header-less relayed peer is admitted as OWNER
  (now loud). `allow_remote`/`owner_key_remote` also ride the heuristic. The
  architectural fix (Unix socket / `SO_PEERCRED`, or a trusted-proxy allowlist)
  is out of scope. Operator controls: configure a key, set
  `allow_keyless_local=false`, use header-injecting proxies.
  (`docs/security-review/auth-trust-boundary.md` §3.)
- **Key-source fallback (F-10 / 76d74ef9, conditional residual).** The
  adapter-key fix closes the keyless route on gateways exposing
  `app['api_server_adapter']` (both installed variants). Gateways without that
  hook fall back to the plugin-setting + env view; the plugin has no signal to
  distinguish "gateway keyless" from "gateway key invisible" and keeps loud keyless local
  trust. (`outrigger/ws.py:28-43`; auth-trust-boundary.md §1.)
- **Per-token admin API gap (F-10 / ed211e05, residual).** The gateway identifies
  callers by `user_id`, never the token. Live-connection narrowing therefore:
  mixed admin+non-admin connections narrow **both** (fail closed); connectionless
  turns (cron/outbox) use the per-user admin flag; the first-ever `doc.command`
  on a fresh chat syncs the admin list before `attach`, so `allow_admin_from` can
  include the user for that one message (`outrigger/platform.py:394-396`);
  minting a non-admin token for an already-admin user is silent. True per-token
  admin needs a gateway API change. (auth-trust-boundary.md §2.)
- **F-05 decided in-flight window.** Mid-turn grant narrowing does not abort the
  running `//` command; its notebook tool calls continue to `end_command`
  (authorized at start). Client writes are revoked immediately. Decided behavior
  (owner, 19 Nov 2026), pinned by a preservation test. Also: hardlink aliases are
  not coalesced (distinct realpaths, same inode); a narrowed user still receives
  `doc.changed` broadcasts on a held-open doc until close.
  (`outrigger/notebook.py:504-521`, `outrigger/notebook.py:361-382`; ledger F-05.)
- **Audit rotation / hash-chain not implemented.** `audit.jsonl` grows unbounded;
  there is no size cap and no hash-chain/signature, so offline edits are
  undetectable. 0600 is creation-only (pre-existing artifacts keep their mode).
  Notebook edits and platform chat are outside the audit trail.
  (`outrigger/audit.py:27-65`; ledger F-09 residuals.)
- **~20 s revocation latency.** Live token connections close on the keepalive
  check (`PING_INTERVAL=20`, `outrigger/ws.py:21`, `outrigger/ws.py:197-203`);
  this is a documented design choice. New handshakes and the next `receive`
  re-sync immediately.
- **Notebook cached reads / deleted-note semantics (F-04 residuals).**
  `tool_read` serves cached text (clobbering is avoided by `tool_edit`'s disk
  refresh, not by reads); `_check_disk` returns False on a stat OSError, so a
  deleted note is recreated from cache on edit.
  (`outrigger/notebook.py:604-615`, `outrigger/notebook.py:283-293`.)
- **Browser boundary is the gateway's.** The plugin performs no Origin check;
  the installed gateway's CORS middleware default-denies unlisted browser
  Origins. Do not set `cors_origins: '*'` on a keyless deployment.
  (auth-trust-boundary.md §3; ledger rejected CSWSH finding.)