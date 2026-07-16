"""Synthesis judgment models for mod-modernization-scope (SP2.4, #1321; SP-B, #1382).

The interpretive parts of the four deliverables need the LLM-in-the-loop
extract→judge→continue protocol. The deterministic engine builds the system-map
and per-item skeletons; the SKILL.md body authors the narrative/interpretive
fields (spec requirements/decisions prose, ADR decisions, bounded-context
rationale). ``--continue --judgments <path>`` validates the authored JSON against
:class:`ScopeSynthesis` and renders the role-complete specs + ADRs + roadmap.

The synthesis prompts file (``synthesis_prompts.json``) written by ``--extract-only``
enumerates one prompt item per spec (page/domain) plus one per cross-cutting ADR;
the body returns a ``synthesis.json`` conforming to :class:`ScopeSynthesis`. A
worked example that validates against these models is shipped in
``skill/references/schemas/synthesis.json`` (avoids the #1291-class example gap).

**SP-B schema bump (#1382):** ``ScopeSynthesis`` is now ``schema_version: "2.0"``.
``SpecJudgment`` replaces the old flat ``requirements: list[str]`` and
``decisions: list[str]`` with structured nested models (``Requirement``,
``AcceptanceCriterion``, ``TestScenarioSpec``, ``ArchitectureDecision``, etc.).  The
old flat shape is **rejected** by :func:`validate_synthesis` with a clear,
actionable error message.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# SP-B nested models — all extra="forbid", every field default-empty for
# graceful degradation so a partial synthesis still validates.
# ---------------------------------------------------------------------------


class Requirement(BaseModel):
    """One structured business/functional requirement.

    ``id`` carries the stable ``R-NNN`` identifier authored by the harness.
    ``scope`` names the field/entity this requirement governs.
    ``source`` is a ``file:line`` evidence reference.
    ``priority`` is free-text (e.g. ``"must"`` / ``"should"`` / ``"nice-to-have"``).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    statement: str = ""
    scope: str = ""
    constraint: str = ""
    source: str = ""
    rationale: str = ""
    priority: str = ""


class AcceptanceCriterion(BaseModel):
    """One acceptance criterion in given/when/then form.

    ``covers`` is the list of ``R-`` ids this criterion validates (≥1).
    ``verified_by`` is the list of ``T-`` ids that exercise it.
    ``is_parity`` flags a criterion carried from a functional-map AC
    (i.e. it represents existing/legacy behaviour that must be preserved).

    ACs are a **top-level typed list** on :class:`SpecJudgment` (not nested
    under a single :class:`Requirement`) so one requirement can have multiple
    ACs — this breaks the old 1:1:1 R→AC→T lock.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    given: str = ""
    when: str = ""
    then: str = ""
    covers: list[str] = Field(default_factory=list)
    verified_by: list[str] = Field(default_factory=list)
    is_parity: bool = False
    # runtime_observed (runtime-explore integration): this AC is grounded in a journey
    # observed by driving the RUNNING app. Default false → byte-identical for a
    # synthesized AC; a true AC renders a "(runtime-observed)" label.
    runtime_observed: bool = False


class TestScenarioSpec(BaseModel):
    """One test scenario with structured preconditions, input, and expected output.

    ``type`` is one of ``"unit"`` / ``"integration"`` / ``"e2e"``.
    ``covers`` lists the ``R-`` ids the scenario validates.
    ``is_parity_oracle`` flags scenarios grounded in existing legacy tests
    (populated from SP-G test-inventory; defaults to ``False`` until SP-G lands).

    Named ``TestScenarioSpec`` (not ``TestScenario``) to avoid the pytest
    ``Test*`` collection heuristic warning.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    type: str = ""
    scenario: str = ""
    preconditions: str = ""
    input: str = ""
    expected: str = ""
    covers: list[str] = Field(default_factory=list)
    is_parity_oracle: bool = False


