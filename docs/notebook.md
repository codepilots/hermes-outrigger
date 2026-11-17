# Notebook mode: edit a note on the Hermes host, with `//` commands (proposal)

Date: 2026-11-17. Status: proposal. Builds on the Outrigger platform
(docs/platform.md) and the browser sidebar.

## 1. What the user gets

- **Open and edit.** Open a Markdown file that lives on the machine Hermes runs
  on (for example `~/vaults/notes`, an Obsidian vault of ~500 notes) in the
  sidebar, and edit it there. Changes save back to the file as you type.
- **Inline commands.** A line that starts with `//` is an instruction to Hermes,
  not note text:

  ```
  The quick brwon fox jumpd over teh dog.
  //correct my spelling
  ```

  Pressing Enter on that line sends it. Hermes removes the line from the note,
  does the work (fixes the paragraph, fleshes out a section, finds and adds
  relevant links) and the result appears in the editor.
- **Editing pauses while Hermes works,** so nobody types over its changes. The
  editor is read-only with a "Hermes is working on: correct my spelling…" bar and
  a Stop button. It unlocks when Hermes finishes, showing what changed and an
  **Undo Hermes's change** button.

## 2. Pieces

```
sidebar (editor)  ──WS /v1/outrigger/ws──►  outrigger plugin
  doc.open/update/command                     ├─ notebook service: roots, read/write, versions,
  ◄ doc.content/changed/lock                  │    lock, file watcher, undo snapshots
                                              └─ Outrigger platform: a `//` command becomes a
                                                   Hermes turn in the note's own chat; Hermes
                                                   edits through notebook_* tools
```

- **The plugin owns the file.** It reads and writes only inside configured
  roots, keeps a version for each open note, and serializes writes.
- **Commands are ordinary Hermes turns.** Each note has its own chat on the
  Outrigger platform (`note:<root>:<path>`), so `//` commands get Hermes's full
  pipeline (tools, memory, approvals, `/stop`), and the note's history of
  commands is a normal Hermes session.
- **Hermes edits through scoped tools**, not its general file tools:
  `notebook_read` and `notebook_edit`, offered only on note turns and only for
  that note. This keeps edits atomic under the lock, attributable, and easy to
  undo, and doesn't depend on Hermes's file tools having write access to the
  vault.

## 3. Files and safety

- **Roots.** Plugin setting `notebook_roots`, e.g.
  `{"notes": "~/vaults/notes"}`. Paths are resolved and must stay inside a root
  (no `..`, no symlinks out), and only `.md` files are opened. Hidden folders
  (`.obsidian`, `.git`, `.trash`) are skipped in listings.
- **Who.** The owner only by default. Paired token users (P4) get nothing unless
  a root lists them (`notebook_roots_for: {"ann": ["shared"]}`).
- **Writes** go to a temp file and are renamed over the note, keeping the
  file's mode. A note that's too large (e.g. over 2 MB) opens read-only.
- **Obsidian and sync.** Open notes are polled for mtime/size changes every ~2 s.
  A change from outside becomes `doc.changed`: the editor takes it in if you have
  no unsaved edits, otherwise it does a 3-way merge against the last common
  version and shows a conflict bar if the merge fails.
- **Undo.** Before each command the plugin snapshots the note (the last 10 per
  note, in plugin data). "Undo Hermes's change" restores the snapshot (itself
  undoable).

## 4. Protocol (outrigger v2 additions; capability `notebook`)

Client → server:

| type | fields | meaning |
|---|---|---|
| `doc.list` | `root?`, `query?`, `limit?` | browse/search notes (path, title, mtime) |
| `doc.open` | `root`, `path` | open; get `doc.content`; start watching |
| `doc.close` | `doc_id` | stop watching |
| `doc.update` | `doc_id`, `base_version`, `text` *or* `patch` | save edits; `doc.saved {version}` or `doc.conflict {text, version}` |
| `doc.command` | `doc_id`, `base_version`, `line`, `instruction` | run a `//` command (section 5) |
| `doc.undo` | `doc_id`, `snapshot` | restore the snapshot taken before a command |

Server → client:

| type | fields | meaning |
|---|---|---|
| `doc.content` | `doc_id`, `root`, `path`, `text`, `version`, `locked?` | after open, after Hermes's edits, after undo |
| `doc.changed` | `doc_id`, `text`, `version`, `by` (`disk`/`hermes`/`other-client`) | the file changed underneath |
| `doc.lock` | `doc_id`, `locked`, `by`, `instruction?`, `snapshot?` | editing paused / resumed |
| `doc.saved` / `doc.conflict` | `doc_id`, `version`, … | result of `doc.update` |

`version` is a hash of the file's content. Every write checks it, so a stale
client can never overwrite newer text.

## 5. A `//` command, step by step

1. **The client spots the command.** You press Enter on a line starting with
   `//`, outside fenced code blocks. A literal `//` at line start is written as
   `\//`. The client sends `doc.command {line, instruction}` after flushing
   pending edits.
