"""Regex-based detectors for FM3 (training-data bias) and FM4 (overconfident).

Vendored from forge-prowess-primitives@v0.4.0 (auditor.py:_audit_regex_rules) —
see CHANGELOG-VENDOR.md. Reads patterns from failure_mode_rules.yaml.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from primitives._shared.scan_exclusions import is_excluded
from primitives.failure_mode_audit.shared.findings import Finding, Severity

if TYPE_CHECKING:
    from primitives.failure_mode_audit.shared.config import AuditConfig, Fm5Config, FmConfig

# The FM3/FM4/FM5 rule pack is the canonical source shared by both paths of this
# hybrid primitive: the in-session skill body (skill/SKILL.md cites references/) and
# this CI Python. It lives under skill/references/ so the one rule home serves both.
_RULES_PATH = Path(__file__).parent.parent / "skill" / "references" / "failure_mode_rules.yaml"
assert _RULES_PATH.is_file(), f"failure_mode_rules.yaml not found at {_RULES_PATH}"


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


def _is_mode_enabled(config: AuditConfig, mode: str) -> bool:
    """Return True if *mode* is enabled in *config*."""
    field_map: dict[str, FmConfig | Fm5Config] = {
        "fm3": config.fm3_training_bias,
        "fm4": config.fm4_overconfident,
        "fm5": config.fm5_security_propagation,
    }
    fm = field_map.get(mode)
    return fm is not None and fm.enabled


def audit_regex_mode(
    path: Path,
    *,
    mode: str,
    config: AuditConfig,
) -> list[Finding]:
    """Apply regex-pattern detectors for *mode* (fm3, fm4, or fm5) over *path*."""
    if not _is_mode_enabled(config, mode):
        return []

    rules = _load_rules()
    rule_key_map = {
        "fm3": "fm3_training_bias",
        "fm4": "fm4_overconfident",
        "fm5": "fm5_security_propagation",
    }
    if mode not in rule_key_map or rule_key_map[mode] not in rules:
        return []

    mode_rules = rules[rule_key_map[mode]]
    findings: list[Finding] = []

    for detector in mode_rules.get("detectors", []):
        patterns = detector.get("patterns", [])
        if not patterns:
            continue
        file_patterns = detector.get("file_patterns", ["*"])
        for f in _files_under(path, file_patterns, include_vendored=config.include_vendored):
            try:
                content = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue
            for pat in patterns:
                # Per-pattern file_patterns override (FM4 uses this)
                pat_file_patterns = pat.get("file_patterns")
                if pat_file_patterns and not any(f.match(g) for g in pat_file_patterns):
                    continue
                regex = re.compile(pat["regex"])
                cwe = pat.get("cwe_id")
                rule_kind = detector.get("kind", "regex-rule")
                rule_id = f"{mode}.{rule_kind}"
                severity: Severity = pat["severity"]
                for line_num, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        findings.append(
                            Finding(
                                mode=mode,
                                severity=severity,
                                file=str(f),
                                line=line_num,
                                message=pat["msg"],
                                snippet=line.strip(),
                                rule_id=rule_id,
                                cwe_id=cwe,
                            )
                        )
    return findings
