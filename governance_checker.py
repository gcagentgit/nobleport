#!/usr/bin/env python3
"""Canonical governance checker for the NoblePort supervisor stack.

This is the *repository-appropriate static* enforcement of NoblePort's
governance invariants. It scans tracked source and machine-readable files and
fails closed (exit code 2) if any rule is violated, printing one finding per
line as ``RULE_ID<TAB>path:line<TAB>message`` so both humans and CI can parse
it.

The checker enforces static analogues of five governance invariants:

  GOV001  STRUCTURALBLOCK actions stay blocked.
  GOV002  Guarded actions require explicit human approval.
  GOV003  No direct agent-to-agent bypass of the supervisor / router.
  GOV004  No autonomous CEO / treasury / mainnet authority claims.
  GOV005  Machine-readable "verified"/"live" claims need evidence references.

Design constraints:

  * Deterministic. Same input -> same findings, in a stable order.
  * Fail closed. Any unexpected error while scanning a file is itself a
    finding (GOV000) and forces a non-zero exit; the checker never passes by
    accident.
  * False-positive avoidance. Documentation, tests, generated, vendored, and
    the checker's own fixture content are excluded from code-intent rules.
    This file excludes itself so its rule literals do not self-trigger.

The checker is intentionally dependency-free (standard library only) so it can
run in CI without network access, mirroring the rest of the repo.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Finding:
    rule_id: str
    path: str
    line: int
    message: str

    def format(self) -> str:
        return f"{self.rule_id}\t{self.path}:{self.line}\t{self.message}"


# --------------------------------------------------------------------------- #
# Path classification
# --------------------------------------------------------------------------- #
# Directories/paths whose *content* is not agent-governed code and therefore is
# excluded from the code-intent rules (GOV001-GOV004). Reports and docs
# describe the system; they are allowed to mention blocked actions in prose.
_DOC_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}
_EXCLUDED_DIR_PARTS = {
    "vendor",
    "node_modules",
    "third_party",
    "site-packages",
    ".git",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}
_GENERATED_MARKERS = (".min.", ".generated.", ".pb.", "_pb2.")

# The checker itself and its dedicated fixtures embed rule literals; scanning
# them would be circular. They are validated by the test-suite instead.
_SELF_EXCLUDED = {
    "governance_checker.py",
    "tests/test_governance_checker.py",
}
_FIXTURE_DIR_PART = "governance_fixtures"


def _parts(rel_path: str) -> list[str]:
    return rel_path.replace("\\", "/").split("/")


def is_excluded_dir(rel_path: str) -> bool:
    return any(part in _EXCLUDED_DIR_PARTS for part in _parts(rel_path))


def is_generated(rel_path: str) -> bool:
    name = _parts(rel_path)[-1]
    return any(marker in name for marker in _GENERATED_MARKERS)


def is_doc(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in _DOC_SUFFIXES


def is_test(rel_path: str) -> bool:
    parts = _parts(rel_path)
    name = parts[-1]
    return "tests" in parts or name.startswith("test_") or name.endswith("_test.py")


def is_self_or_fixture(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    return norm in _SELF_EXCLUDED or _FIXTURE_DIR_PART in _parts(norm)


def code_rules_apply(rel_path: str) -> bool:
    """Code-intent rules (GOV001-GOV004) apply to first-party source only."""
    if is_excluded_dir(rel_path) or is_generated(rel_path) or is_self_or_fixture(rel_path):
        return False
    if is_doc(rel_path) or is_test(rel_path):
        return False
    return Path(rel_path).suffix.lower() in {".py", ".pyi"}


def machine_readable_rules_apply(rel_path: str) -> bool:
    """GOV005 applies to first-party machine-readable claim files."""
    if is_excluded_dir(rel_path) or is_generated(rel_path) or is_self_or_fixture(rel_path):
        return False
    return Path(rel_path).suffix.lower() == ".json"


# --------------------------------------------------------------------------- #
# GOV001-GOV004: line-oriented code rules
# --------------------------------------------------------------------------- #
# Each rule is a compiled pattern plus a human message. A line that is a pure
# comment is exempt from the code rules: comments document intent and must not
# be able to *perform* a forbidden action.
_COMMENT_LINE = re.compile(r"^\s*#")

# GOV001: turning a structural block off.
_GOV001 = re.compile(
    r"""
    (?:
        structural[_\s]?block\s*=\s*(?:False|0|None|"")   # flag flip
      | (?:disable|remove|bypass|override|unblock|lift)    # verb ...
        [_\s]?structural[_\s]?block                        # ... structural block
      | STRUCTURAL[_]?BLOCK\s*=\s*(?:False|0|None)         # constant flip
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# GOV002: guarded action executed without human approval.
_GOV002 = re.compile(
    r"""
    (?:
        (?:human_approval|requires?_approval|require_human_approval)\s*=\s*False
      | (?:skip_approval|auto_approve|bypass_approval|no_approval)\s*=\s*True
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# GOV003: direct agent-to-agent path that skips supervisor/router.
_GOV003 = re.compile(
    r"""
    (?:
        bypass_(?:supervisor|router)
      | agent[_\s]?to[_\s]?agent
      | direct_agent_(?:call|send|invoke|message)
      | peer_agent_(?:call|send|invoke|message)
      | send_direct_to_agent
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# GOV004: autonomous authority over CEO / treasury / mainnet actions.
# Co-occurrence rule: an authority noun AND an autonomy/no-approval qualifier on
# the same line. This keeps ordinary mentions ("treasury report") from firing.
_GOV004_AUTHORITY = re.compile(r"(?:ceo|treasury|mainnet)", re.IGNORECASE)
_GOV004_AUTONOMY = re.compile(
    r"(?:autonomous|autonomy|unilateral|without_approval|no_approval|"
    r"auto_approve|self_authoriz)",
    re.IGNORECASE,
)


def _scan_code_line(rel_path: str, lineno: int, line: str) -> Iterator[Finding]:
    if _COMMENT_LINE.match(line):
        return
    if _GOV001.search(line):
        yield Finding("GOV001", rel_path, lineno,
                      "STRUCTURALBLOCK action must stay blocked; unblock/disable detected")
    if _GOV002.search(line):
        yield Finding("GOV002", rel_path, lineno,
                      "guarded action requires human approval; approval disabled")
    if _GOV003.search(line):
        yield Finding("GOV003", rel_path, lineno,
                      "direct agent-to-agent path bypasses supervisor/router")
    if _GOV004_AUTHORITY.search(line) and _GOV004_AUTONOMY.search(line):
        yield Finding("GOV004", rel_path, lineno,
                      "autonomous CEO/treasury/mainnet authority claim is not permitted")


def scan_code_file(rel_path: str, text: str) -> Iterator[Finding]:
    for i, line in enumerate(text.splitlines(), start=1):
        yield from _scan_code_line(rel_path, i, line)


# --------------------------------------------------------------------------- #
# GOV005: machine-readable verified/live claims need evidence
# --------------------------------------------------------------------------- #
_CLAIM_KEYS = {"status", "state", "claim", "verification"}
_CLAIM_VALUES = {"verified", "live"}
_TRUTHY_CLAIM_KEYS = {"verified", "live"}
_EVIDENCE_KEYS = {
    "evidence", "evidence_ref", "evidence_refs", "evidence_reference",
    "source", "sources", "reference", "references", "proof", "proofs",
    "citation", "citations", "attestation", "attestations",
}


def _dict_makes_live_claim(obj: dict) -> bool:
    for key, value in obj.items():
        lkey = str(key).lower()
        if lkey in _TRUTHY_CLAIM_KEYS and value is True:
            return True
        if lkey in _CLAIM_KEYS and isinstance(value, str) and value.lower() in _CLAIM_VALUES:
            return True
    return False


def _dict_has_evidence(obj: dict) -> bool:
    for key, value in obj.items():
        if str(key).lower() in _EVIDENCE_KEYS:
            # An empty evidence field is not evidence.
            if value in (None, "", [], {}):
                continue
            return True
    return False


def _walk_json(node: object) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json(item)


def scan_machine_readable_file(rel_path: str, text: str) -> Iterator[Finding]:
    data = json.loads(text)  # a decode error is caught by the caller -> GOV000
    for obj in _walk_json(data):
        if _dict_makes_live_claim(obj) and not _dict_has_evidence(obj):
            yield Finding(
                "GOV005", rel_path, 1,
                "machine-readable verified/live claim lacks an evidence reference",
            )


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #
def list_tracked_files(root: Path) -> list[str]:
    """Return repo-relative paths. Prefer git; fall back to a filesystem walk."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root),
            capture_output=True,
            check=True,
            text=True,
        )
        files = [p for p in out.stdout.split("\0") if p]
        if files:
            return sorted(files)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    files = []
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if not is_excluded_dir(rel):
                files.append(rel)
    return sorted(files)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def check_paths(root: Path, rel_paths: Iterable[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in rel_paths:
        abs_path = root / rel
        try:
            run_code = code_rules_apply(rel)
            run_mr = machine_readable_rules_apply(rel)
            if not (run_code or run_mr):
                continue
            text = abs_path.read_text(encoding="utf-8", errors="strict")
            if run_code:
                findings.extend(scan_code_file(rel, text))
            if run_mr:
                findings.extend(scan_machine_readable_file(rel, text))
        except FileNotFoundError:
            # Listed but absent (e.g. broken symlink); ignore rather than fail.
            continue
        except Exception as exc:  # noqa: BLE001 — fail closed on anything else
            findings.append(
                Finding("GOV000", rel, 1, f"checker error while scanning: {exc!r}")
            )
    findings.sort(key=lambda f: (f.path, f.line, f.rule_id))
    return findings


def run(root: Path, paths: Optional[list[str]] = None) -> list[Finding]:
    rel_paths = paths if paths else list_tracked_files(root)
    return check_paths(root, rel_paths)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="NoblePort canonical governance checker")
    parser.add_argument(
        "--root", default=".", help="repository root to scan (default: cwd)"
    )
    parser.add_argument(
        "paths", nargs="*", help="optional explicit paths; default scans tracked files"
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        findings = run(root, args.paths or None)
    except Exception as exc:  # noqa: BLE001 — fail closed on orchestration errors
        print(f"GOV000\t{args.root}:1\tfatal checker error: {exc!r}", file=sys.stderr)
        return 2

    if findings:
        for f in findings:
            print(f.format())
        print(f"\ngovernance-checker: FAIL ({len(findings)} finding(s))", file=sys.stderr)
        return 2

    print("governance-checker: PASS (0 findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