class ArchitectureDecision(BaseModel):
    """One inline architecture decision scoped to a spec (not a standalone ADR).

    ``id`` carries the stable ``ADR-NNN`` identifier.
    ``affects`` lists the ``R-`` ids this decision is relevant to.
    ``alternatives`` and ``consequences`` are free-text lists.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    title: str = ""
    context: str = ""
    decision: str = ""
    consequences: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    affects: list[str] = Field(default_factory=list)


class DataFieldMapping(BaseModel):
    """One legacy-column → target-field mapping (populated from M1 in SP-E).

    ``legacy`` is ``entity.column``; ``target`` is ``entity.field``.
    ``migration_notes`` is free-text; defaults empty until SP-E lands.
    """

    model_config = ConfigDict(extra="forbid")

    legacy: str = ""
    target: str = ""
    type: str = ""
    migration_notes: str = ""


class ApiEndpoint(BaseModel):
    """One REST-API endpoint — current (§8) or synthesized target (§8b).

    ``validation`` lists the ``R-`` ids the endpoint's contract enforces.
    ``security`` lists the roles permitted to call this endpoint.

    ``kind`` (SP3, #1411) distinguishes a ``"current"`` endpoint (the app's
    existing contract — from S40 api-contract-review or the functional-map
    controller inventory; the unset default reads as current) from a
    ``"target"`` endpoint (a resource-per-aggregate REST design the harness
    synthesizes in the target backend's idiom, rendered only when a target is
    set). ``from_controller`` traces a target endpoint back to its originating
    current controller form-flow (e.g. ``OwnerController.processCreationForm``);
    empty for a genuinely new target endpoint or for a current one.
    """

    model_config = ConfigDict(extra="forbid")

    verb: str = ""
    path: str = ""
    request: str = ""
    response: str = ""
    status_codes: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    security: list[str] = Field(default_factory=list)
    kind: str = ""  # "current" | "target"  (unset reads as current)
    from_controller: str = ""  # target → originating controller form-flow


class UserFlowSpec(BaseModel):
    """One user/navigation flow derived from the slice's flows and nav-edges (SP-A).

    ``steps`` is an ordered list of step descriptions.
    ``variant`` distinguishes happy-path / error / alternate flows.

    ``sequence_diagram`` (SP5, #1413) carries a harness-authored Mermaid
    ``sequenceDiagram`` string depicting the *target* request path for this flow
    (e.g. Angular component → REST controller → service → DB). Default-empty and
    additive under ``schema_version`` ``"2.1"``; rendered into the §6 flows
    section **only** when a target is set (no target / empty → omitted, no
    fabrication).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    name: str = ""
    steps: list[str] = Field(default_factory=list)
    variant: str = ""
    sequence_diagram: str = ""
    # runtime_observed (runtime-explore integration): this flow's steps were seen by
    # driving the RUNNING app (/mod-runtime-explore), not synthesized from source.
    # Default false → a synthesized flow is byte-identical; a true flow renders a
    # "(runtime-observed)" label (mirrors AcceptanceCriterion / is_parity_oracle).
    runtime_observed: bool = False


class Nfr(BaseModel):
    """One non-functional requirement.

    ``category`` is one of ``"perf"`` / ``"security"`` / ``"scalability"`` /
    ``"a11y"`` / ``"i18n"``.
    ``source`` is a ``file:line`` evidence reference (optional).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    category: str = ""
    requirement: str = ""
    source: str = ""


# ---------------------------------------------------------------------------
# Updated SpecJudgment — structured typed lists replace the old flat lists.
# ---------------------------------------------------------------------------


class SpecJudgment(BaseModel):
    """The authored content for one role-complete spec (one page/domain item).

    ``spec_id`` matches a skeleton item's ``id`` from ``synthesis_prompts.json``.

    **SP-B (#1382):** the old flat ``requirements: list[str]`` and
    ``decisions: list[str]`` are replaced with structured nested models.
    The harness now authors each requirement as a :class:`Requirement` object
    (carrying ``id``, ``statement``, ``source``, etc.); acceptance criteria,
    tests (:class:`TestScenarioSpec`), ADRs, data mappings, API endpoints, user
    flows, and NFRs each have their own typed list.
    :func:`validate_synthesis` rejects the old flat shape with a clear,
    actionable error message.

    ``citations`` are ``path:line`` evidence refs the engine appends to the spec
    so ``refcheck`` validates them against the tree.

    ``ui_mockup`` (SP5, #1413) carries a harness-authored Mermaid string depicting
    the *target* UI screen for this spec, designed in the target frontend's idiom
    (e.g. an Angular component + reactive-form field layout). Default-empty and
    additive under ``schema_version`` ``"2.1"``; rendered into §7 UX/UI **only**
    when a target is set (no target / empty → omitted, no fabrication).

    **Track C (#1429):** the BRD narrative + business-diagram fields carry the
    business-audience prose the harness authors for the per-context ``BRD.md``
    beside this spec. All default-empty and additive under ``schema_version``
    ``"2.2"``: ``business_narrative`` is a plain-language executive framing;
    ``business_process_diagram`` / ``capability_map`` are optional harness-authored
    Mermaid strings (the deterministic business-diagram emitters in
    :mod:`~primitives._shared.modernization.render` are the primary source — these
    only augment where the harness has richer framing); ``kpis`` /
    ``business_glossary`` / ``assumptions`` / ``business_risks`` are plain-language
    bullet lists. Empty → the BRD renders the structural sections alone (sparser,
    honest, no fabrication). The BRD shares this spec's ``R-`` / ``FR-`` ids — a
    business reader and an engineer trace the same requirement.
    """

    model_config = ConfigDict(extra="forbid")

    spec_id: str
    title: str = "Specification"
    description: str = ""
    context: str = ""
    ui_mockup: str = ""
    requirements: list[Requirement] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    test_scenarios: list[TestScenarioSpec] = Field(default_factory=list)
    architecture_decisions: list[ArchitectureDecision] = Field(default_factory=list)
    data_field_mappings: list[DataFieldMapping] = Field(default_factory=list)
    api_endpoints: list[ApiEndpoint] = Field(default_factory=list)
    user_flows: list[UserFlowSpec] = Field(default_factory=list)
    nfrs: list[Nfr] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    dod_extra: list[str] = Field(default_factory=list)
    # Track C (#1429) — BRD narrative + business-diagram fields (schema 2.2).
    business_narrative: str = ""
    business_process_diagram: str = ""
    capability_map: str = ""
    kpis: list[str] = Field(default_factory=list)
    business_glossary: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    business_risks: list[str] = Field(default_factory=list)


class AdrJudgment(BaseModel):
    """The authored content for one standalone cross-cutting ADR.

    ``adr_id`` matches a skeleton ADR item's ``id`` from
    ``synthesis_prompts.json``. ``affected_requirements`` cites the ``R-`` ids the
    decision addresses.
    """

    model_config = ConfigDict(extra="forbid")

    adr_id: str
    title: str = "Architecture Decision"
    status: Literal["Proposed", "Accepted", "Deprecated", "Superseded"] = "Proposed"
    context: str = ""
    decision: str = ""
    consequences: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    affected_requirements: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class ScopeSynthesis(BaseModel):
    """The full ``synthesis.json`` payload the SKILL.md body authors.

    ``schema_version`` is ``"2.0"`` (bumped by SP-B, #1382 — the old ``"1.0"``
    flat-list shape is rejected), ``"2.1"`` (SP3, #1411 — additive: a payload
    may carry ``kind="target"`` / ``from_controller`` on its
    :class:`ApiEndpoint`s for the synthesized §8b Target REST API; SP5, #1413 —
    additive: a spec may carry a ``ui_mockup`` Mermaid string and each
    :class:`UserFlowSpec` a ``sequence_diagram`` Mermaid string for the
    target UI screen / request-path sequence), or ``"2.2"`` (Track C, #1429 —
    additive: a spec may carry the BRD narrative + business-diagram fields
    ``business_narrative`` / ``business_process_diagram`` / ``capability_map`` /
    ``kpis`` / ``business_glossary`` / ``assumptions`` / ``business_risks`` for the
    per-context ``BRD.md``). A ``"2.0"`` / ``"2.1"`` payload still validates
    unchanged.  ``specs`` carries one :class:`SpecJudgment` per page/domain
    skeleton item; ``adrs`` carries one :class:`AdrJudgment` per cross-cutting ADR
    skeleton item.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0", "2.1", "2.2"] = "2.0"
    specs: list[SpecJudgment] = Field(default_factory=list)
    adrs: list[AdrJudgment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# validate_synthesis — rejects the old flat shape with a clear message.
# ---------------------------------------------------------------------------

_OLD_SHAPE_MSG = (
    "requirements must now be a list of Requirement objects with id/statement/scope/"
    "constraint/source/rationale/priority fields — the flat list[str] shape was removed "
    "in the SP-B schema bump (#1382). Update your synthesis.json to schema_version '2.0' "
    "and replace each plain string with a Requirement object "
    '(e.g. {"id": "R-001", "statement": "...", ...}).'
)


def validate_synthesis(raw: dict[str, object]) -> ScopeSynthesis:
    """Validate a parsed ``synthesis.json`` dict into a :class:`ScopeSynthesis`.

    Raises :class:`ValueError` with a clear, actionable message when the old
    ``schema_version: "1.0"`` flat-list shape is detected (``requirements`` is a
    ``list[str]``).  Raises :class:`pydantic.ValidationError` on any other schema
    violation (unknown key, wrong type, out-of-range enum) — both map to exit 3.
    """
    # Detect the old flat-list shape: any spec item whose ``requirements`` field
    # is a non-empty list of strings (not dicts).  We check before calling
    # model_validate so the error is human-readable rather than a raw pydantic
    # traceback.
    specs_raw = raw.get("specs")
    if isinstance(specs_raw, list):
        for spec_item in specs_raw:
            if not isinstance(spec_item, dict):
                continue
            reqs = spec_item.get("requirements")
            if isinstance(reqs, list) and reqs and isinstance(reqs[0], str):
                raise ValueError(_OLD_SHAPE_MSG)
    return ScopeSynthesis.model_validate(raw)
