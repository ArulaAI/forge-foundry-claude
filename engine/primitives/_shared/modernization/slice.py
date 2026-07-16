"""System-map dependency-closure engine for per-feature slice scoping (Layer 1, #1540).

To modernize a single bounded context self-containedly, the caller needs that context's
**dependency closure** — the units it (transitively) consumes interfaces from, the
``data:<table>`` it reads, and the shared libraries it uses. This module computes that
closure over a :class:`~primitives._shared.modernization.models.SystemMap` (whose edges
are the ``src → dst`` dependency arcs ``correlate.build_system_map`` emits) and provides
the induced subgraph so ``sequencer.order`` can wave *just the slice + its prerequisites*
into a scoped roadmap.

The split is the load-bearing distinction for slice scoping:

* **in_scope** — the units the operator asked to modernize (fully specced downstream).
* **dependencies** — everything reachable from them via out-edges that is NOT in scope
  (documented as boundary interface contracts, never re-specced).

Pure and deterministic: no LLM, no clock, no filesystem. Imports only sibling ``_shared``
modules, so it stays clean under the QUAL-2 cross-primitive-import guard.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from primitives._shared.modernization.models import SystemMap, SystemMapEdge, SystemMapNode

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Node kinds that are never a modernizable unit — an external/unknown integration or a
#: shared library. They may appear in a slice's ``dependencies`` (as a boundary contract),
#: but never in ``in_scope`` (there is nothing to deconstruct in them).
_NON_UNIT_KINDS = frozenset({"external", "shared_lib"})


class SliceClosure(BaseModel):
    """A bounded-context slice + its transitive dependency closure over the system-map.

    ``in_scope`` are the requested units (modernized fully). ``dependencies`` are the
    out-edge-reachable units NOT in scope — the **boundary** the slice depends on,
    documented as interface contracts rather than re-specced. ``internal_edges`` join two
    in-scope units; ``boundary_edges`` cross from an in-scope unit to a dependency (the
    "depends on X because of interface Y" evidence). All fields are deterministically
    sorted so a rendered boundary section / roadmap is reproducible.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    in_scope: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    internal_edges: list[SystemMapEdge] = Field(default_factory=list)
    boundary_edges: list[SystemMapEdge] = Field(default_factory=list)


def nodes_for_domains(system_map: SystemMap, domains: Iterable[str]) -> set[str]:
    """Node ids whose ``domains`` include any of *domains* (the resolved contexts).

    The bridge from a resolved bounded context (a domain label) to the concrete unit ids
    the closure walks. Matching is case-insensitive + stripped, mirroring the
    ``context_resolver`` normalization discipline so this join can never diverge from the
    endpoint→context / FR→context joins. Non-unit nodes (external / shared_lib) are never
    returned — a domain names a modernizable unit, not a boundary node.
    """
    wanted = {str(d).strip().lower() for d in domains if str(d).strip()}
    if not wanted:
        return set()
    out: set[str] = set()
    for node in system_map.nodes:
        if node.kind in _NON_UNIT_KINDS:
            continue
        if any(str(dom).strip().lower() in wanted for dom in node.domains):
            out.add(node.id)
    return out


def compute_slice_closure(system_map: SystemMap, scope_node_ids: Iterable[str]) -> SliceClosure:
    """Compute the dependency closure of *scope_node_ids* over *system_map*.

    Starting from the in-scope units, follow ``src → dst`` out-edges transitively: every
    ``dst`` reached that is not itself in scope becomes a **dependency** (the boundary).
    Cycles are handled (a ``visited`` set bounds the walk). ``external`` / ``shared_lib``
    nodes are valid *dependencies* (boundary contracts) but are never promoted into
    ``in_scope``. An empty scope → an empty closure; a scope unit with no out-edges →
    just itself.
    """
    known = {n.id for n in system_map.nodes}
    in_scope = {nid for nid in scope_node_ids if nid in known}

    # Adjacency: src → [dst, ...] over the directed dependency edges.
    out_adj: dict[str, list[str]] = defaultdict(list)
    for edge in system_map.edges:
        out_adj[edge.src].append(edge.dst)

    # BFS over out-edges from the in-scope frontier; a dst not in scope is a dependency.
    dependencies: set[str] = set()
    visited: set[str] = set(in_scope)
    frontier = list(in_scope)
    while frontier:
        src = frontier.pop()
        for dst in out_adj.get(src, ()):
            if dst in in_scope:
                continue
            if dst not in dependencies:
                dependencies.add(dst)
            if dst not in visited:
                visited.add(dst)
                frontier.append(dst)

    closure = in_scope | dependencies
    internal: list[SystemMapEdge] = []
    boundary: list[SystemMapEdge] = []
    for edge in system_map.edges:
        if edge.src in in_scope and edge.dst in in_scope:
            internal.append(edge)
        elif edge.src in in_scope and edge.dst in closure:
            # An in-scope unit depending on a boundary unit (or a boundary unit reached
            # transitively) — the "depends on X via interface Y" evidence.
            boundary.append(edge)

    return SliceClosure(
        in_scope=sorted(in_scope),
        dependencies=sorted(dependencies),
        internal_edges=_sorted_edges(internal),
        boundary_edges=_sorted_edges(boundary),
    )


def induced_subgraph(system_map: SystemMap, closure: SliceClosure) -> SystemMap:
    """The system-map restricted to a slice's closure (in_scope plus dependencies).

    Keeps only the closure's nodes and the edges among them, so ``sequencer.order`` waves
    just the slice + its prerequisites into a scoped roadmap (dependencies surface as
    prerequisite/boundary units). Entry points are restricted to closure targets. The
    ``generated_at`` provenance is preserved.
    """
    keep = set(closure.in_scope) | set(closure.dependencies)
    nodes = [n for n in system_map.nodes if n.id in keep]
    edges = [e for e in system_map.edges if e.src in keep and e.dst in keep]
    entry_points = [ep for ep in system_map.entry_points if getattr(ep, "target", "") in keep]
    return SystemMap(
        nodes=_sorted_nodes(nodes),
        edges=_sorted_edges(edges),
        entry_points=entry_points,
        generated_at=system_map.generated_at,
    )


def _sorted_edges(edges: list[SystemMapEdge]) -> list[SystemMapEdge]:
    return sorted(edges, key=lambda e: (e.src, e.dst, e.kind, e.evidence))


def _sorted_nodes(nodes: list[SystemMapNode]) -> list[SystemMapNode]:
    return sorted(nodes, key=lambda n: n.id)


__all__ = [
    "SliceClosure",
    "compute_slice_closure",
    "induced_subgraph",
    "nodes_for_domains",
]
