# Refactor for human audit — campaign log

Branch: `refactor/human-audit` (from main at merge `27c07ba`, the 0.2.0 security
remediation). Method: the `refactor-for-human-audit` skill — behavior-preserving,
TDD-gated, suite green at every step, docs/diagrams treated as claims.

Baseline at branch start: **386 passed, 1 skipped**
(`.venv/bin/python -m pytest tests/ -q`). Core: 3,260 lines / 14 modules,
doc+comment density ~14%.

## Plan (skill procedure)

1. AUDIT.md map + mermaid diagrams (trust boundary, sequences, activity),
   anchors verified against current code.
2. Cohesion restructure — security concerns into named units; behavior-preserving;
   re-exports keep public names stable; suite green throughout.
3. Document to standard — module docstrings (responsibility / boundary / what it
   deliberately does NOT do), actor-input-control-sink on security functions,
   fail-loud contracts.
4. Claims pass — every claim and diagram node/edge vs every implementing path;
   overclaim grep adjudicated.
5. Wire enforcement — docs-audit CI (anchor freshness, mermaid parse, hub
   pointers), AGENTS.md + CLAUDE.md, inline pointers, required status check.

Then: whole-project evaluation, skill improvement if gaps surfaced, rerun of the
improved deltas.

## Log

### Step 1 — AUDIT.md (dispatched)
- Delegation `deleg_af714b4c`: documentation-only, all 14 modules + plugin.yaml +
  security docs read; anchors must resolve at current line numbers; honesty rules
  (config-named enforcement claims, no unqualified overclaim words, mitigated ≠
  fixed). Child self-checks anchors with its own probe; parent re-verifies.

### Step 1 — verified + committed
- Child: AUDIT.md (355 lines), 6 sections, 6 mermaid diagrams, 126 anchor
  occurrences; self-reported all anchors resolve, all diagrams render, 5
  overclaim hits adjudicated/reworded. One self-correction worth noting: the
  trust-boundary diagram originally drew notebook_edit through get(); the child
  caught that the tool path uses doc_for_tool (the decided F-05 window) and fixed
  the edge before reporting.
- **Parent verification (independent):** probe
  `cache/scratch/verify_audit_map_parent.py` — **233 assertions pass**: 107
  unique anchors resolve (bare diagram names included) + 17 semantic spot-checks
  (cited ranges actually contain the claimed functions). One probe failure was
  the parent's own needle (literal "WARNING" vs code's `log.warning`) — doc
  correct, needle fixed. All 6 mermaid blocks re-rendered by the parent
  (mmdc, --no-sandbox puppeteer config; host AppArmor blocks the default
  sandbox). Overclaim grep: zero hits. Auth region (ws.py:28-143) and review
  gate (dispatch.py:95-140) read directly — diagrams (c) and (d) match the code.
- Working tree: only AUDIT.md + this log added; no .py touched.
- Commit: `4f14540` (AUDIT.md + this log).

### Step 2 — cohesion restructure (dispatched)
- Delegation `deleg_a5f6ae3d`, design decided by parent after reading the auth
  region, the review gate, and the test monkeypatch targets:
  1. `auth.py` ← ws.py auth region (28-143); ws re-exports every moved name.
  2. `notebook_tools.py` ← notebook tool surface (doc_for_tool/tool_read/
     tool_edit/apply_edits/schemas); module-attribute access for patchable
     helpers; notebook re-exports; one-way dependency.
  3. `audit.py` absorbs dispatch._save_capture (capture = audit artifact).
  4. `authz.py` ← platform admin scope, CONDITIONAL on dependency cleanliness.
  5. registry subagent gate stays (small, state-entangled) — banner + docstring.
  6. Module docstrings + Audit pointers + section banners across outrigger/.
  7. AUDIT.md anchors/units updated to the post-move layout in the same tree.
  8. tests/test_cohesion.py first (RED), then moves (GREEN): re-export identity,
     import boundaries, pointer presence.
