"""Cross-unit dependency correlator (Layer 1) — digests → SystemMap.

Generalized from ``mfx``'s correlation layer (design spec §3, Layer 1).
:func:`build_system_map` takes a sequence of :class:`ModernizationDigest` objects
(one per unit) and produces the joint :class:`SystemMap` by correlating consumed
↔ exposed interfaces and shared-library coordinates across units.

Pure and deterministic: no LLM, no clock, no filesystem. The ``generated_at``
timestamp is accepted as an optional parameter (pass a fixed value for golden-file
tests; omit for production where the model default fires). Output lists are sorted
so the same input always yields the same output.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from primitives._shared.modernization.models import (
    ModernizationDigest,
    SystemMap,
    SystemMapEdge,
    SystemMapEntryPoint,
    SystemMapNode,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime


def _to_system_map_entry_point(ep: object) -> SystemMapEntryPoint:
    """Adapt a duck-typed discovery ``EntryPoint`` → a :class:`SystemMapEntryPoint`.

    Read by attribute so the modernization engine never imports the discovery
    ``EntryPoint`` model (QUAL-2 cross-layer layering). ``target_unit`` (the
    discovery field name) maps to ``target``; ``trigger``/``source`` carry
    through verbatim.
    """
    return SystemMapEntryPoint(
        name=str(getattr(ep, "name", "")),
        kind=str(getattr(ep, "kind", "")),
        trigger=str(getattr(ep, "trigger", "")),
        target=str(getattr(ep, "target_unit", "") or getattr(ep, "target", "")),
        source=str(getattr(ep, "source", "")),
    )


def _collect_entry_points(entry_points: Sequence[object] | None) -> list[SystemMapEntryPoint]:
    """De-duplicate + sort duck-typed entry points into :class:`SystemMapEntryPoint`s.

    Keyed on ``(kind, name)`` (first occurrence wins); sorted by the same key so
    the map is deterministic. ``None``/empty → ``[]``.
    """
    by_key: dict[tuple[str, str], SystemMapEntryPoint] = {}
    for raw in entry_points or ():
        sm_ep = _to_system_map_entry_point(raw)
        by_key.setdefault((sm_ep.kind, sm_ep.name), sm_ep)
    return sorted(by_key.values(), key=lambda e: (e.kind, e.name))


def _normalize(s: str) -> str:
    """Lowercase, strip surrounding whitespace, strip trailing ``/``."""
    return s.strip().lower().rstrip("/")


def _is_unmatched_data_key(name: str) -> bool:
    """True for a ``data:<token>`` consumed interface that matched no exposer (#1462).

    A ``data:`` interface is a DB-join key by contract — it should match an exposed
    ``data:<table>`` on the DB unit. Reaching the unresolved branch means it matched
    none, so it is a mis-parsed table token (English noise / a singular-vs-plural
    mis-stem), not a real external dependency; the caller drops it instead of leaking
    an ``external:data:*`` phantom node.
    """
    return name.lower().startswith("data:")


def build_system_map(
    digests: Sequence[ModernizationDigest],
    *,
    entry_points: Sequence[object] | None = None,
    generated_at: datetime | None = None,
) -> SystemMap:
    """Correlate *digests* into a cross-unit :class:`SystemMap`.

    Args:
        digests: one digest per unit, in any order.
        entry_points: optional typed inbound triggers (Track B, #1427) — a
            duck-typed sequence of discovery ``EntryPoint`` records (read by
            attribute so the engine stays decoupled from the discovery model).
            Each becomes a :class:`SystemMapEntryPoint` on the map, sorted by
            ``(kind, name)`` and de-duplicated. ``None``/empty → no entry-point
            nodes (byte-identical to prior behavior).
        generated_at: explicit timestamp for reproducible output.  When ``None``
            the :class:`SystemMap` default (``datetime.now(UTC)``) fires.

    Returns:
        A :class:`SystemMap` with nodes sorted by ``id`` and edges sorted by
        ``(src, dst, kind, evidence, confidence)``.  Duplicate edges are removed.
    """
    # ------------------------------------------------------------------
    # 1. Unit nodes — one per digest.
    # ------------------------------------------------------------------
    unit_nodes: dict[str, SystemMapNode] = {}
    for d in digests:
        unit_nodes[d.code_root] = SystemMapNode(
            id=d.code_root,
            kind=d.flavor,
            # Carry ALL of a unit's domains (#1375). The pre-fix code kept only
            # ``domains[0]``, silently collapsing every other domain of a
            # multi-domain unit into ``_unassigned_`` downstream. Bounded contexts
            # now derive from the union of all units' domains.
            domains=list(d.domains),
        )

    # ------------------------------------------------------------------
    # 2a. Build exposed interface indices.
    # ------------------------------------------------------------------
    # exact index: raw interface name → set of unit ids that expose it.
    exposed_raw: dict[str, set[str]] = defaultdict(set)
    for d in digests:
        for name in d.exposed_interfaces:
            exposed_raw[name].add(d.code_root)

    # normalized index: _normalize(name) → set of unit ids.
    exposed_norm: dict[str, set[str]] = defaultdict(set)
    for name, units in exposed_raw.items():
        exposed_norm[_normalize(name)].update(units)

    # ------------------------------------------------------------------
    # 2b. Interface edges.
    # ------------------------------------------------------------------
    extra_nodes: dict[str, SystemMapNode] = {}
    edges: set[SystemMapEdge] = set()

    for d in digests:
        u = d.code_root
        for name in d.consumed_interfaces:
            # Exact providers — everyone exposing `name` except u itself. A
            # cross-unit provider always wins, even when u *also* exposes the
            # coordinate (e.g. a functional-map ``.#ui`` unit exposes its own
            # handler routes AND consumes them as nav targets — the real edge is
            # still ``.#ui → .#api``).
            exact_providers = exposed_raw.get(name, set()) - {u}
            if exact_providers:
                for v in exact_providers:
                    edges.add(
                        SystemMapEdge(
                            src=u,
                            dst=v,
                            kind="interface",
                            evidence=name,
                            confidence="exact",
                        )
                    )
                continue

            # Heuristic providers — via normalized match, still excluding u.
            norm_key = _normalize(name)
            norm_providers = exposed_norm.get(norm_key, set()) - {u}
            if norm_providers:
                for v in norm_providers:
                    edges.add(
                        SystemMapEdge(
                            src=u,
                            dst=v,
                            kind="interface",
                            evidence=name,
                            confidence="heuristic",
                        )
                    )
                continue

            # Drop rather than emit an external node when:
            #  * ``u`` itself exposes ``name`` — an intra-unit self-reference (#1373):
            #    an *internal* dependency (e.g. the schema adapter emits each table as
            #    an exposed ``data:<t>`` AND each FK as a consumed ``data:<ref>`` on the
            #    same ``.#db`` unit); a ``u → u`` self-edge would read as a trivial
            #    cycle and wrongly push the unit into never-extract; or
            #  * ``name`` is an unmatched ``data:<token>`` — a DB-join key by contract
            #    (#1462) that matches no exposed ``data:<table>``, i.e. a mis-parsed
            #    table token (English noise / singular-vs-plural mis-stem), NOT a real
            #    external dependency. Non-``data:`` unmatched interfaces (real external
            #    services) still externalize below.
            if (
                u in exposed_raw.get(name, set())
                or u in exposed_norm.get(norm_key, set())
                or _is_unmatched_data_key(name)
            ):
                continue

            # Unresolved — emit an external node.
            ext_id = f"external:{name}"
            if ext_id not in extra_nodes:
                extra_nodes[ext_id] = SystemMapNode(id=ext_id, kind="external")
            edges.add(
                SystemMapEdge(
                    src=u,
                    dst=ext_id,
                    kind="interface",
                    evidence=name,
                    confidence="unresolved",
                )
            )

    # ------------------------------------------------------------------
    # 3. Shared-library edges — only for components used by ≥2 units.
    # ------------------------------------------------------------------
    shared_users: dict[str, set[str]] = defaultdict(set)
    for d in digests:
        for comp in d.shared_components:
            shared_users[comp].add(d.code_root)

    lib_nodes: dict[str, SystemMapNode] = {}
    _min_shared_users = 2
    for comp, users in shared_users.items():
        if len(users) < _min_shared_users:
            continue
        lib_id = f"lib:{comp}"
        lib_nodes[lib_id] = SystemMapNode(id=lib_id, kind="shared_lib")
        for u in users:
            edges.add(
                SystemMapEdge(
                    src=u,
                    dst=lib_id,
                    kind="shared_lib",
                    evidence=comp,
                    confidence="exact",
                )
            )

    # ------------------------------------------------------------------
    # 4. Assemble + sort.
    # ------------------------------------------------------------------
    all_nodes: list[SystemMapNode] = sorted(
        list(unit_nodes.values()) + list(extra_nodes.values()) + list(lib_nodes.values()),
        key=lambda n: n.id,
    )
    all_edges: list[SystemMapEdge] = sorted(
        edges,
        key=lambda e: (e.src, e.dst, e.kind, e.evidence, e.confidence),
    )

    # ------------------------------------------------------------------
    # 5. Typed inbound entry points (Track B, #1427) — de-duplicated + sorted.
    # ------------------------------------------------------------------
    all_entry_points = _collect_entry_points(entry_points)

    if generated_at is not None:
        return SystemMap(
            nodes=all_nodes,
            edges=all_edges,
            entry_points=all_entry_points,
            generated_at=generated_at,
        )
    return SystemMap(nodes=all_nodes, edges=all_edges, entry_points=all_entry_points)
