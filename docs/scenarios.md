# Scenarios: where an outrigger fits

An outrigger is a thin client that connects out to Hermes, attaches to a session,
and offers tools that only make sense where the client is. It fits when the tool:

- lives somewhere that cannot host a server (browser extension, phone app,
  add-in, device behind NAT),
- only exists while a person has something open (a tab, a document, a scene),
- needs that person present to approve or see it happen,
- is scoped to one conversation, not to the whole agent.

If none of those hold, use MCP or a normal plugin (last section).

Each scenario lists: client, tools, why not MCP, risks.

---

## 1. Browser sidebar (first target)

Client: Hermes Sidebar, a Chromium MV3 extension in a side panel.

Tools: `get_page`, `get_selection`, `run_js` (via chrome.debugger
Runtime.evaluate), `click(selector)`, `fill(selector, text)`, `screenshot_tab`,
`list_tabs`, `open_url`.

Why not MCP: an extension cannot listen on a port. The useful context is the tab
the user is looking at, with their logins, right now.

Why not browser_exec over CDP: Chrome 136+ ignores the debug port on the default
profile; Chrome 144+ prompts on every connection; a snapshot profile goes stale.
The extension already runs inside the real profile.

Risks: prompt injection from page content into `run_js`. `run_js`, `click` and
`fill` are `confirm`; `get_page` is `auto`. Banking, mail and shopping pages are
in scope of whatever the user opens, so confirmation is the main control.

## 2. Phone as field kit

Client: small Android app or PWA on the user's phone.

Tools: `location_now`, `take_photo` (user taps shutter), `scan_barcode`,
`share_to_hermes`, `notify(text)`, `pick_contact` (user chooses).

Why not MCP: the phone is behind mobile NAT, sleeps, and changes networks. It
can dial out over a VPN; Hermes cannot dial in reliably. Camera and location
need the person holding the phone.

Example: on a walk, "what is this plant?" Hermes calls `take_photo`, the phone
shows a shutter, the photo comes back as the tool result.

Risks: location and photos are personal data. All tools `confirm` except
`notify`. Bind only to a session the phone itself started.

## 3. Editor and document panels

Clients: an Obsidian plugin, a VS Code extension, a LibreOffice
or Office add-in.

Tools: `get_open_file`, `get_selection`, `replace_selection(text)`,
`insert_at_cursor(text)`, `open_note(path)`, `show_diff(before, after)` (user
accepts or rejects in the editor).

Why not MCP or file tools: Hermes can already edit files on disk, but not the
editor's state: unsaved buffer, cursor, selection, undo history. Writing through
the editor keeps undo and avoids clobbering unsaved changes.

Risks: low to medium. Writes go through the editor's own undo; `show_diff` makes
the user the approver.

## 4. Teaching: student-side code execution

Client: a web page used in a lab or tutorial that runs Python in the student's browser via Pyodide.

Tools: `run_python(code)` in the student's browser sandbox, `get_exercise`,
`get_student_code`, `show_hint(text)`.

Why not MCP or terminal: code runs on the student's machine, in a sandbox, not
on the Hermes host. Each student's session is separate and ephemeral.

Risks: shared deployments need per-student tokens and no access to other
sessions; this pushes the v2 pairing-token work. API_SERVER_KEY must never go to
a student device. Likely a later scenario, after the single-user design works.

## 5. Desktop app or creative tool add-ons

Clients: Blender or Godot add-on, a FilmCraft or DesignCraft panel, a
spreadsheet add-in.

Tools: `get_scene`, `select(objects)`, `apply_op(name, params)`, `render_preview`,
`undo`.

Why not MCP: the tool surface exists only while the app has a document open, and
operations should run on the app's main thread with its undo stack. An add-on
can open a WebSocket; it is awkward for it to be a server.

Risks: medium. Destructive ops `confirm`; `undo` exposed so Hermes can back out.

## 6. A second machine as hands for a remote gateway

Client: a small daemon on a laptop attached to a session on a Hermes gateway
running on another host (home server, desktop), over a VPN.

Tools: `screenshot`, `local_file_read(path in allowlist)`, `play_sound`,
`clipboard_get`.

Why not MCP: the laptop sleeps and moves networks. It can dial the gateway; the
gateway cannot reliably dial it. Screen and clipboard need the person at it.

Risks: medium to high: it hands a remote agent access to a local machine. Tools
`confirm` by default; strict path allowlist; only with per-client pairing tokens
(v2), never the gateway's main key.

## 7. Hardware and home devices

Client: a Raspberry Pi or ESP32 bridge behind home NAT.

Tools: `read_sensor(name)`, `relay_set(name, on)`, `camera_snapshot`.

Why it is borderline: always-on devices are usually better as MCP servers or
Home Assistant integrations. An outrigger fits only if the device cannot accept
inbound connections and is used inside conversations, not by automations.

## 8. Human-in-the-loop forms

Client: any UI that wants Hermes to ask structured questions inside it rather
than in chat: a kiosk, a dashboard, an accessibility front end.

Tools: `ask_form(schema)` that renders a form and returns the answers,
`pick_file`, `confirm(text)`.

Why not clarify: Hermes's clarify tool renders in Hermes's own surfaces. A
outrigger lets a third-party front end own the interaction.

---

## When not to use an outrigger

| Need | Use instead |
|---|---|
| Tool service that should always be available, any session | MCP server |
| Tool that runs on the Hermes host | normal plugin tool |
| Scheduled or unattended automation | cron job + plugin/MCP |
| Driving a dedicated browser profile | browser_exec over CDP |
| Talking to another Hermes agent | peer DM / A2A |
| Chat UI only, no local tools | plain Chat Completions client |

Rule of thumb: if the tool should work when nobody has the client open, it is
not an outrigger tool.

## Comparison summary

| | Outrigger | MCP | Plugin tool |
|---|---|---|---|
| Who connects | client dials Hermes | Hermes dials server (or spawns it) | in-process |
| Lifetime | while client is open | long-lived | gateway lifetime |
| Scope | one session | all sessions with the server enabled | all sessions |
| Runs on | user's device/app | server host | Hermes host |
| User presence | expected (confirm UI) | not assumed | not assumed |
| Tool list | dynamic per client | fixed per server (can change) | fixed at load |
