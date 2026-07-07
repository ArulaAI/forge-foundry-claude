"""M1 Stage E — ``detect_hotspots`` (high-incoming-FK + orphan flags).

Deterministic post-pass over the Stage B :class:`Table` inventory + the
Stage C ``(source, target)`` edge list. Sets two flags on each table:

* ``is_hotspot`` — incoming FK in-degree at or above ``hotspot_threshold``
  (default 5). The Mermaid hotspot DAG in Stage F's report renderer
  surfaces these tables; M3 also uses them to drive bounded-context
  discovery (high-fan-in tables often correspond to aggregate roots).

* ``is_orphan`` — both in-degree and out-degree are zero. The table is
  not referenced by any FK or view and does not reference any other
  table. Standalone configuration tables and legacy "junk drawer"
  tables typically surface here; downstream M3 can strangler-fig them
  in isolation.

The function is deterministic and side-effect free — same inputs
produce the same flags. Lookup-table detection (no outgoing FK + small
row counts) is deferred to v1.1 per the issue body; the present
heuristic is incoming-degree only.

Design spec §M1 Stage E
(``docs/superpowers/specs/2026-05-22-data-modernization-design.md``).
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.db_schema_analyze.shared.models import Table


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_HOTSPOT_THRESHOLD = 5
"""Default incoming-FK in-degree above which a table is a hotspot.

Five is the conservative number sized for legacy core-banking schemas:
a customer / account / GL hub typically attracts well over 5 inbound
FKs (often 20+), while typical satellite tables attract 0-3. The
threshold is configurable via :func:`detect_hotspots`'s
``hotspot_threshold`` parameter and the CLI will surface
``--hotspot-threshold N`` in M1-11.
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def detect_hotspots(
    tables: list[Table],
    edges: list[tuple[str, str]],
    hotspot_threshold: int = DEFAULT_HOTSPOT_THRESHOLD,
) -> list[Table]:
    """Return a new ``list[Table]`` with hotspot / orphan flags populated.

    The input list is *not mutated* — :class:`Table` is Pydantic-validated
    and we produce updated copies via ``model_copy(update=...)`` so the
    caller's reference is preserved (the same pattern Stage D uses for
    ``lob_classification``).

    Args:
        tables: Stage B output (already augmented with Stage D
            ``lob_classification`` if Stage D ran). Every table is
            scored against the edge list; tables not present in the
            edge list still receive the flags (a table absent from
            both source + target sides has degree 0).
        edges: Stage C output — list of ``(source_qn, target_qn)``
            qualified-name tuples. Edges include FK references and
            view references (Stage C combines both).
        hotspot_threshold: A table whose incoming in-degree is greater
            than or equal to this value is flagged ``is_hotspot=True``.
            Must be a positive integer; values below 1 are clamped to 1
            so the threshold-at-1 edge case behaves sensibly.

    Returns:
        A new list of :class:`Table` objects in the input order, with
        :attr:`Table.is_hotspot` and :attr:`Table.is_orphan` set per
        the deterministic rules above.
    """
    # Clamp the threshold so degenerate values do not produce surprising
    # flags. A threshold of 0 would flag *every* table including pure
    # orphans, which the M3 sequencer would then mis-treat as aggregate
    # roots; clamping to 1 keeps the contract intuitive.
    effective_threshold = max(1, hotspot_threshold)

    in_degree, out_degree = _degree_maps(edges)

    out: list[Table] = []
    for table in tables:
        qn = _qn(table.schema_, table.name)
        incoming = in_degree.get(qn, 0)
        outgoing = out_degree.get(qn, 0)
        is_hotspot = incoming >= effective_threshold
        is_orphan = incoming == 0 and outgoing == 0
        out.append(table.model_copy(update={"is_hotspot": is_hotspot, "is_orphan": is_orphan}))
    return out


# ---------------------------------------------------------------------------
# Degree maps
# ---------------------------------------------------------------------------


def _degree_maps(
    edges: list[tuple[str, str]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Return ``(in_degree, out_degree)`` keyed by qualified name.

    Edges are walked once; the maps are sparse — a table that appears
    in neither side has no entry and the lookup falls back to ``0``
    via ``dict.get(qn, 0)``. Stage C de-duplicates parallel edges
    already (same source/target across multiple FKs collapses to one
    edge); this counter therefore reflects distinct *referencing
    tables*, not distinct FK constraints.

    The :class:`collections.Counter` import gives the deterministic
    insertion-order iteration tests rely on.
    """
    out_degree: Counter[str] = Counter()
    in_degree: Counter[str] = Counter()
    for source, target in edges:
        out_degree[source] += 1
        in_degree[target] += 1
    return dict(in_degree), dict(out_degree)


def _qn(schema: str, name: str) -> str:
    """Return ``schema.name`` or just ``name`` if ``schema`` is empty.

    Mirror of :func:`primitives.db_schema_analyze.shared.dependency_graph._qn`
    so Stage E qualifiers match the edge-list keys Stage C emitted byte-
    for-byte.
    """
    if schema:
        return f"{schema}.{name}"
    return name


__all__ = [
    "DEFAULT_HOTSPOT_THRESHOLD",
    "detect_hotspots",
]
