"""Shared modernization-engine data models (Layer 1).

The cross-primitive deterministic engine generalized from ``mfx`` (design spec
``docs/superpowers/specs/2026-06-12-mtier-token-efficiency-mfx-engine-design.md``
§3, Layer 1). These are the four shared artifact shapes M3/M4/M5 produce and
consume:

* :class:`EngineFacts` — the generic *full facts* input. A primitive's
  discovery output (M5 ``AnalysisDigest``, M3 ``SchemaDigest``, …) is mapped onto
  this language-agnostic contract so the engine never depends on a flavor-specific
  model. The model queries slices of the full ``facts``/``*.json``; it never
  re-reads source (§2 two-artifact discipline).
* :class:`ModernizationDigest` — the **token-budgeted digest** (target ≤1.5k
  tokens) :func:`primitives._shared.modernization.digest.compress` produces from
  :class:`EngineFacts`. Carries only what cross-unit reasoning needs (identity,
  stack + migration-state summary, exposed/consumed interfaces, domains, key
  entities, tier hint, debt summary) so cross-unit reasoning fits in one context.
* :class:`SystemMap` — the cross-unit dependency graph ``correlate.py`` emits.
* :class:`Roadmap` — the risk-weighted, wave-sequenced plan ``sequencer.py`` emits.

All four are **persisted machine artifacts** (Python-produced, model-read), so —
like ``discovery`` ``ReconReport``/``AnalysisDigest`` — they carry a pinned
``schema_version`` and ``@accepts_extended_schema`` (their JSON Schemas use
``$ref``/``additionalProperties`` beyond the Gemini ``response_format`` subset;
they are never used as an LLM response schema). They are pure data with no LLM
involvement (golden-file testable).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from primitives._response_schema_validator import accepts_extended_schema

# ---------------------------------------------------------------------------
# EngineFacts — the generic full-facts input contract
# ---------------------------------------------------------------------------


class FactsUnit(BaseModel):
    """A unit of work (program / class / service / batch job).

    ``weight`` is a flavor-supplied ranking proxy (lines of code or a complexity
    score) used to pick the *key entities* for the digest; the engine does not
    interpret its absolute value, only its ordering.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str = ""
    weight: int = 0
    calls_to: tuple[str, ...] = ()


class FactsInterface(BaseModel):
    """A boundary the system exposes to, or consumes from, the outside.

    ``direction`` separates the two halves of cross-unit correlation: an
    ``exposed`` interface of unit A that matches a ``consumed`` interface of unit
    B is an edge ``B → A`` (``correlate.py``, #634).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str = ""
    direction: Literal["exposed", "consumed"]


@accepts_extended_schema
class EngineFacts(BaseModel):
    """The language-agnostic *full facts* a primitive hands the engine.

    A flavor/primitive maps its rich discovery model onto this contract (M5's
    ``analyze.py`` adapts ``AnalysisDigest`` → ``EngineFacts``). The engine reads
    only this shape, so it stays decoupled from any one platform flavor.

    ``migration_state`` / ``domains`` / ``tier_hint`` are optional interpretive
    fields populated by later detectors (M3 ``context_detector`` /
    ``antipattern_detector``); they pass straight through ``compress`` and are
    ``None`` / empty for a primitive that does not compute them (e.g. M5).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    code_root: str
    flavor: str
    loc_by_language: dict[str, int] = Field(default_factory=dict)
    units: list[FactsUnit] = Field(default_factory=list)
    interfaces: list[FactsInterface] = Field(default_factory=list)
    shared_components: list[str] = Field(default_factory=list)
    debt: list[str] = Field(default_factory=list)
    migration_state: str | None = None
    domains: list[str] = Field(default_factory=list)
    tier_hint: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# ModernizationDigest — the token-budgeted compressed digest
# ---------------------------------------------------------------------------


