# Agent rules — Outrigger (Hermes gateway plugin)

## Audit hub
`AUDIT.md` is the single audit map: trust boundaries, entry points → controls →
sinks, key workflow/boundary diagrams, and the security-sensitive units. Read it
before changing any security-sensitive code. Every security module carries an
inline `# Audit: see AUDIT.md#...` header comment pointing at its section.

## Release is gated on documentation freshness
The `docs-audit` CI check must pass before merge (it is a required status check):
- every `AUDIT.md` `file:line` anchor resolves to real code (code that moves
  breaks the anchor — update the doc in the same PR);
- every mermaid diagram renders;
- no unqualified overclaim words in AUDIT.md / README.md / plugin.yaml;
- every security-sensitive module keeps its inline AUDIT.md pointer.

## Changing a security-sensitive unit
Any change to authentication (`auth.py`), authorization/grants (`notebook.py`,
`platform.py` admin scope, `tokens.py` scopes), input validation
(`protocol.py`), audit (`audit.py`), approval/review (`reviewer.py`), the
subagent gate (`registry.py`), or trust settings (`settings.py`) MUST update its
AUDIT.md section and diagrams in the SAME PR, and keep the claims honest: a claim
about enforcement names the config that enables it (default vs opt-in). Do not
assert enforcement the default config does not provide (reviewer deny is
advisory unless `review_deny=block`; keyless-local owner trust is a heuristic,
not verified peer identity).

## Refactor vs remediate
Readability refactors are behavior-preserving and separate from security fixes.
Never mix a refactor with a security fix in one diff (it invalidates a pinned
security-scan snapshot). Security findings go through the remediation workflow
on their own branch. `docs/security-review/` is frozen scan evidence — do not
rewrite it; new security work gets new documents.

## Tests
`.venv/bin/python -m pytest tests/ -q` (fresh env: `pip install -r
requirements-dev.txt`). The suite is the behavior-preservation safety net for
refactors; run it twice and compare counts when adding test files.
