"""C6 /confidence-gate — public API.

Aggregator primitive. Consumes already-produced JSON reports from upstream
review primitives and computes a deterministic PASS/REVIEW/FAIL risk verdict.
"""

from __future__ import annotations

from primitives.confidence_gate.shared.models import (
    ConfidenceGateReport,
    ConfidenceLevel,
    GateScore,
    NormalizedFinding,
    PrimitiveRiskSummary,
    Recommendation,
    ReportType,
    Severity,
    TopFinding,
)
from primitives.confidence_gate.shared.orchestrator import run_pre_llm
from primitives.confidence_gate.shared.policy_loader import load_gate_policy
from primitives.confidence_gate.shared.rationale_drafter import (
    apply_rationale_judgment,
    build_prompt_item_for_rationale,
)
from primitives.confidence_gate.shared.report_dispatchers import detect_report_type, parse_report
from primitives.confidence_gate.shared.report_renderer import list_formats, render_report
from primitives.confidence_gate.shared.risk_scorer import compute_risk_score

__all__ = [
    "ConfidenceGateReport",
    "ConfidenceLevel",
    "GateScore",
    "NormalizedFinding",
    "PrimitiveRiskSummary",
    "Recommendation",
    "ReportType",
    "Severity",
    "TopFinding",
    "apply_rationale_judgment",
    "build_prompt_item_for_rationale",
    "compute_risk_score",
    "detect_report_type",
    "list_formats",
    "load_gate_policy",
    "parse_report",
    "render_report",
    "run_pre_llm",
]
