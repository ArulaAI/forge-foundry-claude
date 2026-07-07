"""Stage A — bounded-context detection (deterministic, LLM-free).

:func:`detect_contexts` builds candidate bounded contexts from a *weighted cohesion
graph* over table names.

**Algorithm (v2 — LoB-primary, FK-secondary with max-size cap)**

The previous union-find approach merged every FK-connected component into one set,
causing FK-hub tables (e.g. ``customers`` referenced by a dozen downstream tables)
to absorb weakly-coupled subgraphs into a single mega-context. v2 uses a
three-phase approach that resists hub absorption (#887):

1. **LoB-primary merge (weight = 3):** Tables sharing a ``lob_classification``
   are always co-located — they are the strongest semantic signal available from
   M1, and a cross-LoB merge must be justified by additional evidence.

2. **LoB-compatible FK/SP merge (weight ≤ 2):** An FK or SP co-touch edge between
   tables ``A`` and ``B`` merges their components only when the merge is
   *LoB-compatible* — that is, when the result does not join two non-empty,
   *distinct* LoB groups.  Concretely: both sides share a LoB, or at least one
   side has no LoB of its own.  This prevents a hub table (e.g. ``customers``
   with LoB ``crm``) from absorbing ``orders`` (LoB ``sales``) merely because
   ``orders`` carries a FK to ``customers``.

3. **Max-size cap with LoB-based splitting (cap = 6 tables):** After the first
   two phases, any component exceeding ``_MAX_CONTEXT_SIZE`` that contains tables
   with two or more *distinct* LoB labels is split into per-LoB sub-clusters. The
   remainder (tables in the oversize component that share no LoB, or whose LoB was
   not used as a split key) form their own residual context. This guards against
   pathological all-same-LoB schemas being over-fragmented: the split only fires
   when distinct LoB groups exist inside the oversized component.

The algorithm is fully deterministic: context ``tables`` are sorted and the
context list is sorted by ``name``.

**Compatibility note:** The antipattern detector, ownership analyser, and
sequencer all accept ``list[BoundedContext]`` directly.  When the detector
produces more contexts than before (the expected outcome for hub schemas), the
downstream stages automatically fire cross-context FK antipatterns and
multi-wave sequencing — no downstream changes required.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import TYPE_CHECKING

from primitives.modernization_strategist.shared.models import BoundedContext

if TYPE_CHECKING:
    from primitives.db_schema_analyze import SchemaDigest
    from primitives.stored_proc_analyze import StoredProcDigest

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

#: A component with more than this many tables is eligible for LoB-split.
_MAX_CONTEXT_SIZE: int = 6


# ---------------------------------------------------------------------------
# Union-Find (unchanged from v1 — deterministic lex merge)
# ---------------------------------------------------------------------------


class _UnionFind:
    """Minimal union-find over string node ids (deterministic)."""

    def __init__(self, nodes: list[str]) -> None:
        self._parent: dict[str, str] = {n: n for n in nodes}

    def find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression.
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Deterministic merge: the lexicographically-smaller root wins.
        if ra < rb:
            self._parent[rb] = ra
        else:
            self._parent[ra] = rb


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------


def detect_contexts(schema: SchemaDigest, procs: StoredProcDigest) -> list[BoundedContext]:
    """Group *schema*'s tables into candidate bounded contexts (Stage A, v2).

    Uses three-phase LoB-primary, FK-secondary clustering with a max-size cap to
    resist hub-absorption (see module docstring for the full algorithm).
    """
    table_names: list[str] = sorted(t.name for t in schema.tables)
    if not table_names:
        return []
    known: set[str] = set(table_names)

    # Build a lookup: table name → LoB classification (None if unset).
    lob_by_table: dict[str, str | None] = {t.name: t.lob_classification for t in schema.tables}

    uf = _UnionFind(table_names)

    # ------------------------------------------------------------------
    # Phase 1 — LoB-primary merge (strongest signal; always merge same LoB).
    # ------------------------------------------------------------------
    by_lob: dict[str, list[str]] = {}
    for table in schema.tables:
        lob = table.lob_classification
        if lob:
            by_lob.setdefault(lob, []).append(table.name)
    for members in by_lob.values():
        for a, b in combinations(sorted(members), 2):
            uf.union(a, b)

    # ------------------------------------------------------------------
    # Phase 2 — LoB-compatible FK/SP merge (merges only when safe).
    # ------------------------------------------------------------------

    def _lob_compatible(a: str, b: str) -> bool:
        """True when merging component(a) with component(b) does not join two
        distinct non-empty LoB groups.

        A merge is safe when:
        * Both tables have the same LoB, OR
        * At least one side has no dominant LoB (all members LoB-unclassified).

        The "dominant LoB" of a component is the majority LoB of its current
        members (or None when no member has a LoB).
        """
        lob_a = _dominant_lob(uf.find(a), uf, lob_by_table, table_names)
        lob_b = _dominant_lob(uf.find(b), uf, lob_by_table, table_names)
        # Safe if either side has no dominant LoB, or they share the same LoB.
        return lob_a is None or lob_b is None or lob_a == lob_b

    # FK edges — conditional on LoB compatibility.
    for table in schema.tables:
        for fk in table.foreign_keys:
            if (
                fk.referenced_table in known
                and table.name in known
                and _lob_compatible(table.name, fk.referenced_table)
            ):
                uf.union(table.name, fk.referenced_table)

    # SP co-touch — conditional on LoB compatibility.
    for proc in procs.procedures:
        touched = sorted({t for t in proc.touched_tables if t in known})
        for a, b in combinations(touched, 2):
            if _lob_compatible(a, b):
                uf.union(a, b)

    # ------------------------------------------------------------------
    # Phase 3 — Max-size cap: split oversized components on LoB boundaries.
    # ------------------------------------------------------------------
    components = _collect_components(uf, table_names)
    final_contexts: list[BoundedContext] = []
    for members in components.values():
        members_sorted = sorted(members)
        if len(members_sorted) <= _MAX_CONTEXT_SIZE:
            final_contexts.append(
                BoundedContext(
                    name=_name_context(members_sorted, lob_by_table),
                    tables=tuple(members_sorted),
                    rationale=_rationale(members_sorted),
                )
            )
        else:
            # Oversized component — split by distinct LoB groups if present.
            sub_clusters = _lob_split(members_sorted, lob_by_table)
            for cluster in sub_clusters:
                final_contexts.append(
                    BoundedContext(
                        name=_name_context(cluster, lob_by_table),
                        tables=tuple(cluster),
                        rationale=_rationale(cluster, split=True),
                    )
                )

    return sorted(final_contexts, key=lambda c: c.name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_components(uf: _UnionFind, table_names: list[str]) -> dict[str, list[str]]:
    """Return {root → [member, ...]} for each connected component."""
    components: dict[str, list[str]] = {}
    for name in table_names:
        components.setdefault(uf.find(name), []).append(name)
    return components


def _dominant_lob(
    root: str,
    uf: _UnionFind,
    lob_by_table: dict[str, str | None],
    all_tables: list[str],
) -> str | None:
    """Majority LoB of all tables in *root*'s component, or None if unclassified."""
    lobs = [lob for t in all_tables if uf.find(t) == root and (lob := lob_by_table.get(t))]
    if not lobs:
        return None
    counts = Counter(lobs)
    # Break ties lexicographically (smallest LoB name wins) for determinism.
    return min(counts, key=lambda lob: (-counts[lob], lob))


