"""S70 /compliance-map — Report renderer.

Renders a ComplianceReport to markdown, JSON, or SARIF 2.1.0.

SARIF 2.1.0 multi-tool output:
  - runs[0..N-1]: one run per framework in the report
  - runs[N]: C10 (FM1-FM5) findings (when ai_quality is present)
  - runs[N+1]: C11 (FM6-FM10) findings (when ai_quality is present)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from primitives._tool_version import get_tool_version
from primitives.compliance_map.shared.ai_quality_aggregator import (
    AIQualityReport,  # noqa: TC001 — used in type annotations, runtime-required
)
from primitives.failure_mode_audit import (
    Finding,  # noqa: TC001 — used in function signatures, runtime-required
)

if TYPE_CHECKING:
    from primitives.compliance_map.shared.models import (
        ComplianceReport,
        ControlMatch,
        Gap,
    )

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


def _severity_icon(severity: str) -> str:
    return {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(severity, "⚪")


def _severity_to_sarif_level(severity: str) -> str:
    return {"low": "note", "medium": "warning", "high": "error", "critical": "error"}.get(
        severity, "warning"
    )


def _status_icon(status: str) -> str:
    return {
        "addressed": "✅",
        "partial": "⚠️",
        "unaddressed": "❌",
        "not_applicable": "-",
    }.get(status, "❓")


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_markdown(report: ComplianceReport) -> str:
    lines: list[str] = [
        f"# Compliance Map Report: {report.spec_title}",
        "",
        f"**Frameworks:** {', '.join(report.frameworks)}  ",
        f"**Generated:** {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Controls assessed:** {len(report.matches)}  ",
        f"**Gaps:** {len(report.gaps)}",
        "",
    ]

    # --- Summary table ---
    if report.matches:
        lines += [
            "## Control Matrix",
            "",
            "| Framework | Control ID | Title | Status | Evidence | Severity |",
            "|-----------|-----------|-------|--------|----------|----------|",
        ]
        for m in report.matches:
            status_display = f"{_status_icon(m.status)} {m.status}"
            evidence_count = len(m.evidence)
            lines.append(
                f"| {m.control.framework} | {m.control.id} | {m.control.title[:40]} | "
                f"{status_display} | {evidence_count} ref(s) | "
                f"{_severity_icon(m.control.severity)} {m.control.severity} |"
            )
        lines.append("")

    # --- Gaps section ---
    if report.gaps:
        lines += [
            "## Gaps",
            "",
            f"**{len(report.gaps)} gap(s) identified.**",
            "",
        ]
        for g in report.gaps:
            gap_heading = (
                f"### {_severity_icon(g.severity)} [{g.severity.upper()}] "
                f"{g.control.framework} — {g.control.id}: {g.control.title}"
            )
            lines += [
                gap_heading,
                "",
                f"**Missing evidence:** {g.missing_evidence}",
                "",
                f"**Suggested remediation:** {g.suggested_remediation}",
                "",
            ]

    # --- Evidence details ---
    matched_with_evidence = [m for m in report.matches if m.evidence]
    if matched_with_evidence:
        lines += ["## Evidence References", ""]
        for m in matched_with_evidence:
            lines += [
                f"### {m.control.id} — {m.control.title}",
                "",
            ]
            for ev in m.evidence:
                lines.append(f"- `{ev.file}:{ev.line}` [{ev.confidence}] — `{ev.snippet[:80]}`")
            lines.append("")

    # --- AI Quality section ---
    if report.ai_quality is not None:
        aq: AIQualityReport = report.ai_quality
        lines += [
            "## AI Quality Findings",
            "",
            aq.summary,
            "",
        ]
        if aq.c10_findings:
            lines += ["### C10 Findings (FM1-FM5)", ""]
            for f in aq.c10_findings:
                lines.append(
                    f"- [{f.severity.upper()}] `{f.rule_id}` — {f.message} ({f.file}:{f.line})"
                )
            lines.append("")
        if aq.c11_findings:
            lines += ["### C11 Findings (FM6-FM10)", ""]
            for f in aq.c11_findings:
                lines.append(
                    f"- [{f.severity.upper()}] `{f.rule_id}` — {f.message} ({f.file}:{f.line})"
                )
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON rendering
# ---------------------------------------------------------------------------


def _match_to_dict(m: ControlMatch) -> dict[str, Any]:
    return {
        "control": {
            "id": m.control.id,
            "framework": m.control.framework,
            "title": m.control.title,
            "family": m.control.family,
            "severity": m.control.severity,
            "cwe_ids": m.control.cwe_ids,
        },
        "status": m.status,
        "evidence": [
            {
                "file": ev.file,
                "line": ev.line,
                "snippet": ev.snippet,
                "confidence": ev.confidence,
            }
            for ev in m.evidence
        ],
        "gap_description": m.gap_description,
        "remediation_owner": m.remediation_owner,
    }


def _gap_to_dict(g: Gap) -> dict[str, Any]:
    return {
        "control": {
            "id": g.control.id,
            "framework": g.control.framework,
            "title": g.control.title,
            "family": g.control.family,
            "severity": g.control.severity,
        },
        "missing_evidence": g.missing_evidence,
        "suggested_remediation": g.suggested_remediation,
        "severity": g.severity,
    }


def _render_json(report: ComplianceReport) -> str:
    obj: dict[str, Any] = {
        "schema_version": report.schema_version,
        "spec_title": report.spec_title,
        "frameworks": report.frameworks,
        "generated_at": report.generated_at.isoformat(),
        "matches": [_match_to_dict(m) for m in report.matches],
        "gaps": [_gap_to_dict(g) for g in report.gaps],
    }
    if report.ai_quality is not None:
        aq: AIQualityReport = report.ai_quality
        obj["ai_quality"] = {
            "summary": aq.summary,
            "c10_findings": [
                {
                    "mode": f.mode,
                    "severity": f.severity,
                    "file": f.file,
                    "line": f.line,
                    "message": f.message,
                    "rule_id": f.rule_id,
                    "cwe_id": f.cwe_id,
                }
                for f in aq.c10_findings
            ],
            "c11_findings": [
                {
                    "mode": f.mode,
                    "severity": f.severity,
                    "file": f.file,
                    "line": f.line,
                    "message": f.message,
                    "rule_id": f.rule_id,
                    "cwe_id": f.cwe_id,
                }
                for f in aq.c11_findings
            ],
        }
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# SARIF rendering
# ---------------------------------------------------------------------------


def _build_compliance_sarif_run(
    framework: str,
    matches: list[ControlMatch],
    gaps: list[Gap],
) -> dict[str, Any]:
    """Build a SARIF run for one framework's compliance findings."""
    fw_matches = [m for m in matches if m.control.framework == framework]
    fw_gaps = [g for g in gaps if g.control.framework == framework]

    # Rules = unique control IDs
    rule_ids = sorted({m.control.id for m in fw_matches} | {g.control.id for g in fw_gaps})
    rules = [
        {
            "id": f"{framework}/{rid}",
            "shortDescription": {"text": f"{framework} control {rid}"},
            "defaultConfiguration": {"level": "warning"},
        }
        for rid in rule_ids
    ]

    results: list[dict[str, Any]] = []

    # Add gap results
    for g in fw_gaps:
        result: dict[str, Any] = {
            "ruleId": f"{framework}/{g.control.id}",
            "level": _severity_to_sarif_level(g.severity),
            "message": {"text": f"[GAP] {g.control.title}: {g.missing_evidence}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": "spec"},
                        "region": {"startLine": 1},
                    }
                }
            ],
            "properties": {
                "control_id": g.control.id,
                "framework": framework,
                "family": g.control.family,
                "suggested_remediation": g.suggested_remediation,
                "cwe_ids": g.control.cwe_ids,
            },
        }
        results.append(result)

    # Add evidence results (addressed/partial)
    for m in fw_matches:
        if m.status in ("addressed", "partial") and m.evidence:
            for ev in m.evidence[:1]:  # first evidence ref
                res: dict[str, Any] = {
                    "ruleId": f"{framework}/{m.control.id}",
                    "level": "note",
                    "message": {
                        "text": (
                            f"[{m.status.upper()}] {m.control.title}: "
                            f"evidence at {ev.file}:{ev.line}"
                        )
                    },
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": ev.file},
                                "region": {"startLine": ev.line},
                            }
                        }
                    ],
                    "properties": {
                        "control_id": m.control.id,
                        "framework": framework,
                        "status": m.status,
                        "confidence": ev.confidence,
                    },
                }
                results.append(res)

    return {
        "tool": {
            "driver": {
                "name": f"forge-foundry/compliance-map/{framework}",
                "version": _TOOL_VERSION,
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                "rules": rules,
            }
        },
        "results": results,
    }


