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
    SystemMapNode,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime


def _normalize(s: str) -> str:
    """Lowercase, strip surrounding whitespace, strip trailing ``/``."""
    return s.strip().lower().rstrip("/")


def build_system_map(
    digests: Sequence[ModernizationDigest],
    *,
    generated_at: datetime | None = None,
) -> SystemMap:
    """Correlate *digests* into a cross-unit :class:`SystemMap`.

    Args:
        digests: one digest per unit, in any order.
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
            domain=d.domains[0] if d.domains else None,
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
            # Exact providers — everyone exposing `name` except u itself.
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

    if generated_at is not None:
        return SystemMap(nodes=all_nodes, edges=all_edges, generated_at=generated_at)
    return SystemMap(nodes=all_nodes, edges=all_edges)
