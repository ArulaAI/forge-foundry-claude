"""S60 /data-flow-map — Shared helpers for the skill-first continue path.

Bundles the deterministic Stage B-D pipeline so the CLI can call it from
both the round-1 continue path (after PII judgments) and the extract-only
path (when there are no ambiguous fields).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.data_flow_map.shared.classification_loader import ClassificationEntry
    from primitives.data_flow_map.shared.models import FlowEdge, PIISource, RetentionMarker, Sink


def _run_stages_b_to_d(
    *,
    code_path: Path,
    sources: list[PIISource],
    catalog: list[ClassificationEntry],
    language: str = "auto",
    include_vendored: bool = False,
) -> tuple[list[FlowEdge], list[Sink], list[RetentionMarker]]:
    """Run the deterministic Stages B (flow tracing), C (sink classification),
    and D (retention check) and return their results.

    This is split out so both the round-1 --continue path and the no-ambiguous-
    fields --extract-only path can share the same logic without duplicating imports.

    Args:
        code_path: The root directory/file to scan.
        sources: Resolved PIISources (from Stage A.1 + applied A.2 judgments).
        catalog: The classification catalog (needed by Stage D).
        language: Language hint ('auto' for auto-detect).

    Returns:
        (flows, sinks, retention)
    """
    from primitives.data_flow_map.shared.flow_tracer import trace_flows  # noqa: PLC0415
    from primitives.data_flow_map.shared.retention_checker import check_retention  # noqa: PLC0415
    from primitives.data_flow_map.shared.sink_classifier import classify_sinks  # noqa: PLC0415

    flows = trace_flows(
        code_path=code_path, sources=sources, language=language, include_vendored=include_vendored
    )
    sinks = classify_sinks(
        code_path=code_path, sources=sources, language=language, include_vendored=include_vendored
    )
    retention = check_retention(
        code_path=code_path,
        sources=sources,
        catalog=catalog,
        language=language,
        include_vendored=include_vendored,
    )
    return flows, sinks, retention
