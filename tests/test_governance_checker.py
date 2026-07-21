"""Tests for the canonical governance checker.

Each governance rule is exercised with a positive (violating) fixture and a
negative (clean / correctly-guarded) fixture. We also assert that the checker
passes cleanly on the real repository, that findings carry rule id + file +
line, that documentation/tests/generated/vendor content is exempt, and that
the checker fails closed on unreadable input.

Fixtures are written into a temporary tree and the checker is pointed at
explicit relative paths, so classification (docs/tests/vendor/generated vs.
first-party source) is exercised directly. This test module is excluded from
the checker's own repository scan, so its rule literals do not self-trigger.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import governance_checker as gc


def _write(tmp_path: Path, rel: str, text: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


def _ids(findings) -> list[str]:
    return [f.rule_id for f in findings]


# --------------------------------------------------------------------------- #
# GOV001 STRUCTURALBLOCK
# --------------------------------------------------------------------------- #
def test_gov001_flags_unblocking(tmp_path):
    rel = _write(tmp_path, "app/policy.py", "structural_block = False\n")
    findings = gc.check_paths(tmp_path, [rel])
    assert "GOV001" in _ids(findings)
    assert findings[0].line == 1


def test_gov001_flags_disable_verb(tmp_path):
    rel = _write(tmp_path, "app/policy.py", "def go():\n    disable_structural_block()\n")
    findings = gc.check_paths(tmp_path, [rel])
    assert "GOV001" in _ids(findings)
    assert any(f.line == 2 for f in findings if f.rule_id == "GOV001")


def test_gov001_clean_when_block_enforced(tmp_path):
    rel = _write(tmp_path, "app/policy.py", "structural_block = True\nraise PermissionError('blocked')\n")
    assert gc.check_paths(tmp_path, [rel]) == []


# --------------------------------------------------------------------------- #
# GOV002 guarded actions require human approval
# --------------------------------------------------------------------------- #
def test_gov002_flags_disabled_approval(tmp_path):
    rel = _write(tmp_path, "app/actions.py", "run_guarded(action, human_approval=False)\n")
    assert "GOV002" in _ids(gc.check_paths(tmp_path, [rel]))


def test_gov002_flags_auto_approve(tmp_path):
    rel = _write(tmp_path, "app/actions.py", "execute(auto_approve=True)\n")
    assert "GOV002" in _ids(gc.check_paths(tmp_path, [rel]))


def test_gov002_clean_when_approval_required(tmp_path):
    rel = _write(tmp_path, "app/actions.py", "run_guarded(action, human_approval=True)\n")
    assert gc.check_paths(tmp_path, [rel]) == []


# --------------------------------------------------------------------------- #
# GOV003 no direct agent-to-agent bypass
# --------------------------------------------------------------------------- #
def test_gov003_flags_bypass_supervisor(tmp_path):
    rel = _write(tmp_path, "app/agent/x.py", "bypass_supervisor(target_agent, msg)\n")
    assert "GOV003" in _ids(gc.check_paths(tmp_path, [rel]))


def test_gov003_flags_agent_to_agent(tmp_path):
    rel = _write(tmp_path, "app/agent/x.py", "agent_to_agent(a, b)\n")
    assert "GOV003" in _ids(gc.check_paths(tmp_path, [rel]))


def test_gov003_clean_when_routed(tmp_path):
    rel = _write(tmp_path, "app/agent/x.py", "router.dispatch(target_agent, msg)\n")
    assert gc.check_paths(tmp_path, [rel]) == []


# --------------------------------------------------------------------------- #
# GOV004 no autonomous CEO/treasury/mainnet authority
# --------------------------------------------------------------------------- #
def test_gov004_flags_autonomous_treasury(tmp_path):
    rel = _write(tmp_path, "app/exec.py", "autonomous_treasury_transfer = True\n")
    assert "GOV004" in _ids(gc.check_paths(tmp_path, [rel]))


def test_gov004_flags_mainnet_without_approval(tmp_path):
    rel = _write(tmp_path, "app/exec.py", "sign_mainnet(tx, without_approval=True)\n")
    assert "GOV004" in _ids(gc.check_paths(tmp_path, [rel]))


def test_gov004_clean_mention_only(tmp_path):
    # A benign mention of treasury without an autonomy qualifier must not fire.
    rel = _write(tmp_path, "app/exec.py", "treasury_report = build_report()\n")
    assert gc.check_paths(tmp_path, [rel]) == []


# --------------------------------------------------------------------------- #
# GOV005 verified/live claims need evidence
# --------------------------------------------------------------------------- #
def test_gov005_flags_verified_without_evidence(tmp_path):
    rel = _write(tmp_path, "data/claims.json", '{"status": "verified", "value": 42}\n')
    assert "GOV005" in _ids(gc.check_paths(tmp_path, [rel]))


def test_gov005_flags_live_true_without_evidence(tmp_path):
    rel = _write(tmp_path, "data/claims.json", '{"live": true}\n')
    assert "GOV005" in _ids(gc.check_paths(tmp_path, [rel]))


def test_gov005_clean_with_evidence(tmp_path):
    rel = _write(
        tmp_path, "data/claims.json",
        '{"status": "verified", "evidence": "https://ci/run/123"}\n',
    )
    assert gc.check_paths(tmp_path, [rel]) == []


def test_gov005_clean_non_claim_json(tmp_path):
    rel = _write(tmp_path, "data/claims.json", '{"status": "issued", "total": 5}\n')
    assert gc.check_paths(tmp_path, [rel]) == []


# --------------------------------------------------------------------------- #
# Context-class exemptions (no false positives)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rel",
    [
        "docs/notes.md",
        "reports/weekly/report.md",
        "tests/test_example.py",
        "vendor/lib/thing.py",
        "app/gen/schema.generated.py",
    ],
)
def test_exempt_paths_do_not_trigger_code_rules(tmp_path, rel):
    _write(tmp_path, rel, "structural_block = False\nagent_to_agent(a, b)\n")
    assert gc.check_paths(tmp_path, [rel]) == []


def test_comment_lines_are_exempt(tmp_path):
    rel = _write(tmp_path, "app/policy.py", "# structural_block = False is forbidden\n")
    assert gc.check_paths(tmp_path, [rel]) == []


# --------------------------------------------------------------------------- #
# Finding shape + fail-closed behaviour
# --------------------------------------------------------------------------- #
def test_finding_reports_rule_file_and_line(tmp_path):
    rel = _write(tmp_path, "app/policy.py", "x = 1\nstructural_block = False\n")
    findings = gc.check_paths(tmp_path, [rel])
    f = next(f for f in findings if f.rule_id == "GOV001")
    assert f.path == rel
    assert f.line == 2
    assert "GOV001\t" in f.format() and ":2\t" in f.format()


def test_fails_closed_on_bad_json(tmp_path):
    rel = _write(tmp_path, "data/claims.json", "{not valid json")
    findings = gc.check_paths(tmp_path, [rel])
    assert "GOV000" in _ids(findings)


def test_main_returns_nonzero_on_violation(tmp_path, capsys):
    _write(tmp_path, "app/policy.py", "structural_block = False\n")
    rc = gc.main(["--root", str(tmp_path), "app/policy.py"])
    assert rc == 2


def test_main_passes_clean_tree(tmp_path, capsys):
    _write(tmp_path, "app/ok.py", "structural_block = True\n")
    rc = gc.main(["--root", str(tmp_path), "app/ok.py"])
    assert rc == 0


# --------------------------------------------------------------------------- #
# The real repository must be clean.
# --------------------------------------------------------------------------- #
def test_repository_is_clean():
    root = Path(__file__).resolve().parents[1]
    findings = gc.run(root)
    assert findings == [], "\n".join(f.format() for f in findings)
