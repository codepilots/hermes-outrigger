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
