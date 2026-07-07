"""C6 dispatcher for C4 CodeReviewReport JSON."""

from __future__ import annotations

from primitives.confidence_gate.shared.models import NormalizedFinding

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
_VALID_CONFIDENCE_LEVELS = {"low", "medium", "high"}


def sniff(data: dict[str, object]) -> bool:
    """Return True if data looks like a C4 CodeReviewReport."""
    return (
        "code_path" in data
        and "issues" in data
        and "lenses" in data
        and "layer_violations" not in data
        and "traces" not in data
        and "threats" not in data
        and "contract_path" not in data
        and "iac_path" not in data
        and "sources" not in data
        and "matches" not in data
    )


def dispatch(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from a C4 CodeReviewReport JSON dict."""
    findings: list[NormalizedFinding] = []
    issues = data.get("issues", [])
    if not isinstance(issues, list):
        return findings

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity = issue.get("severity", "")
        confidence = issue.get("confidence", "")
        if severity not in _VALID_SEVERITIES or confidence not in _VALID_CONFIDENCE_LEVELS:
            continue
        findings.append(
            NormalizedFinding(
                report_type="c4_code_review",
                severity=severity,
                confidence_level=confidence,
                file=issue.get("file") or None,
                line=issue.get("line") if isinstance(issue.get("line"), int) else None,
                title=str(issue.get("title", "")),
                description=str(issue.get("description", "")),
            )
        )
    return findings
