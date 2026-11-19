# F-10 — Auth trust boundary: mitigations and residual threat model

Scope: remediation item **F-10** — the three deployment-conditional auth
sub-findings from the sealed security review (`scan-report.md` /
`scan-findings.json`, all validator verdicts `inconclusive` / `NOT_RUN`):

| Candidate | Rule | Sub-finding |
|---|---|---|
| `cand_76d74ef9e8999479aa13639d` | `auth.credential-source-mismatch` | Gateway key outside the plugin's view leaves the WS route keyless |
| `cand_e75c70514cccb2f7b2395ad9` | `authz.proxy-header-locality-failopen` | Header-less same-host relays make remote clients look local |
| `cand_ed211e057b1ca49bbdc38436` | `authz.admin-scope-mismatch` | Non-admin token inherits admin when the same user holds any admin token |

**Acceptance class: mitigate + document residual. Status: PARTIAL.**
None of these is "fully fixed". Two admit tested in-process mitigations that
close the reported exploit under the installed gateway; the third (locality)
has a fundamental limit no in-process code can remove. This doc states exactly
what each mitigation proves, what it does not, and which controls belong to
the operator. No agent-vs-human context signals were invented, and the gateway
core was read as data only — never modified.

Code changes (working tree, branch `security-remediation`):
`outrigger/ws.py` (`_expected_key`, `authorize`), `outrigger/platform.py`
(`toolsets_for_user`, `access_extra`), `outrigger/settings.py`
(`allow_keyless_local`), tests in `tests/test_auth_boundary.py` (25 tests).

---

## 1. Credential-source mismatch (76d74ef9) — mitigated, conditional residual

**Mechanism (review).** `_expected_key()` read only the plugin `auth_token`
setting and raw env `API_SERVER_KEY`. The gateway resolves its key from
`platforms.api_server.extra.key` or a profile-scoped secret
(installed `gateway/platforms/api_server.py`:
`self._api_key = extra.get("key", _get_scoped_secret("API_SERVER_KEY", ""))`;
the pip-installed variant uses `os.getenv` as the fallback). With the key in
one of those sources and both plugin sources empty, `authorize()` entered
keyless mode and admitted any loopback peer without proxy headers as OWNER,
while every other gateway route required the key.

**Mitigation.** `_expected_key(request)` now consults the gateway's *live
resolved key*: the api_server adapter stores itself in the aiohttp app as
`app["api_server_adapter"]` (a hook the gateway source documents for feature
detection), and the plugin route is mounted on that same app. Resolution order:

1. plugin `auth_token` (an explicit operator credential for this route),
2. `request.app["api_server_adapter"]._api_key` — whatever the gateway actually
   resolved at startup (extra key, scoped secret, env),
3. raw env `API_SERVER_KEY` (legacy fallback).

**What this proves (tested).** On any gateway that exposes
`app["api_server_adapter"]` with a non-empty resolved key — both installed
variants do — the keyless-local-owner branch is unreachable: a credential-less
loopback connection gets 401, a wrong key gets 401, and the gateway's own key
authenticates the owner. A key held only in `extra['key']` or a scoped secret
(env empty) no longer creates a keyless route. Verified end-to-end over real
WebSocket handshakes, including a genuine header-less L4 relay in front of the
app (probe §3).

**What this does NOT prove / residual.**
- **Gateways that do not expose the app key.** If `app["api_server_adapter"]`
  is absent (older gateway, manual/unsupported wiring), the plugin falls back
  to the plugin-setting + env view and the mismatch can persist. The plugin
  cannot distinguish "gateway keyless" from "gateway key invisible" in that
  case; it keeps keyless local trust (loudly warned) rather than breaking
  legitimate local dev. *Operator control:* set `auth_token` or export
  `API_SERVER_KEY` in the gateway process, or set `allow_keyless_local: false`.
- **In-process trust.** Reading the adapter's resolved key trusts the gateway
  process itself. An attacker who can already mutate objects inside the
  gateway process is outside this control's threat model (they own the key
  anyway).
- **Startup-time resolution.** The adapter resolves its key at construction;
  the plugin deliberately prefers that live value over a later-changed env.
  Rotating the key requires a gateway restart (gateway behavior, not plugin).
- **Plugin-key precedence.** With `auth_token` set, that value — not the
  gateway key — unlocks this route. This is preserved existing behavior and an
  explicit operator choice; the two credentials may differ.
- The installed gateway refuses to start without a strong key, so a *truly*
  keyless running gateway is unsupported wiring; the plugin's keyless mode
  exists for dev/harness use and is now loud (§3).

## 2. Admin-scope mismatch (ed211e05) — mitigated at the connection scope, API gap residual

**Mechanism (review).** `ws._limited` is per-token, but
`platform.toolsets_for_user` / `access_extra` derived admin from
`tokens.active_users()` — PER-USER across all active tokens. A non-admin token
of a user holding any admin token got the full default toolsets (terminal,
files, MCP) and every slash command.

