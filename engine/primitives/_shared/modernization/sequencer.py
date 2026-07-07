"""Risk-weighted topological wave sequencer (Layer 1) — SystemMap → Roadmap.

Generalized from ``mfx``'s sequencing layer (design spec §3, Layer 1).
:func:`order` takes a :class:`SystemMap` (from :mod:`correlate`) and a
``risk_weights`` mapping and produces a :class:`Roadmap` that partitions unit
nodes into wave-ordered extraction batches, a deferred list (high-risk or
blocked), a never-extract list (cycle members), and the raw cycle list.

Pure and deterministic: no LLM, no clock, no filesystem. Tarjan's SCC is used
for cycle detection so the result is independent of iteration order (we drive
the DFS over sorted node ids). The ``generated_at`` timestamp is accepted as an
optional parameter for golden-file testing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from primitives._shared.modernization.models import (
    Roadmap,
    RoadmapItem,
    RoadmapWave,
    SystemMap,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

_RISK_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
_VALID_RISKS: frozenset[str] = frozenset(_RISK_RANK)
_NON_UNIT_KINDS: frozenset[str] = frozenset({"external", "shared_lib"})


def _resolve_risk(
    node_id: str,
    risk_weights: Mapping[str, str] | None,
) -> Literal["low", "medium", "high"]:
    """Return the validated risk for *node_id*, defaulting to ``"medium"``."""
    if risk_weights is None:
        return "medium"
    raw = risk_weights.get(node_id, "medium")
    if raw not in _VALID_RISKS:
        return "medium"
    return raw  # type: ignore[return-value]


def _tarjan_scc(
    nodes: list[str],
    adj: dict[str, set[str]],
) -> list[list[str]]:
    """Tarjan's SCC algorithm over *nodes* visited in sorted order.

    Returns a list of SCCs (each a list of node ids), in reverse topological
    order (leaves first). Every id in *nodes* appears in exactly one SCC.
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in sorted(adj.get(v, set())):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    for v in sorted(nodes):
        if v not in index:
            strongconnect(v)

    return sccs


