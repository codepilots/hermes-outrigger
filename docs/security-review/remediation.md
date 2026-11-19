# Outrigger security remediation ledger

Source review: `scan-report.md` / `scan-findings.json` (hermes-security scan
`scan_7606f034b84449191cdab8fa`, sealed at revision `1a818f0`, snapshot
`sha256:9ddb0f72…`). 14 reportable findings (3 medium, 9 low, 2 informational)
plus 5 deployment-conditional "couldn't confirm" items.

**Standing directive (user, 2026-11-19):** where the plugin has failure points,
make them **fail loud / fail closed** per the Hermes plugin-catalog conventions
— no silent swallowing of security-relevant errors. This applies across items
F-01 (jsonschema ImportError), F-02 (reviewer truncation), F-04 (watcher
exception), F-08 (subagent eviction), F-09 (audit write failure), F-10 (auth
locality/key source).

**Baseline:** `.venv/bin/python -m pytest tests/ -q` → **145 passed, 1 skipped**
(proven 2026-11-19 before any fix). Test deps declared in `requirements-dev.txt`.
Branch: `security-remediation`.

Method: delegated-remediation — one finding (or tightly-coupled group) per
subagent, strictly sequential; parent verifies each independently (full diff,
full suite re-run, parent-written exploit probe, static-scan greps on added
lines), commits the fix, then the ledger entry, then dispatches the next.

## Items

| Item | Findings (candidate IDs) | Severity | Files | Status |
|------|--------------------------|----------|-------|--------|
| F-01 | SSRF via schema `$ref` (8a3f0a04) + schema-check fail-open (231342bf) | Medium + Low | protocol.py | **fixed (f3bd438)** |
| F-02 | Reviewer padded-args truncation (27db6f36) + `</untrusted>` delimiter escape (5a654c56) | Medium + Low | reviewer.py, plugin.yaml | **fixed (93a58ea)** |
| F-03 | Notebook unbounded cache + on-loop `doc.list` DoS (f3441f81) | Medium | notebook.py, ws.py | **fixed (aa95320)** |
| F-04 | Notebook watcher silent exception (245216c2) + `tool_edit` no disk refresh (b26e85aa) | Low + Low | notebook.py | **fixed (ec569df)** |
| F-05 | Notebook stale root grants (4ea8d7ce) + doc-identity aliasing (539075fd) | Low + Low | notebook.py | **fixed (7c4608c)** |
| F-06 | Notebook shared-root indirect injection labeling (39fe8161) | Low | notebook.py | **fixed (43c5d82)** |
| F-07 | Unbounded 4 MB error relay (0bf92726) | Low | dispatch.py, ws.py | **fixed (48c0983)** |
| F-08 | Subagent marker eviction fail-open (9c9370fa) | Low | registry.py | **fixed (5e81db0)** |
| F-09 | Audit silent loss + identity + tamper (f37f9e4d) + artifact permissions (2eaf3124) + malformed-request unrecorded (6af3ae03) | Inconclusive→fix + Info | audit.py, dispatch.py, notebook.py | **fixed (42bf71b)** |
| F-10 | Auth locality fail-open (e75c7051) + credential-source mismatch (76d74ef9) + admin-scope mismatch (ed211e05) | Inconclusive→mitigate+residual | ws.py, platform.py, settings.py, docs | **mitigated (4668957) — partial** |
| F-11 | Regex `$` trailing newline (c0efd191) | Informational | protocol.py, ws.py | **fixed (b2f594c)** |

Rejected by the review (no action): CSWSH/Origin (4a1ae1ca — gateway CORS
default-denies), revocation latency (84be8801/ef3b710a — documented ~20s design),
and duplicate merges.

## Log

### Evidence freeze (2026-11-19)
- Copied sealed scan artifacts into `docs/security-review/` (report, findings,
  coverage) so every fix is a clean diff against the reviewed state.
- Declared `requirements-dev.txt`; created `.venv`; baseline 145 passed / 1 skipped.
- Branch `security-remediation` created from `1a818f0`.

### F-01 — SSRF via schema `$ref` + schema-check fail-open — FIXED (f3bd438)
- **Delegation:** one subagent, TDD. Traced the mechanism in installed
  jsonschema 4.26.0 / referencing as data before fixing.
