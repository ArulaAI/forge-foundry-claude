"""Composite / aggregate models: EvidenceRow, AuditFinding, ReconReport,
AnalysisDigest (with backwards-compat properties), and PlaybookState.

Imports entity/legacy/Literal types from ``.entities`` and ``.legacy``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from primitives._response_schema_validator import accepts_extended_schema
from primitives._shared.discovery.models.entities import (  # noqa: TC001
    AuditFindingKind,
    BatchUnit,
    Confidence,
    DataStore,
    DeploymentUnit,
    EntryPoint,
    ExecutionUnit,
    MainframeFlavor,
    Severity,
    SharedComponent,
    UIElement,
)
from primitives._shared.discovery.models.legacy import (  # noqa: TC001
    BMSMapRecord,
    CICSTransaction,
    CopybookRecord,
    JCLJobRecord,
    ProgramRecord,
    VSAMFileRecord,
)

# ---------------------------------------------------------------------------
# EvidenceRow — append-only JSONL ledger row
# ---------------------------------------------------------------------------


class EvidenceRow(BaseModel):
    """One row of ``state-dir/evidence-ledger.jsonl``.

    Every claim a Phase C renderer makes about an entity must be backed
    by at least one ``EvidenceRow``. ``path`` is the source file
    (repo-relative). ``line_start`` and ``line_end`` are the inclusive
    line span ``rg`` (or the dialect parser) matched against. ``claim``
    is the human-readable summary of what this evidence supports.
    ``cross_references`` is the optional list of *other* entity names
    this row also corroborates (e.g. an ``EXEC CICS LINK PROGRAM(X)``
    line is evidence for the calling program AND for program X).

    ``confidence`` is the closed ``Confidence`` Literal — the renderer
    + auditor make decisions based on these three levels (high =
    cross-validated; medium = single-source; low = ``⚠️ UNVERIFIED``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    claim: str
    confidence: Confidence
    cross_references: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AuditFinding — Phase D self-audit result
# ---------------------------------------------------------------------------


class AuditFinding(BaseModel):
    """A single Phase D self-audit finding.

    Findings are appended to the §Validation Report section at the end
    of ``PLAYBOOK.md`` — Phase D does **not** silently drop content (see
    design spec §"Phase D — Self-Audit" rationale). ``kind`` partitions
    findings into the four check families. ``affected_path`` is the
    source-tree path (or playbook section anchor for
    ``section_missing`` / ``mermaid_node_orphan``). ``severity`` lets the
    consumer prioritise the manual review queue.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AuditFindingKind
    message: str
    affected_path: str
    severity: Severity


# ---------------------------------------------------------------------------
# ReconReport — Phase A output
# ---------------------------------------------------------------------------


@accepts_extended_schema
class ReconReport(BaseModel):
    """Phase A reconnaissance output (``{--state-dir}/recon.json``).

    Schema-versioned at ``"1.0"`` — downstream M6 / consumers refuse a
    mismatched output.

    ``file_tree_summary`` is the curated digest of ``find -maxdepth 5``
    output (truncated to the top-N entries — the depth budget is enforced
    by ``shared/recon.py``, not by this model). ``loc_by_language`` is
    keyed by ``SourceLanguage`` (as a string for JSON-roundtrip
    friendliness) and maps to a non-negative integer count.
    ``manifest_hits`` is the list of repo-relative paths of build
    descriptors / CSDs / system configs Phase A located.
    ``existing_doc_inventory`` is the list of repo-relative paths of any
    Markdown / RST / TXT documentation the source tree already carries
    (these inform §16 Appendices).

    Gemini subset (issue #548 / SPEC §6.3 + §7.2): persisted M5 Phase-A
    artefact, not an LLM ``response_format`` schema. The
    ``loc_by_language`` dict (``additionalProperties: <schema>``) plus
    ``anyOf`` for ``detected_flavor`` Optionals exceed Gemini's accepted
    subset. Decorated with ``@accepts_extended_schema`` because Phase A
    writes ``recon.json`` deterministically to disk.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    code_root: str
    mainframe_flavor: MainframeFlavor | None = None
    file_tree_summary: str = ""
    loc_by_language: dict[str, int] = Field(default_factory=dict)
    manifest_hits: list[str] = Field(default_factory=list)
    existing_doc_inventory: list[str] = Field(default_factory=list)
    # M-EPIC-3 / M5-FLAVOR-AUTO-DETECT (#525) — when Phase A is invoked
    # with ``--platform-flavor auto`` it walks every registered flavor's
    # ``detect()`` and records the winner here. Both fields default to
    # ``None`` so a Phase A run that was given an explicit flavor (the
    # historical M-EPIC-2 path) round-trips the existing v1.0 schema
    # unchanged — additive backwards-compat per the design spec
    # §"Backwards compatibility".
    #
    # ``detected_flavor`` is a string (not the closed
    # :data:`MainframeFlavor` Literal) because the auto-detect surface
    # is open over the entry-point-registered flavor names — a
    # third-party ``python`` / ``java_ee`` pack can win the detection
    # even though it's not in the closed mainframe Literal. Validation
    # against the registered names is the consumer's responsibility.
    detected_flavor: str | None = None
    detected_flavor_confidence: Confidence | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# AnalysisDigest — Phase B aggregated output
# ---------------------------------------------------------------------------


@accepts_extended_schema
class AnalysisDigest(BaseModel):
    """Phase B aggregated output (``{--state-dir}/analysis.json``).

    The six per-entity lists (``programs`` / ``copybooks`` /
    ``cics_transactions`` / ``vsam_files`` / ``bms_maps`` /
    ``jcl_jobs``) are the closed universe Phase C renderers consult and
    Phase D auditors verify against. Adding a new entity type requires a
    schema bump (this is the "schema-versioned so downstream primitives
    can refuse to consume a mismatched output" rule from the design
    spec).

    ``security_findings`` carries the security-surface scan output from
    Stage B's coupling-metrics pass — open-shaped so the renderer can
    pull from heterogeneous detectors (RACF references, hard-coded
    credentials, dynamic ``EXEC CICS`` paths) without forcing a Literal
    edit per detector. Substantive cross-referencing happens through the
    structured per-entity lists; ``security_findings`` is for narrative
    rendering only.

    Gemini subset (issue #548 / SPEC §6.3 + §7.2): persisted M5 Phase-B
    artefact, not an LLM ``response_format`` schema. The per-entity
    ``$ref`` chains (Program/Copybook/CICSTransaction/…/ExecutionUnit/
    SharedComponent) and several ``additionalProperties: <schema>``
    dictionaries (DataStore.accessed_by, VSAMFileRecord.accessed_by)
    exceed Gemini's accepted subset. Decorated with
    ``@accepts_extended_schema`` because Phase B writes ``analysis.json``
    deterministically to disk for Phase C / M6 consumption.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    code_root: str
    mainframe_flavor: MainframeFlavor
    #: The real resolved platform flavor (an open string over the entry-point-
    #: registered flavor names — ``"generic"`` / ``"java_ee"`` / ``"cics_cobol"`` / …),
    #: persisted so the continue leg reports the SAME flavor the extract leg detected
    #: (#1650). ``mainframe_flavor`` above is a closed ``MainframeFlavor`` Literal that
    #: defaults to ``"cics_cobol"`` when the resolved flavor is not a member, so it is
    #: NOT a reliable flavor identity. Additive + optional: absent (``None``) on older
    #: serialized digests, which then fall back to ``mainframe_flavor``.
    resolved_flavor: str | None = None
    programs: list[ProgramRecord] = Field(default_factory=list)
    copybooks: list[CopybookRecord] = Field(default_factory=list)
    cics_transactions: list[CICSTransaction] = Field(default_factory=list)
    vsam_files: list[VSAMFileRecord] = Field(default_factory=list)
    bms_maps: list[BMSMapRecord] = Field(default_factory=list)
    jcl_jobs: list[JCLJobRecord] = Field(default_factory=list)
    security_findings: list[str] = Field(default_factory=list)
    # M-EPIC-3 (M5-MODELS-V2 / #522) — abstract entity vocabulary added
    # additively so flavour packs can populate the polyglot surface
    # while the as-shipped mainframe pipeline (which populates the six
    # concrete fields above) keeps round-tripping unchanged.
    execution_units: list[ExecutionUnit] = Field(default_factory=list)
    shared_components: list[SharedComponent] = Field(default_factory=list)
    entry_points: list[EntryPoint] = Field(default_factory=list)
    data_stores: list[DataStore] = Field(default_factory=list)
    ui_elements: list[UIElement] = Field(default_factory=list)
    batch_units: list[BatchUnit] = Field(default_factory=list)
    # Deployment-topology objects from infrastructure manifests (#949) — added
    # additively (like the M5-MODELS-V2 abstract vocab) so §11 Deployment
    # Architecture can render real topology. Populated by a dedicated manifest
    # scan, not the per-stack flavor's six-category parse() contract.
    deployment_units: list[DeploymentUnit] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # ---------------------------------------------------------------
    # Backwards-compat property accessors (M-EPIC-3 / #522)
    # ---------------------------------------------------------------
    # The six properties below mirror the M-EPIC-2 mainframe-flavoured
    # field names while reading from the new abstract-typed fields.
    # They return *filtered subsets* of the abstract lists keyed on
    # the concrete-flavour ``kind`` Literal value. Flavour packs that
    # populate ``entry_points`` / ``data_stores`` / … directly can
    # rely on these properties to surface the mainframe-shaped view
    # to downstream renderers that still consume the concrete vocab.

    @property
    def cics_transactions_view(self) -> list[EntryPoint]:
        """Mainframe-shaped view: ``EntryPoint`` records with
        ``kind == "cics_transaction"``.

        Distinct from the concrete ``cics_transactions`` field
        (``list[CICSTransaction]``) — this property surfaces the
        polyglot abstract records a flavour pack populates onto
        ``entry_points``. See the M5-MODELS-V2 / #522 design note in
        the module docstring for the additive-coexistence rationale.
        """
        return [ep for ep in self.entry_points if ep.kind == "cics_transaction"]

    @property
    def vsam_files_view(self) -> list[DataStore]:
        """Mainframe-shaped view: ``DataStore`` records with
        ``kind == "vsam_file"``."""
        return [ds for ds in self.data_stores if ds.kind == "vsam_file"]

    @property
    def bms_maps_view(self) -> list[UIElement]:
        """Mainframe-shaped view: ``UIElement`` records with
        ``kind == "bms_map"``."""
        return [ue for ue in self.ui_elements if ue.kind == "bms_map"]

    @property
    def jcl_jobs_view(self) -> list[BatchUnit]:
        """Mainframe-shaped view: ``BatchUnit`` records with
        ``kind == "jcl_job"``."""
        return [bu for bu in self.batch_units if bu.kind == "jcl_job"]

    @property
    def programs_view(self) -> list[ExecutionUnit]:
        """Mainframe-shaped view: ``ExecutionUnit`` records with
        ``kind == "program"``."""
        return [eu for eu in self.execution_units if eu.kind == "program"]

    @property
    def copybooks_view(self) -> list[SharedComponent]:
        """Mainframe-shaped view: ``SharedComponent`` records with
        ``kind == "copybook"``."""
        return [sc for sc in self.shared_components if sc.kind == "copybook"]


# ---------------------------------------------------------------------------
# PlaybookState — resume / checkpoint state
# ---------------------------------------------------------------------------


@accepts_extended_schema
class PlaybookState(BaseModel):
    """Resume checkpoint shape (``{--state-dir}/playbook-state.json``).

    Written at every phase-and-section boundary so ``--resume`` can skip
    completed work. ``last_completed_phase`` carries one of ``"A"`` /
    ``"B"`` / ``"C"`` / ``"D"`` (or ``None`` if no phase has yet
    completed). ``phase_outputs`` maps phase letters to repo-relative
    paths of the artefacts produced (``"A": "recon.json"``, etc.).
    ``run_id`` is a stable identifier for the run — used as a prefix
    on the evidence ledger so reruns can be distinguished.

    Gemini subset (issue #548 / SPEC §6.3 + §7.2): resume-checkpoint
    artefact, not an LLM ``response_format`` schema. ``anyOf`` for the
    Optional ``last_completed_phase`` + ``additionalProperties: <schema>``
    for ``phase_outputs`` exceed Gemini's subset. Decorated with
    ``@accepts_extended_schema`` because the state file is written
    deterministically by the orchestrator, never returned by an LLM.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    last_completed_phase: Literal["A", "B", "C", "D"] | None = None
    phase_outputs: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "AnalysisDigest",
    "AuditFinding",
    "EvidenceRow",
    "PlaybookState",
    "ReconReport",
]
