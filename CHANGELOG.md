# Changelog

## 0.2.0 — 2026-11-19 — Security remediation

Full security review (`docs/security-review/scan-report.md`, sealed scan
`scan_7606f034b84449191cdab8fa`) and remediation of every retained finding.
Item-by-item evidence, RED/GREEN tests and independent parent verification are in
`docs/security-review/remediation.md`. Suite: **360 passed, 1 skipped** (145 at
scan baseline). No functional/behaviour changes beyond the security fixes below;
legitimate local-owner and paired-token access preserved throughout.

### Medium
- **F-01 — Schema `$ref` SSRF / fail-open validation** (`protocol.py`): a
  local-only `referencing.Registry` that refuses all retrieval (loud backstop),
  declaration- and call-time rejection of non-`#` `$ref`/`$dynamicRef`/
  `$recursiveRef`, fail-closed on missing jsonschema, and strict unknown-`type`
  rejection. ReDoS `pattern` stripping and disabled format checking retained.
- **F-02 — Reviewer padded-args truncation / prompt injection** (`reviewer.py`,
  `plugin.yaml`): args serialized first under an 8000-char budget (over-budget or
  unserializable args force escalate + loud warning before any model call);
  separate 4000-char context budget; `<untrusted>` tag defanging in the data
  region; docstring/manifest corrected to "deny is advisory unless
  `review_deny=block`".
- **F-03 — Notebook denial-of-service** (`notebook.py`, `ws.py`): open-doc caps
  (64 total / 16 per client) with LRU eviction of clientless, unlocked docs and
  loud refusal otherwise; `doc.list` walk limit (5000) moved off the event loop;
  rate limiting on `doc.list`/`doc.open`.

### Low
- **F-04 — Watcher fragility** (`notebook.py`): per-tick/per-doc/per-client
  guards, a supervisor that restarts a dead watcher loudly, lock-timeout config
  coercion, and disk refresh before `tool_edit` version compare.
- **F-05 — Stale root grants / doc identity** (`notebook.py`): grant recheck on
  every `get()` (narrowed roots revoke open-doc access); docs keyed by resolved
  file identity so overlapping/alias roots share one mutex/lock/version.
- **F-06 — Notebook content injection labeling** (`notebook.py`): `tool_read`
  results marked `untrusted: True`; schema/command framing states note content is
  collaborator-written data, never instructions.
- **F-07 — Unbounded error relay** (`dispatch.py`, `ws.py`): error payloads capped
  before relay to clients.
- **F-08 — Subagent marker eviction fail-open** (`registry.py`): `mark_subagent`
  never evicts and never refuses (the mark *is* the deny signal); the cap is a
  loud warning threshold. Missed stop-hooks leak memory, never authority.
- **F-09 — Audit silent loss / identity / permissions** (`audit.py`,
  `dispatch.py`, `notebook.py`): audit write failures log loudly and increment a
  counter (never silent, never breaking a call); entries carry the stable client
  principal (no secret); audit/capture/snapshot artifacts created `0600`;
  malformed requests audited.

### Mitigated (partial — residual trust boundary documented)
- **F-10 — Auth locality / key-source / admin-scope** (`ws.py`, `platform.py`,
  `settings.py`): `_expected_key` reads the gateway's live resolved key (a
  config-keyed gateway never falls into keyless local-owner); new
  `allow_keyless_local` switch + loud warning on every keyless-owner admission;
  admin scope narrowed by live connections (a live non-admin connection restricts
  the user even with an idle admin token; mixed connections fail closed).
  **Residuals open and operator-owned** — see
  `docs/security-review/auth-trust-boundary.md`: the locality heuristic cannot
  distinguish a header-less same-host relay over TCP; gateways without the
  app-key hook fall back to plugin+env; true per-token admin needs a gateway API
  change.

### Informational
- **F-11 — Regex trailing newline** (`protocol.py`, `ws.py`): tool-name and
  chat-id anchors `$` → `\Z`; a trailing newline is rejected. No escalation
  resulted.

### Rejected by the review (no action)
- CSWSH/Origin (gateway CORS default-denies unlisted Origins); revocation latency
  (~20 s, documented design).

### Docs / manifest
- Declared `allow_keyless_local` in `config_schema`; `auth_token` description now
  reflects the gateway-key precedence; `_rate_check` docstring covers
  `doc.list`/`doc.open`; `docs/remote-access.md` cross-references the residual
  trust-boundary model.

## 0.1.0
- Initial release.