def _build_audit_sarif_run(
    findings: list[Finding],
    tool_name: str,
) -> dict[str, Any]:
    """Build a SARIF run for C10 or C11 findings."""
    rule_ids = sorted({f.rule_id for f in findings})
    rules = [
        {
            "id": rid,
            "shortDescription": {"text": rid},
            "defaultConfiguration": {"level": "warning"},
        }
        for rid in rule_ids
    ]

    results: list[dict[str, Any]] = []
    for f in findings:
        res: dict[str, Any] = {
            "ruleId": f.rule_id,
            "level": _severity_to_sarif_level(f.severity),
            "message": {"text": f.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file},
                        "region": {"startLine": f.line},
                    }
                }
            ],
        }
        if f.cwe_id:
            res["taxa"] = [{"id": f.cwe_id, "toolComponent": {"name": "CWE"}}]
        results.append(res)

    return {
        "tool": {
            "driver": {
                "name": tool_name,
                "version": _TOOL_VERSION,
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                "rules": rules,
            }
        },
        "results": results,
    }


def _render_sarif(report: ComplianceReport) -> str:
    runs: list[dict[str, Any]] = []

    # One run per framework
    for fw in report.frameworks:
        runs.append(_build_compliance_sarif_run(fw, report.matches, report.gaps))

    # AI quality runs
    if report.ai_quality is not None:
        aq: AIQualityReport = report.ai_quality
        if aq.c10_findings:
            runs.append(_build_audit_sarif_run(aq.c10_findings, "forge-foundry/failure-mode-audit"))
        if aq.c11_findings:
            runs.append(
                _build_audit_sarif_run(aq.c11_findings, "forge-foundry/production-failure-audit")
            )

    sarif: dict[str, Any] = {
        "$schema": _SARIF_SCHEMA_URI,
        "version": _SARIF_VERSION,
        # Document-level property bag carrying OUR compliance schema version (distinct
        # from the SARIF spec version) for SOX/PCI audit replay (GOV-3, #619).
        "properties": {"schema_version": report.schema_version},
        "runs": runs,
    }
    return json.dumps(sarif, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_formats() -> list[str]:
    """Return the list of supported output formats (FLEX-3)."""
    return list(_FORMATS)


def render_report(
    report: ComplianceReport,
    format: str = "markdown",
) -> str:
    """Render a ComplianceReport to the specified format.

    Args:
        report: The compliance map report to render.
        format: Output format — 'markdown', 'json', or 'sarif'.

    Returns:
        Rendered string in the requested format.
    """
    if format not in _FORMATS:
        raise ValueError(f"unknown output format {format!r}; valid: {', '.join(_FORMATS)}")
    if format == "json":
        return _render_json(report)
    if format == "sarif":
        return _render_sarif(report)
    return _render_markdown(report)
