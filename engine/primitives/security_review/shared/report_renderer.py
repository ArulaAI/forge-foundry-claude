"""S11 /security-review — Report renderer.

Renders a SecurityReviewReport to markdown, JSON, or SARIF 2.1.0.

SARIF multi-tool:
  - runs[0]: S11 (security findings)
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

    from primitives.security_review.shared.models import SecurityReviewReport

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


def _render_markdown(report: SecurityReviewReport) -> str:
    lines: list[str] = [
        "# Security Review Report",
        "",
        f"**Code path:** `{report.code_path}`  ",
        f"**Lenses:** {', '.join(report.lenses)}  ",
        f"**Generated:** {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    # Sensitive areas
    if report.sensitive_areas:
        lines += [
            "## Sensitive Areas Detected",
            "",
            "| Path | Kind | Reason |",
            "|---|---|---|",
        ]
        for area in report.sensitive_areas:
            path_short = area.path.split("/")[-1]
            lines.append(f"| `{path_short}` | {area.kind} | {area.reason} |")
        lines.append("")

    # Summary
    lines += [
        "## Summary",
        "",
        "| Severity | Count |",
        "|---|---|",
    ]
    counts = report.finding_count_by_severity
    for sev in ("critical", "high", "medium", "low"):
        icon = _sev_icon(sev)
        lines.append(f"| {icon} {sev} | {counts[sev]} |")

    lines += ["", f"**Total findings:** {len(report.findings)}", ""]

    if report.findings:
        lines += ["## Findings", ""]
        lines += [
            "| # | Lens | File | Line | Severity | Confidence | OWASP | Title |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for idx, f in enumerate(report.findings, 1):
            file_short = f.file.split("/")[-1]
            owasp = f.owasp_category or "-"
            lines.append(
                f"| {idx} | `{f.lens}` | `{file_short}` | {f.line} "
                f"| {_sev_icon(f.severity)} {f.severity} "
                f"| {f.confidence} ({f.confidence_score:.2f}) "
                f"| {owasp} "
                f"| {f.title} |"
            )
        lines.append("")

        # Detail section
        lines += ["## Finding Details", ""]
        for idx, f in enumerate(report.findings, 1):
            lines += [
                f"### {idx}. [{f.lens}] {f.title}",
                "",
                f"**File:** `{f.file}` - Line {f.line}" + (f"-{f.end_line}" if f.end_line else ""),
                f"**Severity:** {_sev_icon(f.severity)} {f.severity}  ",
                f"**Confidence:** {f.confidence} ({f.confidence_score:.2f})",
            ]
            if f.owasp_category:
                lines.append(f"**OWASP:** {f.owasp_category}")
            if f.cwe_ids:
                lines.append(f"**CWE:** {', '.join(f.cwe_ids)}")
            if f.compliance_refs:
                lines.append(f"**Compliance:** {', '.join(f.compliance_refs)}")
            lines += ["", f.description]
            if f.suggestion:
                lines += ["", f"**Suggestion:** {f.suggestion}"]
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
            for c10_f in aq.c10_findings:
                lines.append(
                    f"- **{c10_f.mode.upper()}** `{c10_f.file}:{c10_f.line}` "
                    f"[{c10_f.severity}] {c10_f.message}"
                )
            lines.append("")

        if aq.c11_findings:
            lines += [f"### C11 Findings ({len(aq.c11_findings)})", ""]
            for c11_f in aq.c11_findings:
                lines.append(
                    f"- **{c11_f.mode.upper()}** `{c11_f.file}:{c11_f.line}` "
                    f"[{c11_f.severity}] {c11_f.message}"
                )
            lines.append("")

        if aq.deduplicated_findings:
            lines += [f"### Deduplicated Findings ({len(aq.deduplicated_findings)})", ""]
            lines += [
                "_These C10/C11 findings overlapped with S11 findings at the same file:line. "
                "The S11 finding is canonical; these are moved here to avoid double-counting._",
                "",
            ]
            for entry in aq.deduplicated_findings:
                lines.append(
                    f"- **{entry.finding.mode.upper()}** "
                    f"`{entry.finding.file}:{entry.finding.line}` "
                    f"[{entry.finding.severity}] {entry.finding.message} "
                    f"-- {entry.note}"
                )
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _render_json(report: SecurityReviewReport) -> str:
    findings_data = []
    for f in report.findings:
        findings_data.append(
            {
                "lens": f.lens,
                "file": f.file,
                "line": f.line,
                "end_line": f.end_line,
                "severity": f.severity,
                "confidence": f.confidence,
                "confidence_score": f.confidence_score,
                "title": f.title,
                "description": f.description,
                "suggestion": f.suggestion,
                "owasp_category": f.owasp_category,
                "cwe_ids": f.cwe_ids,
                "compliance_refs": f.compliance_refs,
            }
        )

    sensitive_areas_data = [
        {"path": a.path, "kind": a.kind, "reason": a.reason} for a in report.sensitive_areas
    ]

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

        dedup_data = [
            {"finding": finding_to_dict(entry.finding), "note": entry.note}
            for entry in aq.deduplicated_findings
        ]

        ai_quality_data = {
            "summary": aq.summary,
            "c10_findings": [finding_to_dict(f) for f in aq.c10_findings],
            "c11_findings": [finding_to_dict(f) for f in aq.c11_findings],
            "deduplicated_findings": dedup_data,
        }

    output: dict[str, Any] = {
        "code_path": report.code_path,
        "lenses": list(report.lenses),
        "sensitive_areas": sensitive_areas_data,
        "findings": findings_data,
        "finding_count": len(report.findings),
        "severity_counts": report.finding_count_by_severity,
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


def _render_sarif(report: SecurityReviewReport) -> str:
    # Build S11 run
    s11_rules: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    for f in report.findings:
        rule_id = f"S11/{f.lens}/{f.severity}"
        if rule_id not in seen_rule_ids:
            seen_rule_ids.add(rule_id)
            s11_rules.append(
                {
                    "id": rule_id,
                    "name": f"S11{f.lens.replace('_', '').title()}",
                    "shortDescription": {"text": f"S11 {f.lens} lens security finding"},
                    "properties": {
                        "security-severity": f.severity,
                        **({"owasp-category": f.owasp_category} if f.owasp_category else {}),
                    },
                }
            )

    s11_results: list[dict[str, Any]] = []
    for f in report.findings:
        result: dict[str, Any] = {
            "ruleId": f"S11/{f.lens}/{f.severity}",
            "level": _sev_to_sarif_level(f.severity),
            "message": {"text": f"[{f.confidence}] {f.title}: {f.description}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file},
                        "region": {
                            "startLine": f.line,
                            **({"endLine": f.end_line} if f.end_line else {}),
                        },
                    }
                }
            ],
            "properties": {
                "lens": f.lens,
                "confidence": f.confidence,
                "confidence_score": f.confidence_score,
                "owasp_category": f.owasp_category,
                "cwe_ids": f.cwe_ids,
                "compliance_refs": f.compliance_refs,
            },
        }
        if f.suggestion:
            result["message"]["text"] += f" Suggestion: {f.suggestion}"
        s11_results.append(result)

    s11_run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "forge-foundry-s11",
                "version": _TOOL_VERSION,
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                "rules": s11_rules,
            }
        },
        "results": s11_results,
    }

    runs: list[dict[str, Any]] = [s11_run]

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
# the `forge_foundry.renderers.security_review` entry-point group override on same
# name. `render_report` dispatches through the registry so a `--format junit`
# value works once a plugin declares it — no edits here.
_REGISTRY: RendererRegistry[SecurityReviewReport] = RendererRegistry("security_review")
_REGISTRY.register("markdown", _render_markdown)
_REGISTRY.register("json", _render_json)
_REGISTRY.register("sarif", _render_sarif)


def register_renderer(name: str, fn: Callable[[SecurityReviewReport], str]) -> None:
    """Register a renderer programmatically (in-process; for tests and downstream code)."""
    _REGISTRY.register(name, fn)


def list_formats() -> list[str]:
    """Names of all output formats discovered for this primitive (FLEX-3)."""
    return _REGISTRY.list_formats()


def render_report(report: SecurityReviewReport, output_format: str = "markdown") -> str:
    """Render a SecurityReviewReport in the requested format.

    Raises :class:`ValueError` for an unknown name with the available list, so
    a typo at the CLI surfaces fast.
    """
    return _REGISTRY.render(report, output_format)
