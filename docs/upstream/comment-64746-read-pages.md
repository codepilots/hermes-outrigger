# Comment for NousResearch/hermes-agent#64746

---

There's a plugin-based version of this you can try now (disclosure: I wrote it):
[hermes-sidebar](https://github.com/codepilots/hermes-sidebar), a Chrome side
panel, and [hermes-outrigger](https://github.com/codepilots/hermes-outrigger),
the gateway plugin it connects to.

What it does for the read-only case:

- **No debug port.** The extension reads the rendered page from the tab
  you're on, through a content script. `get_page` returns readable text, and
  `snapshot` returns a compact list of interactive elements with refs for
  follow-up actions.
- **Cheap on tokens.**
  - Hermes is told the active tab (title and URL) each turn and reads the page
    only when the question needs it.
  - When the page is attached without tools, it's sent once per conversation
    unless it changes.
  - A selection is sent on its own.
- **Your own browser:** your logins, and JavaScript-heavy pages as you see
  them, because they've already rendered.
- **Private by default.** Personal data (emails, phone numbers, cards, names)
  is replaced with placeholders in the browser before anything is sent.
- **Over the internet:** the extension can connect through a tunnel, with a
  per-device token.

Limits:
- Chromium only so far.
- Actions such as click and fill exist but go through approval cards.
- Only `run_js` and the devtools readers use `chrome.debugger`. That shows
  Chrome's "debugging this browser" banner while they run.
