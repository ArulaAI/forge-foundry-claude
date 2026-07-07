"""C6 dispatcher for C10 (failure_mode_audit) and C11 (production_failure_audit) raw Finding lists.

Both C10 and C11 produce a JSON array of Finding objects (or a dict with a 'findings' list).
Each Finding has: mode, severity, file, line, message, snippet, rule_id, cwe_id.
"""

from __future__ import annotations

from typing import cast

from primitives.confidence_gate.shared.models import NormalizedFinding, ReportType, Severity

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}

# C10 failure mode prefixes
_C10_MODES = {"fm1", "fm2", "fm3", "fm4", "fm5"}
# C11 failure mode prefixes
_C11_MODES = {"fm6", "fm7", "fm8", "fm9", "fm10"}


def _is_finding_list(data: dict[str, object]) -> bool:
    """Return True if data is a findings-list-style report."""
    findings_key = data.get("findings")
    # Also accept a dict with a top-level list under "items"
    return isinstance(findings_key, list)


def _has_mode_field(data: dict[str, object], expected_modes: set[str]) -> bool:
    """Check if the findings list contains items with mode in expected_modes."""
    findings = data.get("findings", [])
    if not isinstance(findings, list) or not findings:
        return False
    for item in findings[:5]:  # check first 5 items
        if isinstance(item, dict):
            mode = str(item.get("mode", "")).lower()
            for expected in expected_modes:
                if mode.startswith(expected):
                    return True
    return False


def sniff_c10(data: dict[str, object]) -> bool:
    """Return True if data looks like a C10 (failure_mode_audit) report."""
    if not _is_finding_list(data):
        return False
    # C10-specific marker
    if data.get("report_type") == "c10_failure_mode_audit":
        return True
    if data.get("primitive") == "c10_failure_mode_audit":
        return True
    return _has_mode_field(data, _C10_MODES)


def sniff_c11(data: dict[str, object]) -> bool:
    """Return True if data looks like a C11 (production_failure_audit) report."""
    if not _is_finding_list(data):
        return False
    # C11-specific marker
    if data.get("report_type") == "c11_production_failure_audit":
        return True
    if data.get("primitive") == "c11_production_failure_audit":
        return True
    return _has_mode_field(data, _C11_MODES)


def _extract_findings(
    data: dict[str, object],
    report_type_str: str,
) -> list[NormalizedFinding]:
    """Shared extraction logic for C10/C11 Finding JSON."""
    results: list[NormalizedFinding] = []
    raw = data.get("findings", [])
    if not isinstance(raw, list):
        return results

    rt = cast("ReportType", report_type_str)
    for item in raw:
        if not isinstance(item, dict):
            continue
        severity = item.get("severity", "")
        if severity not in _VALID_SEVERITIES:
            continue
        results.append(
            NormalizedFinding(
                report_type=rt,
                severity=cast("Severity", severity),
                confidence_level="high",  # regex/rule-based → high confidence
                file=item.get("file") or None,
                line=item.get("line") if isinstance(item.get("line"), int) else None,
                title=str(item.get("rule_id", item.get("mode", ""))),
                description=str(item.get("message", "")),
            )
        )
    return results


def dispatch_c10(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from a C10 failure_mode_audit JSON dict."""
    return _extract_findings(data, "c10_failure_mode_audit")


def dispatch_c11(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from a C11 production_failure_audit JSON dict."""
    return _extract_findings(data, "c11_production_failure_audit")
