"""S60 /data-flow-map — PII/data-flow inventory primitive.

The harness-driven orchestrator and per-stage LLM entry points were removed with
the harness layer; the CLI now runs the skill-first extract→judge→continue
protocol. Only the deterministic stages and the shared models are exported.

Public API:
  load_classification    — catalog loader
  detect_sources_deterministic — Stage A.1 (regex/AST)
  trace_flows            — Stage B
  classify_sinks         — Stage C
  check_retention        — Stage D
  aggregate_ai_quality   — sub-call §3.4
  render_report          — report renderer

Model exports:
  DataClassification, Category, SinkKind, RetentionStatus
  PIISource, FlowEdge, Sink, RetentionMarker, PurposeIssue, DataFlowReport
"""

from primitives.data_flow_map.shared.ai_quality_aggregator import (
    AIQualityReport,
    aggregate_ai_quality,
)
from primitives.data_flow_map.shared.classification_loader import load_classification
from primitives.data_flow_map.shared.flow_tracer import trace_flows
from primitives.data_flow_map.shared.models import (
    Category,
    DataClassification,
    DataFlowReport,
    FlowEdge,
    PIISource,
    PurposeIssue,
    RetentionMarker,
    RetentionStatus,
    Sink,
    SinkKind,
)
from primitives.data_flow_map.shared.report_renderer import list_formats, render_report
from primitives.data_flow_map.shared.retention_checker import check_retention
from primitives.data_flow_map.shared.sink_classifier import classify_sinks
from primitives.data_flow_map.shared.source_detector import detect_sources_deterministic

__all__ = [  # noqa: RUF022 — intentional grouping (stages, literals, models)
    # Pipeline stages
    "load_classification",
    "detect_sources_deterministic",
    "trace_flows",
    "classify_sinks",
    "check_retention",
    "aggregate_ai_quality",
    "list_formats",
    "render_report",
    # Literals
    "DataClassification",
    "Category",
    "SinkKind",
    "RetentionStatus",
    # Models
    "PIISource",
    "FlowEdge",
    "Sink",
    "RetentionMarker",
    "PurposeIssue",
    "DataFlowReport",
    "AIQualityReport",
]
