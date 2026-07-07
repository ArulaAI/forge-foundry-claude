"""C6 dispatcher for S70 ComplianceReport JSON.

S70 findings come from 'gaps' (Gap objects) and 'matches' with non-addressed status.
"""

from __future__ import annotations

from primitives.confidence_gate.shared.models import NormalizedFinding

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}


def sniff(data: dict[str, object]) -> bool:
    """Return True if data looks like an S70 ComplianceReport."""
    return "matches" in data and "gaps" in data and "frameworks" in data and "spec_title" in data


def dispatch(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from an S70 ComplianceReport JSON dict."""
    findings: list[NormalizedFinding] = []

    # Gap findings
    gaps = data.get("gaps", [])
    if isinstance(gaps, list):
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            severity = gap.get("severity", "")
            if severity not in _VALID_SEVERITIES:
                # Try from embedded control
                control = gap.get("control", {})
                if isinstance(control, dict):
                    severity = control.get("severity", "medium")
            if severity not in _VALID_SEVERITIES:
                severity = "medium"
            control = gap.get("control", {})
            control_id = ""
            if isinstance(control, dict):
                control_id = str(control.get("id", ""))
            findings.append(
                NormalizedFinding(
                    report_type="s70_compliance_map",
                    severity=severity,
                    confidence_level="medium",
                    file=None,
                    line=None,
                    title=f"Gap: {control_id}",
                    description=str(gap.get("missing_evidence", "")),
                )
            )

    return findings
