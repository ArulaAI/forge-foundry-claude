"""Risk-weighted topological wave sequencer (Layer 1) — SystemMap → Roadmap.

Generalized from ``mfx``'s sequencing layer (design spec §3, Layer 1).
:func:`order` takes a :class:`SystemMap` (from :mod:`correlate`) and a
``risk_weights`` mapping and produces a :class:`Roadmap` that partitions unit
nodes into wave-ordered extraction batches, a deferred list (high-risk or
blocked), a never-extract list (cycle members), an isolated list (edge-less
standalone units, #1378), and the raw cycle list.

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
# IaC nodes (MR6, #1555) are a deployment boundary, not an extractable app unit — a
# provisioned-infrastructure node is never sequenced into an extract wave. Kept
# alongside ``external``/``shared_lib`` as the non-unit kinds ``order`` skips.
_NON_UNIT_KINDS: frozenset[str] = frozenset({"external", "shared_lib", "iac"})


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
    skip_units: frozenset[str] | None = None,
) -> Roadmap:
    """Sequence *graph*'s unit nodes into risk-weighted topological waves.

    Args:
        graph: the cross-unit dependency graph from :func:`correlate.build_system_map`.
        risk_weights: optional mapping of unit node id → ``"low"|"medium"|"high"``.
            Any id absent from the map, or mapped to an unrecognised value, gets
            ``"medium"``.
        generated_at: explicit timestamp for reproducible output.  When ``None``
            the :class:`Roadmap` default fires.
        skip_units: optional set of unit node ids that are **already modern** (MR5,
            #1554) and so are not extractable work. They are REMOVED from the output
            unit set — they never appear in ``waves`` / ``deferred`` /
            ``never_extract`` / ``isolated``. Critically they are kept in the graph
            for the topo / wave-depth computation, so a modern unit that others
            depend on still orders those dependents correctly (its edges inform
            ordering); it is only dropped at emit time. ``None`` / empty →
            byte-identical to a plain call (the #1408 additive invariant).

    Returns:
        A :class:`Roadmap` with ``waves``, ``deferred``, ``never_extract``,
        ``isolated`` (genuinely standalone edge-less units, #1378), and ``cycles``
        all in deterministic sorted order.
    """
    # MR5 (#1554): the modern-unit skip set. Empty when not filtering — every
    # partition below stays byte-identical, so the sequencer goldens are unchanged.
    skip: frozenset[str] = skip_units or frozenset()

    # ------------------------------------------------------------------
    # 1. Partition unit nodes from non-unit nodes.
    # ------------------------------------------------------------------
    all_unit_ids: list[str] = sorted(n.id for n in graph.nodes if n.kind not in _NON_UNIT_KINDS)

    # ------------------------------------------------------------------
    # 1b. Isolate genuinely-standalone units (#1378).
    #
    # A unit that touches NO edge at all (neither src nor dst of any SystemMapEdge,
    # including edges to external/shared_lib nodes) while the rest of the system IS
    # wired together has nothing to sequence against. Scheduling it as a wave-1
    # extraction target is a meaningless "risk: low" leaf (the S40 graceful-skip
    # leftover: an ``.#api`` node with 0 edges and no components, disconnected from
    # the wired ``.#db`` → ``.#app`` → ``.#ui`` spine). It is neither ``deferred``
    # (no dependency block) nor ``never_extract`` (no cycle) — it is standalone, so
    # surface it in its own ``isolated`` bucket and drop it from the wave graph
    # below. (``shared_lib`` carriers are already non-unit and excluded above; a
    # data carrier others depend on necessarily touches an edge, so it is never
    # isolated.)
    #
    # The isolation only fires when the graph HAS edges: a graph with no edges at
    # all is a set of intentionally-independent units (the pre-existing "parallel
    # wave 1" semantics), not a mix of a wired spine plus a disconnected orphan —
    # so those units keep flowing through normal risk-weighted sequencing.
    # ------------------------------------------------------------------
    edge_touched: set[str] = set()
    for edge in graph.edges:
        edge_touched.add(edge.src)
        edge_touched.add(edge.dst)
    isolated_ids: list[str] = (
        [u for u in all_unit_ids if u not in edge_touched] if edge_touched else []
    )
    isolated_set: set[str] = set(isolated_ids)

    isolated_items: list[RoadmapItem] = [
        RoadmapItem(
            id=uid,
            name=uid,
            risk=_resolve_risk(uid, risk_weights),
            depends_on=(),
            rationale="isolated: no dependencies discovered",
        )
        for uid in isolated_ids
        if uid not in skip  # MR5 (#1554): a modern unit is not extraction work.
    ]

    # Only connected units flow into cycle detection + wave sequencing.
    unit_ids: list[str] = [u for u in all_unit_ids if u not in isolated_set]
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
        if uid not in skip  # MR5 (#1554): drop modern units from the output.
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

    # Reverse-dep index over ALL unit nodes (including cycle members) so a
    # schedulable unit depending on a cycle member is reached by the worklist.
    # rdeps[v] = list of schedulable units whose deps include v.
    rdeps: dict[str, list[str]] = {u: [] for u in unit_ids}
    for u in schedulable:
        for v in deps.get(u, set()) & unit_set:
            rdeps[v].append(u)

    def _propagate(seed: set[str]) -> set[str]:
        """Transitive closure of *seed* over reverse-deps within the schedulable set."""
        blocked = set(seed)
        worklist = list(seed)
        while worklist:
            v = worklist.pop()
            for u in rdeps.get(v, []):
                if u not in blocked:
                    blocked.add(u)
                    worklist.append(u)
        return blocked

    # Cycle-blocked units are genuinely un-extractable (they transitively depend
    # on a dependency cycle). Risk-deferred units are high-risk (+ their
    # dependents) — a strangler-fig "extract later, mitigate first" bucket that is
    # tracked SEPARATELY so it can be relaxed if deferring on risk alone would
    # leave the whole roadmap empty (#1197 6c). ``cycle_members`` itself is not
    # schedulable, so restrict to schedulable units.
    cycle_blocked: set[str] = _propagate(set(cycle_members)) & schedulable_set
    high_risk: set[str] = {u for u in schedulable if _resolve_risk(u, risk_weights) == "high"}
    risk_deferred: set[str] = (_propagate(high_risk) & schedulable_set) - cycle_blocked

    # Collapse guard (#1197 6c-ii): if every extractable (non-cycle-blocked) unit
    # is risk-deferred, the roadmap would collapse to a single empty/short wave
    # (the reported "5 contexts deferred, one-node sequencing"). You cannot defer
    # the ENTIRE system — something must be extracted first — so drop the pure-risk
    # deferral and sequence the risk-deferred units into dependency waves anyway
    # (they stay risk-ordered within each wave, so the metering-before-billing
    # dependency ordering still emerges). Cycle-blocked units remain deferred.
    extractable_after_cycles = schedulable_set - cycle_blocked
    if extractable_after_cycles and extractable_after_cycles <= risk_deferred:
        risk_deferred = set()

    deferred_set: set[str] = cycle_blocked | risk_deferred

    deferred_items: list[RoadmapItem] = []
    for uid in sorted(deferred_set):
        if uid in skip:  # MR5 (#1554): modern units are not deferred work.
            continue
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
        # MR5 (#1554): a skipped (modern) unit still contributed its wave_depth to
        # the units that depend on it (``wave_depth`` runs over the full schedulable
        # graph, above) — so its dependents order correctly — but it is dropped from
        # the emitted wave items here. A wave that reduces to nothing is not emitted;
        # the depth of a surviving later wave is unchanged (numbering is depth-based,
        # not sequential), so removing a modern wave-1 unit leaves wave 2 as "wave 2".
        wave_unit_ids = [u for u in waves_by_depth[wave_num] if u not in skip]
        if not wave_unit_ids:
            continue
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
            isolated=isolated_items,
            cycles=cycles,
            generated_at=generated_at,
        )
    return Roadmap(
        waves=wave_objects,
        deferred=deferred_items,
        never_extract=never_extract_items,
        isolated=isolated_items,
        cycles=cycles,
    )
