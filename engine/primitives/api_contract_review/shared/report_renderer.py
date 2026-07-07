"""S40 /api-contract-review — Report renderer.

Renders an APIContractReviewReport to markdown, JSON, or SARIF 2.1.0.

SARIF multi-tool:
  - runs[0]: S40 (api-contract-review findings)
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

    from primitives.api_contract_review.shared.models import APIContractReviewReport, Endpoint

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


def _render_markdown(report: APIContractReviewReport) -> str:  # noqa: PLR0912
    lines: list[str] = [
        "# API Contract Review Report (S40)",
        "",
        f"**Contract path:** `{report.contract_path}`  ",
        f"**Format:** {report.format}  ",
        f"**Lenses reviewed:** {', '.join(report.lenses)}  ",
        f"**Endpoints parsed:** {len(report.endpoints)}  ",
        f"**Generated:** {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    # Summary by severity
    lines += [
        "## Summary by Severity",
        "",
        "| Severity | Count |",
        "|---|---|",
    ]
    counts = report.finding_count_by_severity
    for sev in ("critical", "high", "medium", "low"):
        icon = _sev_icon(sev)
        lines.append(f"| {icon} {sev} | {counts[sev]} |")

    lines += ["", f"**Total findings:** {len(report.findings)}", ""]

    # Baseline diff
    if report.baseline_diff is not None:
        bd = report.baseline_diff
        lines += ["## Baseline Diff (Backward Compatibility)", ""]
        if bd.removed:
            lines += [f"### Removed ({len(bd.removed)})", ""]
            for item in bd.removed:
                lines.append(f"- `{item}`")
            lines.append("")
        if bd.type_narrowed:
            lines += [f"### Type Narrowed ({len(bd.type_narrowed)})", ""]
            for item in bd.type_narrowed:
                lines.append(f"- `{item}`")
            lines.append("")
        if bd.new_required_fields:
            lines += [f"### New Required Fields ({len(bd.new_required_fields)})", ""]
            for item in bd.new_required_fields:
                lines.append(f"- `{item}`")
            lines.append("")
        if not bd.has_breaking_changes:
            lines += ["_No breaking changes detected._", ""]

    # Summary by lens
    if report.findings:
        lens_counts = report.finding_count_by_lens
        lines += [
            "## Summary by Lens",
            "",
            "| Lens | Count |",
            "|---|---|",
        ]
        lens_order = [
            "versioning",
            "error_shapes",
            "auth_spec",
            "idempotency",
            "backward_compatibility",
            "pagination",
            "rate_limiting",
        ]
        for lens in lens_order:
            count = lens_counts.get(lens, 0)
            if count > 0:
                lines.append(f"| {lens} | {count} |")
        lines.append("")

        # Findings table
        lines += [
            "## Findings",
            "",
            "| # | Lens | File | Line | Endpoint | Severity | Confidence "
            "| RFC | Breaking | Title |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for idx, f in enumerate(report.findings, 1):
            file_short = f.file.split("/")[-1]
            rfc = f.rfc_ref or "-"
            ep = f.endpoint or "-"
            breaking_str = "yes" if f.breaking else "no"
            lines.append(
                f"| {idx} | `{f.lens}` | `{file_short}` | {f.line} "
                f"| {ep} "
                f"| {_sev_icon(f.severity)} {f.severity} "
                f"| {f.confidence} ({f.confidence_score:.2f}) "
                f"| {rfc} "
                f"| {breaking_str} "
                f"| {f.title} |"
            )
        lines.append("")

        # Detail section
        lines += ["## Finding Details", ""]
        for idx, f in enumerate(report.findings, 1):
            lines += [
                f"### {idx}. [{f.lens}] {f.title}",
                "",
                f"**File:** `{f.file}` - Line {f.line}",
                f"**Severity:** {_sev_icon(f.severity)} {f.severity}  ",
                f"**Confidence:** {f.confidence} ({f.confidence_score:.2f})",
            ]
            if f.endpoint:
                lines.append(f"**Endpoint:** `{f.endpoint}`")
            if f.rfc_ref:
                lines.append(f"**RFC Ref:** {f.rfc_ref}")
            if f.breaking:
                lines.append("**Breaking:** yes")
            lines += ["", f.description]
            if f.suggestion:
                lines += ["", f"**Suggestion:** {f.suggestion}"]
            lines.append("")

    # Parsed endpoints
    if report.endpoints:
        lines += [
            "## Parsed Endpoints",
            "",
            "| Path/Channel/Type | Method/Op | Format | File | Line |",
            "|---|---|---|---|---|",
        ]
        _MAX_EPS_DISPLAYED = 30
        for ep_obj in report.endpoints[:_MAX_EPS_DISPLAYED]:
            ep_r: Endpoint = ep_obj
            file_short = ep_r.file.split("/")[-1]
            lines.append(
                f"| `{ep_r.path}` | `{ep_r.method_or_op}` | {ep_r.format} "
                f"| `{file_short}` | {ep_r.line} |"
            )
        if len(report.endpoints) > _MAX_EPS_DISPLAYED:
            lines.append(f"| ... | _{len(report.endpoints) - _MAX_EPS_DISPLAYED} more_ | | | |")
        lines.append("")

    # AI Quality
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
                "_These C11 findings overlapped with S40 findings at the same file:line. "
                "The S40 finding is canonical; these are moved here to avoid double-counting._",
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


def _render_json(report: APIContractReviewReport) -> str:
    findings_data = []
    for f in report.findings:
        findings_data.append(
            {
                "lens": f.lens,
                "file": f.file,
                "line": f.line,
                "endpoint": f.endpoint,
                "severity": f.severity,
                "confidence": f.confidence,
                "confidence_score": f.confidence_score,
                "title": f.title,
                "description": f.description,
                "suggestion": f.suggestion,
                "rfc_ref": f.rfc_ref,
                "breaking": f.breaking,
            }
        )

    endpoints_data = [
        {
            "path": ep.path,
            "method_or_op": ep.method_or_op,
            "format": ep.format,
            "file": ep.file,
            "line": ep.line,
        }
        for ep in report.endpoints
    ]

    baseline_diff_data: dict[str, Any] | None = None
    if report.baseline_diff is not None:
        bd = report.baseline_diff
        baseline_diff_data = {
            "removed": bd.removed,
            "type_narrowed": bd.type_narrowed,
            "new_required_fields": bd.new_required_fields,
            "has_breaking_changes": bd.has_breaking_changes,
        }

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
        "contract_path": report.contract_path,
        "format": report.format,
        "lenses": list(report.lenses),
        "endpoints": endpoints_data,
        "findings": findings_data,
        "finding_count": len(report.findings),
        "severity_counts": report.finding_count_by_severity,
        "lens_counts": report.finding_count_by_lens,
        "baseline_diff": baseline_diff_data,
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


def _render_sarif(report: APIContractReviewReport) -> str:
    # Build S40 run
    s40_rules: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    for f in report.findings:
        rule_id = f"S40/{f.lens}/{f.severity}"
        if rule_id not in seen_rule_ids:
            seen_rule_ids.add(rule_id)
            s40_rules.append(
                {
                    "id": rule_id,
                    "name": f"S40{f.lens.replace('_', '').title()}",
                    "shortDescription": {"text": f"S40 {f.lens} lens API contract review finding"},
                    "properties": {
                        "security-severity": f.severity,
                        **({"rfc_ref": f.rfc_ref} if f.rfc_ref else {}),
                    },
                }
            )

    s40_results: list[dict[str, Any]] = []
    for f in report.findings:
        result: dict[str, Any] = {
            "ruleId": f"S40/{f.lens}/{f.severity}",
            "level": _sev_to_sarif_level(f.severity),
            "message": {
                "text": (
                    f"[{f.confidence}] [{f.lens}] {f.title}: {f.description}"
                    + (f" Suggestion: {f.suggestion}" if f.suggestion else "")
                )
            },
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file},
                        "region": {"startLine": f.line},
                    }
                }
            ],
            "properties": {
                "lens": f.lens,
                "endpoint": f.endpoint,
                "confidence": f.confidence,
                "confidence_score": f.confidence_score,
                "rfc_ref": f.rfc_ref,
                "breaking": f.breaking,
            },
        }
        s40_results.append(result)

    s40_run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "forge-foundry-s40",
                "version": _TOOL_VERSION,
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                "rules": s40_rules,
            }
        },
        "results": s40_results,
    }

    runs: list[dict[str, Any]] = [s40_run]

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
# the `forge_foundry.renderers.api_contract_review` entry-point group override on
# same name. `render_report` dispatches through the registry so a `--format junit`
# value works once a plugin declares it — no edits here.
_REGISTRY: RendererRegistry[APIContractReviewReport] = RendererRegistry("api_contract_review")
_REGISTRY.register("markdown", _render_markdown)
_REGISTRY.register("json", _render_json)
_REGISTRY.register("sarif", _render_sarif)


def register_renderer(name: str, fn: Callable[[APIContractReviewReport], str]) -> None:
    """Register a renderer programmatically (in-process; for tests and downstream code)."""
    _REGISTRY.register(name, fn)


def list_formats() -> list[str]:
    """Names of all output formats discovered for this primitive (FLEX-3)."""
    return _REGISTRY.list_formats()


def render_report(report: APIContractReviewReport, output_format: str = "markdown") -> str:
    """Render an APIContractReviewReport in the requested format.

    Raises :class:`ValueError` for an unknown name with the available list, so
    a typo at the CLI surfaces fast.
    """
    return _REGISTRY.render(report, output_format)
