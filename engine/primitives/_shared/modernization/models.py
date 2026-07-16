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
from typing import Any, Literal

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
    """One unit in the cross-unit graph.

    ``domains`` carries **all** of the unit's bounded-context / domain labels
    (#1375). A unit that a discovery pass tags with several domains (e.g. a
    petclinic app spanning ``owner``/``pet``/``vet``/``visit``) keeps every one,
    so the bounded contexts a consumer derives are the *union* of every node's
    domains — not just the first label of each node (the pre-#1375 behaviour that
    silently collapsed the other domains into ``_unassigned_`` downstream).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str = ""
    domains: list[str] = Field(default_factory=list)


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


class SystemMapEntryPoint(BaseModel):
    """A typed **inbound** node on the system-map (Track B, #1427).

    Unlike a :class:`SystemMapNode` (a unit) or an interface edge (a
    unit→unit dependency), an entry point is the *outside trigger* that drives
    the system: an HTTP route, a scheduled/cron job, a CLI command, a message
    consumer, or an external integration / webhook / SOAP endpoint. ``trigger``
    carries the real value (cron expr / topic / queue / route / command),
    ``target`` names the context/unit it dispatches into, and ``source`` is the
    ``file:line`` provenance. All but ``name``/``kind`` default empty so a
    thinly-captured entry point still threads through honestly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str = ""
    trigger: str = ""
    target: str = ""
    source: str = ""


@accepts_extended_schema
class SystemMap(BaseModel):
    """The joined cross-unit dependency graph (``correlate.py`` output).

    Track B (#1427): ``entry_points`` carries the typed inbound triggers that
    drive the system (scheduled jobs, message consumers, CLI entries, webhooks,
    …). Default-empty — a system with no captured entry points threads through
    byte-identically to prior behavior.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    nodes: list[SystemMapNode] = Field(default_factory=list)
    edges: list[SystemMapEdge] = Field(default_factory=list)
    entry_points: list[SystemMapEntryPoint] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Roadmap — risk-weighted, wave-sequenced plan (sequencer.py, #634)
# ---------------------------------------------------------------------------


class RoadmapItem(BaseModel):
    """One work item: a unit placed in a wave, deferred, or never-extract.

    ``rationale`` explains a ``deferred`` / ``never_extract`` / ``isolated``
    placement (e.g. "in dependency cycle [a, b]", "external/unknown dependency",
    "no dependencies discovered"); it is empty for items that sit cleanly in an
    ``extract_order`` wave.
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
    #: Genuinely standalone units — no edges to or from any other unit and not a
    #: shared-lib / data carrier others depend on. Surfaced here (not silently
    #: dropped, nor scheduled as a meaningless wave-1 extraction target) because an
    #: isolated unit is neither ``deferred`` (that implies a dependency block) nor
    #: ``never_extract`` (that implies a cycle) — it simply has nothing to sequence
    #: against; each carries a ``rationale`` (#1378).
    isolated: list[RoadmapItem] = Field(default_factory=list)
    #: Dependency cycles surfaced (not silently dropped) — each a tuple of the
    #: node ids that form the strongly-connected component.
    cycles: list[tuple[str, ...]] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# FunctionalInventory — the functional-map FR layer (app-mod FR-threading, #1355)
# ---------------------------------------------------------------------------


class FunctionalInventory(BaseModel):
    """The synthesized *functional layer* of a functional-map (S61) artifact.

    The :class:`ModernizationDigest` is deliberately structural — it feeds
    ``correlate.build_system_map``, which needs only interfaces/coordinates. The
    functional-map's synthesized requirements (FR statements, acceptance criteria,
    business rules, flows, journeys) therefore travel on this **separate typed
    channel** (#1355) rather than bloating the digest. It is extracted from the
    raw ``functional-map.json`` by
    :func:`primitives._shared.modernization.adapters.functional_map.functional_map_to_inventory`
    and threaded into the scope synthesizer's spec/roadmap steps (#1356/#1357) and
    the app-analysis playbook render (#1358).

    All collections default to empty so a functional-map missing any field (or a
    ``--no-synthesis`` run) yields a valid, empty inventory rather than crashing.
    Entries are kept as plain dicts (the ``model_dump()`` shapes the functional-map
    render emits) so the channel stays decoupled from the S61 model classes.
    """

    model_config = ConfigDict(extra="forbid")

    #: FR statements: ``{id, statement, priority, fr_type, source_evidence, domain?}``.
    fr_statements: list[dict[str, Any]] = Field(default_factory=list)
    #: Acceptance criteria: ``{fr_id, id, given, when, then}``.
    acceptance_criteria: list[dict[str, Any]] = Field(default_factory=list)
    #: Business rules: ``{step_ref, rule, source}``.
    business_rules: list[dict[str, Any]] = Field(default_factory=list)
    #: User flows: ``{id, name, steps, variant}``.
    flows: list[dict[str, Any]] = Field(default_factory=list)
    #: User journeys: ``{id, persona, goal, flow_ids}``.
    journeys: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when no functional layer was present (e.g. a --no-synthesis map)."""
        return not (
            self.fr_statements
            or self.acceptance_criteria
            or self.business_rules
            or self.flows
            or self.journeys
        )


# ---------------------------------------------------------------------------
# RepoEntry / RepoSet — multi-repo modernization set (MR1, #1550)
# ---------------------------------------------------------------------------


class RepoEntry(BaseModel):
    """One repository in a multi-repo modernization set (MR1, #1550).

    ``role`` is the tier this repo plays in the split-repo enterprise layout.
    ``state`` is an OPTIONAL, UNTRUSTED prior hint about modernization state —
    downstream (MR3/MR4) treats it as a prior only, never a verdict; a repo with
    no reliable docs simply omits it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["ui", "api", "db", "iac"]
    path: str
    state: Literal["legacy", "partial", "modern"] | None = None


class RepoSet(BaseModel):
    """A validated set of repositories keyed by role (MR1, #1550).

    Built from ``.foundry/repos.yaml`` and/or ``--repo role=path`` overrides.
    Order-stable (insertion order of ``entries``). Empty when no manifest and no
    override are supplied — callers then fall back to the single-repo path
    (additive invariant, #1408).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    entries: tuple[RepoEntry, ...] = ()

    def by_role(self, role: str) -> RepoEntry | None:
        for e in self.entries:
            if e.role == role:
                return e
        return None
