"""C6 /confidence-gate — orchestrator helpers.

The harness-driven ``confidence_gate(...)`` orchestrator was removed with the
harness layer (the CLI now runs the skill-first extract→judge→continue
protocol). Only the deterministic ``run_pre_llm`` helper — Stages A + B, the
intermediates the ``--extract-only`` path persists — remains.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.confidence_gate.shared.policy_loader import load_gate_policy
from primitives.confidence_gate.shared.report_dispatchers import parse_report
from primitives.confidence_gate.shared.risk_scorer import (
    compute_primitive_summaries,
    compute_risk_score,
)

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.confidence_gate.shared.models import (
        GateScore,
        NormalizedFinding,
        PrimitiveRiskSummary,
        ReportType,
    )


def run_pre_llm(
    report_paths: list[Path],
    policy_path: Path | None = None,
) -> tuple[list[PrimitiveRiskSummary], GateScore, list[NormalizedFinding]]:
    """Run Stages A and B of the C6 pipeline without the LLM call.

    Used by the skill-first --extract-only path to compute deterministic
    intermediates (risk summaries + gate score + flattened findings) that
    are later consumed by --continue after in-session judgment.

    Args:
        report_paths: List of paths to upstream review JSON reports.
        policy_path: Optional path to a gate-policy.yaml file.

    Returns:
        (primitive_summaries, gate_score, all_findings)

    Raises:
        FileNotFoundError: If a report path does not exist.
        ValueError: If a report is malformed or policy is invalid.
    """
    policy = load_gate_policy(policy_path)
    findings_by_report: dict[ReportType, list[NormalizedFinding]] = {}

    for path in report_paths:
        if not path.exists():
            raise FileNotFoundError(f"Report path not found: {path}")
        report_type, findings = parse_report(path)
        if report_type in findings_by_report:
            findings_by_report[report_type].extend(findings)
        else:
            findings_by_report[report_type] = list(findings)

    primitive_summaries: list[PrimitiveRiskSummary] = compute_primitive_summaries(
        findings_by_report, policy
    )
    gate_score = compute_risk_score(primitive_summaries, policy)

    all_findings: list[NormalizedFinding] = []
    for findings in findings_by_report.values():
        all_findings.extend(findings)

    return primitive_summaries, gate_score, all_findings
