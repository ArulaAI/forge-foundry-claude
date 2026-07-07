"""C6 dispatcher for S60 DataFlowReport JSON.

S60 findings live in 'sources' (PIISource objects with severity + confidence).
Purpose issues also carry severity but no confidence field.
"""

from __future__ import annotations

from typing import cast

from primitives.confidence_gate.shared.models import ConfidenceLevel, NormalizedFinding, Severity

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
# S60 PIISource confidence is "regex" | "llm" | "yaml" — NOT standard low/medium/high.
# We map these to standard ConfidenceLevel for scoring.
_S60_CONFIDENCE_MAP: dict[str, str] = {
    "regex": "high",
    "llm": "medium",
    "yaml": "high",
}


def sniff(data: dict[str, object]) -> bool:
    """Return True if data looks like an S60 DataFlowReport."""
    return "sources" in data and "flows" in data and "sinks" in data and "retention" in data


def dispatch(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from an S60 DataFlowReport JSON dict."""
    findings: list[NormalizedFinding] = []

    # PIISource findings
    sources = data.get("sources", [])
    if isinstance(sources, list):
        for src in sources:
            if not isinstance(src, dict):
                continue
            severity = src.get("severity", "")
            raw_confidence = src.get("confidence", "")
            confidence = cast(
                "ConfidenceLevel", _S60_CONFIDENCE_MAP.get(str(raw_confidence), "medium")
            )
            if severity not in _VALID_SEVERITIES:
                continue
            findings.append(
                NormalizedFinding(
                    report_type="s60_data_flow_map",
                    severity=cast("Severity", severity),
                    confidence_level=confidence,
                    file=src.get("file") or None,
                    line=src.get("line") if isinstance(src.get("line"), int) else None,
                    title=str(src.get("name", "")),
                    description=(
                        f"PII source: {src.get('classification', '')} / {src.get('category', '')}"
                    ),
                )
            )

    # Purpose issues — severity only, default confidence to "medium"
    purpose_issues = data.get("purpose_issues", [])
    if isinstance(purpose_issues, list):
        for pi in purpose_issues:
            if not isinstance(pi, dict):
                continue
            severity = pi.get("severity", "")
            if severity not in _VALID_SEVERITIES:
                continue
            findings.append(
                NormalizedFinding(
                    report_type="s60_data_flow_map",
                    severity=cast("Severity", severity),
                    confidence_level="medium",
                    file=None,
                    line=None,
                    title=f"Purpose issue: {pi.get('field', '')}",
                    description=str(pi.get("observed_use", "")),
                )
            )

    return findings
