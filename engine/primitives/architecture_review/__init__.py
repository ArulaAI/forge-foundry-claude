"""C8 /architecture-review — public API."""

from __future__ import annotations

from primitives.architecture_review.shared.ai_quality_aggregator import (
    AIQualityReport,
    aggregate_ai_quality,
)
from primitives.architecture_review.shared.coupling_metrics import compute_coupling_metrics
from primitives.architecture_review.shared.cycle_detector import detect_cycles
from primitives.architecture_review.shared.dep_graph import build_dep_graph
from primitives.architecture_review.shared.layer_checker import detect_layer_violations
from primitives.architecture_review.shared.models import (
    ArchitectureReport,
    CouplingMetric,
    CyclicDependency,
    DepGraph,
    Language,
    LayerViolation,
    Module,
    SubjectiveIssue,
    SubjectiveIssueKind,
)
from primitives.architecture_review.shared.report_renderer import list_formats, render_report

__all__ = [
    "AIQualityReport",
    "ArchitectureReport",
    "CouplingMetric",
    "CyclicDependency",
    "DepGraph",
    "Language",
    "LayerViolation",
    "Module",
    "SubjectiveIssue",
    "SubjectiveIssueKind",
    "aggregate_ai_quality",
    "build_dep_graph",
    "compute_coupling_metrics",
    "detect_cycles",
    "detect_layer_violations",
    "list_formats",
    "render_report",
]
