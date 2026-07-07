"""C6 dispatcher for S30 FrontendDesignReport JSON."""

from __future__ import annotations

from primitives.confidence_gate.shared.models import NormalizedFinding

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
_VALID_CONFIDENCE_LEVELS = {"low", "medium", "high"}


def sniff(data: dict[str, object]) -> bool:
    """Return True if data looks like an S30 FrontendDesignReport."""
    return (
        "code_path" in data
        and "findings" in data
        and "lenses" in data
        and "components" in data
        and "sensitive_areas" not in data
        and "layer_violations" not in data
        and "iac_path" not in data
        and "sources" not in data
        and "matches" not in data
        and "endpoints" not in data
    )


def dispatch(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from an S30 FrontendDesignReport JSON dict."""
    findings: list[NormalizedFinding] = []
    raw_findings = data.get("findings", [])
    if not isinstance(raw_findings, list):
        return findings

    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        severity = f.get("severity", "")
        # The renderer emits `confidence_score` (a float) and `confidence_level`
        # (low/medium/high) — the gate keys off the level (#953).
        confidence = f.get("confidence_level", "")
        if severity not in _VALID_SEVERITIES or confidence not in _VALID_CONFIDENCE_LEVELS:
            continue
        findings.append(
            NormalizedFinding(
                report_type="s30_frontend_design",
                severity=severity,
                confidence_level=confidence,
                file=f.get("file") or None,
                line=f.get("line") if isinstance(f.get("line"), int) else None,
                title=str(f.get("title", "")),
                description=str(f.get("description", "")),
            )
        )
    return findings
