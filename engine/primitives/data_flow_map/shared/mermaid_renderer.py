"""S60 /data-flow-map — Mermaid sequence diagram renderer.

Generates mermaid sequenceDiagram syntax.
Chunks diagrams at 30 nodes (spec §3.3).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

_MAX_NODES_PER_DIAGRAM = 30


def _safe_id(name: str) -> str:
    """Return a mermaid-safe participant ID (strip special chars)."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name) if name else "unknown"


if TYPE_CHECKING:
    from primitives.data_flow_map.shared.models import DataFlowReport, FlowEdge, PIISource, Sink


def _build_diagram(
    sources: list[PIISource],
    flows: list[FlowEdge],
    sinks: list[Sink],
    title: str,
) -> str:
    """Build a single mermaid sequenceDiagram block."""
    lines: list[str] = [
        '%%{init: {"theme": "default"}}%%',
        "sequenceDiagram",
        f"    %% {title}",
    ]

    # Declare participants: sources first, then unique intermediaries, then sinks
    participants: list[str] = []

    src_names = [s.name for s in sources]
    for name in src_names:
        if name not in participants:
            participants.append(name)

    # Intermediary nodes from flow edges
    for edge in flows:
        for node in (edge.from_node, edge.to_node):
            if node not in participants:
                participants.append(node)

    sink_names = [s.name for s in sinks]
    for name in sink_names:
        if name not in participants:
            participants.append(name)

    for p in participants:
        lines.append(f"    participant {_safe_id(p)} as {p}")

    lines.append("")

    # Draw flow edges
    for edge in flows:
        from_id = _safe_id(edge.from_node)
        to_id = _safe_id(edge.to_node)
        label = edge.transformation or "flows_to"
        lines.append(f"    {from_id}->>{to_id}: {label}")

    # Draw source → sink edges (when flows are empty or incomplete)
    for source in sources:
        src_id = _safe_id(source.name)
        for sink in sinks:
            if source.name in sink.references_pii:
                sink_id = _safe_id(sink.name)
                lines.append(f"    {src_id}->>{sink_id}: {sink.kind}")

    return "\n".join(lines)


def render_mermaid(report: DataFlowReport) -> list[str]:
    """Render the DataFlowReport as one or more mermaid sequenceDiagram blocks.

    Chunks at _MAX_NODES_PER_DIAGRAM nodes per diagram (spec §3.3).

    Args:
        report: DataFlowReport from the orchestrator.

    Returns:
        List of mermaid diagram strings (usually one; chunked if >30 nodes).
    """
    # Group sources by classification / severity for chunking
    all_sources = report.sources
    all_flows = report.flows
    all_sinks = report.sinks

    if not all_sources and not all_sinks:
        return [_build_diagram([], [], [], "No PII sources or sinks detected")]

    # Count unique nodes
    all_nodes: set[str] = set()
    for s in all_sources:
        all_nodes.add(s.name)
    for f in all_flows:
        all_nodes.add(f.from_node)
        all_nodes.add(f.to_node)
    for sk in all_sinks:
        all_nodes.add(sk.name)

    if len(all_nodes) <= _MAX_NODES_PER_DIAGRAM:
        return [
            _build_diagram(
                all_sources,
                all_flows,
                all_sinks,
                f"Data Flow Map — {report.code_path}",
            )
        ]

    # Chunk: one diagram per source (each source + its flows + relevant sinks)
    diagrams: list[str] = []
    for source in all_sources:
        source_flows = [f for f in all_flows if f.from_node == source.name]
        source_sinks = [sk for sk in all_sinks if source.name in sk.references_pii]
        chunk_nodes: set[str] = {source.name}
        for f in source_flows:
            chunk_nodes.add(f.from_node)
            chunk_nodes.add(f.to_node)
        for sk in source_sinks:
            chunk_nodes.add(sk.name)

        diagrams.append(
            _build_diagram(
                [source],
                source_flows,
                source_sinks,
                f"Data Flow: {source.name} ({source.classification}/{source.category})",
            )
        )

    return diagrams
