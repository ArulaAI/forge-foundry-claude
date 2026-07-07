"""C6 dispatcher for S20 CloudArchReviewReport JSON."""

from __future__ import annotations

from primitives.confidence_gate.shared.models import NormalizedFinding

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
_VALID_CONFIDENCE_LEVELS = {"low", "medium", "high"}


def sniff(data: dict[str, object]) -> bool:
    """Return True if data looks like an S20 CloudArchReviewReport.

    The S20 renderer (``cloud_arch_render.py``) groups findings by WAF pillar
    under ``pillar_findings`` and emits a ``workload_context`` block — both
    unique to S20 (#953). It does not emit a top-level ``findings`` list.
    """
    return "pillar_findings" in data and "workload_context" in data


def dispatch(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from an S20 CloudArchReviewReport JSON dict."""
    findings: list[NormalizedFinding] = []
    raw_findings = data.get("pillar_findings", [])
    if not isinstance(raw_findings, list):
        return findings

    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        severity = f.get("severity", "")
        # The renderer emits both `confidence` (a float score) and
        # `confidence_level` (low/medium/high) — the gate keys off the level.
        confidence = f.get("confidence_level", "")
        if severity not in _VALID_SEVERITIES or confidence not in _VALID_CONFIDENCE_LEVELS:
            continue
        findings.append(
            NormalizedFinding(
                report_type="s20_cloud_arch_review",
                severity=severity,
                confidence_level=confidence,
                file=f.get("file") or None,
                line=f.get("line") if isinstance(f.get("line"), int) else None,
                title=str(f.get("title", "")),
                description=str(f.get("description", "")),
            )
        )
    return findings
