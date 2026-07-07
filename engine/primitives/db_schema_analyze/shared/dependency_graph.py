"""M1 Stage C — ``build_dependency_dag`` (FK + view-reference DAG + cycles).

Builds the directed dependency graph for the M1 :class:`SchemaDigest`
from the Stage B :class:`Table` + :class:`View` lists. Every directed
edge ``(source, target)`` means "source depends on target" — i.e.
``source`` carries a foreign-key referencing ``target``, or ``source``
is a view that reads from the ``target`` table.

The edge list flows into :attr:`SchemaDigest.dependency_dag_edges` so
Stage E can score hotspots (high-incoming-FK degree) and so M3 can
discover bounded contexts cheaply.

Cycle handling
--------------

Legacy schemas routinely have FK cycles (the canonical example is
``departments.manager_id`` → ``employees.dept_id`` → ``departments``).
Stage C **flags** cycles via the return tuple's second element and
logs each cycle at ``WARNING`` level, but does **not** raise — per
the M-tier philosophy in SPEC.md §"File contract" ("partial coverage
of a legacy schema beats no coverage").

Algorithm
---------

Tarjan's strongly-connected-components algorithm, iterative form so a
pathological FK chain deeper than :func:`sys.getrecursionlimit` does
not stack-overflow. The pattern originally mirrored M3's
``_find_sccs`` (#221), reusing the proven workstack-of-(node,
successors) shape; M3's Python pipeline was archived in the charter-v2
reshape (recover via the ``v1.1.0-pre-archival`` tag), but the
workstack shape here is unchanged.

Any SCC with ``len(scc) > 1`` is a cycle. A single-node SCC with a
self-loop (rare — Oracle allows it) is also a cycle. SCCs are sorted
lexicographically within each cycle, and the cycle list is sorted by
each cycle's lexicographically smallest member, so the output is
byte-stable run-over-run.

Qualified names
---------------

Every node in the graph carries a ``<schema>.<table>`` qualified name
(or just ``<table>`` when the schema is empty — DB2 fixtures often
omit it). The qualifier matches the convention M3's
:mod:`shared.context_detector` uses, so the edge list flows into M3
without re-qualification.

Design spec §M1 Stage C
(``docs/superpowers/specs/2026-05-22-data-modernization-design.md``).
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.db_schema_analyze.shared.models import Table, View

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_dependency_dag(
    tables: list[Table],
    views: list[View],
) -> tuple[list[tuple[str, str]], list[list[str]]]:
    """Return the FK + view-reference edge list and the cycle list.

    Args:
        tables: Stage B output — every FK on every table contributes one
            directed edge from the owning table to the FK's referenced
            table.
        views: Stage B output — every entry in :attr:`View.referenced_tables`
            contributes one directed edge from the view to the referenced
            table.

    Returns:
        ``(edges, cycles)``:

        * ``edges`` — list of ``(source_qualified_name, target_qualified_name)``
          tuples in deterministic order (table-walk then view-walk; FKs
          in declaration order within each table). Duplicate edges (same
          source/target across multiple FKs) are de-duplicated.
        * ``cycles`` — list of cycles. Each cycle is the sorted list of
          qualified names participating in a strongly-connected
          component of size >= 2 (or a single-node SCC with a
          self-loop). The outer list is sorted by each cycle's smallest
          member so the output is byte-stable.

    Cycles are logged at ``WARNING`` level. The function never raises
    on cyclic input.
    """
    edges = _collect_edges(tables, views)
    cycles = _find_cycles(edges)
    for cycle in cycles:
        logger.warning("dependency cycle detected: %s", " -> ".join([*cycle, cycle[0]]))
    return edges, cycles


# ---------------------------------------------------------------------------
# Edge collection
# ---------------------------------------------------------------------------


def _collect_edges(
    tables: list[Table],
    views: list[View],
) -> list[tuple[str, str]]:
    """Return the de-duplicated edge list in table-walk + view-walk order.

    FK edges: every ``Table.foreign_keys`` entry produces one edge from
    the owning table's qualified name to the FK's referenced table.
    The referenced-table qualifier is best-effort: Stage B's
    ``ForeignKey.referenced_table`` is the bare name (sqlglot drops the
    schema on cross-table refs in some dialects), so we look up the
    target in ``tables`` by case-insensitive bare-name match to recover
    its real schema qualifier. When no match exists (a dangling FK), we
    use the bare name unqualified — the edge still surfaces in the
    digest so consumers can spot the dangling reference.

    View edges: every entry in :attr:`View.referenced_tables` is
    already-qualified by Stage B, so we use it verbatim.

    Duplicates are dropped in first-occurrence order; the same logical
    FK declared via inline-column + ALTER would otherwise show up
    twice.
    """
    # Build a lookup from bare-name → qualified-name for FK target resolution.
    # When multiple schemas declare the same bare name we keep the first
    # (table-walk-order) winner; the design spec's anti-hallucination
    # guard ensures consumers see exactly the Stage B inventory.
    bare_to_qn: dict[str, str] = {}
    for t in tables:
        bare = t.name.lower()
        if bare not in bare_to_qn:
            bare_to_qn[bare] = _qn(t.schema_, t.name)

    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # FK edges first (table-walk + per-table FK declaration order).
    for table in tables:
        source = _qn(table.schema_, table.name)
        for fk in table.foreign_keys:
            target = bare_to_qn.get(fk.referenced_table.lower()) or fk.referenced_table
            edge = (source, target)
            if edge in seen:
                continue
            seen.add(edge)
            edges.append(edge)

    # View-reference edges (view-walk + per-view first-occurrence order).
    for view in views:
        source = _qn(view.schema_, view.name)
        for target in view.referenced_tables:
            edge = (source, target)
            if edge in seen:
                continue
            seen.add(edge)
            edges.append(edge)

    return edges


def _qn(schema: str, name: str) -> str:
    """Return ``schema.name`` if schema is set, otherwise just ``name``.

    Mirror of :func:`primitives.db_schema_analyze.shared.object_inventory._qn`
    so both Stage B and Stage C produce identical qualifier strings for
    the same ``(schema, name)`` pair.
    """
    if schema:
        return f"{schema}.{name}"
    return name


# ---------------------------------------------------------------------------
# Cycle detection — iterative Tarjan SCC
# ---------------------------------------------------------------------------


def _find_cycles(edges: list[tuple[str, str]]) -> list[list[str]]:
    """Return the cycles in the directed graph defined by ``edges``.

    A cycle is a strongly-connected component with either more than one
    member or a self-loop. The result is sorted for determinism (each
    cycle's members sorted lexicographically; the outer list sorted by
    each cycle's smallest member).

    The graph adjacency is built from ``edges`` directly; nodes with
    no out-edges are still scanned in case they participate in a cycle
    purely as incoming targets — Tarjan walks every node anyway.
    """
    graph: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for src, tgt in edges:
        graph.setdefault(src, set()).add(tgt)
        nodes.add(src)
        nodes.add(tgt)
    if not nodes:
        return []
    for node in nodes:
        graph.setdefault(node, set())

    sccs = _tarjan_scc(graph)
    cycles: list[list[str]] = []
    for scc in sccs:
        if len(scc) > 1 or _has_self_loop(scc, graph):
            cycles.append(sorted(scc))
    cycles.sort(key=lambda c: (c[0], c))
    return cycles


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return the list of strongly-connected components in ``graph``.

    Iterative Tarjan's algorithm — the recursive form blows the Python
    stack on pathological FK chains (e.g. a 1000-table view-of-views
    legacy schema). The work-stack carries ``(node, list[successor])``
    frames; when we advance into a successor we push a new frame, when
    a frame's successor list is exhausted we pop and propagate the
    lowlink to the parent.

    Output order is deterministic: we iterate ``sorted(graph)`` for the
    outer loop, and each frame's successors are pre-sorted before
    pushing.

    The implementation originally mirrored M3's ``_find_sccs`` (#221)
    so behaviour matched across the M-tier; M3's Python pipeline was
    archived in the charter-v2 reshape (``v1.1.0-pre-archival`` tag),
    leaving this as the standalone reference shape.
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(start: str) -> None:
        # Iterative Tarjan via a work-stack of (node, successors-deque).
        # deque.popleft() is O(1); list.pop(0) is O(n).
        work: list[tuple[str, deque[str]]] = [(start, deque(sorted(graph.get(start, set()))))]
        indices[start] = index_counter[0]
        lowlinks[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, successors = work[-1]
            advanced = False
            while successors:
                succ = successors.popleft()
                if succ not in indices:
                    indices[succ] = index_counter[0]
                    lowlinks[succ] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, deque(sorted(graph.get(succ, set())))))
                    advanced = True
                    break
                if succ in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[succ])
            if advanced:
                continue
            # All successors processed — finalise this frame.
            work.pop()
            if work:
                parent = work[-1][0]
                lowlinks[parent] = min(lowlinks[parent], lowlinks[node])
            if lowlinks[node] == indices[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            strongconnect(node)
    return result


def _has_self_loop(scc: list[str], graph: dict[str, set[str]]) -> bool:
    """``True`` if any node in the SCC has itself as a dependency.

    A single-node SCC with a self-loop is still a cycle (Oracle allows
    ``employees.manager_id`` referencing the same table's PK column),
    so this check augments the ``len(scc) > 1`` cycle test.
    """
    return any(node in graph.get(node, set()) for node in scc)


__all__ = ["build_dependency_dag"]
