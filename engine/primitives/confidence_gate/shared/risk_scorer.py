"""C6 /confidence-gate — Stage B: deterministic risk scoring.

Risk math: sum(severity_weight * confidence_weight * primitive_weight * count)
per (severity, confidence_level) bucket.

Thresholds (default, overridable in policy):
  score < pass_threshold  → PASS
  pass_threshold ≤ score < review_threshold  → REVIEW
  score ≥ review_threshold  → FAIL
"""

from __future__ import annotations

from primitives.confidence_gate.shared.models import (
    GatePolicy,
    GateScore,
    NormalizedFinding,
    PrimitiveRiskSummary,
    Recommendation,
    ReportType,
    Severity,
)
from primitives.confidence_gate.shared.policy_loader import (
    get_primitive_weight,
)

_SEVERITY_ORDER: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


def _get_recommendation(
    score: float,
    pass_threshold: float,
    review_threshold: float,
) -> Recommendation:
    """Compute recommendation from score and thresholds."""
    if score < pass_threshold:
        return "pass"
    if score < review_threshold:
        return "review"
    return "fail"


def _per_primitive_score(
    findings: list[NormalizedFinding],
    policy: GatePolicy,
    report_type: ReportType,
) -> tuple[float, dict[Severity, int]]:
    """Compute contributed score and by-severity histogram for one primitive."""
    by_severity: dict[Severity, int] = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    total = 0.0

    primitive_weight = get_primitive_weight(report_type, policy)

    for finding in findings:
        sev_weight = policy.severity_weights.get(finding.severity, 1.0)
        conf_weight = policy.confidence_weights.get(finding.confidence_level, 0.5)
        total += sev_weight * conf_weight * primitive_weight
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1

    return total, by_severity


def compute_risk_score(
    summaries: list[PrimitiveRiskSummary],
    policy: GatePolicy,
) -> GateScore:
    """Compute the aggregate GateScore from per-primitive summaries.

    Args:
        summaries: List of PrimitiveRiskSummary (already computed per primitive).
        policy: The gate policy with weights and thresholds.

    Returns:
        A GateScore with total score, recommendation, and thresholds.
    """
    total = sum(s.contributed_score for s in summaries)

    pass_threshold = policy.thresholds.get("pass", 20.0)
    review_threshold = policy.thresholds.get("review", 50.0)
    recommendation = _get_recommendation(total, pass_threshold, review_threshold)

    return GateScore(
        score=total,
        recommendation=recommendation,
        threshold_pass=pass_threshold,
        threshold_review=review_threshold,
    )


def compute_primitive_summaries(
    findings_by_report: dict[ReportType, list[NormalizedFinding]],
    policy: GatePolicy,
) -> list[PrimitiveRiskSummary]:
    """Build PrimitiveRiskSummary objects for each report type.

    Args:
        findings_by_report: Mapping from report_type to its findings list.
        policy: The gate policy with weights.

    Returns:
        List of PrimitiveRiskSummary, one per report type present.
    """
    summaries: list[PrimitiveRiskSummary] = []
    for report_type, findings in findings_by_report.items():
        score, by_severity = _per_primitive_score(findings, policy, report_type)
        summaries.append(
            PrimitiveRiskSummary(
                report_type=report_type,
                total_findings=len(findings),
                by_severity=by_severity,
                contributed_score=score,
            )
        )
    return summaries
