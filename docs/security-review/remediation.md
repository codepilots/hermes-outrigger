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
| F-01 | SSRF via schema `$ref` (8a3f0a04) + schema-check fail-open (231342bf) | Medium + Low | protocol.py | pending |
| F-02 | Reviewer padded-args truncation (27db6f36) + `</untrusted>` delimiter escape (5a654c56) | Medium + Low | reviewer.py, dispatch.py, plugin.yaml | pending |
| F-03 | Notebook unbounded cache + on-loop `doc.list` DoS (f3441f81) | Medium | notebook.py, ws.py | pending |
| F-04 | Notebook watcher silent exception (245216c2) + `tool_edit` no disk refresh (b26e85aa) | Low + Low | notebook.py | pending |
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
