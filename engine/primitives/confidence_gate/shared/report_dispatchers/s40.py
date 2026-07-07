"""C6 dispatcher for S40 APIContractReviewReport JSON."""

from __future__ import annotations

from primitives.confidence_gate.shared.models import NormalizedFinding

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
_VALID_CONFIDENCE_LEVELS = {"low", "medium", "high"}


def sniff(data: dict[str, object]) -> bool:
    """Return True if data looks like an S40 APIContractReviewReport."""
    return (
        "contract_path" in data and "findings" in data and "lenses" in data and "endpoints" in data
    )


def dispatch(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from an S40 APIContractReviewReport JSON dict."""
    findings: list[NormalizedFinding] = []
    raw_findings = data.get("findings", [])
    if not isinstance(raw_findings, list):
        return findings

    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        severity = f.get("severity", "")
        confidence = f.get("confidence", "")
        if severity not in _VALID_SEVERITIES or confidence not in _VALID_CONFIDENCE_LEVELS:
            continue
        findings.append(
            NormalizedFinding(
                report_type="s40_api_contract_review",
                severity=severity,
                confidence_level=confidence,
                file=f.get("file") or None,
                line=f.get("line") if isinstance(f.get("line"), int) else None,
                title=str(f.get("title", "")),
                description=str(f.get("description", "")),
            )
        )
    return findings
