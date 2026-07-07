"""Stage D — antipattern detection (deterministic, LLM-free).

:func:`detect_antipatterns` scans the Stage-A contexts + the M1 schema +
M2 procs for four structural decomposition smells:

* ``cross_context_fk`` — a foreign key whose owning table and referenced
  table fall in different bounded contexts.
* ``distributed_transaction`` — a stored proc whose ``touched_tables`` span
  ≥2 contexts AND include a multi-writer (``shared``) table.
* ``view_of_views`` — a view whose ``referenced_tables`` names another view
  (a view-on-view chain).
* ``multi_context_sp`` — a stored proc whose ``touched_tables`` span ≥2
  contexts.

Each finding is an :class:`Antipattern` carrying a precise ``detail`` and
the ``evidence`` names involved. The result is sorted by ``(kind, detail)``
for determinism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.modernization_strategist.shared.models import Antipattern

if TYPE_CHECKING:
    from collections.abc import Sequence

    from primitives.db_schema_analyze import SchemaDigest
    from primitives.modernization_strategist.shared.models import BoundedContext
    from primitives.stored_proc_analyze import StoredProcDigest

#: A proc/table relationship qualifies as "spanning"/"multi-writer" at ≥ this count.
_MIN_SPAN = 2


def detect_antipatterns(
    contexts: Sequence[BoundedContext],
    schema: SchemaDigest,
    procs: StoredProcDigest,
) -> list[Antipattern]:
    """Detect the four decomposition antipatterns (Stage D)."""
    context_of: dict[str, str] = {table: ctx.name for ctx in contexts for table in ctx.tables}
    found: list[Antipattern] = []

    # --- cross_context_fk --------------------------------------------------
    for table in schema.tables:
        src_ctx = context_of.get(table.name)
        for fk in table.foreign_keys:
            dst_ctx = context_of.get(fk.referenced_table)
            if src_ctx is None or dst_ctx is None or src_ctx == dst_ctx:
                continue
            found.append(
                Antipattern(
                    kind="cross_context_fk",
                    detail=(
                        f"FK {fk.constraint_name}: "
                        f"{table.name} ({src_ctx}) -> "
                        f"{fk.referenced_table} ({dst_ctx})"
                    ),
                    evidence=(table.name, fk.referenced_table, fk.constraint_name),
                )
            )

    # --- view_of_views -----------------------------------------------------
    view_names: set[str] = {v.name for v in schema.views}
    for view in schema.views:
        chained = sorted(r for r in view.referenced_tables if r in view_names)
        if chained:
            found.append(
                Antipattern(
                    kind="view_of_views",
                    detail=f"view {view.name} references view(s): {', '.join(chained)}",
                    evidence=(view.name, *chained),
                )
            )

    # --- multi_context_sp + distributed_transaction ------------------------
    shared_tables: set[str] = _shared_tables(procs, context_of)
    for proc in procs.procedures:
        spanned_tables = [t for t in proc.touched_tables if t in context_of]
        spanned = sorted({context_of[t] for t in spanned_tables})
        if len(spanned) < _MIN_SPAN:
            continue
        found.append(
            Antipattern(
                kind="multi_context_sp",
                detail=(f"proc {proc.name} spans contexts: {', '.join(spanned)}"),
                evidence=(proc.name, *spanned),
            )
        )
        touched_shared = sorted(set(spanned_tables) & shared_tables)
        if touched_shared:
            found.append(
                Antipattern(
                    kind="distributed_transaction",
                    detail=(
                        f"proc {proc.name} writes shared table(s) "
                        f"{', '.join(touched_shared)} across contexts "
                        f"{', '.join(spanned)}"
                    ),
                    evidence=(proc.name, *touched_shared),
                )
            )

    return sorted(found, key=lambda a: (a.kind, a.detail))


def _shared_tables(procs: StoredProcDigest, context_of: dict[str, str]) -> set[str]:
    """Tables with ≥2 distinct writers (multi-writer = shared)."""
    write_modes = frozenset({"reads_writes", "writes_only"})
    writers: dict[str, set[str]] = {}
    for proc in procs.procedures:
        if proc.io_shape not in write_modes:
            continue
        for t in proc.touched_tables:
            if t in context_of:
                writers.setdefault(t, set()).add(proc.name)
    return {t for t, w in writers.items() if len(w) >= _MIN_SPAN}
