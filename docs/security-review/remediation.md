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
| F-05 | Notebook stale root grants (4ea8d7ce) + doc-identity aliasing (539075fd) | Low + Low | notebook.py, ws.py | pending |
| F-06 | Notebook shared-root indirect injection labeling (39fe8161) | Low | notebook.py, dispatch.py | pending |
| F-07 | Unbounded 4 MB error relay (0bf92726) | Low | dispatch.py, ws.py | pending |
| F-08 | Subagent marker eviction fail-open (9c9370fa) | Low | registry.py, __init__.py | pending |
| F-09 | Audit silent loss + identity + tamper (f37f9e4d) + artifact permissions (2eaf3124) + malformed-request unrecorded (6af3ae03) | Inconclusive→fix + Info | audit.py, dispatch.py, notebook.py, ws.py | pending |
| F-10 | Auth locality fail-open (e75c7051) + credential-source mismatch (76d74ef9) + admin-scope mismatch (ed211e05) | Inconclusive→mitigate+residual | ws.py, platform.py, settings.py, docs | pending |
| F-11 | Regex `$` trailing newline (c0efd191) | Informational | protocol.py, ws.py | pending |

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
