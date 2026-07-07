"""C5 /spec-validate — Report renderer.

Renders a SpecValidateReport to markdown, JSON, or SARIF 2.1.0.

SARIF multi-tool:
  - runs[0]: C5 (spec-validate traces + scope drift)
  - runs[1]: C10 (FM1-FM5) findings (when ai_quality is present)
  - runs[2]: C11 (FM6-FM10) findings (when ai_quality is present)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from primitives._tool_version import get_tool_version
from primitives.failure_mode_audit import (
    Finding,  # noqa: TC001 — used in function signatures, runtime-required
)

if TYPE_CHECKING:
    from primitives.spec_validate.shared.models import SpecValidateReport

_FORMATS: tuple[str, ...] = ("markdown", "json", "sarif")

_SARIF_VERSION = "2.1.0"
_SARIF_SCHEMA_URI = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/"
    "sarif-2.1/schema/sarif-schema-2.1.0.json"
)

_TOOL_VERSION = get_tool_version()


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------


def _sev_icon(severity: str) -> str:
    return {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(severity, "⚪")


def _status_icon(status: str) -> str:
    return {
        "satisfied": "✅",
        "partial": "🟡",
        "unsatisfied": "❌",
        "not_applicable": "⚫",
    }.get(status, "❓")


def _sev_to_sarif_level(severity: str) -> str:
    return {"low": "note", "medium": "warning", "high": "error", "critical": "error"}.get(
        severity, "warning"
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _render_markdown(report: SpecValidateReport) -> str:
    lines: list[str] = [
        "# Spec Validate Report",
        "",
        f"**Spec:** `{report.spec_path}`  ",
        f"**Code:** `{report.code_path}`  ",
        f"**Generated:** {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|---|---|",
    ]

    counts = report.status_counts
    for status in ("satisfied", "partial", "unsatisfied", "not_applicable"):
        icon = _status_icon(status)
        lines.append(f"| {icon} {status} | {counts[status]} |")

    lines += ["", f"**Total criteria:** {len(report.traces)}", ""]

    if report.traces:
        lines += ["## Acceptance Traces", ""]
        lines += [
            "| # | Criterion | Status | Confidence | Severity |",
            "|---|---|---|---|---|",
        ]
        for idx, trace in enumerate(report.traces, 1):
            _TRUNCATE = 60
            short_text = trace.criterion_text[:_TRUNCATE] + (
                "..." if len(trace.criterion_text) > _TRUNCATE else ""
            )
            lines.append(
                f"| {idx} | {short_text} "
                f"| {_status_icon(trace.status)} {trace.status} "
                f"| {trace.confidence} "
                f"| {_sev_icon(trace.severity)} {trace.severity} |"
            )
        lines.append("")

        lines += ["## Trace Details", ""]
        for idx, trace in enumerate(report.traces, 1):
            lines += [
                f"### {idx}. [{trace.status}] {trace.criterion_text[:80]}",
                "",
                f"**Criterion ID:** `{trace.criterion_id}`  ",
                f"**Status:** {_status_icon(trace.status)} {trace.status}  ",
                f"**Severity:** {_sev_icon(trace.severity)} {trace.severity}  ",
                f"**Confidence:** {trace.confidence}",
                "",
                f"**Rationale:** {trace.rationale}",
                "",
            ]
            if trace.code_elements:
                lines.append("**Code Elements:**")
                for elem in trace.code_elements:
                    lines.append(f"- `{elem.file}:{elem.line}` — `{elem.name}` ({elem.kind})")
                lines.append("")

    if report.scope_drift:
        lines += [
            f"## Scope Drift ({len(report.scope_drift)} item(s))",
            "",
            "_These implementation features have no spec backing:_",
            "",
            "| File | Line | Name | Kind | Description |",
            "|---|---|---|---|---|",
        ]
        for item in report.scope_drift:
            lines.append(
                f"| `{item.file}` | {item.line} | `{item.name}` | {item.kind} "
                f"| {item.description[:60]} |"
            )
        lines.append("")

    if report.ai_quality is not None:
        aq = report.ai_quality
        lines += [
            "## AI Quality (C10 + C11 Sub-calls)",
            "",
            aq.summary,
            "",
        ]

        if aq.c10_findings:
            lines += [f"### C10 Findings ({len(aq.c10_findings)})", ""]
            for f in aq.c10_findings:
                lines.append(
                    f"- **{f.mode.upper()}** `{f.file}:{f.line}` [{f.severity}] {f.message}"
                )
            lines.append("")

        if aq.c11_findings:
            lines += [f"### C11 Findings ({len(aq.c11_findings)})", ""]
            for f in aq.c11_findings:
                lines.append(
                    f"- **{f.mode.upper()}** `{f.file}:{f.line}` [{f.severity}] {f.message}"
                )
            lines.append("")

        if aq.deduplicated_findings:
            lines += [f"### Deduplicated Findings ({len(aq.deduplicated_findings)})", ""]
            lines += [
                "_These C10/C11 findings overlapped with C5 traces at the same file:line. "
                "The C5 trace is canonical; these are moved here to avoid double-counting._",
                "",
            ]
            for entry in aq.deduplicated_findings:
                lines.append(
                    f"- **{entry.finding.mode.upper()}** "
                    f"`{entry.finding.file}:{entry.finding.line}` "
                    f"[{entry.finding.severity}] {entry.finding.message} "
                    f"— {entry.note}"
                )
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _render_json(report: SpecValidateReport) -> str:
    traces_data = []
    for trace in report.traces:
        elems = []
        for elem in trace.code_elements:
            elems.append(
                {
                    "file": elem.file,
                    "line": elem.line,
                    "name": elem.name,
                    "kind": elem.kind,
                }
            )
        traces_data.append(
            {
                "criterion_id": trace.criterion_id,
                "criterion_text": trace.criterion_text,
                "status": trace.status,
                "code_elements": elems,
                "rationale": trace.rationale,
                "severity": trace.severity,
                "confidence": trace.confidence,
            }
        )

    scope_drift_data = []
    for item in report.scope_drift:
        scope_drift_data.append(
            {
                "file": item.file,
                "line": item.line,
                "name": item.name,
                "kind": item.kind,
                "description": item.description,
            }
        )

    ai_quality_data: dict[str, Any] | None = None
    if report.ai_quality is not None:
        aq = report.ai_quality

        def finding_to_dict(f: Finding) -> dict[str, Any]:
            return {
                "mode": f.mode,
                "severity": f.severity,
                "file": f.file,
                "line": f.line,
                "message": f.message,
                "snippet": f.snippet,
                "rule_id": f.rule_id,
                "cwe_id": f.cwe_id,
            }

        dedup_data = []
        for entry in aq.deduplicated_findings:
            dedup_data.append(
                {
                    "finding": finding_to_dict(entry.finding),
                    "note": entry.note,
                }
            )

        ai_quality_data = {
            "summary": aq.summary,
            "c10_findings": [finding_to_dict(f) for f in aq.c10_findings],
            "c11_findings": [finding_to_dict(f) for f in aq.c11_findings],
            "deduplicated_findings": dedup_data,
        }

    output: dict[str, Any] = {
        "spec_path": report.spec_path,
        "code_path": report.code_path,
        "traces": traces_data,
        "trace_count": len(report.traces),
        "status_counts": report.status_counts,
        "scope_drift": scope_drift_data,
        "ai_quality": ai_quality_data,
        "generated_at": report.generated_at.isoformat(),
    }
    return json.dumps(output, indent=2)


# ---------------------------------------------------------------------------
# SARIF 2.1.0
# ---------------------------------------------------------------------------


def _finding_to_sarif_result(finding: Finding) -> dict[str, Any]:
    """Convert a C10/C11 Finding to a SARIF result."""
    return {
        "ruleId": finding.rule_id,
        "level": _sev_to_sarif_level(finding.severity),
        "message": {"text": finding.message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": finding.file},
                    "region": {"startLine": finding.line},
                }
            }
        ],
    }


def _render_sarif(report: SpecValidateReport) -> str:
    # Build C5 run
    c5_rules: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    for trace in report.traces:
        rule_id = f"C5/{trace.status}/{trace.severity}"
        if rule_id not in seen_rule_ids:
            seen_rule_ids.add(rule_id)
            c5_rules.append(
                {
                    "id": rule_id,
                    "name": f"C5{trace.status.replace('_', '').title()}",
                    "shortDescription": {"text": f"C5 spec-validate {trace.status}"},
                    "properties": {"security-severity": trace.severity},
                }
            )

    c5_results: list[dict[str, Any]] = []
    for trace in report.traces:
        locations = []
        if trace.code_elements:
            elem = trace.code_elements[0]
            locations.append(
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": elem.file},
                        "region": {"startLine": elem.line},
                    }
                }
            )

        c5_results.append(
            {
                "ruleId": f"C5/{trace.status}/{trace.severity}",
                "level": _sev_to_sarif_level(trace.severity),
                "message": {
                    "text": (f"[{trace.status}] {trace.criterion_text[:120]} — {trace.rationale}")
                },
                "locations": locations,
                "properties": {
                    "criterion_id": trace.criterion_id,
                    "status": trace.status,
                    "confidence": trace.confidence,
                },
            }
        )

    # Add scope drift as C5 findings
    for item in report.scope_drift:
        c5_results.append(
            {
                "ruleId": "C5/scope-drift",
                "level": "warning",
                "message": {"text": f"Scope drift: {item.name} — {item.description}"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": item.file},
                            "region": {"startLine": item.line},
                        }
                    }
                ],
                "properties": {"kind": item.kind},
            }
        )

    c5_run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "forge-foundry-c5",
                "version": _TOOL_VERSION,
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                "rules": c5_rules,
            }
        },
        "results": c5_results,
    }

    runs: list[dict[str, Any]] = [c5_run]

    # Build C10 and C11 runs if ai_quality is present
    if report.ai_quality is not None:
        aq = report.ai_quality

        c10_results = [_finding_to_sarif_result(f) for f in aq.c10_findings]
        c10_run: dict[str, Any] = {
            "tool": {
                "driver": {
                    "name": "forge-foundry-c10",
                    "version": _TOOL_VERSION,
                    "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                    "rules": [],
                }
            },
            "results": c10_results,
        }
        runs.append(c10_run)

        c11_results = [_finding_to_sarif_result(f) for f in aq.c11_findings]
        c11_run: dict[str, Any] = {
            "tool": {
                "driver": {
                    "name": "forge-foundry-c11",
                    "version": _TOOL_VERSION,
                    "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                    "rules": [],
                }
            },
            "results": c11_results,
        }
        runs.append(c11_run)

    sarif: dict[str, Any] = {
        "$schema": _SARIF_SCHEMA_URI,
        "version": _SARIF_VERSION,
        "runs": runs,
    }
    return json.dumps(sarif, indent=2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_formats() -> list[str]:
    """Return the list of supported output formats (FLEX-3)."""
    return list(_FORMATS)


def render_report(
    report: SpecValidateReport,
    output_format: str = "markdown",
) -> str:
    """Render a SpecValidateReport to the specified format.

    Args:
        report: The spec validate report to render.
        output_format: "markdown", "json", or "sarif".

    Returns:
        Rendered string in the requested format.
    """
    if output_format not in _FORMATS:
        raise ValueError(f"unknown output format {output_format!r}; valid: {', '.join(_FORMATS)}")
    if output_format == "json":
        return _render_json(report)
    if output_format == "sarif":
        return _render_sarif(report)
    return _render_markdown(report)