- **Fix (protocol.py only):** local-only `referencing.Registry` refuses all
  retrieval (logged loud backstop); `check_schema` + `validate_args` reject
  non-`#` `$ref`/`$dynamicRef`/`$recursiveRef`; `ImportError` fails closed +
  `log.error` (was silent pass); `_type_ok` rejects unknown type keywords.
  Local fragment refs and ReDoS/size/depth hardening preserved.
- **Accepted deviations:** `_remote_ref` scan also in `validate_args` (covers
  `$recursiveRef`, which the validator never evaluates, and the no-jsonschema
  fallback); `check_schema` builds the meta-validator explicitly (classmethod
  takes no registry); ref-like DATA in `const`/`enum` is conservatively
  over-rejected (fail-closed by design, documented).
- **Parent verification (independent):** full diff read; full suite re-run
  **180 passed, 1 skipped** (145 baseline + 35 new, zero regressions);
  parent-written probe `cache/scratch/verify_ssrf_parent.py` — **19 assertions
  pass, 0 urlopen attempts**, fail-closed + loud logs confirmed; static-scan
  greps on added lines clean (no eval/exec/subprocess/secret/fetch sinks).
- **RED evidence:** new file first run 31 failed / 4 passed (the 4 are
  regression guards); fetch tests failed with the exploit reproduced in-test.

### F-02 — Reviewer truncation + delimiter escape — FIXED (93a58ea)
- **Delegation:** one subagent, TDD (reused FakeLLM/setup from test_reviewer.py).
- **Fix:** args serialized first with `ARGS_BUDGET=8000`; over-budget or
  unserializable args force `escalate` + loud warning BEFORE any model call;
  context bounded separately (`CONTEXT_BUDGET=4000`) with truncation marker so
  padding cannot eat args; `_DEFANG` neutralizes `<untrusted>`/`</untrusted>`
  case/whitespace variants in the data region; docstring + plugin.yaml `review`
  description corrected to advisory-unless-block. `review_deny` default unchanged.
- **Accepted deviations:** defang (strip/escape) instead of a nonce boundary —
  existing test_reviewer.py pins the literal wrapper tags and small-call JSON
  validity; dispatch.py NOT modified (escalate already travels to the client as
  advisory; confirmed by integration tests under ask and block); budget values
  are a design choice, not operator settings; defang mutates only the reviewer
  prompt copy, never the executed args sent to the client.
- **Parent verification (independent):** full diff read; suite re-run
  **203 passed, 1 skipped** (180 + 23 new, zero regressions); parent probe
  `cache/scratch/verify_reviewer_parent.py` — **19 assertions pass**: oversized
  → escalate with model never called, delimiter variants leave exactly one
  wrapper tag, args complete under 27 KB context padding, caller args dict
  untouched, circular args escalate, config/docs honest; static greps clean.
- **RED evidence:** new file first run 14 failed / 9 passed (the 9 are guards);
  oversized-approve, delimiter-breakout and honest-docs tests failed for the
  right reason.

### F-03 — Notebook open-doc memory + listing CPU DoS — FIXED (aa95320)
- **Delegation:** one subagent, TDD (reused test_notebook.py setup).
- **Fix:** global cap `MAX_OPEN_DOCS=64` + per-client `MAX_DOCS_PER_CLIENT=16`;
  `_evict_idle_docs` LRU-evicts only clientless AND unlocked docs, else refuses
  `too_many_docs` (held/locked notes never evicted), loud WARNING; listing walk
  bounded at `LIST_WALK_LIMIT=5000` visited files (loud WARNING) and dispatched
  off the event loop via `run_in_executor` in ws.py doc.list; `_rate_check`
  applied to doc.list/doc.open (owner/admin exempt). `Doc.last_used` field added
  for LRU recency.
- **Accepted deviations:** caps are module constants, not operator settings
  (settings.py fenced); worst-case retained text 64 × notebook_max_bytes
  (128 MiB default); eviction is a safety net (close_doc already pops clientless
  unlocked docs), refusal is the primary bound; shared `_rate_check` budget
  reused for notebook reads (permitted).
