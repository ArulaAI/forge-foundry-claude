"""M3 diagram renderer — three Mermaid views of the :class:`DecompositionPlan`.

:func:`render_strategy_diagrams` produces three deterministic Mermaid flowcharts
via the Layer-1 :func:`primitives._shared.modernization.render.flowchart` helper
(token-offload: the verbose Mermaid text is Python-rendered, not model-emitted):

* ``01_decomposition.mmd`` — bounded contexts as nodes, inter-context edges from
  the roadmap's ``depends_on`` plus cross-context antipattern evidence.
* ``02_sequencing.mmd`` — the roadmap waves, ``wave N`` → ``wave N+1``.
* ``03_ownership.mmd`` — each context → its owned / shared tables.

Pure and deterministic: no LLM, no clock, no filesystem. Returns a
``{filename: mermaid_body}`` dict the CLI writes under ``diagrams/``. Bodies are
**raw** Mermaid (``fenced=False``) — a ``.mmd`` file must hold raw source, not a
``` ```mermaid ``` fence, so ``mmdc`` and other ``.mmd`` tools accept it (#947).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._shared.modernization import flowchart

if TYPE_CHECKING:
    from primitives.modernization_strategist.shared.models import DecompositionPlan

_DECOMPOSITION_FILENAME = "01_decomposition.mmd"
_SEQUENCING_FILENAME = "02_sequencing.mmd"
_OWNERSHIP_FILENAME = "03_ownership.mmd"

#: A cross-context FK antipattern's evidence is ``(src_table, dst_table, ...)`` —
#: we need both endpoints to draw the coupling edge.
_FK_EVIDENCE_MIN = 2


def _decomposition_diagram(plan: DecompositionPlan) -> str:
    """Contexts as nodes; inter-context dependency edges (roadmap + antipatterns)."""
    nodes = [(ctx.name, f"{ctx.name} ({len(ctx.tables)} tables)") for ctx in plan.contexts]

    edges: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    # Roadmap dependency edges: item -> each dependency.
    for wave in plan.roadmap.waves:
        for item in wave.items:
            for dep in item.depends_on:
                key = (item.id, dep)
                if key not in seen:
                    seen.add(key)
                    edges.append((item.id, dep, "depends on"))
    # Cross-context FK antipatterns add a coupling edge (evidence = src, dst, ...).
    table_to_ctx = {t: ctx.name for ctx in plan.contexts for t in ctx.tables}
    for ap in plan.antipatterns:
        if ap.kind == "cross_context_fk" and len(ap.evidence) >= _FK_EVIDENCE_MIN:
            src_ctx = table_to_ctx.get(ap.evidence[0])
            dst_ctx = table_to_ctx.get(ap.evidence[1])
            if src_ctx and dst_ctx and src_ctx != dst_ctx:
                key = (src_ctx, dst_ctx)
                if key not in seen:
                    seen.add(key)
                    edges.append((src_ctx, dst_ctx, "cross-context FK"))
    return flowchart(nodes, edges, direction="LR", fenced=False)


def _sequencing_diagram(plan: DecompositionPlan) -> str:
    """Roadmap waves as nodes; ``wave N`` → ``wave N+1``."""
    waves = sorted(plan.roadmap.waves, key=lambda w: w.wave)
    nodes: list[tuple[str, str]] = []
    for w in waves:
        names = ", ".join(item.name for item in w.items) or "(empty)"
        nodes.append((f"wave{w.wave}", f"Wave {w.wave}: {names}"))
    edges: list[tuple[str, str, str]] = [
        (f"wave{waves[i].wave}", f"wave{waves[i + 1].wave}", "then") for i in range(len(waves) - 1)
    ]
    return flowchart(nodes, edges, direction="TD", fenced=False)


def _ownership_diagram(plan: DecompositionPlan) -> str:
    """Each context → its owned / shared tables."""
    owned = set(plan.ownership.owned)
    shared = set(plan.ownership.shared)
    nodes: list[tuple[str, str]] = []
    edges: list[tuple[str, str, str]] = []
    for ctx in plan.contexts:
        nodes.append((ctx.name, ctx.name))
        for table in ctx.tables:
            tnode = f"{ctx.name}::{table}"
            label = "shared" if table in shared else ("owned" if table in owned else "")
            nodes.append((tnode, table))
            edges.append((ctx.name, tnode, label))
    return flowchart(nodes, edges, direction="LR", fenced=False)


def render_strategy_diagrams(plan: DecompositionPlan) -> dict[str, str]:
    """Return the three Mermaid strategy views keyed by output filename.

    Deterministic given *plan*. The CLI writes each value to
    ``<output-dir>/diagrams/<filename>``.
    """
    return {
        _DECOMPOSITION_FILENAME: _decomposition_diagram(plan),
        _SEQUENCING_FILENAME: _sequencing_diagram(plan),
        _OWNERSHIP_FILENAME: _ownership_diagram(plan),
    }


__all__ = ["render_strategy_diagrams"]
