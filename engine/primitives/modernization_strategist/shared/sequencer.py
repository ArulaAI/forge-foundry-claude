"""Stage C — context sequencing (reuses the Layer-1 engine sequencer).

:func:`sequence_contexts` builds a :class:`SystemMap` whose nodes are the
Stage-A bounded contexts and whose edges are inter-context foreign keys
(``A → B`` when a table in ``A`` has an FK referencing a table in ``B``),
derives a per-context ``low``/``medium``/``high`` risk weight from the
aggregate M2 :class:`RiskScore`, and delegates to the engine's
:func:`primitives._shared.modernization.sequence` (``sequencer.order``) to
produce the risk-weighted, wave-sequenced :class:`Roadmap`.

Risk thresholds (on the summed ``RiskScore.total`` of every proc touching a
context's tables):

* ``< _MEDIUM_THRESHOLD`` (10)  → ``low``
* ``< _HIGH_THRESHOLD`` (30)   → ``medium``
* ``>= _HIGH_THRESHOLD`` (30)  → ``high``

Pure and deterministic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._shared.modernization import (
    SystemMap,
    SystemMapEdge,
    SystemMapNode,
    sequence,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from primitives._shared.modernization import Roadmap
    from primitives.db_schema_analyze import SchemaDigest
    from primitives.modernization_strategist.shared.models import BoundedContext
    from primitives.stored_proc_analyze import StoredProcDigest

#: Summed ``RiskScore.total`` thresholds bucketing a context's risk weight.
_MEDIUM_THRESHOLD = 10
_HIGH_THRESHOLD = 30


def _risk_bucket(total: int) -> str:
    """Map a summed risk total onto ``low`` / ``medium`` / ``high``."""
    if total < _MEDIUM_THRESHOLD:
        return "low"
    if total < _HIGH_THRESHOLD:
        return "medium"
    return "high"


def sequence_contexts(
    contexts: Sequence[BoundedContext],
    schema: SchemaDigest,
    procs: StoredProcDigest,
    *,
    generated_at: datetime | None = None,
) -> Roadmap:
    """Sequence *contexts* into risk-weighted extraction waves (Stage C)."""
    context_of: dict[str, str] = {table: ctx.name for ctx in contexts for table in ctx.tables}

    nodes = [SystemMapNode(id=ctx.name, kind="bounded_context") for ctx in contexts]

    # --- Inter-context FK edges (A → B when A's table FK-references B's). ---
    seen: set[tuple[str, str]] = set()
    edges: list[SystemMapEdge] = []
    for table in schema.tables:
        src_ctx = context_of.get(table.name)
        if src_ctx is None:
            continue
        for fk in table.foreign_keys:
            dst_ctx = context_of.get(fk.referenced_table)
            if dst_ctx is None or dst_ctx == src_ctx:
                continue
            key = (src_ctx, dst_ctx)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                SystemMapEdge(
                    src=src_ctx,
                    dst=dst_ctx,
                    kind="fk",
                    evidence=f"{table.name}.{','.join(fk.columns)}->{fk.referenced_table}",
                    confidence="exact",
                )
            )

    system_map = SystemMap(
        nodes=sorted(nodes, key=lambda n: n.id),
        edges=sorted(edges, key=lambda e: (e.src, e.dst, e.kind, e.evidence)),
    )

    # --- Per-context risk weight from aggregate M2 RiskScore.total. --------
    risk_total: dict[str, int] = {ctx.name: 0 for ctx in contexts}
    for proc in procs.procedures:
        touched_ctxs = {context_of[t] for t in proc.touched_tables if t in context_of}
        for ctx_name in touched_ctxs:
            risk_total[ctx_name] += proc.risk_score.total

    risk_weights: dict[str, str] = {name: _risk_bucket(total) for name, total in risk_total.items()}

    return sequence(system_map, risk_weights, generated_at=generated_at)
