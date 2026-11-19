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