- Compatibility pre-checked by parent: no test monkeypatches moved auth names;
  notebook/platform patch targets stay on their origin modules; __init__ tool
  wiring rides the re-exports.

### Step 2 — verified + committed
- Child report: 393 passed / 1 skipped twice (386 + 7 cohesion), RED evidence
  (ImportError before moves), 126 anchor occurrences remapped, 6/6 diagrams
  render, authz move DECLINED (toolsets_for_user reads platform CHATS — live
  chat state, per the conditional rule it stays under a named banner).
- **Deviations adjudicated (all correct):**
  1. Parent's "no monkeypatch of moved auth names" premise was WRONG —
     test_lifecycle.py:37,46 + test_tokens.py:70 patch wsmod._is_loopback;
     3 lines retargeted to authmod (sanctioned exception; verified correct:
     auth._is_local resolves _is_loopback via auth globals).
  2. dispatch._save_capture re-export required (test_audit_hardening +
     test_fuzz call it) — verified present and identical.
  3. notebook re-exports via lazy PEP 562 __getattr__ (honors the one-way
     load dependency) — identity AND from-import spelling parent-verified.
  4. authz kept in platform.py — correct per the conditional rule.
- **Parent verification (independent):**
  `cache/scratch/verify_cohesion_parent.py` — **48 assertions pass**: moved
  bodies ast-identical to HEAD (auth verbatim, capture verbatim,
  notebook_tools modulo notebook. qualification); re-export identity incl.
  PEP 562 from-import; patchability across boundaries (notebook._session_chat
  patch drives notebook_tools.doc_for_tool; auth._is_loopback patch drives
  ws.authenticate); keyless-local refusal 403 through BOTH spellings +
  admission when allowed; import boundaries (auth⊄ws, notebook⊅notebook_tools
  at load, audit⊄dispatch).
  `cache/scratch/verify_anchors_postmove_parent.py` — **250 assertions pass**:
  107 unique anchors resolve; semantic spot-checks at new locations; moved
  functions no longer defined in their origin modules.
  Suite re-run twice by parent: 393 passed, 1 skipped, identical. All 6
  mermaid blocks re-rendered from the updated AUDIT.md. Overclaim grep clean.
  Fence check: docs/security-review/, CHANGELOG, plugin.yaml, .github/,
  requirements-dev.txt untouched.
- Commit: `dec4c17` (moves + docstrings/pointers + AUDIT.md remap + cohesion
  tests + 3 retargeted monkeypatch lines + this log).

### Step 3 — document to standard (dispatched)
- Delegation `deleg_0bee4a5d`: docstrings/comments ONLY, zero code changes,
  proven mechanically (ast.dump identity with docstrings stripped, HEAD vs
  working tree, every modified file). Function-level actor/input/control/sink +
  invariant + fail-loud contracts across all security functions; concise audit
  prose, no padding, existing honest content preserved (decided F-05 window,
  F-10 heuristic caveats). AUDIT.md anchors remapped in the same tree
  (docstring lines shift anchors — the freshness mechanism as designed).

### Step 3 — verified + committed
- Child: 84 functions documented (85 edits), 15/15 ast-identity, 393×2,
  212 anchor occurrences remapped, 6/6 renders, SyntaxWarning-clean.
- **Parent verification (independent):** own ast-identity probe
  (`verify_docstring_pass_parent.py`, NodeTransformer docstring strip) —
  **15/15 modules identical**; suite 393 passed ×2; compile clean under
  -W error::SyntaxWarning; 250 anchor assertions pass; gate script hard PASS;
  6/6 diagrams re-rendered; docstring spot-reads (authenticate, get,
  mark_subagent) honest and code-matching; fences clean.
- Commit: `9050391`.

### Step 4 — claims pass (in progress)
- README.md:66 metaphor reworded ("cannot go" → "doesn't reach") — the only
  overclaim-grep hit on shipping surfaces; keeps the strict gate clean.
