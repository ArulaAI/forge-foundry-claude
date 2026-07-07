"""M4 Stage A — ``resolve`` (deterministic SP + table resolution).

Pulls the target SP's M2 :class:`StoredProc` profile out of the
:class:`StoredProcDigest` and the M1 :class:`Table` metadata for every entry in
its ``touched_tables``. The result :class:`ResolvedSp` is the bounded,
trusted universe of names every downstream stage (the Stage B LLM prompt, the
Stage C skeletons, the Stage E estimator) is allowed to reference — the
anti-hallucination ground truth.

Fails fast with :class:`SpNotFoundError` (the CLI maps this to exit 2) if
``sp_name`` is not present in the M2 digest. Pure and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.db_schema_analyze import SchemaDigest, Table
    from primitives.stored_proc_analyze import StoredProc, StoredProcDigest


class SpNotFoundError(LookupError):
    """Raised when ``sp_name`` is not present in the M2 ``StoredProcDigest``.

    The CLI maps this to exit 2 (SPEC §"Exit codes"): a missing SP is a hard
    stop, not a prompt to fabricate a conversion.
    """


@dataclass(frozen=True)
class ResolvedSp:
    """The deterministic Stage-A resolution for one SP.

    ``proc`` is the M2 profile; ``tables`` are the M1 :class:`Table` metadata
    for the SP's ``touched_tables`` (only those resolvable in the M1 digest —
    a touched table absent from the schema is recorded in
    ``unresolved_tables`` rather than fabricated).
    """

    proc: StoredProc
    tables: list[Table]
    unresolved_tables: list[str] = field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        """Resolved M1 table names, in ``touched_tables`` order."""
        return [t.name for t in self.tables]


def resolve(
    sp_name: str,
    *,
    schema: SchemaDigest,
    procs: StoredProcDigest,
) -> ResolvedSp:
    """Resolve ``sp_name`` against the M1 schema + M2 stored-proc digests.

    Args:
        sp_name: the stored-procedure name to convert (case-sensitive; must
            match a :attr:`StoredProc.name` in *procs*).
        schema: the M1 :class:`SchemaDigest` (table + column metadata).
        procs: the M2 :class:`StoredProcDigest` (SP profile + risk score).

    Returns:
        A :class:`ResolvedSp` carrying the target proc and its resolved tables.

    Raises:
        SpNotFoundError: ``sp_name`` is not present in *procs*.
    """
    proc = next((p for p in procs.procedures if p.name == sp_name), None)
    if proc is None:
        known = ", ".join(sorted(p.name for p in procs.procedures)) or "(none)"
        raise SpNotFoundError(
            f"stored procedure {sp_name!r} not found in the M2 digest; known: {known}"
        )

    by_name = {t.name: t for t in schema.tables}
    tables: list[Table] = []
    unresolved: list[str] = []
    for name in proc.touched_tables:
        table = by_name.get(name)
        if table is not None:
            tables.append(table)
        else:
            unresolved.append(name)
    return ResolvedSp(proc=proc, tables=tables, unresolved_tables=unresolved)


__all__ = [
    "ResolvedSp",
    "SpNotFoundError",
    "resolve",
]
