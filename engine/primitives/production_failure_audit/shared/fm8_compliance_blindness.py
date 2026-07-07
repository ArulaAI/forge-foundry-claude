"""FM8 compliance-blindness detector.

Reads fm8_compliance_blindness detectors from production_rules.yaml and emits
a Finding for every line that matches a pattern regex.

When config.production.fm8_compliance_blindness.warn_only is True (the
default until S70 ships) all finding severities are downgraded to "low" so
they do not trip --max-severity gates.
"""

from __future__ import annotations

import dataclasses
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


def audit_fm8(path: Path, *, config: AuditConfig) -> list[Finding]:
    """Audit path for FM8 compliance-blindness findings. Returns list (empty if disabled).

    Strategy:
        1. Check config.production.fm8_compliance_blindness.enabled — return [] if False.
        2. Load production_rules.yaml; iterate fm8_compliance_blindness.detectors.
        3. For each file matching file_patterns, apply regex; emit Finding per match.
        4. If warn_only is True, downgrade all finding severities to "low".
    """
    fm8_cfg = config.production.fm8_compliance_blindness
    if not fm8_cfg.enabled:
        return []
    rules = _load_rules()
    mode_rules = rules.get("fm8_compliance_blindness", {})
    findings: list[Finding] = []
    for detector in mode_rules.get("detectors", []):
        patterns = detector.get("patterns", [])
        for pat in patterns:
            file_patterns = pat.get("file_patterns") or detector.get("file_patterns", ["*"])
            regex = re.compile(pat["regex"])
            cwe = pat.get("cwe_id")
            kind = detector.get("kind", "regex-rule")
            rule_id = f"fm8.{kind}"
            severity: Severity = pat["severity"]
            for f in _files_under(path, file_patterns, include_vendored=config.include_vendored):
                try:
                    content = f.read_text(encoding="utf-8")
                except (UnicodeDecodeError, PermissionError):
                    continue
                for line_num, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        findings.append(
                            Finding(
                                mode="fm8",
                                severity=severity,
                                file=str(f),
                                line=line_num,
                                message=pat["msg"],
                                snippet=line.strip(),
                                rule_id=rule_id,
                                cwe_id=cwe,
                            )
                        )
    if fm8_cfg.warn_only:
        findings = [dataclasses.replace(fi, severity="low") for fi in findings]
    return findings
