"""C6 dispatcher for C5 SpecValidateReport JSON."""

from __future__ import annotations

from primitives.confidence_gate.shared.models import NormalizedFinding

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
_VALID_CONFIDENCE_LEVELS = {"low", "medium", "high"}


def sniff(data: dict[str, object]) -> bool:
    """Return True if data looks like a C5 SpecValidateReport."""
    return "spec_path" in data and "code_path" in data and "traces" in data


def dispatch(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from a C5 SpecValidateReport JSON dict.

    C5 findings live in the 'traces' list as AcceptanceTrace objects.
    Each trace has severity + confidence fields.
    """
    findings: list[NormalizedFinding] = []
    traces = data.get("traces", [])
    if not isinstance(traces, list):
        return findings

    for trace in traces:
        if not isinstance(trace, dict):
            continue
        severity = trace.get("severity", "")
        confidence = trace.get("confidence", "")
        if severity not in _VALID_SEVERITIES or confidence not in _VALID_CONFIDENCE_LEVELS:
            continue
        # Only include non-satisfied traces as actionable findings
        status = trace.get("status", "")
        if status == "satisfied":
            continue
        findings.append(
            NormalizedFinding(
                report_type="c5_spec_validate",
                severity=severity,
                confidence_level=confidence,
                file=None,
                line=None,
                title=str(trace.get("criterion_id", "")),
                description=str(trace.get("rationale", "")),
            )
        )
    return findings