def order(
    graph: SystemMap,
    risk_weights: Mapping[str, str] | None = None,
    *,
    generated_at: datetime | None = None,
) -> Roadmap:
    """Sequence *graph*'s unit nodes into risk-weighted topological waves.

    Args:
        graph: the cross-unit dependency graph from :func:`correlate.build_system_map`.
        risk_weights: optional mapping of unit node id → ``"low"|"medium"|"high"``.
            Any id absent from the map, or mapped to an unrecognised value, gets
            ``"medium"``.
        generated_at: explicit timestamp for reproducible output.  When ``None``
            the :class:`Roadmap` default fires.

    Returns:
        A :class:`Roadmap` with ``waves``, ``deferred``, ``never_extract``, and
        ``cycles`` all in deterministic sorted order.
    """
    # ------------------------------------------------------------------
    # 1. Partition unit nodes from non-unit nodes.
    # ------------------------------------------------------------------
    unit_ids: list[str] = sorted(n.id for n in graph.nodes if n.kind not in _NON_UNIT_KINDS)
    unit_set: set[str] = set(unit_ids)

    # ------------------------------------------------------------------
    # 2. Build unit dependency graph (drop edges to external/shared_lib).
    # ------------------------------------------------------------------
    deps: dict[str, set[str]] = {u: set() for u in unit_ids}
    for edge in graph.edges:
        if edge.src in unit_set and edge.dst in unit_set:
            deps[edge.src].add(edge.dst)

    # Self-edges are valid input; Tarjan handles them (trivial cycle).

    # ------------------------------------------------------------------
    # 3. Detect cycles via Tarjan's SCC.
    # ------------------------------------------------------------------
    sccs = _tarjan_scc(unit_ids, deps)

    cycle_members: set[str] = set()
    raw_cycles: list[tuple[str, ...]] = []

    for scc in sccs:
        # A cycle is: >1 member OR a single node with a self-edge.
        is_cycle = len(scc) > 1 or (len(scc) == 1 and scc[0] in deps.get(scc[0], set()))
        if is_cycle:
            key = tuple(sorted(scc))
            raw_cycles.append(key)
            cycle_members.update(scc)

    cycles: list[tuple[str, ...]] = sorted(raw_cycles)

    # Map each cycle member to the sorted SCC tuple it belongs to (for rationale).
    member_to_cycle: dict[str, tuple[str, ...]] = {}
    for cyc in cycles:
        for uid in cyc:
            member_to_cycle[uid] = cyc

    never_extract_items: list[RoadmapItem] = [
        RoadmapItem(
            id=uid,
            name=uid,
            risk=_resolve_risk(uid, risk_weights),
            depends_on=tuple(sorted(deps.get(uid, set()))),
            rationale=f"in dependency cycle: {member_to_cycle[uid]}",
        )
        for uid in sorted(cycle_members)
    ]

    # ------------------------------------------------------------------
    # 4. Topological waves over acyclic unit nodes.
    # ------------------------------------------------------------------
    schedulable: list[str] = [u for u in unit_ids if u not in cycle_members]
    schedulable_set: set[str] = set(schedulable)

    # Memoized wave-depth computation.
    wave_cache: dict[str, int] = {}

    def wave_depth(u: str) -> int:
        if u in wave_cache:
            return wave_cache[u]
        unit_deps_in_sched = deps.get(u, set()) & schedulable_set
        depth = 1 + max((wave_depth(v) for v in unit_deps_in_sched), default=0)
        wave_cache[u] = depth
        return depth

    # First pass: determine which units are deferred due to high risk.
    high_risk: set[str] = {u for u in schedulable if _resolve_risk(u, risk_weights) == "high"}

    # Second pass: propagate deferred status to dependents.
    # A unit is blocked if any of its unit-deps are in never_extract or deferred.
    # We resolve this iteratively using a worklist (only re-check newly blocked
    # units' dependants instead of scanning all schedulable units per pass).
    deferred_set: set[str] = set(high_risk)
    # Build reverse-dep index over ALL unit nodes (including cycle members) so
    # schedulable units that depend on a cycle member are reached by the worklist.
    # rdeps[v] = list of schedulable units whose deps include v.
    rdeps: dict[str, list[str]] = {u: [] for u in unit_ids}
    for u in schedulable:
        for v in deps.get(u, set()) & unit_set:
            rdeps[v].append(u)
    # Seed worklist with high-risk schedulable units AND cycle members so that
    # any schedulable unit depending on either gets propagated into deferred_set.
    worklist: list[str] = list(high_risk) + list(cycle_members)
    while worklist:
        newly_blocked = worklist.pop()
        for u in rdeps.get(newly_blocked, []):
            if u not in deferred_set:
                deferred_set.add(u)
                worklist.append(u)

    deferred_items: list[RoadmapItem] = []
    for uid in sorted(deferred_set):
        if _resolve_risk(uid, risk_weights) == "high":
            rationale = "high-risk; defer pending mitigation"
        else:
            # Find the lexicographically first blocking dep for the rationale.
            blocking_sorted: list[str] = sorted(
                (deps.get(uid, set()) & unit_set) & (cycle_members | deferred_set)
            )
            rationale = f"depends on deferred/un-extractable unit {blocking_sorted[0]!r}"
        deferred_items.append(
            RoadmapItem(
                id=uid,
                name=uid,
                risk=_resolve_risk(uid, risk_weights),
                depends_on=tuple(sorted(deps.get(uid, set()) & unit_set)),
                rationale=rationale,
            )
        )

    # ------------------------------------------------------------------
    # 5. Build waves from the remaining extractable units.
    # ------------------------------------------------------------------
    extract_ids: list[str] = [u for u in schedulable if u not in deferred_set]

    waves_by_depth: dict[int, list[str]] = {}
    for u in extract_ids:
        d = wave_depth(u)
        waves_by_depth.setdefault(d, []).append(u)

    wave_objects: list[RoadmapWave] = []
    for wave_num in sorted(waves_by_depth):
        wave_unit_ids = waves_by_depth[wave_num]
        items = tuple(
            RoadmapItem(
                id=uid,
                name=uid,
                risk=_resolve_risk(uid, risk_weights),
                depends_on=tuple(sorted(deps.get(uid, set()) & unit_set)),
                rationale="",
            )
            for uid in sorted(
                wave_unit_ids,
                key=lambda u: (_RISK_RANK[_resolve_risk(u, risk_weights)], u),
            )
        )
        rationale = (
            "no unextracted dependencies" if wave_num == 1 else "depends only on earlier waves"
        )
        wave_objects.append(RoadmapWave(wave=wave_num, rationale=rationale, items=items))

    # ------------------------------------------------------------------
    # 6. Assemble Roadmap.
    # ------------------------------------------------------------------
    if generated_at is not None:
        return Roadmap(
            waves=wave_objects,
            deferred=deferred_items,
            never_extract=never_extract_items,
            cycles=cycles,
            generated_at=generated_at,
        )
    return Roadmap(
        waves=wave_objects,
        deferred=deferred_items,
        never_extract=never_extract_items,
        cycles=cycles,
    )