- **Docs follow-up (for the refactor/audit pass):** `_rate_check`'s docstring
  ("messages that start Hermes turns") is now understated — it also guards
  doc.list/doc.open. Left unedited (outside the ws.py allowlist); flag for the
  documentation pass.
- **Parent verification (independent):** full diff read; suite re-run
  **212 passed, 1 skipped** (203 + 9 new, zero regressions); parent probe
  `cache/scratch/verify_notebook_dos_parent.py` — **12 assertions pass**:
  per-client/global caps bounded, held + locked clientless docs survive
  pressure, walk bounded at 5000, non-admin rate-limited / owner exempt, normal
  use intact; loud warnings confirmed; static greps clean.
- **RED evidence:** new file first run 8 failed / 1 passed (the 1 is the
  normal-use guard); cap/eviction/rate-limit/off-loop/walk-bound tests failed
  for the right reason.

### F-04 — Notebook watcher fail-loud + tool_edit disk refresh — FIXED (ec569df)
- **Delegation:** one subagent, TDD (WATCH_INTERVAL shrunk to 0.01 for real ticks).
- **Fix:** `_watch` guards expire_locks / per-doc `_check_disk` / broadcast each
  tick (broad Exception incl UnicodeDecodeError), loud + continue; `broadcast`
  guards per client (closed loop → loud skip, healthy clients still receive);
  `expire_locks` per-doc guard leaves a bad doc locked (fail closed);
  `_lock_timeout_s` coerces null/bad config to default, loud; `_watch_done`
  supervisor logs death at ERROR and restarts without doc.open; `tool_edit`
  calls `_check_disk` before the version compare, OSError/ValueError →
  NotebookError('unreadable') JSON refusal (never raises/clobbers).
- **Accepted deviations:** per-client guarding inside broadcast (a tick-only
  guard would lose a one-shot disk-change broadcast to healthy clients);
  per-doc guard inside expire_locks; `_check_disk`/`_read` bodies unchanged
  (guarding at call sites keeps update() byte-identical).
- **Residuals (for the audit pass, out of F-04 scope):** `tool_read` still serves
  cached text (clobbering now prevented by tool_edit's refresh); deleted-note
  semantics unchanged (`_check_disk` returns False on stat OSError, so a deleted
  note is recreated from cache — tool_edit never raises there).
- **Parent verification (independent):** full diff read; suite re-run
  **225 passed, 1 skipped** (212 + 13 new, zero regressions); parent probe
  `cache/scratch/verify_watcher_parent.py` — **11 assertions pass**: watcher
  alive after non-UTF-8 write, bad timeouts coerced, closed-loop broadcast
  non-fatal, supervisor restart, tool_edit refuses stale/unreadable + preserves
  external save, normal edit works; static greps clean. (One probe assertion
  initially failed on a wrong `edits` format — probe bug, corrected; apply_edits
  is line-based from_line/to_line/text.)
- **RED evidence:** new file first run 11 failed / 2 passed (the 2 are normal-
  behavior guards); watcher-death, bad-timeout, closed-loop and tool_edit-clobber
  tests failed for the right reason.

### F-05 — Notebook stale grants + doc-identity aliasing — FIXED (7c4608c)
- **Delegation:** one subagent, TDD. ws.py untouched (all three doc ops authorize
  via get()).
- **Fix:** `get()` re-checks the note's resolved location against the granted
  roots' directories every call (`not_allowed` when no longer granted) —
  directory-aware so an alias root name for the same tree still counts; owner
  and still-granted roots unaffected; full revocation preserved. `open_doc`
  keys DOCS by resolved file identity (canonical config-order root +
  realpath-relative path) with a retained-handle fallback for settings
  reload/reorder, so in-root symlinks and overlapping/nested root names share
  one Doc/mutex/lock/version; canonical doc_id byte-identical for disjoint roots.
- **Accepted deviations:** directory-aware grant recheck (not literal name
  membership) to preserve alias-root grants; roots() NOT coalesced (would drop
  operator-granted names from allowed_roots); retained-handle fallback added
  (closes an identity-split edge found adversarially).
- **Residuals (documented, for the audit pass):** hardlink aliases not coalesced
  (distinct realpaths, same inode; inode keying unstable because atomic _write
  os.replace changes the inode); case/Unicode variants on case-insensitive hosts
  (not protocol-triggerable on the pinned Linux/ext4); a narrowed user still
  receives doc.changed broadcasts on a held-open doc until close/disconnect
  (broadcast is F-04 territory). Client-initiated writes via get() are fully
  revoked; an in-flight // command's Hermes notebook_edit (doc_for_tool path,
  authorized at command start) continues until turn end — a narrow mid-turn
  revocation window, not a new client write path (independent review follow-up).
  **Owner decision 19 Nov 2026: kept as-is** — see the decision entry at close-out.
