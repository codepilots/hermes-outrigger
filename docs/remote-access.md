# Remote access (Cloudflare Tunnel)

How to reach the outrigger plugin from outside your machine (a laptop elsewhere,
a phone, a friend) without exposing the rest of the Hermes gateway.

Date: 2026-11-17. Applies to hermes-outrigger with `allow_remote` /
`owner_key_remote` / `user_toolsets` (this document's settings).

## The short version

1. Expose **only** `/v1/outrigger/ws` through the tunnel (ingress `path` rule).
2. Turn on `allow_remote` in the plugin settings.
3. Pair a token for every remote device (`hermes outrigger pair`), `--admin` for
   your own devices. Never put `API_SERVER_KEY` on a remote device.
4. Optionally put Cloudflare Access and a rate-limit rule in front.

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
| No key configured at all | allowed (local process) | refused, always |
| `API_SERVER_KEY` | allowed | refused unless `allow_remote` **and** `owner_key_remote` |
| Paired token (`otr_…`) | allowed | allowed if `allow_remote` |
| Revoked or unknown token | refused (401) | refused (401) |

## 1. Tunnel config: only the plugin's route

`~/.cloudflared/config.yml`:

```yaml
tunnel: <your-tunnel-id>
credentials-file: /home/<you>/.cloudflared/<your-tunnel-id>.json
ingress:
  - hostname: hermes.example.com
    path: ^/v1/outrigger/ws$
    service: http://127.0.0.1:8642
  - service: http_status:404
```

Leave the gateway listening on 127.0.0.1 (the default). Don't change
`API_SERVER_HOST`.

## 2. Plugin settings

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

Restart the gateway: `systemctl --user restart hermes-gateway`.

## 3. Tokens for remote devices

```sh
hermes outrigger pair --user me-laptop --label "work laptop" --admin   # you
hermes outrigger pair --user ann --name Ann --label "Ann's laptop"   # someone else
hermes outrigger tokens                # list (never shows secrets)
hermes outrigger revoke <token-id>     # takes effect within ~20 s
```

In the sidebar's settings, set the address to `https://hermes.example.com` and
paste the token. "Test connection" checks the WebSocket.

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
- The user id `owner` (`platform_owner_id`) is reserved; pairing refuses it.

Your own devices on the Hermes machine can keep using the API key.

## 4. Optional: Cloudflare Access and rate limiting

- **Access application** for `hermes.example.com` adds a login before
  anything reaches the gateway. Browsers can't add custom headers to a
  WebSocket, so Access service tokens won't work for the sidebar. A
  browser login (the `CF_Authorization` cookie) or mTLS client certificates
  can. Untested with the extension: if the cookie isn't sent on the
  extension's WebSocket, use mTLS, or rely on tokens alone (they carry about
  190 bits of randomness and are checked before the WebSocket opens).
- **Rate-limiting rule** on the path (for example 30 requests a minute per
  IP) cuts noise from scanners. The plugin itself doesn't limit failed
  logins.

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
