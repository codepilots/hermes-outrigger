# Remote access (Cloudflare Tunnel)

How to reach the outrigger plugin from outside your machine (a laptop elsewhere,
a phone, a friend) without exposing the rest of the Hermes gateway.

Date: 2026-11-17. Applies to hermes-outrigger with `allow_remote` /
`owner_key_remote` / `user_toolsets` (this document's settings).

## The short version

1. Create a Cloudflare Tunnel that exposes **only** `/v1/outrigger/ws`
   (steps 1-3).
2. Turn on `allow_remote` in the plugin settings (step 4).
3. Pair a token for every remote device, with `--admin` for your own devices
   (step 5). Never put `API_SERVER_KEY` on a remote device.
4. Check from outside, then add a rate-limit rule (steps 7-8).

## Why this setup

The gateway's API server (port 8642) serves the whole OpenAI-compatible API:
`/v1/chat/completions`, `/v1/responses`, runs, and so on. Each route takes the
`API_SERVER_KEY`, and that key is a full Hermes agent with terminal, file and
code tools. Exposing the port exposes all of it. The plugin's WebSocket is
the only route a client needs, so it is the only one to expose.

`cloudflared` runs on the same machine and connects to the gateway from
127.0.0.1. The plugin therefore treats a request as local only when it comes
from loopback **and** carries no reverse-proxy headers (`CF-Connecting-IP`,
`CF-Ray`, `X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `Via`, …). Everything
through the tunnel counts as remote:

| Who connects | From this machine | Remote (tunnel or proxy) |
|---|---|---|
| No key configured at all | allowed if `allow_keyless_local` (default true) | refused, always |
| `API_SERVER_KEY` | allowed | refused unless `allow_remote` **and** `owner_key_remote` |
| Paired token (`otr_…`) | allowed | allowed if `allow_remote` |
| Revoked or unknown token | refused (401) | refused (401) |

> **Locality is a heuristic, not a proof.** "Local" means loopback **and** no
> proxy headers. Cloudflare always adds headers, so everything through the tunnel
> counts as remote — but a *header-less* same-host relay (`ssh -L`, `socat`, a
> bare `proxy_pass`) looks local. With no key anywhere and `allow_keyless_local`
> true, such a relay can attach as owner (now logged loudly). On a shared host
> set `allow_keyless_local: false` or configure a key. The plugin also reads the
> gateway's resolved key (config/secret), so a config-keyed gateway never falls
> into keyless mode. Full residual model and deployment controls:
> [security-review/auth-trust-boundary.md](security-review/auth-trust-boundary.md).

## Before you start

- **Prerequisite:** a domain whose DNS is on Cloudflare (free plan is fine).
  Below, `hermes.example.com` stands for the hostname you pick.
- **Cloudflare can read your traffic.** It terminates TLS at its edge, so it
  can technically see what passes through: your chats, and page and note
  contents. If that's unacceptable, use a private network such as Tailscale
  or WireGuard instead. That way there's no third party and nothing is
  exposed publicly.
- **Keep the gateway on loopback.** Leave it listening on 127.0.0.1 (the
  default) and don't set `API_SERVER_HOST`. The tunnel is the only way in.

## 1. Install cloudflared and create the tunnel

On the Hermes machine:

```sh
sudo pacman -S cloudflared                      # Arch (extra); other distros: Cloudflare's packages
cloudflared tunnel login                        # opens a browser; pick the domain. Writes ~/.cloudflared/cert.pem
cloudflared tunnel create hermes                # prints the tunnel ID; writes ~/.cloudflared/<ID>.json
cloudflared tunnel route dns hermes hermes.example.com   # creates the DNS record for the tunnel
```

Both `cert.pem` and `<ID>.json` are credentials: keep them private, and out of
backups that leave the machine.

## 2. Tunnel config: only the plugin's route

`~/.cloudflared/config.yml`:

```yaml
tunnel: <ID>
credentials-file: /home/<you>/.cloudflared/<ID>.json
ingress:
  - hostname: hermes.example.com
    path: ^/v1/outrigger/ws$
    service: http://127.0.0.1:8642
  - service: http_status:404
```

The first rule sends only the WebSocket path to Hermes. The catch-all answers
404 for everything else, including `/health`, `/v1/models` and
`/v1/chat/completions`.

Check the rules before going live:

```sh
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://hermes.example.com/v1/outrigger/ws   # → rule 0 (Hermes)
cloudflared tunnel ingress rule https://hermes.example.com/v1/models         # → rule 1 (404)
```

## 3. Run the tunnel as a service

A user service, next to `hermes-gateway`.
`~/.config/systemd/user/cloudflared-hermes.service`:

```ini
[Unit]
Description=Cloudflare Tunnel for the Hermes outrigger plugin
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/cloudflared tunnel --config %h/.cloudflared/config.yml run hermes
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now cloudflared-hermes
loginctl enable-linger "$USER"     # keep user services running while logged out
journalctl --user -u cloudflared-hermes -f   # should show "Registered tunnel connection" (x4)
```

## 4. Plugin settings

`~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    outrigger:
      settings:
        allow_remote: true
        # owner_key_remote: false        # default: the API key never works remotely
        # user_toolsets: [web, outrigger, no_mcp]   # default, for paired non-admin users
        # max_clients_per_token: 4
        # user_messages_per_min: 20
        # max_chat_chars: 100000
```

Then `systemctl --user restart hermes-gateway`.

## 5. Tokens for remote devices

One token per device, so each can be revoked on its own:

```sh
hermes outrigger pair --user me-laptop --label "work laptop" --admin   # you, on another device
hermes outrigger pair --user ann --name Ann --label "Ann's laptop"     # someone else
hermes outrigger tokens                # list (never shows secrets)
hermes outrigger revoke <token-id>     # takes effect within ~20 s
```

The token is shown once. Copy it straight into the device. Don't keep it in
chat history or notes.

What a token gets:

- **Admin token (`--admin`)**: you, on another device. All slash commands
  (including `/model`) and Hermes's normal toolset for the Outrigger platform
  (terminal, files, code, browser tools, MCP servers).
- **Paired user**:
  - Hermes runs their chats with `user_toolsets` only. The default is `web`,
    `outrigger` (their own browser tools and notes) and `no_mcp`, so no
    terminal, files, code, memory, delegation, cron or MCP.
  - They can run only the slash commands in `user_allowed_commands`, and can
    open only the note folders mapped to them in `notebook_roots_for`.
  - Their chats are separate from yours and from other users' chats.
  - They are limited to `max_clients_per_token` connections and
    `user_messages_per_min` messages.
  - Hermes's own `/resume` and `/sessions` show only their own chats.
  - They see your Hermes user profile (see Known limits).
- The user id `owner` (`platform_owner_id`) is reserved; pairing refuses it.

Your own devices on the Hermes machine can keep using the API key at
`http://127.0.0.1:8642`.

## 6. Set up the remote device

1. Install the Hermes Sidebar extension.
2. Open its settings → Connection.
3. Set the gateway address to `https://hermes.example.com` (no path) and
   paste the token.
4. Click **Test connection**: it should say "Connected with token ✓".

The sidebar switches to `wss://` itself.

## 7. Check from outside

From another network (a phone hotspot, say):

```sh
curl -i https://hermes.example.com/health              # 404: the rest of the gateway isn't reachable
curl -i https://hermes.example.com/v1/models           # 404
curl -i https://hermes.example.com/v1/outrigger/ws     # 401 or 403 without a token: the plugin answered
```

On the Hermes machine, the gateway log shows each remote connection with the
address Cloudflare reports:

```sh
journalctl --user -u hermes-gateway | grep outrigger
```

## 8. Cloudflare dashboard (recommended)

- **Rate limiting.** Security → WAF → Rate limiting rules → create a rule:
  - When: URI Path equals `/v1/outrigger/ws`.
  - Rate: 30 requests per 1 minute, counted per IP.
  - Action: Block for 10 minutes.

  A normal sidebar opens one connection and keeps it, so this only affects
  scanners and runaway reconnect loops. The plugin itself doesn't limit failed
  logins, but guessing a token isn't feasible (about 190 bits).
- **Cloudflare Access (optional, untested with the extension).** An Access
  application for `hermes.example.com` adds a login before anything reaches
  the gateway.
  - Browsers can't add custom headers to a WebSocket, so Access service
    tokens won't work for the sidebar.
  - A browser login (the `CF_Authorization` cookie) or an mTLS client
    certificate can work, but whether the extension's WebSocket sends the
    cookie hasn't been tested.
  - Try it with one device first. If the connection is refused, use mTLS or
    rely on tokens alone.
- **SSL/TLS:** the tunnel is encrypted end to end to the edge, so no
  certificate setup is needed. Leave "Always Use HTTPS" on.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Test connection: "Refused or unreachable" | `allow_remote` not set or gateway not restarted. The gateway log says `refused remote client … (allow_remote is off)` |
| Same, with the API key | By design. Use a paired token (`owner_key_remote` is off) |
| `curl …/v1/outrigger/ws` times out or returns 502 | Tunnel down (`systemctl --user status cloudflared-hermes`) or the gateway isn't running |
| Works, then drops after a gateway restart | The sidebar reconnects by itself within a few seconds; routes mount only at gateway start |
| "Token accepted, but the Outrigger platform is off" | The plugin's `platform` setting is off; remote chat needs it |
| `too_many_clients` | That token already has `max_clients_per_token` connections; close old tabs or windows |

## Turning remote access off

- **One device:** `hermes outrigger revoke <token-id>`. Its connections close
  within about 20 s.
- **Everything, quickly:** `systemctl --user stop cloudflared-hermes`, or set
  `allow_remote: false` and restart the gateway.
- **For good:**
  1. `systemctl --user disable --now cloudflared-hermes`.
  2. `cloudflared tunnel delete hermes`.
  3. Delete the DNS record in the Cloudflare dashboard.
  4. Remove `~/.cloudflared/<ID>.json`.

## Verified live (2026-11-17)

Against the running gateway:

- **Connections:**
  - the API key works locally and is refused (403) with Cloudflare headers;
  - a token with `allow_remote` off is refused (403);
  - a bogus token gets 401.
- **A paired non-admin user's chat:**
  - Hermes offered only `web_search`, `web_extract`, the outrigger and
    notebook tools, and Tool Search's `tool_search`, `tool_describe` and
    `tool_call`;
  - `tool_search` found no terminal, file, memory or email tools;
  - `tool_call` was refused for `terminal` and `memory` ("not available in
    this execution context").

## What stays true when exposed

- **Credentials:** the key and tokens are checked in constant time before the
  WebSocket is upgraded. Tokens are stored as SHA-256 hashes in
  `plugin-data/outrigger/tokens.json` (mode 0600).
- **Owner-only actions:** only the owner can bind a connection to a Hermes API
  session. A paired user can't see, or answer prompts in, another user's chat.
- **Notes:** paths are resolved before they are checked, so `..`, absolute
  paths, hidden folders and symlinks out of a root are refused.
- **Tool results** from clients reach the model as untrusted data, capped at
  `max_result_chars`.
- **Logs:** the plugin logs the client address the proxy reports
  (`CF-Connecting-IP`), for logs only, never for access decisions.

## Known limits

- **Your profile and memory reach paired users (confirmed 2026-11-17).**
  - What happens: Hermes builds every chat's system prompt with your USER
    PROFILE (and MEMORY.md when built-in memory is on), including a paired
    user's chats. They can't call the `memory` tool, but they can ask Hermes
    what it knows about you.
  - Why the plugin can't stop it: this is set only by Hermes's global memory
    config, and there is no public per-source hook (unlike toolsets).
  - So: pair only people you'd share that with, or keep tokens to your own
    devices (`--admin`).
  - A per-source hook such as `memory_for_source()` would need an upstream
    change.
- **`/health`:** the gateway's unauthenticated `/health` reports its version.
  The ingress rule above keeps it off the internet.
- **Owner key fallback:** with the owner key and the Outrigger platform off,
  the sidebar falls back to `/v1/chat/completions`, which the tunnel doesn't
  expose. Remote use needs the platform (on by default).
