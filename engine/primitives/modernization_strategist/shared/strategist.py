"""M3 orchestrator + emit — assemble Stages A-D and serialize the plan.

:func:`build_decomposition` runs Stage A (context detection) → B (ownership)
→ C (sequencing) → D (antipattern detection) and assembles the result into a
:class:`DecompositionPlan`. :func:`decomposition_digest_yaml` maps the plan
onto the Layer-1 :class:`EngineFacts`, compresses it through the engine's
:func:`compress`, and renders a token-budgeted (≤1.5k) YAML digest.
:func:`write_decomposition` persists both ``decomposition.json`` and
``decomposition-digest.yaml``.

Pure and deterministic (modulo ``generated_at``): the same digests + a fixed
``generated_at`` always produce the same plan and the same serialized output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from primitives._shared.modernization import (
    EngineFacts,
    FactsInterface,
    FactsUnit,
    compress,
)
from primitives.modernization_strategist.shared.antipattern_detector import detect_antipatterns
from primitives.modernization_strategist.shared.context_detector import detect_contexts
from primitives.modernization_strategist.shared.models import DecompositionPlan
from primitives.modernization_strategist.shared.ownership_analyzer import analyze_ownership
from primitives.modernization_strategist.shared.sequencer import sequence_contexts

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from primitives.db_schema_analyze import SchemaDigest
    from primitives.stored_proc_analyze import StoredProcDigest


def build_decomposition(
    schema: SchemaDigest,
    procs: StoredProcDigest,
    *,
    generated_at: datetime | None = None,
) -> DecompositionPlan:
    """Run Stages A→B→C→D and assemble the :class:`DecompositionPlan`."""
    contexts = detect_contexts(schema, procs)
    ownership = analyze_ownership(contexts, schema, procs)
    roadmap = sequence_contexts(contexts, schema, procs, generated_at=generated_at)
    antipatterns = detect_antipatterns(contexts, schema, procs)

    if generated_at is not None:
        return DecompositionPlan(
            contexts=contexts,
            ownership=ownership,
            roadmap=roadmap,
            antipatterns=antipatterns,
            generated_at=generated_at,
        )
    return DecompositionPlan(
        contexts=contexts,
        ownership=ownership,
        roadmap=roadmap,
        antipatterns=antipatterns,
    )


def _to_engine_facts(plan: DecompositionPlan) -> EngineFacts:
    """Map a :class:`DecompositionPlan` onto the Layer-1 :class:`EngineFacts`."""
    units = [
        FactsUnit(name=ctx.name, kind="bounded_context", weight=len(ctx.tables))
        for ctx in plan.contexts
    ]
    # Inter-context dependency edges → an exposed/consumed interface pair so
    # the engine's correlator/digest can see the cross-context coupling.
    interfaces: list[FactsInterface] = []
    for wave in plan.roadmap.waves:
        for item in wave.items:
            for dep in item.depends_on:
                interfaces.append(
                    FactsInterface(name=f"{item.id}->{dep}", kind="fk", direction="consumed")
                )
                interfaces.append(
                    FactsInterface(name=f"{item.id}->{dep}", kind="fk", direction="exposed")
                )
    debt = [f"{a.kind}: {a.detail}" for a in plan.antipatterns]
    return EngineFacts(
        code_root="decomposition",
        flavor="sql_database",
        units=units,
        interfaces=interfaces,
        debt=debt,
        domains=[ctx.name for ctx in plan.contexts],
        generated_at=plan.generated_at,
    )


def decomposition_digest_yaml(plan: DecompositionPlan) -> str:
    """Render the plan as a token-budgeted (≤1.5k) YAML digest."""
    digest = compress(_to_engine_facts(plan))
    return yaml.safe_dump(digest.model_dump(mode="json"), sort_keys=False)


def write_decomposition(plan: DecompositionPlan, out_dir: Path) -> None:
    """Write ``decomposition.json`` + ``decomposition-digest.yaml`` to *out_dir*."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "decomposition.json").write_text(
        plan.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "decomposition-digest.yaml").write_text(
        decomposition_digest_yaml(plan), encoding="utf-8"
    )
