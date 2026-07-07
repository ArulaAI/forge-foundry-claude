"""C6 /confidence-gate — Stage D: report renderer.

Supports markdown, JSON, and SARIF 2.1.0 (multi-tool, one runs[] entry per source primitive).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.confidence_gate.shared.models import (
        ConfidenceGateReport,
        NormalizedFinding,
    )

_FORMATS: tuple[str, ...] = ("markdown", "json", "sarif")

# SARIF severity mapping
_SARIF_LEVEL: dict[str, str] = {
    "low": "note",
    "medium": "warning",
    "high": "error",
    "critical": "error",
}

_RECOMMENDATION_EMOJI: dict[str, str] = {
    "pass": "PASS",
    "review": "REVIEW",
    "fail": "FAIL",
}


def list_formats() -> list[str]:
    """Return the list of supported output formats (FLEX-3)."""
    return list(_FORMATS)


def render_report(
    report: ConfidenceGateReport,
    format_: str = "markdown",
    findings_by_report: dict[str, list[NormalizedFinding]] | None = None,
) -> str:
    """Render a ConfidenceGateReport to the requested format.

    Args:
        report: The gate report to render.
        format_: Output format (markdown, json, sarif).
        findings_by_report: Optional mapping of report_type → findings,
                            used by SARIF to generate per-primitive runs.

    Returns:
        Rendered string.
    """
    if format_ not in _FORMATS:
        raise ValueError(f"unknown output format {format_!r}; valid: {', '.join(_FORMATS)}")
    if format_ == "json":
        return _render_json(report)
    if format_ == "sarif":
        return _render_sarif(report, findings_by_report or {})
    return _render_markdown(report)


def _render_markdown(report: ConfidenceGateReport) -> str:
    """Render the gate report as markdown."""
    rec = report.gate_score.recommendation.upper()
    score = report.gate_score.score

    lines: list[str] = [
        "# Confidence Gate Report",
        "",
        f"**Recommendation:** {rec}  ",
        f"**Risk Score:** {score:.2f}  ",
        f"**Pass threshold:** {report.gate_score.threshold_pass:.1f}  ",
        f"**Review threshold:** {report.gate_score.threshold_review:.1f}  ",
        f"**Generated:** {report.generated_at.isoformat()}",
        "",
        "## Rationale",
        "",
        report.rationale,
        "",
    ]

    if report.top_findings:
        lines.append("## Top Findings")
        lines.append("")
        for tf in report.top_findings:
            loc = f"{tf.file}:{tf.line}" if tf.file else "n/a"
            lines.append(f"- **[{tf.report_type}]** `{tf.severity}` @ `{loc}`: {tf.title}")
            if tf.description:
                lines.append(f"  {tf.description}")
        lines.append("")

    lines.append("## Per-Primitive Risk Summary")
    lines.append("")
    lines.append("| Primitive | Findings | Low | Medium | High | Critical | Score |")
    lines.append("|-----------|----------|-----|--------|------|----------|-------|")
    for s in report.primitive_summaries:
        by_sev = s.by_severity
        lines.append(
            f"| {s.report_type} | {s.total_findings} "
            f"| {by_sev.get('low', 0)} "
            f"| {by_sev.get('medium', 0)} "
            f"| {by_sev.get('high', 0)} "
            f"| {by_sev.get('critical', 0)} "
            f"| {s.contributed_score:.2f} |"
        )
    lines.append("")

    lines.append("## Input Reports")
    lines.append("")
    for p in report.report_paths:
        lines.append(f"- `{p}`")
    lines.append("")

    return "\n".join(lines)


def _render_json(report: ConfidenceGateReport) -> str:
    """Render the gate report as JSON."""
    return report.model_dump_json(indent=2)


def _render_sarif(
    report: ConfidenceGateReport,
    findings_by_report: dict[str, list[NormalizedFinding]],
) -> str:
    """Render a SARIF 2.1.0 multi-tool report.

    One runs[] entry per source primitive, aggregating all findings.
    """
    runs: list[dict[str, object]] = []

    for summary in report.primitive_summaries:
        rt = summary.report_type
        findings = findings_by_report.get(rt, [])
        tool_name = f"forge-foundry/{rt}"

        results: list[dict[str, object]] = []
        for f in findings:
            level = _SARIF_LEVEL.get(f.severity, "warning")
            location: dict[str, object] = {}
            if f.file:
                location = {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file},
                        "region": {"startLine": f.line or 1},
                    }
                }
            results.append(
                {
                    "ruleId": f.title or rt,
                    "level": level,
                    "message": {"text": f.description or f.title},
                    "locations": [location] if location else [],
                    "properties": {
                        "severity": f.severity,
                        "confidence_level": f.confidence_level,
                    },
                }
            )

        runs.append(
            {
                "tool": {
                    "driver": {
                        "name": tool_name,
                        "version": "0.21.0",
                        "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                    }
                },
                "results": results,
                "properties": {
                    "primitive": rt,
                    "contributed_score": summary.contributed_score,
                    "total_findings": summary.total_findings,
                },
            }
        )

    # Add a synthetic gate-decision run
    gate_run: dict[str, object] = {
        "tool": {
            "driver": {
                "name": "forge-foundry/confidence-gate",
                "version": "0.21.0",
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
            }
        },
        "results": [
            {
                "ruleId": "C6.gate-decision",
                "level": "error" if report.gate_score.recommendation == "fail" else "note",
                "message": {
                    "text": (
                        f"Gate recommendation: {report.gate_score.recommendation.upper()} "
                        f"(score={report.gate_score.score:.2f}). {report.rationale}"
                    )
                },
                "locations": [],
                "properties": {
                    "recommendation": report.gate_score.recommendation,
                    "score": report.gate_score.score,
                    "threshold_pass": report.gate_score.threshold_pass,
                    "threshold_review": report.gate_score.threshold_review,
                },
            }
        ],
        "properties": {
            "primitive": "c6_confidence_gate",
            "recommendation": report.gate_score.recommendation,
            "score": report.gate_score.score,
        },
    }
    runs.append(gate_run)

    sarif_doc: dict[str, object] = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": runs,
    }
    return json.dumps(sarif_doc, indent=2)