def _lob_split(
    members: list[str],
    lob_by_table: dict[str, str | None],
) -> list[list[str]]:
    """Split *members* into per-LoB sub-clusters (+ a residual cluster).

    Only splits when two or more distinct LoB labels appear in *members*; if
    all tables share one LoB (or none), returns the original list unchanged so
    genuinely cohesive large schemas are not over-fragmented.
    """
    lob_groups: dict[str, list[str]] = {}
    no_lob: list[str] = []
    for t in members:
        lob = lob_by_table.get(t)
        if lob:
            lob_groups.setdefault(lob, []).append(t)
        else:
            no_lob.append(t)

    distinct_lobs = sorted(lob_groups)
    _min_lobs_to_split = 2
    if len(distinct_lobs) < _min_lobs_to_split:
        # No meaningful split available — return as-is (one cluster).
        return [members]

    clusters: list[list[str]] = [sorted(lob_groups[lob]) for lob in distinct_lobs]
    if no_lob:
        clusters.append(sorted(no_lob))
    return clusters


def _name_context(members: list[str], lob_by_table: dict[str, str | None]) -> str:
    """Name a context after its most-common LoB, else ``<first-table>-context``."""
    lobs = [lob for t in members if (lob := lob_by_table.get(t))]
    if lobs:
        counts = Counter(lobs)
        top = max(counts, key=lambda lob: (counts[lob], _neg_lex(lob)))
        return top
    return f"{members[0]}-context"


def _rationale(members: list[str], *, split: bool = False) -> str:
    """Short human-readable rationale for the context's membership."""
    suffix = "; split from oversized component by LoB boundary" if split else ""
    return f"{len(members)} tables; cohesion: FK+SP+LoB{suffix}"


def _neg_lex(s: str) -> tuple[int, ...]:
    """Negated codepoint tuple so ``max`` picks the lexicographically-first."""
    return tuple(-ord(c) for c in s)