- **Parent verification (independent):** full diff read; suite re-run
  **234 passed, 1 skipped** (225 + 9 new, zero regressions); parent probe
  `cache/scratch/verify_grants_parent.py` — **10 assertions pass**: narrowing
  revokes, still-granted/owner preserved, full revocation blocks, symlink +
  overlapping-root aliases coalesce to one lock, distinct files separate,
  normal update works, F-03 caps intact; static greps clean.
- **RED evidence:** new file first run 7 failed / 2 passed (the 2 are
  preservation guards); grant-recheck and alias-coalescing tests failed for the
  right reason.

### F-06 — Notebook shared-root indirect-injection labeling — FIXED (43c5d82)
- **Delegation:** one subagent, TDD. dispatch.py untouched (_format_ok already
  labels client results; notebook_read needed its own label).
- **Fix (labeling hardening, behavior-preserving):** `tool_read` result carries
  `untrusted:True`; `READ_SCHEMA` description states note content may be
  collaborator-written and is untrusted data, never instructions;
  `command_message` frames the note body as untrusted data to edit (the user's
  // instruction is the intent), preserving the read/edit workflow + one-line
  reply. Lock/snapshot logic untouched.
- **Accepted deviations:** new test file adds an autouse `_keep_session_env`
  restore fixture — REQUIRED for suite determinism because test_fuzz.py's
  `plat` fixture installs a persistent fake gateway with no teardown and
  conftest does not reset SESSION_ENV; without restore the new tests leaked
  SESSION_ENV into test_outrigger.py (14 failures). Test-only, state-neutral.
- **Residual (test hygiene, out of F-06 scope):** test_fuzz.py's fake-gateway
  persistence + conftest not resetting SESSION_ENV is a pre-existing test-
  isolation weakness; flag for the audit/test pass.
- **Parent verification (independent):** full diff read; suite re-run TWICE
  **239 passed, 1 skipped** (234 + 5 new, zero regressions, deterministic);
  parent probe `cache/scratch/verify_injection_parent.py` — **12 assertions
  pass**: injection payloads returned as data labeled untrusted:True (full +
  ranged), command_message frames data + keeps instruction/workflow (inline +
  selection), schema honest, version/note/total_lines preserved; static greps
  clean.
- **RED evidence:** new file first run 4 failed / 1 passed (the 1 is the
  preservation guard); schema/command_message/tool_read labeling tests failed
  for the right reason (the // flow itself passed).

### F-07 — Unbounded error relay — FIXED (48c0983)
- **Delegation:** one subagent, TDD (real aiohttp ws end-to-end + _run direct).
- **Fix:** `_run` coerces non-string errors (json.dumps), strips control chars
  and caps outrigger_-prefixed strings AND non-string values with the ok path's
  bound `min(decl.max_result_chars, settings.max_result_chars)`; message carrier
  bounded; short gateway codes pass through unchanged (outrigger_cancelled keeps
  cancelled status; a long forgery maps to error). ws result handler coerces
  non-string errors and bounds the error field at ingress (defense in depth).
- **Accepted deviations:** error cap mirrors ok path min(decl, settings);
  ingress coercion turns falsy non-string errors (0/False/[]/{}) into short JSON
  strings riding the 2000-char detail path (bounded; no test depended on the old
  behavior); ws._decode already rejects lone surrogates at ingress.
- **Parent verification (independent):** full diff read; suite re-run
  **256 passed, 1 skipped** (239 + 17 new, zero regressions); parent probe
  `cache/scratch/verify_error_relay_parent.py` — **13 assertions pass**:
  oversized/dict/list errors capped, control chars stripped, short codes +
  cancelled status intact, forged long cancelled → error, ok path unchanged,
  decl cap + message carrier bounded; static greps clean.
- **RED evidence:** new file first run 8 failed / 9 passed (the 9 are
  preservation guards); cap/coercion/control-char/ingress tests failed for the
  right reason.

### F-08 — Subagent marker eviction fail-open — FIXED (5e81db0)
- **Delegation:** one subagent, TDD. __init__.py untouched (hooks already correct).
- **Fix:** the mark IS the allow_subagents deny signal, so both eviction
  (set.pop) and refusing a new mark fail open. `mark_subagent` now never evicts
  and never refuses — idempotent, retains overflow marks, loud WARNING at/above
  `MAX_TRACKED_SUBAGENTS` (re-commented as a warning threshold, not an eviction
  bound). Every marked session stays denied while allow_subagents=False;
  normal/owner sessions unaffected; allow=True still permits.
- **Accepted deviations / design:** chose "never evict / never refuse" over
  TTL/liveness eviction — no safe liveness oracle in scope (only subagent_stop
  unmarks; session-end/turns logic fenced), and a TTL could unmark a live
  long-lived subagent (fail-open). WARNING per overflow mark (not throttled, to
  keep every overflow denial loud).
- **Residual (documented):** missed subagent_stop hooks now leak MEMORY (marks
  retained until unmarked) instead of authority; the cap is a loud-warning
  threshold, not a hard bound. Bounded only by delegation volume (no established
  attacker primitive to force marks); never a gate bypass.
- **Parent verification (independent):** full diff read; suite re-run
  **265 passed, 1 skipped** (256 + 9 new, zero regressions); parent probe
  `cache/scratch/verify_subagent_parent.py` — **10 assertions pass**: no mark
  evicted at/over cap, all subagents denied, owner binding safe, normal session
  allowed, allow=True permits, unmark/re-mark correct, cap warning loud; static
  greps clean.
- **RED evidence:** new file first run 6 failed / 3 passed (the 3 are
  preservation guards); eviction/overflow-denial/no-untracked tests failed for
  the right reason.

### F-09 — Audit fail-loud + identity + artifact permissions — FIXED (42bf71b)
- **Delegation:** one subagent, TDD. ws.py not needed (ws._register already sets
  client.principal; identity threaded at dispatch call sites).
- **Fix (fail-loud priority):** `audit.record` logs ERROR (path+reason+tool/
  session/outcome) and increments a thread-safe `_write_failures` counter on
  OSError instead of silent pass — a lost entry never breaks a call but is
  observable; entries carry the stable `client.principal` (kind/user_id/token id,
  no secret) across all dispatch audit sites incl. rejections; audit.jsonl,
  captures and snapshots created 0600 via os.open (like the token store);
  malformed outrigger_call routed through reject() so it is audited.
- **Accepted deviations:** principal stored as the full client.principal dict
  (no secret/hash; probe-verified absent), omitted for pre-client rejections;
  0600 is creation-only (pre-existing artifacts keep their mode, same semantics
  as tokens._save); counter under the existing _lock, log ERROR without exc_info.
- **Residuals (follow-ups, out of scope):** audit rotation/size cap (file grows
  unbounded); hash-chain/signature tamper evidence (offline edits undetectable);
  notebook edits + platform chat outside audit.jsonl scope; tightening modes of
  pre-existing artifacts / plugin-data dir.
- **Parent verification (independent):** full diff read; suite re-run
  **281 passed, 1 skipped** (265 + 16 new, zero regressions); parent probe
  `cache/scratch/verify_audit_parent.py` — **12 assertions pass**: write failure
  loud + counted + never raises, counter climbs, recovery writes, principal in
  entry, audit/snapshot 0600, malformed audited, note-file mode preserved;
  static greps clean.
- **RED evidence:** new file first run 14 failed / 2 passed (the 2 are
  preservation guards); fail-loud/identity/0600/malformed tests failed for the
  right reason.

### F-10 — Auth locality / key-source / admin-scope — MITIGATED, honest PARTIAL (4668957)
- **Acceptance class:** architectural / trust-boundary — mitigate + document
  residual, NOT "fully fixed". Child returned status=partial (correct).
- **Mitigations (tested):** `_expected_key(request)` reads the gateway's live
  resolved key via `app['api_server_adapter']._api_key` (precedence plugin
  auth_token > adapter key > env) — a config-keyed gateway never falls into
  keyless local-owner; `authorize` gains `allow_keyless_local` (default true;
  false refuses every credential-less owner attach) + loud WARNING on every
  keyless-local-owner admission; `toolsets_for_user`/`access_extra` scope admin
  by live connections (live non-admin narrows even with an idle admin token;
  mixed fail closed; registry-race fail closed; per-user fallback only with no
  live connection).
- **Residual doc:** `docs/security-review/auth-trust-boundary.md` — per-finding
  proves/does-not-prove, operator deployment controls, fail-loud inventory,
  residual summary table. Honest about the locality heuristic's fundamental
  limit and the per-token-admin API gap.
- **Residuals (OPEN, documented):** locality e75c7051 fundamental (header-less
  relay indistinguishable over TCP; needs Unix socket/SO_PEERCRED or trusted-
  proxy allowlist — out of scope); credential-source conditional (gateways
  without the app-key hook fall back to plugin+env view); admin-scope API gap
  (true per-token admin needs a gateway API change — mixed-connection narrowing,
  connectionless per-user fallback, first-doc.command sync race, mint-time
  warning unfenced). Browser boundary stays with gateway CORS.
- **Accepted deviations:** admin scope uses live-connection state (CHATS), not
  true per-token threading (SessionSource carries user_id only; adapter wrapper
  fenced); allow_keyless_local default true preserves local dev; plugin.yaml
  config_schema not updated (fenced) — setting works via loader, declaration is
  a docs follow-up; tokens.py untouched.
- **Parent verification (independent):** full diff read + residual doc read
  (honest, no overclaim); suite re-run **306 passed, 1 skipped** (281 + 25 new,
  zero regressions); parent probe `cache/scratch/verify_auth_parent.py` —
  **13 assertions pass**: adapter-key precedence closes keyless route on a keyed
  gateway, allow_keyless_local gate + loud warnings, live non-admin narrows
  toolsets/admin list, no-live admin fallback; static greps clean (3 hits were
  benign config reads/docstring).
- **RED evidence:** new file first run 12 failed / 13 passed (the 13 are
  preservation guards); keyless/wrong-key admission, missing-warning and
  admin-scope tests failed for the right reason.

### F-11 — Regex trailing-newline — FIXED (b2f594c)
- **Delegation:** one subagent, TDD. Informational (CWE-20), no escalation.
- **Fix:** `_NAME_RE` (protocol.py:16) and `_CHAT_ID_RE` (ws.py:332) anchors
  `$` → `\Z` (minimal 2-char diff); character classes, length bounds and `.match`
  call sites unchanged. Trailing LF in tool names / chat ids now rejected.
- **Parent verification (independent):** full diff read; suite re-run
  **360 passed, 1 skipped** (306 + 54 new, zero regressions); parent probe
  `cache/scratch/verify_names_parent.py` — **16 assertions pass**: trailing LF
  rejected (tool + chat id), valid names accepted, leading LF/trailing space/CRLF
  rejected, length boundaries (64/65, 128/129) unchanged; static greps clean.
- **RED evidence:** new file first run 5 failed / 49 passed (the 49 are invariant
  guards); the 5 trailing-newline acceptances failed for the right reason.
- **Note:** scan-report cites pre-F-09/F-10 line numbers (protocol.py:13/37,
  ws.py:308/315); current HEAD lines are 16/41 and 332/340 — mechanism identical.

## Backlog empty — close-out
All 11 items addressed: F-01..F-09 + F-11 fixed; F-10 mitigated (honest partial,
residual trust boundary documented in auth-trust-boundary.md).

### Claims pass + release metadata (06fa627)
- Manifest/doc consistency: declared `allow_keyless_local` in `config_schema`;
  `auth_token` description reflects the gateway-key precedence; `_rate_check`
  docstring covers `doc.list`/`doc.open`; `remote-access.md` locality table +
  heuristic caveat cross-reference `auth-trust-boundary.md`.
- `CHANGELOG.md` added (all 11 items, F-10 honest partial); version 0.1.0 → 0.2.0.
- Overclaim grep over code/manifest/docs: clean (the F-02 "DENY enforced"
  docstring was already corrected to advisory-unless-block).

### Independent read-only review of HEAD (fresh subagent, no repo writes)
- **Verdict:** all F-01..F-11 confirmed present AND correct in current source,
  each re-probed via stdin (zero-fetch SSRF probe, args-budget, caps/eviction,
  grant narrowing, 2000 retained subagent marks, audit 0600 under umask 0,
  keyed-gateway keyless refusal, trailing-LF rejection). F-10 residual doc
  cross-checked against the installed gateway — honest, no overclaim. Suite
  360 passed, 1 skipped. No fix absent/incorrect/partial beyond the ledger.
- **One actionable LOW new issue + informational items found** → fixed below.

### Review follow-ups (this commit)
- **LOW (fixed): `allow_keyless_local` string fail-open.** `authorize` tested raw
  truthiness; a quoted YAML `"false"` is truthy → keyless owner stayed ON.
  `settings.load` now coerces bool-typed settings (`_as_bool`: true/false-ish
  strings + 0/1; unrecognized → loud warning + declared default). Fixes the new
  switch AND the pre-existing `allow_remote`/`owner_key_remote` latent pattern.
  Production path confirmed: `register()` → `settings.load(ctx.get_config)`
  (`__init__.py:82`). TDD: `tests/test_settings_bool.py` (21 tests, RED 17 fail →
  GREEN). Parent probe `cache/scratch/verify_bool_parent.py` — 8 assertions pass
  (quoted "false" → 403; "true" admits; allow_remote/owner_key_remote coerced).
- **INFO (fixed): dir-heavy walk.** `doc.list` now bounds directory visits as well
  as files (`dirs_visited` vs `LIST_WALK_LIMIT`); file-cap semantics unchanged.
  Test `test_listing_walk_bounds_directories`.
- **INFO (fixed): `tokens.py` regex consistency.** `TOKEN_RE`/`USER_RE` `$` → `\Z`
  (F-11 class; practically unexploitable — Bearer strips, headers carry no LF).
  Tests `tests/test_review_followups.py`.
- **INFO (documented): F-05 in-flight-command window.** A narrowed user's
  in-flight `//` command keeps editing via `doc_for_tool` until turn end
  (authorized at start; no new client write path). Added to F-05 residuals;
  "fully revoked" wording qualified. Behavior kept by owner decision 19 Nov 2026
  (mid-turn abort would be a functional change) — recorded below.
- **Doc nits fixed:** `auth-trust-boundary.md` stale "docs follow-up" (now
  declared + coerced); CHANGELOG F-07 direction ("to the model"); this close-out
  entry added.
- **Suite after follow-ups: 385 passed, 1 skipped** (360 + 21 bool + 1 dir + 3
  tokens), zero regressions; static greps clean.

### F-05 in-flight window — owner decision (19 Nov 2026)
- **Decision (Autumn):** keep the behavior as-is — a mid-turn grant narrowing does
  NOT abort an in-flight `//` command; the turn finishes (authorized at start).
  Document it as decided behavior, not an open question.
- **Documentation (4abf966):** `doc_for_tool` docstring now states the authorization
  model and the decided window (client writes fully revoked; tool path continues to
  `end_command`); CHANGELOG gains a "Known limitations" section carrying this decision,
  the F-10 operator-owned residuals and the ~20 s revocation latency; CHANGELOG suite
  count refreshed (360 → 386).
- **Preservation test:** `test_inflight_command_completes_after_midturn_narrowing`
  (tests/test_notebook_grants.py) pins the decision — mid-turn narrowing refuses
  `get()` with `not_allowed`, `doc_for_tool`/`tool_read`/`tool_edit` still work and
  write to disk, `end_command` closes the window (`no_command`). Guards against a
  future silent behavior change in either direction.
- **Verification:** suite re-run twice, identical counts — **386 passed, 1 skipped**
  (385 + 1 preservation test), zero regressions. No behavior change in this commit.
