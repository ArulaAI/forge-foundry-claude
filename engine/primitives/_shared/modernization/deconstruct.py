"""Deconstruction renderer (Layer 1) -- SystemMap -> MODERNIZATION.md + system-map.json.

:func:`render_deconstruction` turns a :class:`SystemMap` (from
:func:`primitives._shared.modernization.correlate.build_system_map`) into the
two-artifact DECONSTRUCTION deliverable:

* A ``MODERNIZATION.md`` Markdown document with:

  * §1 Bounded Contexts -- per-domain grouping of nodes with their components
    and inferred surface types (data / API / UI).
  * §2 Cross-Layer Dependency Threads -- the full edge inventory as a table and
    a Mermaid flowchart showing UI→API→DB threads through the graph.
  * §3 Modernization Risk -- per-context confidence breakdown derived from edge
    confidence grades (``exact`` / ``heuristic`` / ``unresolved``).

* A machine ``system-map.json``-shaped dict (a curated, JSON-serializable view
  of the SystemMap) suitable for downstream tooling.

Pure and deterministic: no LLM, no clock, no filesystem.  Reuses the Mermaid
and table helpers from :mod:`primitives._shared.modernization.render` -- no
Mermaid logic is duplicated here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._shared.modernization.render import (
    component_diagram,
    flowchart,
    heading,
    md_table,
)

# WS1 (#1449) single-source-of-truth honesty helpers, reused so the deconstruction
# narrative and the target-stack ADR never diverge. Same package, no import cycle
# (scope imports render + this module does not import scope elsewhere).
from primitives._shared.modernization.scope import (
    _entry_points_section,
    _ordered_target_dims,
    _target_dim_label,
)

if TYPE_CHECKING:
    from primitives._shared.modernization.models import SystemMap
    from primitives._shared.modernization.target_stack import TargetStack

# ---------------------------------------------------------------------------
# Surface-type heuristic
# ---------------------------------------------------------------------------

#: Node ``kind`` substrings that indicate a UI-tier component.
_UI_KINDS: frozenset[str] = frozenset(
    {"ui", "frontend", "web", "react", "angular", "vue", "spa", "html", "css"}
)

#: Node ``kind`` substrings that indicate a data-tier component.
_DATA_KINDS: frozenset[str] = frozenset(
    {"db", "database", "data", "sql", "postgres", "mysql", "oracle", "mongo", "redis", "kafka"}
)


def _surface_type(kind: str) -> str:
    """Classify a node ``kind`` into a surface type label.

    Returns ``"UI"``, ``"Data"``, or ``"API"`` (the catch-all for everything
    that is not clearly UI or data-tier).
    """
    k = kind.lower()
    for token in _UI_KINDS:
        if token in k:
            return "UI"
    for token in _DATA_KINDS:
        if token in k:
            return "Data"
    return "API"


# ---------------------------------------------------------------------------
# Risk derivation
# ---------------------------------------------------------------------------


def _context_risk(unresolved: int, heuristic: int) -> str:
    """Derive a qualitative risk level from the edge-confidence breakdown.

    * ``high``   — any unresolved dependencies (external/unknown targets).
    * ``medium`` — no unresolved but at least one heuristic (fuzzy) match.
    * ``low``    — all dependencies are exact matches (or no dependencies).
    """
    if unresolved > 0:
        return "high"
    if heuristic > 0:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_bounded_contexts(
    contexts: dict[str, list[str]],
    node_kind: dict[str, str],
) -> str:
    """Render §1 Bounded Contexts.

    Args:
        contexts: mapping of domain label → list of node ids in that domain.
        node_kind: mapping of node id → ``kind`` string.

    Returns:
        Markdown section string.
    """
    lines: list[str] = [heading("Bounded Contexts", level=2), ""]
    if not contexts:
        lines.append("_No bounded contexts detected — SystemMap has no nodes with domain labels._")
        return "\n".join(lines)

    for domain in sorted(contexts):
        node_ids = sorted(contexts[domain])
        lines.append(heading(domain, level=3))
        lines.append("")
        headers = ["Component", "Surface Type", "Kind"]
        rows = [
            [nid, _surface_type(node_kind.get(nid, "")), node_kind.get(nid, "")] for nid in node_ids
        ]
        lines.append(md_table(headers, rows))
        lines.append("")

    return "\n".join(lines)


def _render_dependency_threads(
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
) -> str:
    """Render §2 Cross-Layer Dependency Threads.

    Args:
        nodes: raw node dicts from the SystemMap model dump.
        edges: raw edge dicts from the SystemMap model dump.

    Returns:
        Markdown section string (includes a table and a Mermaid flowchart).
    """
    lines: list[str] = [heading("Cross-Layer Dependency Threads", level=2), ""]

    if not edges:
        lines.append("_No dependency edges detected in the SystemMap._")
        return "\n".join(lines)

    # Edge inventory table.
    headers = ["Source", "Target", "Kind", "Evidence", "Confidence"]
    rows = [
        [
            str(e["src"]),
            str(e["dst"]),
            str(e.get("kind", "")),
            str(e.get("evidence", "")),
            str(e.get("confidence", "")),
        ]
        for e in edges
    ]
    lines.append(md_table(headers, rows))
    lines.append("")

    # Mermaid flowchart: all nodes + edges.
    fc_nodes = [
        (
            str(n["id"]),
            f"{n['id']} ({n.get('kind', '')})" if n.get("kind") else str(n["id"]),
        )
        for n in nodes
    ]
    fc_edges: list[tuple[str, str, str]] = [
        (str(e["src"]), str(e["dst"]), str(e.get("confidence", ""))) for e in edges
    ]
    lines.append(flowchart(fc_nodes, fc_edges, direction="LR"))
    lines.append("")

    return "\n".join(lines)


def _render_entry_point_component_view(
    display_contexts: dict[str, list[str]],
    entry_points: list[dict[str, object]],
) -> str:
    """Render §"Entry-Point Component View (C4)" — the inbound tier over the contexts.

    Track B (#1427/#1455): reuses :func:`component_diagram`'s entry-point logic so the
    top-level MODERNIZATION.md gains a distinctly-styled inbound entry-point tier
    (``classDef`` + ``triggers`` edges) wired into the bounded contexts — the C4 tier
    the issue calls for, present regardless of whether a modernization ``target`` is
    set. Empty *entry_points* → the section omits entirely (byte-identical output)."""
    if not entry_points:
        return ""
    contexts = [(domain, domain) for domain in sorted(display_contexts)]
    ep_tuples: list[tuple[str, str, str]] = [
        (str(ep.get("name", "")), str(ep.get("kind", "")), str(ep.get("target", "")))
        for ep in entry_points
    ]
    lines: list[str] = [
        heading("Entry-Point Component View (C4)", level=2),
        "",
        "The system's inbound triggers (HTTP routes, scheduled/batch jobs, message "
        "consumers, …) as a distinct entry-point tier feeding the bounded contexts:",
        "",
        component_diagram(contexts, [], entry_points=ep_tuples, direction="LR"),
        "",
    ]
    return "\n".join(lines)


def _render_infrastructure_boundary(
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
) -> str:
    """Render §"Infrastructure / Deployment Boundary" — IaC nodes + provision edges.

    MR6 (#1555): an Infrastructure-as-Code node (``kind == "iac"``) is a deployment
    boundary, not an extractable app unit. This purely-additive section lists every
    IaC node and the ``kind == "provisions"`` edges from it to the app/db units it
    provisions. Returns ``""`` when the map carries no IaC node — so a map without one
    (every existing fixture) renders byte-identically.
    """
    iac_ids = [str(n["id"]) for n in nodes if n.get("kind") == "iac"]
    if not iac_ids:
        return ""
    lines: list[str] = [
        heading("Infrastructure / Deployment Boundary", level=2),
        "",
        "Infrastructure-as-Code (Terraform / CloudFormation / SAM / Helm) provisions "
        "the units below. IaC nodes are a **deployment boundary** — they are not "
        "extractable application units and are excluded from the extraction roadmap.",
        "",
    ]
    lines.append(heading("IaC sources", level=3))
    lines.append("")
    lines.append(md_table(["IaC node"], [[nid] for nid in sorted(iac_ids)]))
    lines.append("")
    provision_edges = [
        e for e in edges if e.get("kind") == "provisions" and str(e["src"]) in set(iac_ids)
    ]
    lines.append(heading("Provisions", level=3))
    lines.append("")
    if provision_edges:
        rows = [
            [str(e["src"]), str(e["dst"]), str(e.get("evidence", ""))]
            for e in sorted(provision_edges, key=lambda e: (str(e["src"]), str(e["dst"])))
        ]
        lines.append(md_table(["IaC node", "Provisions", "Resource"], rows))
    else:
        lines.append("_No provisioned unit matched — the IaC resources map to no analysed unit._")
    lines.append("")
    return "\n".join(lines)


def _render_modernization_risk(
    contexts: dict[str, list[str]],
    edge_confidence_by_src: dict[str, list[str]],
) -> str:
    """Render §3 Modernization Risk per bounded context.

    Args:
        contexts: mapping of domain label → list of node ids.
        edge_confidence_by_src: mapping of node id → list of confidence values
            for outgoing edges from that node.

    Returns:
        Markdown section string.
    """
    lines: list[str] = [heading("Modernization Risk", level=2), ""]

    if not contexts:
        lines.append("_No bounded contexts to assess._")
        return "\n".join(lines)

    headers = ["Context", "Exact", "Heuristic", "Unresolved", "Risk"]
    rows: list[list[str]] = []
    for domain in sorted(contexts):
        exact = heuristic = unresolved = 0
        for nid in contexts[domain]:
            for conf in edge_confidence_by_src.get(nid, []):
                if conf == "exact":
                    exact += 1
                elif conf == "heuristic":
                    heuristic += 1
                elif conf == "unresolved":
                    unresolved += 1
        risk = _context_risk(unresolved, heuristic)
        rows.append([domain, str(exact), str(heuristic), str(unresolved), risk])

    lines.append(md_table(headers, rows))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Target section renderers (WS3, #1449)
# ---------------------------------------------------------------------------


def _render_modernization_target(target: TargetStack) -> str:
    """Render the "Modernization Target" section from a resolved :class:`TargetStack`.

    Deterministic and honest: each dimension is rendered with the WS1 label
    helper (registry label + ``version_source`` honesty; a ``registry_matched=False``
    dimension carried verbatim and flagged ``(unverified)``). No wall-clock, no LLM,
    no fabrication — the target is a *fact*. The caller guards on
    ``target is not None and target.dimensions`` so this is only reached with ≥1
    dimension.
    """
    lines: list[str] = [heading("Modernization Target", level=2), ""]
    lines.append(
        "The modernization designs *toward* the following declared target stack "
        "(resolved offline against the bundled registry — never a live lookup):"
    )
    lines.append("")
    headers = ["Dimension", "Target"]
    rows = [[dim.key, _target_dim_label(dim)] for dim in _ordered_target_dims(target)]
    lines.append(md_table(headers, rows))
    lines.append("")
    return "\n".join(lines)


def _render_target_component_view(
    display_contexts: dict[str, list[str]],
    edges: list[dict[str, object]],
    target: TargetStack,
) -> str:
    """Render §"Target Component View (C4)" — target tiers over the bounded contexts.

    Surfaces the declared target-stack dimensions as C4 component tiers (Angular
    SPA → Spring Boot Service → PostgreSQL Database) feeding the discovered
    bounded contexts, reusing the existing :func:`component_diagram` target logic
    (NOT reimplemented here). Bounded contexts become the component nodes; a
    cross-context edge is drawn whenever a node in one context depends on a node
    in another. The caller guards on ``target is not None and target.dimensions``.
    """
    # Map each node id to the context(s) it belongs to (a node may be multi-domain).
    node_to_contexts: dict[str, list[str]] = {}
    for ctx, node_ids in display_contexts.items():
        for nid in node_ids:
            node_to_contexts.setdefault(nid, []).append(ctx)

    # Context-level component nodes: (context_id, label) with the surface mix.
    context_ids = sorted(display_contexts)
    contexts: list[tuple[str, str]] = [(ctx, ctx) for ctx in context_ids]

    # Cross-context dependency edges, deduplicated + deterministically ordered.
    seen: set[tuple[str, str]] = set()
    ctx_edges: list[tuple[str, str, str]] = []
    for e in edges:
        for src_ctx in node_to_contexts.get(str(e["src"]), []):
            for dst_ctx in node_to_contexts.get(str(e["dst"]), []):
                if src_ctx == dst_ctx:
                    continue
                if (src_ctx, dst_ctx) in seen:
                    continue
                seen.add((src_ctx, dst_ctx))
                ctx_edges.append((src_ctx, dst_ctx, ""))
    ctx_edges.sort()

    lines: list[str] = [heading("Target Component View (C4)", level=2), ""]
    lines.append(
        "The declared target-stack tiers (presentation → application → data) over "
        "the discovered bounded contexts they host:"
    )
    lines.append("")
    lines.append(component_diagram(contexts, ctx_edges, target))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_deconstruction(
    system_map: SystemMap,
    *,
    title: str = "System Deconstruction",
    target: TargetStack | None = None,
) -> tuple[str, dict[str, object]]:
    """Render the DECONSTRUCTION artifact from a :class:`SystemMap`.

    Args:
        system_map: the cross-unit dependency graph produced by
            :func:`primitives._shared.modernization.correlate.build_system_map`.
        title: the top-level ``# Heading`` for the generated Markdown document.
        target: an optional resolved
            :class:`~primitives._shared.modernization.target_stack.TargetStack`
            (WS3, #1449). When it carries ≥1 dimension, two purely-additive
            sections are appended after the title — a "Modernization Target"
            table (labels + ``version_source`` honesty; ``registry_matched=False``
            carried verbatim + flagged ``(unverified)``) and a "Target Component
            View (C4)" diagram surfacing the target tiers over the bounded
            contexts (reusing :func:`component_diagram`'s target logic). ``None``
            or an empty stack → neither section is added and the output (markdown
            **and** the machine dict) is byte-identical to the no-target path.

    Returns:
        A two-tuple ``(markdown, machine_dict)`` where:

        * *markdown* is the full ``MODERNIZATION.md`` content as a ``str``.
        * *machine_dict* is a JSON-serializable ``dict`` with top-level keys
          ``schema_version``, ``generated_at``, ``nodes``, ``edges``, and
          ``contexts`` suitable for writing as ``system-map.json``.
    """
    dump = system_map.model_dump(mode="json")
    nodes: list[dict[str, object]] = dump.get("nodes", [])
    edges: list[dict[str, object]] = dump.get("edges", [])

    # Index: node_id → kind
    node_kind: dict[str, str] = {str(n["id"]): str(n.get("kind", "")) for n in nodes}

    # Group nodes into bounded contexts by their ``domains`` field (#1375). A
    # multi-domain node appears in *every* one of its context buckets — the
    # bounded contexts are the union of all nodes' domains, not just each node's
    # first label. Nodes with no domains fall into an "_unassigned_" bucket so
    # they still appear in the risk section; the bucket is not shown in the
    # contexts section unless it is the only one.
    contexts: dict[str, list[str]] = {}
    for n in nodes:
        raw_domains = n.get("domains") or []
        node_domains = (
            [str(dom) for dom in raw_domains if dom] if isinstance(raw_domains, list) else []
        )
        buckets = node_domains if node_domains else ["_unassigned_"]
        for bucket in buckets:
            contexts.setdefault(bucket, []).append(str(n["id"]))

    # Separate the display contexts (named domains) from the unassigned bucket.
    named_contexts = {k: v for k, v in contexts.items() if k != "_unassigned_"}
    display_contexts = named_contexts if named_contexts else contexts

    # Edge-confidence index: src node id → list of confidence values.
    edge_confidence_by_src: dict[str, list[str]] = {}
    for e in edges:
        src = str(e["src"])
        edge_confidence_by_src.setdefault(src, []).append(str(e.get("confidence", "")))

    # Build the machine artifact (JSON-serializable curated view).
    machine: dict[str, object] = {
        "schema_version": dump.get("schema_version", "1.0"),
        "generated_at": dump.get("generated_at"),
        "nodes": nodes,
        "edges": edges,
        # Track B (#1427/#1455): the typed inbound triggers, carried into
        # system-map.json so a consumer can read the entry-point rollup
        # machine-side. Empty list when the map has none (byte-identical shape).
        "entry_points": dump.get("entry_points", []),
        "contexts": {
            domain: {
                "components": sorted(node_ids),
                "risk": _context_risk(
                    sum(
                        1
                        for nid in node_ids
                        for conf in edge_confidence_by_src.get(nid, [])
                        if conf == "unresolved"
                    ),
                    sum(
                        1
                        for nid in node_ids
                        for conf in edge_confidence_by_src.get(nid, [])
                        if conf == "heuristic"
                    ),
                ),
            }
            for domain, node_ids in sorted(contexts.items())
        },
    }

    # Assemble the Markdown document. The target sections (WS3, #1449) are purely
    # additive and guarded on a non-empty target: a no-target run yields byte-for-byte
    # the pre-WS3 markdown (and the machine dict is never touched by the target at all).
    has_target = target is not None and bool(target.dimensions)
    parts: list[str] = [heading(title, level=1), ""]
    if has_target:
        assert target is not None  # narrowed by has_target; for the type checker.
        parts.append(_render_modernization_target(target))
    parts.append(_render_bounded_contexts(display_contexts, node_kind))
    # Track B (#1427/#1455): the Entry Points rollup (or an honest ``⚠️ requires``
    # stub when a web surface exists but no triggers were captured). Empty + no web
    # surface → no section (byte-identical to the pre-#1455 markdown).
    entry_points_lines = _entry_points_section(system_map)
    if entry_points_lines:
        parts.append("\n".join(entry_points_lines))
    # Track B (#1427/#1455): the C4 entry-point tier over the bounded contexts, so
    # the top-level deliverable gains the inbound tier regardless of a target.
    ep_dump = dump.get("entry_points") or []
    entry_point_c4 = _render_entry_point_component_view(
        display_contexts, ep_dump if isinstance(ep_dump, list) else []
    )
    if entry_point_c4:
        parts.append(entry_point_c4)
    parts.append(_render_dependency_threads(nodes, edges))
    # MR6 (#1555): the Infrastructure / Deployment Boundary section (IaC nodes +
    # provision edges). Empty string when the map has no ``kind == "iac"`` node, so a
    # map without one renders byte-identically.
    infra_section = _render_infrastructure_boundary(nodes, edges)
    if infra_section:
        parts.append(infra_section)
    if has_target:
        assert target is not None
        parts.append(_render_target_component_view(display_contexts, edges, target))
    parts.append(_render_modernization_risk(display_contexts, edge_confidence_by_src))
    markdown = "\n".join(parts)

    return markdown, machine
