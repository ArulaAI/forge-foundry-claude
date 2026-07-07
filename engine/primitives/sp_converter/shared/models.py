"""M4 /mod-sp-converter — Pydantic data models.

The conversion surface M4's pipeline produces: a deterministic
:class:`EffortEstimate` (Stage E), the per-stage skeleton
:class:`ConversionArtifact` list (Stage C), and the top-level
:class:`ConversionPlan` (the single LLM call, Stage B, plus the folded-in
effort estimate). All models are frozen / ``extra="forbid"`` and pure data.

The persisted :class:`ConversionPlan` is decorated with
``@accepts_extended_schema`` because its JSON Schema nests a ``$ref`` chain
(``EffortEstimate``) and ``additionalProperties: <schema>`` (the
``side_effect_mappings`` / ``EffortEstimate.factors`` dict fields) beyond the
Gemini ``response_format`` subset — it is written to disk for downstream
consumption (M0 roll-up, scaffold generator), never sent through
structured-output mode.

The model surface matches ``skill/references/schemas/conversion_plan.json`` and
SPEC.md §"File contract".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from primitives._response_schema_validator import accepts_extended_schema

# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

#: The built-in conversion targets. Third-party FLEX-3 entry-point renderers
#: register additional names at the registry layer (see
#: ``shared/skeletons.py``); those are not part of this persisted Literal.
Target: TypeAlias = Literal[
    "spring-batch",
    "fastapi",
    "celery",
    "spring-boot",
    "plan-only",
]

_ALL_TARGETS: tuple[Target, ...] = (
    "spring-batch",
    "fastapi",
    "celery",
    "spring-boot",
    "plan-only",
)

#: Closed artifact kinds so downstream consumers (a future M4 → C4 sub-call, an
#: IDE extension) can filter on artifact type without re-parsing the extension.
ArtifactKind: TypeAlias = Literal["scaffold", "config", "test", "readme"]

# ---------------------------------------------------------------------------
# EffortEstimate (Stage E)
# ---------------------------------------------------------------------------


class EffortEstimate(BaseModel):
    """T-shirt-size effort estimate for a single SP conversion.

    ``size`` is the Stage E roll-up; ``score`` is the integer composite the
    bucket is derived from; ``factors`` is the per-axis weighting that produced
    it (e.g. ``{"risk_total": 3.0, "table_touch": 2.0, "body_loc": 1.0,
    "target_adjustment": 0.0, "weighted_total": 14.0}``). Keeping ``factors``
    free-form (``dict[str, float]``) lets the calibration in
    ``docs/m4-effort-calibration.md`` evolve without re-shaping the record.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    size: Literal["S", "M", "L", "XL"]
    score: int
    rationale: str
    factors: dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# ConversionArtifact (Stage C)
# ---------------------------------------------------------------------------


class ConversionArtifact(BaseModel):
    """A single rendered skeleton file (path + content).

    ``path`` is relative to the per-target scaffold root (e.g.
    ``pom.xml``, ``app/routers/<sp>.py``); ``kind`` tags the artifact role.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    content: str
    kind: ArtifactKind = "scaffold"


# ---------------------------------------------------------------------------
# ConversionPlan (Stage B + Stage E)
# ---------------------------------------------------------------------------


@accepts_extended_schema
class ConversionPlan(BaseModel):
    """Top-level migration plan produced by the M4 pipeline.

    Schema-versioned so downstream primitives can refuse to consume a
    mismatched output (SPEC §"File contract"). The fields after ``target`` are
    the per-stage outputs the Stage B plan synthesizer
    (``shared/plan_synthesizer.py``) fills in; ``effort`` is folded in from the
    Stage E estimator.

    Gemini subset (issue #548 / SPEC §6.3 + §7.2): persisted M4 artefact, not an
    LLM ``response_format`` schema. Pydantic emits ``$ref`` for the nested
    ``EffortEstimate`` and ``additionalProperties: <schema>`` for the
    ``side_effect_mappings`` / ``EffortEstimate.factors`` dict fields — both
    exceed Gemini's accepted subset. Decorated with ``@accepts_extended_schema``
    because the plan is written to disk for downstream consumption, not sent
    through harness structured-output mode.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    sp_name: str
    target: Target
    data_deps: list[str] = Field(default_factory=list)
    txn_boundaries: list[str] = Field(default_factory=list)
    side_effect_mappings: dict[str, str] = Field(default_factory=dict)
    commit_sequence: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    gotchas: list[str] = Field(default_factory=list)
    effort: EffortEstimate
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "ArtifactKind",
    "ConversionArtifact",
    "ConversionPlan",
    "EffortEstimate",
    "Target",
]
