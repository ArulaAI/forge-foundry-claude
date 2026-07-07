"""Stage B — ownership analysis (deterministic, LLM-free).

:func:`analyze_ownership` computes, for every table, the set of stored
procedures that *write* it, then partitions tables into single-owner
(``owned``) and shared (``shared``) sets.

A proc is a *writer* of a table it touches when the proc's M2 access mode
(``io_shape``) is ``reads_writes`` or ``writes_only`` (``reads_only`` /
``cursor_heavy`` / ``cross_db_link`` are not write-bearing for ownership
purposes). ``io_shape`` is a per-proc property, so a writing proc writes
every table in its ``touched_tables``.

A table is ``shared`` when it has ≥2 distinct writers, OR when any of its
writers touches tables that span more than one bounded context (a
cross-context shared write — a migration blocker). Everything else is
``owned``. All outputs are sorted for determinism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.modernization_strategist.shared.models import OwnershipReport

if TYPE_CHECKING:
    from collections.abc import Sequence

    from primitives.db_schema_analyze import SchemaDigest
    from primitives.modernization_strategist.shared.models import BoundedContext
    from primitives.stored_proc_analyze import StoredProcDigest

#: M2 ``io_shape`` values that count as a write for ownership purposes.
_WRITE_MODES: frozenset[str] = frozenset({"reads_writes", "writes_only"})

#: A table is multi-writer / shared at ≥ this many distinct writers.
_MIN_SHARED_WRITERS = 2


def analyze_ownership(
    contexts: Sequence[BoundedContext],
    schema: SchemaDigest,
    procs: StoredProcDigest,
) -> OwnershipReport:
    """Compute the single-writer / multi-writer ownership report (Stage B)."""
    table_names: set[str] = {t.name for t in schema.tables}
    context_of: dict[str, str] = {table: ctx.name for ctx in contexts for table in ctx.tables}

    # writers_by_table[t] = sorted writer-proc names.
    writers: dict[str, set[str]] = {name: set() for name in table_names}
    # Procs that write across >1 context → mark every table they write as shared.
    cross_context_writers: set[str] = set()

    for proc in procs.procedures:
        if proc.io_shape not in _WRITE_MODES:
            continue
        written = [t for t in proc.touched_tables if t in table_names]
        spanned = {context_of[t] for t in written if t in context_of}
        for t in written:
            writers[t].add(proc.name)
        if len(spanned) > 1:
            cross_context_writers.add(proc.name)

    writers_by_table: dict[str, tuple[str, ...]] = {t: tuple(sorted(w)) for t, w in writers.items()}

    owned: list[str] = []
    shared: list[str] = []
    for name in sorted(table_names):
        w = writers[name]
        is_cross = bool(w & cross_context_writers)
        if len(w) >= _MIN_SHARED_WRITERS or is_cross:
            shared.append(name)
        else:
            owned.append(name)

    return OwnershipReport(
        owned=tuple(owned),
        shared=tuple(shared),
        writers_by_table=writers_by_table,
    )
