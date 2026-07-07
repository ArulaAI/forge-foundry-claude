"""M3 /mod-modernization-strategist — deterministic shared pipeline.

Stages A-D decompose a legacy SQL database (the M1 :class:`SchemaDigest`
+ M2 :class:`StoredProcDigest`) into bounded contexts with ownership,
risk-weighted sequencing, and antipattern analysis — all pure-deterministic,
LLM-free. The Layer-1 modernization engine
(:mod:`primitives._shared.modernization`) is reused for sequencing
(:func:`sequence`) and digest compression (:func:`compress`).
"""

from __future__ import annotations

from primitives.modernization_strategist.shared.antipattern_detector import detect_antipatterns
from primitives.modernization_strategist.shared.context_detector import detect_contexts
from primitives.modernization_strategist.shared.diagram_renderer import render_strategy_diagrams
from primitives.modernization_strategist.shared.models import (
    Antipattern,
    BoundedContext,
    DecompositionPlan,
    OwnershipReport,
)
from primitives.modernization_strategist.shared.narrative_synthesizer import (
    apply_narrative_judgments,
    build_prompt_items_for_contexts,
)
from primitives.modernization_strategist.shared.ownership_analyzer import analyze_ownership
from primitives.modernization_strategist.shared.plan_writer import render_plan_md
from primitives.modernization_strategist.shared.sequencer import sequence_contexts
from primitives.modernization_strategist.shared.strategist import (
    build_decomposition,
    decomposition_digest_yaml,
    write_decomposition,
)
from primitives.modernization_strategist.shared.sub_call import (
    ReviewedAntipattern,
    SubCallResult,
    dedup_against_c8,
)

__all__ = [
    "Antipattern",
    "BoundedContext",
    "DecompositionPlan",
    "OwnershipReport",
    "ReviewedAntipattern",
    "SubCallResult",
    "analyze_ownership",
    "apply_narrative_judgments",
    "build_decomposition",
    "build_prompt_items_for_contexts",
    "decomposition_digest_yaml",
    "dedup_against_c8",
    "detect_antipatterns",
    "detect_contexts",
    "render_plan_md",
    "render_strategy_diagrams",
    "sequence_contexts",
    "write_decomposition",
]
