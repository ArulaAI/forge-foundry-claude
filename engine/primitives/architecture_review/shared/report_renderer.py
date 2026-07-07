"""C8 /architecture-review — Report renderer.

Renders an ArchitectureReport to markdown, JSON, or SARIF 2.1.0.

SARIF multi-tool:
  - runs[0]: C8 (layer violations, cycles, coupling, subjective)
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
    from primitives.architecture_review.shared.models import (
        ArchitectureReport,
        CyclicDependency,
        LayerViolation,
        SubjectiveIssue,
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


def _sev_icon(severity: str) -> str:
    return {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(severity, "⚪")


def _sev_to_sarif_level(severity: str) -> str:
    return {"low": "note", "medium": "warning", "high": "error", "critical": "error"}.get(
        severity, "warning"
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _render_markdown(report: ArchitectureReport) -> str:
    lines: list[str] = [
        "# Architecture Review Report",
        "",
        f"**Code path:** `{report.code_path}`  ",
        f"**Generated:** {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    # Summary counts
    n_violations = len(report.layer_violations)
    n_cycles = len(report.cycles)
    n_subjective = len(report.subjective)
    lines += [
        "## Summary",
        "",
        "| Finding Type | Count |",
        "|---|---|",
        f"| Layer violations | {n_violations} |",
        f"| Cyclic dependencies | {n_cycles} |",
        f"| Coupling metrics | {len(report.coupling)} modules |",
        f"| Subjective issues | {n_subjective} |",
        "",
    ]

    # Layer violations
    if report.layer_violations:
        lines += ["## Layer Violations", ""]
        for v in report.layer_violations:
            src_tag = f" _(source: {v.source})_" if v.source != "c8_static" else ""
            lines += [
                f"### {_sev_icon(v.severity)} `{v.from_module}` → `{v.to_module}`{src_tag}",
                "",
                f"- **Expected direction:** {v.expected_direction}",
                f"- **Actual:** {v.actual_direction}",
                f"- **Severity:** {v.severity}",
                "",
            ]

    # Cyclic dependencies
    if report.cycles:
        lines += ["## Cyclic Dependencies", ""]
        for cycle in report.cycles:
            mods = ", ".join(f"`{m}`" for m in cycle.modules)
            lines += [
                f"### Cycle: {mods}",
                "",
                f"- **Size:** {cycle.size} modules",
                "",
            ]

    # Coupling metrics
    if report.coupling:
        from primitives.architecture_review.shared.coupling_metrics import (  # noqa: PLC0415
            is_fragile_junction,
        )

        fragile = [m for m in report.coupling.values() if is_fragile_junction(m)]
        lines += [
            "## Coupling Metrics",
            "",
            "| Module | Afferent | Efferent | Instability |",
            "|--------|----------|----------|-------------|",
        ]
        for m in sorted(report.coupling.values(), key=lambda x: -x.instability):
            flag = " ⚠️" if is_fragile_junction(m) else ""
            lines.append(
                f"| `{m.module}` | {m.afferent} | {m.efferent} | {m.instability:.2f}{flag} |"
            )
        lines.append("")
        if fragile:
            lines += [
                f"**Fragile junctions (instability > 0.8 AND efferent > 5):** "
                f"{', '.join(f'`{m.module}`' for m in fragile)}",
                "",
            ]

    # Subjective issues
    if report.subjective:
        lines += ["## Subjective Issues (LLM-judged)", ""]
        for issue in report.subjective:
            mod_str = f" in `{issue.module}`" if issue.module else ""
            lines += [
                f"### {_sev_icon(issue.severity)} [{issue.kind}]{mod_str}",
                "",
                f"{issue.description}",
                "",
                f"*Severity: {issue.severity}*",
                "",
            ]

    # AI quality
    if report.ai_quality:
        lines += [
            "## AI Quality (Sub-call §3.4)",
            "",
            report.ai_quality.summary,
            "",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _report_to_dict(report: ArchitectureReport) -> dict[str, Any]:
    return {
        "code_path": report.code_path,
        "generated_at": report.generated_at.isoformat(),
        "layer_violations": [v.model_dump() for v in report.layer_violations],
        "coupling": {name: m.model_dump() for name, m in report.coupling.items()},
        "cycles": [c.model_dump() for c in report.cycles],
        "subjective": [s.model_dump() for s in report.subjective],
        "ai_quality": {
            "summary": report.ai_quality.summary,
            "c10_count": len(report.ai_quality.c10_findings),
            "c11_count": len(report.ai_quality.c11_findings),
            "has_critical": report.ai_quality.has_critical,
        }
        if report.ai_quality
        else None,
    }


def _render_json(report: ArchitectureReport) -> str:
    return json.dumps(_report_to_dict(report), indent=2)


# ---------------------------------------------------------------------------
# SARIF 2.1.0
# ---------------------------------------------------------------------------


def _violation_to_sarif_result(v: LayerViolation) -> dict[str, Any]:
    return {
        "ruleId": "c8.layer-violation",
        "level": _sev_to_sarif_level(v.severity),
        "message": {
            "text": (
                f"Layer violation: {v.from_module} -> {v.to_module}. "
                f"Expected: {v.expected_direction}. "
                f"Actual: {v.actual_direction}. "
                f"Source: {v.source}"
            )
        },
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": v.from_module},
                }
            }
        ],
    }


def _cycle_to_sarif_result(c: CyclicDependency) -> dict[str, Any]:
    return {
        "ruleId": "c8.cyclic-dependency",
        "level": "error",
        "message": {
            "text": (
                f"Cyclic dependency of size {c.size}: "
                + " -> ".join(c.modules)
                + f" -> {c.modules[0]}"
            )
        },
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": c.modules[0]},
                }
            }
        ],
    }


def _subjective_to_sarif_result(s: SubjectiveIssue) -> dict[str, Any]:
    return {
        "ruleId": f"c8.subjective.{s.kind}",
        "level": _sev_to_sarif_level(s.severity),
        "message": {"text": s.description},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": s.module or "(project-level)"},
                }
            }
        ],
    }


def _finding_to_sarif_result(f: Finding, tool_prefix: str) -> dict[str, Any]:
    return {
        "ruleId": f"{tool_prefix}.{f.rule_id}",
        "level": _sev_to_sarif_level(f.severity),
        "message": {"text": f.message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": str(f.file)},
                    "region": {"startLine": f.line},
                }
            }
        ],
    }


def _render_sarif(report: ArchitectureReport) -> str:
    c8_results: list[dict[str, Any]] = []
    for v in report.layer_violations:
        c8_results.append(_violation_to_sarif_result(v))
    for c in report.cycles:
        c8_results.append(_cycle_to_sarif_result(c))
    for s in report.subjective:
        c8_results.append(_subjective_to_sarif_result(s))

    c8_run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "forge-foundry-c8-architecture-review",
                "version": _TOOL_VERSION,
                "rules": [
                    {
                        "id": "c8.layer-violation",
                        "name": "LayerViolation",
                        "shortDescription": {
                            "text": "Module imports across prohibited layer boundary"
                        },
                    },
                    {
                        "id": "c8.cyclic-dependency",
                        "name": "CyclicDependency",
                        "shortDescription": {
                            "text": "Strongly-connected component (cycle) of size >= 2"
                        },
                    },
                    {
                        "id": "c8.subjective.abstraction",
                        "name": "AbstractionIssue",
                        "shortDescription": {"text": "Abstraction quality issue identified by LLM"},
                    },
                    {
                        "id": "c8.subjective.api_design",
                        "name": "ApiDesignIssue",
                        "shortDescription": {"text": "API design issue identified by LLM"},
                    },
                    {
                        "id": "c8.subjective.cross_cutting_leak",
                        "name": "CrossCuttingLeakIssue",
                        "shortDescription": {
                            "text": "Cross-cutting concerns leakage identified by LLM"
                        },
                    },
                ],
            }
        },
        "results": c8_results,
    }

    runs: list[dict[str, Any]] = [c8_run]

    if report.ai_quality:
        # C10 run
        c10_results = [_finding_to_sarif_result(f, "c10") for f in report.ai_quality.c10_findings]
        runs.append(
            {
                "tool": {
                    "driver": {
                        "name": "forge-foundry-c10-failure-mode-audit",
                        "version": _TOOL_VERSION,
                    }
                },
                "results": c10_results,
            }
        )

        # C11 run
        c11_results = [_finding_to_sarif_result(f, "c11") for f in report.ai_quality.c11_findings]
        runs.append(
            {
                "tool": {
                    "driver": {
                        "name": "forge-foundry-c11-production-failure-audit",
                        "version": _TOOL_VERSION,
                    }
                },
                "results": c11_results,
            }
        )

    sarif: dict[str, Any] = {
        "version": _SARIF_VERSION,
        "$schema": _SARIF_SCHEMA_URI,
        "runs": runs,
    }
    return json.dumps(sarif, indent=2)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def list_formats() -> list[str]:
    """Return the list of supported output formats (FLEX-3)."""
    return list(_FORMATS)


def render_report(
    report: ArchitectureReport,
    output_format: str = "markdown",
) -> str:
    """Render *report* to the requested format.

    Args:
        report: The ArchitectureReport to render.
        output_format: One of 'markdown', 'json', 'sarif'.

    Returns:
        Rendered string.
    """
    if output_format not in _FORMATS:
        raise ValueError(f"unknown output format {output_format!r}; valid: {', '.join(_FORMATS)}")
    if output_format == "json":
        return _render_json(report)
    elif output_format == "sarif":
        return _render_sarif(report)
    else:
        return _render_markdown(report)
