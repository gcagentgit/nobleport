# Governance checker

Canonical, repository-appropriate **static** enforcement of NoblePort's
governance invariants. Implemented in `governance_checker.py`, exercised by
`tests/test_governance_checker.py`, and wired into CI via
`.github/workflows/governance-checker.yml`.

## Status

**REPOSITORY VALIDATION REQUIRED (current).**

- IMPLEMENTED — complete.
- REPOSITORY VALIDATION REQUIRED — current gate. Advances to
  **FIRST SUCCESSFUL CI RUN** only when the `Governance checker` workflow
  completes successfully on GitHub Actions for this branch/PR.
- Later gates (branch protection, human-approval drill, deploy, production
  active) are **not** enabled by this change.

## Rules

| ID     | Invariant (static analogue) |
|--------|------------------------------|
| GOV001 | STRUCTURALBLOCK actions stay blocked — no code that disables/removes/bypasses a structural block. |
| GOV002 | Guarded actions require human approval — no `human_approval=False` / `auto_approve=True` style bypass. |
| GOV003 | No direct agent-to-agent path that bypasses the supervisor/router. |
| GOV004 | No autonomous CEO/treasury/mainnet authority claims (authority noun + autonomy qualifier on one line). |
| GOV005 | Machine-readable `verified`/`live` claims must carry an evidence reference. |
| GOV000 | Fail-closed sentinel — any checker error while scanning a file is itself a finding. |

## Behaviour

- **Output:** one finding per line, `RULE_ID<TAB>path:line<TAB>message`.
- **Exit code:** `0` = clean; `2` = one or more findings, or an internal
  error (fails closed).
- **Scope:** code rules (GOV001–GOV004) apply to first-party `.py` source
  only; GOV005 applies to first-party `.json`. Documentation, tests,
  generated, and vendored content are excluded to avoid false positives, as
  is the checker module and its own test module.

## Running locally

```bash
pytest -q tests/test_governance_checker.py
python governance_checker.py --root .
```

Local success does **not** constitute CI success; the evidence to advance the
gate is a green `Governance checker` run on GitHub Actions.