2. **The plugin removes the line and locks the note.** It removes that line from
   the file (under the version check), takes a snapshot, sets the lock, and
   broadcasts `doc.lock {locked: true}` and `doc.content` to every client that has
   the note open. Further `doc.update` calls are refused while locked.
3. **The plugin starts a Hermes turn** in the note's chat on the Outrigger
   platform, as the user who typed it:

   > The user is editing the note `notes:Projects/Garden.md` and wrote an inline
   > instruction at line 42: "correct my spelling". Unless it says otherwise, it
   > applies to the paragraph or section just above line 42. Use notebook_read and
   > notebook_edit to change the note, keep the user's voice and Markdown, then
   > reply with one short line saying what you changed.

4. **Hermes does the work.**
   - **`notebook_read {from_line?, to_line?}`** returns numbered lines and the
     version.
   - **`notebook_edit {version, edits: [{from_line, to_line, text}]}`** applies
     edits atomically and returns the new version.
   - Both work only on this note and only during this command. Hermes may use
     its other tools as usual, for example web search for "find relevant links".
   - Each `notebook_edit` is pushed to the editor as `doc.content`
     (`by: "hermes"`), so you watch the changes land.
5. **The turn ends and the note unlocks.** The plugin sees the end of the turn
   (`on_session_end` for the note chat's session; `/stop` and errors too) and
   broadcasts `doc.lock {locked: false}`. The editor shows Hermes's one-line
   summary and a diff highlight of the changed lines, with **Undo** and
   **Keep**.
6. **Safety valve.** If the turn runs past a limit (setting, e.g. 10 min), the
   lock is released with a notice, and Hermes's edits so far stay (undoable).

Commands queue: a second `//` typed before the first ends isn't possible (the
editor is locked), but the composer can still send ordinary chat or `/stop`.

## 6. Sidebar UI

- **Opening a note.** "Open note…" shows a searchable list from `doc.list`;
  recent notes are pinned. A command (`/note Garden`) does the same. The panel
  switches to a **Note** view, with a tab back to Chat.
- **The editor.** CodeMirror 6, vendored, with Markdown highlighting. `//` lines
  are styled as commands (accent colour and a "↵ send to Hermes" hint), and a
  live preview toggle uses the existing Markdown renderer.
- **Status bar.** Shows Saved, Saving…, Conflict, or "Hermes is working…" with a
  Stop button.
- **Open in a tab:** the same editor at full width (`notebook.html`), for longer
  writing.
- **Personal-data redaction doesn't apply to notes.** The text already lives on
  the Hermes host, and redacting it in a file editor would corrupt the file.
  Ordinary chat keeps redaction.

## 7. Open questions

1. **Detecting the end of a turn.** The plan is `on_session_end`, keyed by the
   note chat's session id, which `pre_llm_call` can map through
   `get_session_env("HERMES_SESSION_CHAT_ID")`. Confirm it fires for platform
   turns, including after `/stop`, and that an idle fallback covers anything it
   misses.
2. **Tool scoping.** `notebook_*` should be usable only on the turn that a `//`
   command started. The plan is to check the chat id via `get_session_env` in the
   handler, then confirm subagents of that turn can't edit (they are blocked
   through `subagent_start` as for other outrigger tools).
3. **Merging.** Choose a 3-way merge for disk changes: diff-match-patch (small,
   vendorable) or line-based diff3. It must behave sensibly with Obsidian's sync
   writing whole files.
4. **CodeMirror size.** A minimal Markdown build is ~150-200 KB, bundled like the
   Arcjet bundle (`tools/` build script). Alternatively, start with a styled
   textarea and upgrade later.
5. **Scope of "this".** Paragraph above, section above, or the whole note? Start
   with "the paragraph or section above, unless the instruction says otherwise",
   and see how often users need a selection-based variant (select text, then
   `//`, so the selection is the target).

## 8. Milestones

- **N1, files:** roots, `doc.list/open/update/close`, versions, atomic writes,
  watcher with `doc.changed`, conflict bar. Sidebar Note view with a textarea
  editor and autosave.
- **N2, commands:** `doc.command`, lock, snapshots, note chats on the platform,
  `notebook_read` / `notebook_edit`, unlock on turn end, summary, diff highlight
  and undo.
- **N3, editor:** CodeMirror with `//` styling and preview, full-tab editor, and
  selection-targeted commands.
- **N4, sharing:** per-user roots for paired tokens, and several clients on one
  note (presence, lock shown to everyone).

## 9. Testing

- **Path safety:** `..`, absolute paths, symlinks out of the root, non-`.md`
  files, hidden folders.
- **Versions:** a stale `doc.update` gets `doc.conflict`, never an overwrite.
  An external write while open gives `doc.changed`, plus a merge or a conflict.
- **Commands:** the line is removed exactly once, the lock blocks updates,
  `notebook_edit` is rejected with a wrong version or from another chat, the note
  unlocks on turn end, on `/stop` and on timeout, and undo restores the snapshot.
- **Live:** `//correct my spelling` on a scratch note in the vault (never a real
  note), checked in Obsidian afterwards.
