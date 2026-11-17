# Comment for NousResearch/hermes-agent#149740

---

For the Chrome half of this, a working plugin-based implementation (disclosure: I
wrote it): [hermes-sidebar](https://github.com/codepilots/hermes-sidebar), a side
panel extension, plus [hermes-outrigger](https://github.com/codepilots/hermes-outrigger),
the gateway plugin. Hermes drives the browser you already use, in your own
profile and window, with no remote-debugging port. That matters since Chrome
136 ignores `--remote-debugging-port` on the default profile.

How it handles permissions, since that's the crux of this request:

- **Reading is automatic.** Reading the page, a snapshot, the selection and
  the tab list need no approval.
- **Acting asks.** Click, fill, open-a-page and scroll show an approval card.
  You can allow a class of action on a site for the rest of the conversation
  ("Conversation + site"). Running code asks each time unless you allow it
  for that site in the conversation.
- **The reviewer.** An LLM reviewer in the plugin, similar to Hermes's
  terminal check, can approve routine actions. If it would deny one, the card
  comes to you flagged instead of being silently blocked.
- **Personal data.** It's redacted in the browser before anything leaves.
  Hermes asks for specific values with a `reveal` card when it genuinely needs
  them, for example your email address to fill a form. Form filling on the
  page uses the real values locally.

Form filling, your use case, works through `snapshot` (element refs) plus
`fill` and `click`, with the site granted once per conversation.

Not done: Firefox, and file attachments (planned as an `upload_file` tool that
always asks first).
