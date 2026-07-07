"""C4 /code-review — Report renderer.

Renders a CodeReviewReport to markdown, JSON, or SARIF 2.1.0.

SARIF multi-tool:
  - runs[0]: C4 (code review issues)
  - runs[1]: C10 (FM1-FM5) findings (when ai_quality is present)
  - runs[2]: C11 (FM6-FM10) findings (when ai_quality is present)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from primitives._renderer_registry import RendererRegistry
from primitives._tool_version import get_tool_version
from primitives.failure_mode_audit import (
    Finding,  # noqa: TC001 — used in function signatures, runtime-required
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from primitives.code_review.shared.models import CodeReviewReport

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


def _render_markdown(report: CodeReviewReport) -> str:
    lines: list[str] = [
        "# Code Review Report",
        "",
        f"**Code path:** `{report.code_path}`  ",
        f"**Lenses:** {', '.join(report.lenses)}  ",
        f"**Generated:** {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Summary",
        "",
        "| Severity | Count |",
        "|---|---|",
    ]

    counts = report.issue_count_by_severity
    for sev in ("critical", "high", "medium", "low"):
        icon = _sev_icon(sev)
        lines.append(f"| {icon} {sev} | {counts[sev]} |")

    lines += ["", f"**Total issues:** {len(report.issues)}", ""]

    if report.issues:
        lines += ["## Issues", ""]
        lines += [
            "| # | Lens | File | Line | Severity | Confidence | Title |",
            "|---|---|---|---|---|---|---|",
        ]
        for idx, issue in enumerate(report.issues, 1):
            file_short = issue.file.split("/")[-1]
            lines.append(
                f"| {idx} | `{issue.lens}` | `{file_short}` | {issue.line} "
                f"| {_sev_icon(issue.severity)} {issue.severity} "
                f"| {issue.confidence} ({issue.confidence_score:.2f}) "
                f"| {issue.title} |"
            )
        lines.append("")

        # Detail section
        lines += ["## Issue Details", ""]
        for idx, issue in enumerate(report.issues, 1):
            lines += [
                f"### {idx}. [{issue.lens}] {issue.title}",
                "",
                f"**File:** `{issue.file}` - Line {issue.line}"
                + (f"-{issue.end_line}" if issue.end_line else ""),
                f"**Severity:** {_sev_icon(issue.severity)} {issue.severity}  ",
                f"**Confidence:** {issue.confidence} ({issue.confidence_score:.2f})",
            ]
            if issue.cwe_ids:
                lines.append(f"**CWE:** {', '.join(issue.cwe_ids)}")
            if issue.linked_controls:
                lines.append(f"**Linked controls (S70):** {', '.join(issue.linked_controls)}")
            lines += ["", issue.description]
            if issue.suggestion:
                lines += ["", f"**Suggestion:** {issue.suggestion}"]
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
                "_These C10/C11 findings overlapped with C4 issues at the same file:line. "
                "The C4 issue is canonical; these are moved here to avoid double-counting._",
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


def _render_json(report: CodeReviewReport) -> str:
    issues_data = []
    for issue in report.issues:
        issues_data.append(
            {
                "lens": issue.lens,
                "file": issue.file,
                "line": issue.line,
                "end_line": issue.end_line,
                "severity": issue.severity,
                "confidence": issue.confidence,
                "confidence_score": issue.confidence_score,
                "title": issue.title,
                "description": issue.description,
                "suggestion": issue.suggestion,
                "cwe_ids": issue.cwe_ids,
                "linked_controls": issue.linked_controls,
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
        "code_path": report.code_path,
        "lenses": list(report.lenses),
        "issues": issues_data,
        "issue_count": len(report.issues),
        "severity_counts": report.issue_count_by_severity,
        "ai_quality": ai_quality_data,
        "generated_at": report.generated_at.isoformat(),
    }
    return json.dumps(output, indent=2)


# ---------------------------------------------------------------------------
# SARIF 2.1.0
# ---------------------------------------------------------------------------


def _finding_to_sarif_result(finding: Finding, rule_prefix: str) -> dict[str, Any]:
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


def _render_sarif(report: CodeReviewReport) -> str:
    # Build C4 run
    c4_rules: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    for issue in report.issues:
        rule_id = f"C4/{issue.lens}/{issue.severity}"
        if rule_id not in seen_rule_ids:
            seen_rule_ids.add(rule_id)
            c4_rules.append(
                {
                    "id": rule_id,
                    "name": f"C4{issue.lens.replace('_', '').title()}",
                    "shortDescription": {"text": f"C4 {issue.lens} lens finding"},
                    "properties": {"security-severity": issue.severity},
                }
            )

    c4_results: list[dict[str, Any]] = []
    for issue in report.issues:
        result: dict[str, Any] = {
            "ruleId": f"C4/{issue.lens}/{issue.severity}",
            "level": _sev_to_sarif_level(issue.severity),
            "message": {"text": f"[{issue.confidence}] {issue.title}: {issue.description}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": issue.file},
                        "region": {
                            "startLine": issue.line,
                            **({"endLine": issue.end_line} if issue.end_line else {}),
                        },
                    }
                }
            ],
            "properties": {
                "lens": issue.lens,
                "confidence": issue.confidence,
                "confidence_score": issue.confidence_score,
                "cwe_ids": issue.cwe_ids,
                "linked_controls": issue.linked_controls,
            },
        }
        if issue.suggestion:
            result["message"]["text"] += f" Suggestion: {issue.suggestion}"
        c4_results.append(result)

    c4_run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "forge-foundry-c4",
                "version": _TOOL_VERSION,
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                "rules": c4_rules,
            }
        },
        "results": c4_results,
    }

    runs: list[dict[str, Any]] = [c4_run]

    # Build C10 and C11 runs if ai_quality is present
    if report.ai_quality is not None:
        aq = report.ai_quality

        # C10 run
        c10_results = [_finding_to_sarif_result(f, "C10") for f in aq.c10_findings]
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

        # C11 run
        c11_results = [_finding_to_sarif_result(f, "C11") for f in aq.c11_findings]
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


# FLEX-3 (#175): renderer registry. Built-ins below self-register; plugins under
# the `forge_foundry.renderers.code_review` entry-point group override on same
# name. `render_report` dispatches through the registry so a `--format junit`
# value works once a plugin declares it — no edits here.
_REGISTRY: RendererRegistry[CodeReviewReport] = RendererRegistry("code_review")
_REGISTRY.register("markdown", _render_markdown)
_REGISTRY.register("json", _render_json)
_REGISTRY.register("sarif", _render_sarif)


def register_renderer(name: str, fn: Callable[[CodeReviewReport], str]) -> None:
    """Register a renderer programmatically (in-process; for tests and downstream code)."""
    _REGISTRY.register(name, fn)


def list_formats() -> list[str]:
    """Names of all output formats discovered for this primitive (FLEX-3)."""
    return _REGISTRY.list_formats()


def render_report(report: CodeReviewReport, output_format: str = "markdown") -> str:
    """Render a CodeReviewReport in the requested format.

    Raises :class:`ValueError` for an unknown name with the available list, so
    a typo at the CLI surfaces fast.
    """
    return _REGISTRY.render(report, output_format)