**Mitigation.** `toolsets_for_user(user_id)` now scopes admin by the user's
**live connections** (`platform.CHATS`, whose clients carry the authenticated
token's principal): if any live connection of that user is non-admin, the
user's chats get `user_toolsets` — even when another of their tokens is admin.
`access_extra()` derives `allow_admin_from` from the same function, so slash
gating and toolsets agree. With no live connection, the token store's per-user
admin flag stands (fallback). A registry scan that races a concurrent mutation
fails closed (restricted) with a loud warning.

**What this proves (tested).** The reported exploit is closed on the normal
chat path: a non-admin token's `chat.send` (the ws handler attaches the client
before routing) resolves restricted toolsets through the adapter's
`toolsets_for_source` hook and excludes the user from `allow_admin_from`, while
the same user's admin token sits idle. Admin-only connections keep full
toolsets/commands; revoking the admin token narrows authority on the next
message; `allow_from` (pairing) semantics are unchanged.

**What this does NOT prove / the API gap.** The gateway's per-turn hook
(`run_turn._resolve_enabled_toolsets_for_source` → `adapter.toolsets_for_source(source)`)
and slash gating (`gateway/slash_access.py`) identify the caller by
`SessionSource.user_id` only — **the token that authenticated the connection is
not expressible in the gateway API**. The adapter wrapper and `receive()` that
could thread it are outside this item's change fence. The live-connection
approximation therefore has named residuals:

- **Mixed connections fail closed, not per-token.** With an admin and a
  non-admin connection of one user live at once, *both* users' chats are
  narrowed (the gateway cannot tell us which connection a turn belongs to).
  This is a deliberate fail-closed choice: an admin device loses full toolsets
  while a non-admin device of the same user is connected. Per-token admin would
  require a gateway API change (out of scope).
- **No live connection → per-user fallback.** Turns not tied to a live
  connection (cron deliveries, outbox continuations after disconnect) evaluate
  admin from the token store per-user. A user with an idle admin token is admin
  for those turns.
- **First-message sync race.** `receive()` calls `sync_access()` *before*
  `attach()`. Paths that pre-attach (`chat.send`, `chat.attach`) are safe; a
  first-ever `//` note command on a fresh chat (`doc.command` → `inbound`
  without prior attach) syncs the admin list while the new client is not yet in
  `CHATS`, so `allow_admin_from` can include the user for that one message
  (probe 4k demonstrates it). Toolsets resolve after attach and stay
  restricted. Closing this requires editing `receive()`/`sync_access()`
  (outside the fence) — flagged as a follow-up.
- **Admin remains a user-level attribute in the token store.** Minting a
  non-admin token for an already-admin user is still silent; the CLI warning
  the review suggests lives in `cli.py`/`tokens.mint` (outside the fence).
  *Operator control:* treat `--admin` as a user-level grant; do not mint
  admin + non-admin tokens for one user unless the mixed-connection narrowing
  is acceptable; revoke idle admin tokens.

## 3. Locality fail-open (e75c7051) — hardened + operator switch; fundamental limit RESIDUAL

**Mechanism (review).** `_is_local = loopback peer AND none of PROXY_HEADERS`.
A header-less same-host relay (default `nginx proxy_pass` without
`proxy_set_header`, `ssh -L`, `socat`, cloudflared TCP mode) connects from
127.0.0.1 and adds none of those headers, so remote traffic is classified
local — bypassing `allow_remote` / `owner_key_remote` and (combined with §1's
mismatch) granting credential-less OWNER.

**The fundamental limit (honest statement).** Over TCP there is no in-process
signal that distinguishes a header-less relay from a genuine local process:
the socket peer is loopback in both cases, and header absence is not evidence
of absence of a relay. Verified local-peer identity would require a Unix
socket / `SO_PEERCRED` transport or an operator-maintained allowlist of trusted
local clients — a transport/architecture change outside this item's scope. No
agent-vs-human "context signal" was invented to paper over this. **The
heuristic remains, and the finding remains open as a deployment residual.**

**Mitigations (tested).**
1. **Fail loud:** every keyless-local-owner admission logs a WARNING naming
   the peer, the heuristic's limit, and the two operator remedies (configure a
   key; `allow_keyless_local=false`). Refusals under the new switch are also
   loud. No key or header material is ever logged.
2. **Operator switch `allow_keyless_local` (default `true`):** `false` refuses
   *every* credential-less owner attach, local or relayed, with 403 + loud
   warning. Paired-token and keyed auth are untouched. Default `true` preserves
   legitimate keyless local dev; shared-host operators should set `false` or
   configure a key. (Declared in `plugin.yaml` `config_schema` at close-out;
   `settings.load` now coerces bool-ish strings, so a quoted `"false"` cannot
   leave the switch on — see the review follow-up below.)
3. **§1's key-source fix removes the keyless branch entirely whenever the
   gateway is keyed** — the relay then faces the same key requirement as every
   other gateway route (probe 3a: relayed credential-less connection → 401).

**What remains open (probe-demonstrated).**
- With no key anywhere and `allow_keyless_local=true`, a header-less relayed
  peer **is admitted as OWNER** (probe 3c, live L4 relay). This is the
  heuristic's limit, now loud and switchable, not a verified-local guarantee.
