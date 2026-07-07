"""M4 /mod-sp-converter — Python-load-bearing conversion pipeline.

Five stages convert one legacy SQL stored procedure into a per-target migration
plan + starter scaffold:

* **A** :mod:`sp_resolver` — resolve the SP + its tables against the M1/M2
  digests (deterministic; the anti-hallucination ground truth).
* **B** :mod:`plan_synthesizer` — build the prompt + apply the in-session
  judgment for the :class:`ConversionPlan` (guarded against hallucinated names),
  fold in effort. The judgment is produced via the skill-first protocol.
* **C** :mod:`skeletons` + ``skeleton_<target>`` — render the per-target skeleton
  artifacts via the FLEX-3 ``_TARGET_REGISTRY`` (deterministic).
* **D** :mod:`notes_renderer` — render ``plan.md`` from the plan (deterministic).
* **E** :mod:`effort_estimator` — the S/M/L/XL estimate (pure).

:func:`write_conversion` (:mod:`converter`) persists the assembled outputs.
"""

from __future__ import annotations

from primitives.sp_converter.shared.converter import (
    ConversionResult,
    write_conversion,
)
from primitives.sp_converter.shared.effort_estimator import estimate
from primitives.sp_converter.shared.models import (
    ConversionArtifact,
    ConversionPlan,
    EffortEstimate,
    Target,
)
from primitives.sp_converter.shared.notes_renderer import render_plan_md
from primitives.sp_converter.shared.plan_synthesizer import (
    SYSTEM_PROMPT,
    apply_plan_judgment,
    build_prompt_item_for_sp,
    build_user_prompt,
)
from primitives.sp_converter.shared.skeletons import (
    registered_targets,
    render,
)
from primitives.sp_converter.shared.sp_resolver import (
    ResolvedSp,
    SpNotFoundError,
    resolve,
)

__all__ = [
    "SYSTEM_PROMPT",
    "ConversionArtifact",
    "ConversionPlan",
    "ConversionResult",
    "EffortEstimate",
    "ResolvedSp",
    "SpNotFoundError",
    "Target",
    "apply_plan_judgment",
    "build_prompt_item_for_sp",
    "build_user_prompt",
    "estimate",
    "registered_targets",
    "render",
    "render_plan_md",
    "resolve",
    "write_conversion",
]