@accepts_extended_schema
class ModernizationDigest(BaseModel):
    """The ≤1.5k-token compressed view of one unit's facts.

    Produced by :func:`primitives._shared.modernization.digest.compress`. Carries
    name-only interface/entity lists and counts (never full records) so a model
    can reason across many units in one context without re-reading any
    ``facts.json`` (§2 two-artifact discipline).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    code_root: str
    flavor: str
    #: Stack mix — lines of code per language bucket (small dict, kept whole).
    loc_by_language: dict[str, int] = Field(default_factory=dict)
    #: Migration-state summary (e.g. "Struts↔Spring coexistence"); ``None`` when
    #: the flavor does not detect coexistence signals.
    migration_state: str | None = None
    #: Names only (top-N) of interfaces the unit exposes / consumes.
    exposed_interfaces: list[str] = Field(default_factory=list)
    consumed_interfaces: list[str] = Field(default_factory=list)
    #: Bounded-context / domain labels (M3 ``context_detector``); empty for M5.
    domains: list[str] = Field(default_factory=list)
    #: Highest-weight unit names (top-N) — the cross-unit "key entities".
    key_entities: list[str] = Field(default_factory=list)
    #: Modernization-tier hint (e.g. "strangler-fig" / "lift-and-shift"); M3 fills.
    tier_hint: str | None = None
    #: Top-N debt / security / antipattern one-liners.
    debt_summary: list[str] = Field(default_factory=list)
    #: Top-N shared-component names — kept (not just counted) so ``correlate.py``
    #: can match shared-lib coordinates across units (#634).
    shared_components: list[str] = Field(default_factory=list)
    #: Full counts (the lists above are truncated, these are not).
    unit_count: int = 0
    shared_component_count: int = 0
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# SystemMap — cross-unit dependency graph (correlate.py, #634)
# ---------------------------------------------------------------------------


class SystemMapNode(BaseModel):
    """One unit in the cross-unit graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str = ""
    domain: str | None = None


class SystemMapEdge(BaseModel):
    """A directed dependency ``src → dst`` with provenance.

    ``confidence`` grades how the match was made: ``exact`` (an exact
    consumed↔exposed / shared-coordinate / messaging name match), ``heuristic``
    (a normalized/fuzzy match), or ``unresolved`` (``dst`` is an external/unknown
    node — the consumed interface matched no unit's exposed surface). ``evidence``
    names the matched coordinate(s).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    src: str
    dst: str
    kind: str = ""
    evidence: str = ""
    confidence: Literal["exact", "heuristic", "unresolved"] = "exact"


@accepts_extended_schema
class SystemMap(BaseModel):
    """The joined cross-unit dependency graph (``correlate.py`` output)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    nodes: list[SystemMapNode] = Field(default_factory=list)
    edges: list[SystemMapEdge] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Roadmap — risk-weighted, wave-sequenced plan (sequencer.py, #634)
# ---------------------------------------------------------------------------


class RoadmapItem(BaseModel):
    """One work item: a unit placed in a wave, deferred, or never-extract.

    ``rationale`` explains a ``deferred`` / ``never_extract`` placement (e.g.
    "in dependency cycle [a, b]", "external/unknown dependency"); it is empty for
    items that sit cleanly in an ``extract_order`` wave.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    risk: Literal["low", "medium", "high"] = "medium"
    depends_on: tuple[str, ...] = ()
    rationale: str = ""


class RoadmapWave(BaseModel):
    """A topologically-ordered batch of items that can proceed together."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    wave: int
    rationale: str = ""
    items: tuple[RoadmapItem, ...] = ()


class Roadmap(BaseModel):
    """The wave-sequenced modernization plan (``sequencer.py`` output).

    Unlike its three sibling artifacts this shape stays within the Gemini schema
    subset today, so it needs no ``@accepts_extended_schema`` exemption; add one
    only if a future field introduces ``$ref``/``anyOf`` nesting that trips the
    gate (``tests/test_modernization_engine.py`` would catch it).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    #: ``extract_order`` — units to extract, in risk-weighted topological waves.
    waves: list[RoadmapWave] = Field(default_factory=list)
    #: Extractable but held back (e.g. high-risk with unmet preconditions); each
    #: carries a ``rationale``.
    deferred: list[RoadmapItem] = Field(default_factory=list)
    #: Units that cannot be cleanly extracted — in a dependency cycle or resolving
    #: only to an external/unknown node; each carries a ``rationale``.
    never_extract: list[RoadmapItem] = Field(default_factory=list)
    #: Dependency cycles surfaced (not silently dropped) — each a tuple of the
    #: node ids that form the strongly-connected component.
    cycles: list[tuple[str, ...]] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
