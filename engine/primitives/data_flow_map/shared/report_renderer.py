"""S60 /data-flow-map — Report renderer.

Renders a DataFlowReport to markdown, JSON, SARIF 2.1.0, or mermaid.

SARIF multi-tool:
  - runs[0]: S60 (sources, sinks, retention, purpose issues)
  - runs[1]: C10 (FM1-FM5) findings (when ai_quality is present)
  - runs[2]: C11 (FM6-FM10) findings (when ai_quality is present)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from primitives._tool_version import get_tool_version
from primitives.data_flow_map.shared.mermaid_renderer import render_mermaid

if TYPE_CHECKING:
    from primitives.data_flow_map.shared.models import DataFlowReport

_FORMATS: tuple[str, ...] = ("markdown", "json", "sarif", "mermaid")

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


def _sev_to_sarif_level(severity: str) -> str:
    return {"low": "note", "medium": "warning", "high": "error", "critical": "error"}.get(
        severity, "warning"
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _render_markdown(report: DataFlowReport) -> str:
    lines: list[str] = [
        "# Data Flow Map Report",
        "",
        f"**Code path:** `{report.code_path}`  ",
        f"**Generated:** {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Summary",
        "",
        "| Section | Count |",
        "|---|---|",
        f"| PII Sources | {len(report.sources)} |",
        f"| Flow Edges | {len(report.flows)} |",
        f"| Sinks | {len(report.sinks)} |",
        f"| Retention markers | {len(report.retention)} |",
        f"| Purpose issues | {len(report.purpose_issues)} |",
        "",
    ]

    # Sources
    if report.sources:
        lines += ["## PII Sources", ""]
        lines += [
            "| Field | File | Line | Classification | Category | Severity | Confidence |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in report.sources:
            lines.append(
                f"| `{s.name}` | `{s.file}` | {s.line} | {s.classification} "
                f"| {s.category} | {_sev_icon(s.severity)} {s.severity} | {s.confidence} |"
            )
        lines.append("")

    # Sinks
    if report.sinks:
        lines += ["## Sinks", ""]
        lines += ["| Name | File | Line | Kind | References PII |", "|---|---|---|---|---|"]
        for sk in report.sinks:
            pii_refs = ", ".join(f"`{p}`" for p in sk.references_pii)
            lines.append(f"| `{sk.name}` | `{sk.file}` | {sk.line} | `{sk.kind}` | {pii_refs} |")
        lines.append("")

    # Retention
    if report.retention:
        lines += ["## Retention Markers", ""]
        lines += ["| Field/Sink | Status | Period | Reference |", "|---|---|---|---|"]
        for r in report.retention:
            period = r.retention_period or "—"
            if r.status == "explicit":
                status_icon = "✅"
            elif r.status == "expired":
                status_icon = "⚠️"
            else:
                status_icon = "❌"
            lines.append(
                f"| `{r.field_or_sink}` | {status_icon} {r.status} | {period} | {r.reference} |"
            )
        lines.append("")

    # Purpose issues
    if report.purpose_issues:
        lines += ["## Purpose-Limitation Issues", ""]
        for issue in report.purpose_issues:
            lines += [
                f"### {_sev_icon(issue.severity)} {issue.field} ({issue.severity})",
                "",
                f"**Declared purpose:** {issue.declared_purpose or '(none)'}  ",
                f"**Observed use:** {issue.observed_use}",
                "",
            ]

    # AI Quality
    if report.ai_quality:
        lines += ["## AI Quality (C10 + C11)", ""]
        lines.append(f"_{report.ai_quality.summary}_")
        lines.append("")

    # Mermaid
    diagrams = render_mermaid(report)
    lines += ["## Data Flow Diagrams", ""]
    for i, diagram in enumerate(diagrams, 1):
        if len(diagrams) > 1:
            lines.append(f"### Diagram {i}")
        lines += ["", "```mermaid", diagram, "```", ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _render_json(report: DataFlowReport) -> str:
    data: dict[str, Any] = {
        "code_path": report.code_path,
        "generated_at": report.generated_at.isoformat(),
        "sources": [s.model_dump() for s in report.sources],
        "flows": [f.model_dump() for f in report.flows],
        "sinks": [sk.model_dump() for sk in report.sinks],
        "retention": [r.model_dump() for r in report.retention],
        "purpose_issues": [pi.model_dump() for pi in report.purpose_issues],
    }
    if report.ai_quality is not None:
        data["ai_quality"] = {
            "summary": report.ai_quality.summary,
            "c10_count": len(report.ai_quality.c10_findings),
            "c11_count": len(report.ai_quality.c11_findings),
        }
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# SARIF 2.1.0
# ---------------------------------------------------------------------------


def _make_sarif_location(file: str, line: int) -> dict[str, Any]:
    return {
        "physicalLocation": {
            "artifactLocation": {"uri": file},
            "region": {"startLine": line},
        }
    }


def _render_sarif(report: DataFlowReport) -> str:
    # Run 0: S60 findings
    s60_results: list[dict[str, Any]] = []

    # Sources as informational findings
    for source in report.sources:
        s60_results.append(
            {
                "ruleId": f"S60/pii-source/{source.classification}",
                "level": _sev_to_sarif_level(source.severity),
                "message": {
                    "text": (
                        f"PII source '{source.name}' detected: "
                        f"{source.classification}/{source.category} "
                        f"[confidence={source.confidence}]"
                    )
                },
                "locations": [_make_sarif_location(source.file, source.line)],
            }
        )

    # Retention issues
    for rm in report.retention:
        if rm.status != "explicit":
            s60_results.append(
                {
                    "ruleId": "S60/missing-retention",
                    "level": "warning",
                    "message": {
                        "text": (
                            f"PII field '{rm.field_or_sink}' has {rm.status} retention. "
                            f"Reference: {rm.reference}"
                        )
                    },
                    "locations": [],
                }
            )

    # Purpose issues
    for pi in report.purpose_issues:
        s60_results.append(
            {
                "ruleId": "S60/purpose-limitation",
                "level": _sev_to_sarif_level(pi.severity),
                "message": {
                    "text": (f"Purpose-limitation issue for '{pi.field}': {pi.observed_use}")
                },
                "locations": [],
            }
        )

    s60_run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "s60/data-flow-map",
                "version": _TOOL_VERSION,
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
            }
        },
        "results": s60_results,
    }

    runs: list[dict[str, Any]] = [s60_run]

    # Runs 1 + 2: C10 + C11 (ai_quality)
    if report.ai_quality is not None:
        for source_label, findings in [
            ("c10/failure-mode-audit", report.ai_quality.c10_findings),
            ("c11/production-failure-audit", report.ai_quality.c11_findings),
        ]:
            sub_results = [
                {
                    "ruleId": getattr(f, "mode", "unknown"),
                    "level": _sev_to_sarif_level(getattr(f, "severity", "medium")),
                    "message": {"text": getattr(f, "message", str(f))},
                    "locations": [_make_sarif_location(getattr(f, "file", "unknown"), 1)],
                }
                for f in findings
            ]
            runs.append(
                {
                    "tool": {
                        "driver": {
                            "name": source_label,
                            "version": _TOOL_VERSION,
                        }
                    },
                    "results": sub_results,
                }
            )

    sarif: dict[str, Any] = {
        "$schema": _SARIF_SCHEMA_URI,
        "version": _SARIF_VERSION,
        "runs": runs,
    }
    return json.dumps(sarif, indent=2)


# ---------------------------------------------------------------------------
# Mermaid only
# ---------------------------------------------------------------------------


def _render_mermaid_only(report: DataFlowReport) -> str:
    diagrams = render_mermaid(report)
    return "\n\n".join(diagrams)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def list_formats() -> list[str]:
    """Return the list of supported output formats (FLEX-3)."""
    return list(_FORMATS)


def render_report(
    report: DataFlowReport,
    format_: str = "markdown",
) -> str:
    """Render the DataFlowReport to the specified format.

    Args:
        report: DataFlowReport from the orchestrator.
        format_: Output format.

    Returns:
        Rendered string.
    """
    if format_ not in _FORMATS:
        raise ValueError(f"unknown output format {format_!r}; valid: {', '.join(_FORMATS)}")
    if format_ == "json":
        return _render_json(report)
    elif format_ == "sarif":
        return _render_sarif(report)
    elif format_ == "mermaid":
        return _render_mermaid_only(report)
    else:
        return _render_markdown(report)
