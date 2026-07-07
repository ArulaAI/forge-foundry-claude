"""FM9 test-coverage theater detector.

Two-pass strategy:
1. Regex (from production_rules.yaml) for assert True / 1==1 / not False
   tautologies — applies only to test files (test_*.py / *_test.py).
2. AST pass: find `def test_*` functions with zero ast.Assert nodes in their
   body and emit a medium finding for each.

Only runs when config.production.fm9_test_theater.enabled is True.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from primitives._shared.scan_exclusions import is_excluded
from primitives.production_failure_audit.shared.findings import Finding, Severity

if TYPE_CHECKING:
    from primitives.failure_mode_audit import AuditConfig

# The FM6-FM10 rule pack is the canonical source shared by both paths of this
# hybrid primitive: the in-session skill body (skill/SKILL.md cites references/) and
# this CI Python. It lives under skill/references/ so the one rule home serves both.
_RULES_PATH = Path(__file__).parent.parent / "skill" / "references" / "production_rules.yaml"
assert _RULES_PATH.is_file(), f"production_rules.yaml not found at {_RULES_PATH}"


def _load_rules() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8"))
    return raw


def _is_test_path(p: Path) -> bool:
    name = p.name
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py")


def _files_under(
    path: Path, glob_patterns: list[str], *, include_vendored: bool = False
) -> list[Path]:
    if path.is_file():
        return [path] if any(path.match(g) for g in glob_patterns) else []
    files: list[Path] = []
    for g in glob_patterns:
        files.extend(
            p
            for p in path.rglob(g)
            if p.is_file() and not is_excluded(p, path, include_vendored=include_vendored)
        )
    return files


def _find_zero_assert_tests(file: Path) -> list[Finding]:
    """For each def test_* in file, check if any ast.Assert is in its body."""
    findings: list[Finding] = []
    try:
        source = file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file))
    except (SyntaxError, UnicodeDecodeError, PermissionError):
        return findings
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        has_assert = any(isinstance(n, ast.Assert) for n in ast.walk(node))
        if not has_assert:
            findings.append(
                Finding(
                    mode="fm9",
                    severity="medium",
                    file=str(file),
                    line=node.lineno,
                    message=(
                        f"Test function `{node.name}` has no assert"
                        " — possible test-coverage theater"
                    ),
                    snippet=f"def {node.name}(...)",
                    rule_id="fm9.zero-assert-test",
                    cwe_id="CWE-1100",
                )
            )
    return findings


def audit_fm9(path: Path, *, config: AuditConfig) -> list[Finding]:
    """Audit path for FM9 test-coverage theater findings. Returns list (empty if disabled).

    Strategy:
        1. Check config.production.fm9_test_theater.enabled — return [] if False.
        2. Load production_rules.yaml; apply regex detectors to test files only.
        3. Run AST pass on all test_*.py / *_test.py files; flag zero-assert functions.
    """
    if not config.production.fm9_test_theater.enabled:
        return []
    findings: list[Finding] = []
    rules = _load_rules()
    mode_rules = rules.get("fm9_test_theater", {})

    # 1. Regex tautologies (only apply to test files)
    for detector in mode_rules.get("detectors", []):
        for pat in detector.get("patterns", []):
            file_patterns = pat.get("file_patterns") or detector.get("file_patterns", ["*"])
            regex = re.compile(pat["regex"], re.MULTILINE)
            cwe = pat.get("cwe_id")
            kind = detector.get("kind", "regex-rule")
            severity: Severity = pat["severity"]
            for f in _files_under(path, file_patterns, include_vendored=config.include_vendored):
                if not _is_test_path(f):
                    continue
                try:
                    content = f.read_text(encoding="utf-8")
                except (UnicodeDecodeError, PermissionError):
                    continue
                for line_num, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        findings.append(
                            Finding(
                                mode="fm9",
                                severity=severity,
                                file=str(f),
                                line=line_num,
                                message=pat["msg"],
                                snippet=line.strip(),
                                rule_id=f"fm9.{kind}",
                                cwe_id=cwe,
                            )
                        )

    # 2. AST: zero-assert tests in any test_*.py file
    test_files = [
        p
        for p in _files_under(path, ["*.py"], include_vendored=config.include_vendored)
        if _is_test_path(p)
    ]
    for f in test_files:
        findings.extend(_find_zero_assert_tests(f))

    return findings