- `allow_remote` / `owner_key_remote` still ride the heuristic: a header-less
  relay lets a remote client use a *valid paired token* despite
  `allow_remote=false`, or the *API key* despite `owner_key_remote=false`
  (credentials are still required; the locality restriction is what fails).
  Only header-injecting proxies or deliberate switch settings close this.

**Operator-owned deployment controls (the real fix surface).**
- **Configure the key in a source the plugin reads:** env `API_SERVER_KEY`
  (documented quickstart), plugin `auth_token`, or — on the installed gateway —
  `extra['key']` / a profile-scoped secret (now visible via §1). A keyed
  gateway makes credential-less owner attach impossible on this route.
- **Set `allow_keyless_local: false`** on any shared host or any deployment
  fronted by a relay you do not fully control.
- **Use header-injecting proxies:** cloudflared HTTP ingress (CF headers — the
  documented `docs/remote-access.md` setup), Caddy `reverse_proxy` (adds
  `X-Forwarded-*` by default), nginx with explicit
  `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`. Never front
  the port with bare L4 forwarding (`ssh -L`, `socat`, TCP-mode tunnels)
  without a configured key.
- **Set `allow_remote` / `owner_key_remote` deliberately** (both default
  `false`); prefer paired tokens over the API key for remote devices.
- **Gateway CORS is the browser boundary:** the installed gateway's
  `cors_middleware` default-denies unlisted browser Origins on the same app
  (the review rejected the standalone CSWSH finding on this basis). Do not set
  `cors_origins: '*'` on a keyless deployment — with wildcard CORS the keyless
  local admission is reachable from any website in the owner's browser. The
  plugin performs no Origin check itself; that control belongs to the gateway
  core (read-only here).
- **Host trust:** any local user/process can reach the loopback port. Keyless
  local trust extends OWNER to *all* of them, not just the owner's own
  processes. On multi-user hosts, configure a key or disable keyless trust.

## Fail-loud inventory (F-10 additions)

| Event | Level | Message carries |
|---|---|---|
| Keyless-local-owner admission | WARNING | peer, "keyless local trust", heuristic limit, `auth_token`/`API_SERVER_KEY`/`allow_keyless_local=false` remedies |
| Keyless attach refused (`allow_keyless_local=false`) | WARNING | peer, switch name, remedies |
| Chat-registry scan raced (admin scope) | WARNING | user id, "restricting toolsets" (fail closed) |

Existing refusal warnings (remote keyless, remote key, bad token/key) are
unchanged. No key, token secret, or header value is logged.

## Verification evidence

- Baseline before changes: `.venv/bin/python -m pytest tests/ -q` →
  **281 passed, 1 skipped**.
- TDD: `tests/test_auth_boundary.py` first run **12 failed / 13 passed** —
  failures reproduced the exploits (keyless/wrong-key handshakes admitted with
  a config-keyed gateway; no admission warning; non-admin toolsets `None`
  instead of restricted; `allow_admin_from` including the live non-admin user).
- After mitigation: new file **25 passed**; full suite **306 passed, 1 skipped**
  (281 + 25, zero regressions), run twice with identical counts, plus an
  adversarial ordering run (`test_remote`/`test_fuzz` before the new file) to
  rule out state leakage.
- Child probe: `cache/scratch/probe_f10_boundary.py` — **48 assertions pass,
  exit 0**: key-source matrix (extra/env/scoped/plugin/none/keyless-adapter),
  `_expected_key` robustness (no app, no adapter, `None`/non-str key), a genuine
  header-less L4 byte-piping relay, admin/non-admin/mixed/revoked/detached
  token states through the real adapter hook, the first-message sync race,
  registry-race fail-closed, and legitimate-access preservation (keyless local
  dev, paired tokens local+remote, owner key with both remote switches).

## Residual summary

| Sub-finding | State | Why it remains open |
|---|---|---|
| 76d74ef9 credential-source | **Mitigated; conditional residual** | Closed on gateways exposing `app["api_server_adapter"]` (both installed variants). Older/manual wiring without the app key falls back to the plugin+env view; operator must set a readable key or disable keyless trust. |
| ed211e05 admin-scope | **Mitigated at connection scope; API-gap residual** | Gateway identifies callers by `user_id`, never token. Mixed connections fail closed (admin narrowed too); connectionless turns use per-user admin; first `doc.command` sync race; mint-time warning unfenced. True per-token admin needs a gateway API change. |
| e75c7051 locality | **Open (fundamental limit); hardened + switchable** | Header-less relays are indistinguishable from local processes over TCP. Loud warnings + `allow_keyless_local=false` + key-source fix reduce exposure; `allow_remote`/`owner_key_remote` still ride the heuristic. Verified-local transport (Unix socket/`SO_PEERCRED`) or trusted-proxy allowlist is the architectural fix, out of scope. |

**Overall F-10 status: PARTIAL (honest-partial).** The reported exploit chains
are closed under the installed gateway with a configured key; the keyless
local-trust mode and the locality heuristic remain operator-owned trust
surfaces, documented above, with tested controls and loud failure.