- Delegation `deleg_489c616a`: fresh READ-ONLY claims reviewer — every AUDIT.md
  table row / diagram node-edge / failure contract / residual, all 84 docstrings
  (config defaults vs settings.DEFAULTS + plugin.yaml, log levels, invariants,
  actor/sink), cross-item interactions, manifest/README claims, overclaim
  adjudication incl. docstrings, 20 highest-risk anchor semantics. Decided
  behaviors (F-05 window, F-10 residuals, advisory deny, ~20s latency) fenced
  against false findings. Parent adjudicates + fixes; parent also spot-checks
  high-risk claims independently.

### Step 4 — claims pass: findings, adjudications, fixes
- Reviewer (`deleg_489c616a`, read-only): verdict **findings** — 11 total
  (2 OVERCLAIM, 1 STALE, 8 NIT), zero WRONG. Verified correct: all 6 diagrams'
  nodes/edges vs real call order, 38 table rows, 13 failure contracts, 28 config
  defaults vs settings.DEFAULTS + plugin.yaml, 110 anchors in-bounds, 20/20
  highest-risk anchor semantics, re-export identity, subagent denial in both
  dispatch and notebook_tools, decided behaviors fenced. Overclaim grep: all
  hits adjudicated keep (qualified/config-named/metaphor/identifier).
- **Adjudications:**
  - F-01 (tokens._load non-dict JSON raises, not empty-store) and F-02
    (capture-write OSError skips audit.record): real behavior gaps, but both
    still fail CLOSED and loud. Fixing them is a functional change → NOT done
    in this refactor (skill rule: refactor ≠ remediate; owner directive:
    functional changes to discuss). Docstrings/AUDIT.md reworded to the true
    behavior; both logged as hardening follow-ups for the next remediation
    pass (AUDIT.md residuals + this log). **Owner decision pending.**
  - F-03..F-11 (principal=None wording, stale README status block, diagram (a)
    count_call ordering + omitted decl lookup, ingress coerce-vs-sanitize,
    settings.py misplaced comments, "never logs headers" vs bounded proxy-peer
    logging, "every client operation" vs close, allowed_roots "never sees" vs
    broadcast residual, README key-source omission): all fixed as doc/comment/
    README/diagram corrections. README also refreshed: test command now uses
    requirements-dev.txt; first-client line now present tense (Chromium+Firefox).
- **Parent verification:** 16 patches applied; ast-identity 6/6 modified modules
  (docstrings/comments only — settings comment move included); suite 393×2;
  256 anchor assertions (110 unique); strict overclaim gate PASS (one new
  "safety net" metaphor in the residual bullet reworded to keep the gate clean);
  all 6 diagrams re-rendered incl. the corrected sequence.
- **Hardening follow-ups for the next remediation pass (functional, owner call):**
  1. Guard `_save_capture` OSError → image_error field so the call completes
     audited (closes the audit-trail escape).
  2. `tokens._load`: isinstance(data, dict) guard so non-dict JSON takes the
     documented empty-store path.
- Commit: `5aa496f`.

### Step 5 — wire enforcement (in progress)
- Repo files placed: `scripts/audit_anchor_check.py` (adapted: security-module
  list post-restructure, conditional authz handling, bare-name anchor resolution,
  shipping-surface overclaim globs — docs/security-review/ EXEMPT as frozen scan
  evidence), `.github/workflows/docs-audit.yml` (python 3.14 + node 22, anchor
  gate --strict-overclaim, mermaid-cli render with --no-sandbox puppeteer config),
  `AGENTS.md` + `CLAUDE.md` (identical substance; doc-gate rule, same-PR doc
  update rule, refactor-vs-remediate, frozen-evidence rule).
- Gate proven both ways: PASS on the real tree (110 anchors, 12 pointers, 0
  overclaims); negative self-test with a broken anchor (auth.py:99999) FAILs as
  designed — the gate is not a rubber stamp.
- Inline hub pointers already present in every security module (step 2/3).
- Remaining: commit, push, PR, both CI checks green, then branch protection
  requiring "test" + "docs-audit" (main currently unprotected).
