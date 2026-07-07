"""C6 dispatcher for S10 ThreatReport JSON.

S10 threats have a 'dread' sub-object but no severity/confidence in the standard sense.
We map DREAD risk score to severity and default confidence to "medium".
"""

from __future__ import annotations

from typing import cast

from primitives.confidence_gate.shared.models import NormalizedFinding, Severity

_DREAD_CRITICAL_THRESHOLD = 8.0
_DREAD_HIGH_THRESHOLD = 6.0
_DREAD_MEDIUM_THRESHOLD = 4.0
_DREAD_DIMS = [
    "damage",
    "reproducibility",
    "exploitability",
    "affected_users",
    "discoverability",
]
_DREAD_DEFAULT_RISK = 5.0


def sniff(data: dict[str, object]) -> bool:
    """Return True if data looks like an S10 ThreatReport.

    Real S10 JSON (from primitives/threat_model/shared/report_renderer.py _render_json)
    emits: spec_title, methodology, generated_at, threats, and optionally
    pasta_stage1, pasta_stage4, ai_quality.  It never contains architecture_summary.
    """
    return (
        "threats" in data
        and "methodology" in data
        and "spec_title" in data
        and "layer_violations" not in data  # exclude C8 ArchitectureReport
        and "issues" not in data  # exclude C4 CodeReviewReport
        and "traces" not in data  # exclude C5 SpecValidateReport
        and "contract_path" not in data  # exclude S40 APIContractReviewReport
        and "iac_path" not in data  # exclude S20 CloudArchReviewReport
        and "sources" not in data  # exclude S60 DataFlowReport
        and "matches" not in data  # exclude S70 ComplianceReport
    )


def _dread_risk_to_severity(risk: float) -> str:
    """Map a DREAD risk score (1.0-10.0) to severity."""
    if risk >= _DREAD_CRITICAL_THRESHOLD:
        return "critical"
    if risk >= _DREAD_HIGH_THRESHOLD:
        return "high"
    if risk >= _DREAD_MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def dispatch(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from an S10 ThreatReport JSON dict."""
    findings: list[NormalizedFinding] = []
    threats = data.get("threats", [])
    if not isinstance(threats, list):
        return findings

    for threat in threats:
        if not isinstance(threat, dict):
            continue
        # Determine severity from dread score if available
        dread = threat.get("dread")
        if isinstance(dread, dict):
            # Compute DREAD mean
            try:
                values = [float(dread[d]) for d in _DREAD_DIMS if d in dread]
                risk = sum(values) / len(values) if values else _DREAD_DEFAULT_RISK
            except (TypeError, ValueError, ZeroDivisionError):
                risk = _DREAD_DEFAULT_RISK
            severity = _dread_risk_to_severity(risk)
        else:
            # No DREAD score -- default to medium
            severity = "medium"

        findings.append(
            NormalizedFinding(
                report_type="s10_threat_model",
                severity=cast("Severity", severity),
                confidence_level="medium",
                file=None,
                line=None,
                title=str(threat.get("title", "")),
                description=str(threat.get("description", "")),
            )
        )
    return findings
